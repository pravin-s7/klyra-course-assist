"""Coding-problem generation phase: create + review + independently
validate (by actually running code) a set of coding problems - and its
on-demand revision pass. Mirrors generation_service.py/revision_service.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from klyra.utils.parsing import parse_week_scope


class CodingInputError(ValueError):
    """Raised when a coding-problem request cannot be turned into crew inputs."""


_SOLUTION_EXT_MAP = {
    "python": "py",
    "py": "py",
    "python-db": "py",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "java": "java",
    "js": "js",
    "javascript": "js",
    "node": "js",
    "bash": "sh",
    "sh": "sh",
    "shell": "sh",
    "sql": "sql",
    "sqlite": "sql",
    "postgresql": "sql",
    "postgres": "sql",
}


def _collect_test_cases_from_dir(folder: Path, label_prefix: str) -> list[dict[str, str]]:
    """Read `test_case_*/input.txt` and `output.txt` in deterministic order."""
    if not folder.exists():
        return []

    def _sort_key(p: Path) -> tuple[int, str]:
        suffix = p.name.rsplit("_", 1)[-1]
        return (int(suffix), p.name) if suffix.isdigit() else (999999, p.name)

    cases: list[dict[str, str]] = []
    for tc_dir in sorted(
        (d for d in folder.iterdir() if d.is_dir() and d.name.startswith("test_case_")),
        key=_sort_key,
    ):
        in_file = tc_dir / "input.txt"
        out_file = tc_dir / "output.txt"
        if not in_file.exists() or not out_file.exists():
            continue
        cases.append(
            {
                "label": f"{label_prefix}/{tc_dir.name}",
                "input": in_file.read_text(encoding="utf-8"),
                "expected_output": out_file.read_text(encoding="utf-8"),
            }
        )
    return cases


def verify_on_disk_coding_package(
    subject: str, name: str, language: str
) -> tuple[bool, str]:
    """Verify the on-disk coding package under `docs/<subject>/generated/coding/<slug>/`
    by checking structure and executing the reference solution against every public
    and private test case.

    Returns `(all_passed, diagnostic_report)`.
    """
    from klyra.models.task_outputs import TestCaseValidationOutput
    from klyra.tools.code_execution_tool import docker_available, run_against_test_cases
    from klyra.utils import fs
    from klyra.utils.task_guardrails import verify_and_sync_validated_problem

    slug = fs.slugify(name)
    coding_dir = fs.generated_dir(subject) / "coding"
    pkg_root = fs.coding_package_dir(subject, name)
    lang = language.strip().lower()
    ext = _SOLUTION_EXT_MAP.get(lang, "txt")

    # 1. If structured sidecar `<slug>-testcases.json` exists, verify & sync via guardrail helper
    sidecar_candidates = [
        coding_dir / f"{slug}-testcases-revised.json",
        coding_dir / f"{slug}-testcases.json",
    ]
    for sidecar in sidecar_candidates:
        if sidecar.exists():
            try:
                parsed = TestCaseValidationOutput.model_validate_json(
                    sidecar.read_text(encoding="utf-8")
                )
                total_probs = len(parsed.validated_problems)
                errors: list[str] = []
                for prob in parsed.validated_problems:
                    ok_prob, detail = verify_and_sync_validated_problem(
                        vp=prob,
                        subject=subject,
                        package_name=slug,
                        total_problems=total_probs,
                    )
                    if not ok_prob:
                        errors.append(detail)
                if errors:
                    return False, "\n".join(errors)
            except Exception as exc:
                return False, f"Failed to validate structured testcases JSON ({sidecar.name}): {exc}"
            break

    if not pkg_root.exists():
        return False, f"Coding package directory '{pkg_root}' does not exist."

    # Normalize single-problem package if a stray `problem-1` directory was created alongside root
    import shutil
    from klyra.tools.subject_fs_tool import _infer_single_problem_package, default_evaluation_script

    prob1_dir = pkg_root / "problem-1"
    prob2_dir = pkg_root / "problem-2"
    if prob1_dir.is_dir() and not prob2_dir.exists() and _infer_single_problem_package(subject, slug, pkg_root):
        sub_pub = _collect_test_cases_from_dir(prob1_dir / "public", "public")
        root_pub = _collect_test_cases_from_dir(pkg_root / "public", "public")
        prefer_sub = (
            not (pkg_root / "public").exists()
            or sum(len(c["input"]) for c in sub_pub) > sum(len(c["input"]) for c in root_pub)
        )
        if prefer_sub and (prob1_dir / "public").exists():
            for item in prob1_dir.iterdir():
                dest = pkg_root / item.name
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest, ignore_errors=True)
                    else:
                        dest.unlink(missing_ok=True)
                shutil.move(str(item), str(dest))
        shutil.rmtree(prob1_dir, ignore_errors=True)

    # Ensure only the canonical evaluation_script filename exists in each package directory
    default_script_name, _ = default_evaluation_script(lang)
    if (pkg_root / default_script_name).exists():
        for extra_script in ("evaluation_script.py", "evaluation_script.sh", "evaluation_script"):
            if extra_script != default_script_name and (pkg_root / extra_script).exists():
                (pkg_root / extra_script).unlink(missing_ok=True)

    # Discover either single-problem root or multi-problem `problem-*` subdirectories
    problem_dirs: list[Path] = []
    if (pkg_root / "public").exists() or (pkg_root / "README.md").exists():
        problem_dirs.append(pkg_root)
    for sub in sorted(pkg_root.iterdir()):
        if sub.is_dir() and (
            sub.name.startswith("problem")
            or (sub / "public").exists()
            or (sub / "README.md").exists()
        ):
            if sub not in problem_dirs:
                problem_dirs.append(sub)
    if len(problem_dirs) > 1 and pkg_root in problem_dirs and any(p.name.startswith("problem-") for p in problem_dirs if p != pkg_root):
        # Multi-problem package: ignore root if problem-* subdirectories exist
        problem_dirs = [p for p in problem_dirs if p != pkg_root]

    if not problem_dirs:
        return False, f"No problem package directories found under '{pkg_root}'."

    diagnostics: list[str] = []
    for pdir in problem_dirs:
        rel_label = pdir.relative_to(pkg_root).as_posix() if pdir != pkg_root else slug
        readme = pdir / "README.md"
        if not readme.exists() or not readme.read_text(encoding="utf-8").strip():
            diagnostics.append(f"[{rel_label}] Missing or empty README.md.")
            continue

        pub_cases = _collect_test_cases_from_dir(pdir / "public", "public")
        priv_cases = _collect_test_cases_from_dir(pdir / "private", "private")
        all_cases = pub_cases + priv_cases
        if not pub_cases:
            diagnostics.append(f"[{rel_label}] No public test cases found in public/test_case_*/.")
            continue

        # Locate reference solution file (`solution.<ext>` or any `solution.*`)
        sol_file = pdir / f"solution.{ext}"
        if not sol_file.exists():
            matches = sorted(pdir.glob("solution.*"))
            if matches:
                sol_file = matches[0]
        if not sol_file.exists() or not sol_file.read_text(encoding="utf-8").strip():
            diagnostics.append(
                f"[{rel_label}] Missing reference solution file '{sol_file.name}' in package directory."
            )
            continue

        if docker_available():
            sol_code = sol_file.read_text(encoding="utf-8")
            exec_cases = [
                {"input": c["input"], "expected_output": c["expected_output"]}
                for c in all_cases
            ]
            try:
                import inspect

                run_kwargs: dict[str, Any] = {
                    "language": lang,
                    "source_code": sol_code,
                    "test_cases": exec_cases,
                }
                try:
                    if "batch" in inspect.signature(run_against_test_cases).parameters:
                        run_kwargs["batch"] = True
                except (ValueError, TypeError):
                    pass
                report = run_against_test_cases(**run_kwargs)
            except Exception as exc:
                diagnostics.append(f"[{rel_label}] Sandbox execution error: {exc}")
                continue

            if not report.compiled:
                diagnostics.append(
                    f"[{rel_label}] Reference solution failed to compile:\n{report.compile_error}"
                )
                continue

            if not report.all_passed or len(report.results) != len(exec_cases):
                failed_lines: list[str] = []
                for idx, res in enumerate(report.results):
                    if not res.passed:
                        lbl = all_cases[idx]["label"] if idx < len(all_cases) else f"case_{idx}"
                        reason = "TIMEOUT" if res.timed_out else f"exit={res.exit_code}"
                        failed_lines.append(
                            f"  - {lbl} FAILED ({reason}): stdin={res.stdin!r} | "
                            f"expected={res.expected!r} | actual_stdout={res.stdout!r} | stderr={res.stderr!r}"
                        )
                diagnostics.append(
                    f"[{rel_label}] {report.passed_count}/{len(exec_cases)} test cases passed:\n"
                    + "\n".join(failed_lines)
                )

    if diagnostics:
        return False, "\n".join(diagnostics)
    return True, f"All {len(problem_dirs)} problem package(s) under '{pkg_root}' verified and passed all test cases."


def run_coding_generation(
    subject: str,
    name: str,
    language: str,
    scope: str,
    difficulty: str,
    num_questions: int,
    public_tests: int,
    private_tests: int,
    source: str | None = None,
    pyq_similar: int = 0,
    leetcode_similar: bool = False,
    include_reference: bool = False,
    concepts: str | None = None,
    notes: str | None = None,
    max_rewrite_attempts: int = 2,
) -> Any:
    if pyq_similar > num_questions:
        raise CodingInputError(
            f"pyq_similar ({pyq_similar}) cannot exceed num_questions ({num_questions})."
        )

    import os

    os.environ["CREWAI_STORAGE_DIR"] = f".db/{subject}"

    from klyra.crews.coding_problem_crew.coding_problem_crew import CodingProblemCrew
    from klyra.services.skill_service import build_skill_prompt_inputs, load_subject_skill
    from klyra.utils import fs

    fs.check_subject_exists(subject)

    if not fs.subject_dir(subject).exists():
        raise CodingInputError(f"Subject '{subject}' does not exist in docs/. Please run ingest first.")

    skill = load_subject_skill(subject)
    resolved_lang = (language or skill.default_coding_language or "python").strip().lower()
    if resolved_lang == "default":
        resolved_lang = (skill.default_coding_language or "python").strip().lower()

    coding_dir = fs.generated_dir(subject) / "coding"
    coding_dir.mkdir(parents=True, exist_ok=True)
    slug = fs.slugify(name)
    inputs = {
        "subject": subject,
        "name": slug,
        "language": resolved_lang,
        "syllabus_scope": parse_week_scope(scope),
        "source_filter": source or "any",
        "difficulty": difficulty,
        "pyq_similar_count": pyq_similar,
        "leetcode_similar": "yes" if leetcode_similar else "no",
        "include_reference": "yes" if include_reference else "no",
        "concepts": concepts or "any",
        "num_questions": num_questions,
        "public_test_count": public_tests,
        "private_test_count": private_tests,
        "notes": notes or "(none)",
        **build_skill_prompt_inputs(subject),
    }
    result = CodingProblemCrew().crew().kickoff(inputs=inputs)

    # Post-crew deterministic verification & automatic rewrite-and-rerun loop
    pkg_root = fs.coding_package_dir(subject, name)
    sidecar = coding_dir / f"{slug}-testcases.json"
    if pkg_root.exists() or sidecar.exists():
        ok, diag = verify_on_disk_coding_package(subject, name, resolved_lang)
        attempt = 0
        while not ok and attempt < max_rewrite_attempts:
            attempt += 1
            rewrite_notes = (
                f"AUTOMATED TEST EXECUTION VERIFICATION FAILED (Attempt {attempt}/{max_rewrite_attempts}).\n"
                f"You MUST rewrite the broken reference solution or fix the incorrect test case outputs, "
                f"execute every test case with RunCodeTool until 100% pass, and rewrite the coding package:\n{diag}"
            )
            result = run_coding_revision(
                subject=subject,
                name=name,
                language=resolved_lang,
                notes=rewrite_notes,
                max_rewrite_attempts=0,
            )
            ok, diag = verify_on_disk_coding_package(subject, name, resolved_lang)

    return result


def run_coding_revision(
    subject: str,
    name: str,
    language: str,
    notes: str | None = None,
    max_rewrite_attempts: int = 1,
) -> Any:
    from klyra.crews.coding_revision_crew.coding_revision_crew import CodingRevisionCrew
    from klyra.services.skill_service import build_skill_prompt_inputs, load_subject_skill
    from klyra.utils import fs

    fs.check_subject_exists(subject)

    skill = load_subject_skill(subject)
    resolved_lang = (language or skill.default_coding_language or "python").strip().lower()
    if resolved_lang == "default":
        resolved_lang = (skill.default_coding_language or "python").strip().lower()

    slug = fs.slugify(name)
    coding_dir = fs.generated_dir(subject) / "coding"

    def _read(suffix: str) -> str:
        path = coding_dir / f"{slug}-{suffix}.md"
        if not path.exists():
            return f"(no {suffix} file found at {path})"
        return path.read_text()

    import json

    inferred_num_questions = 1
    for sidecar_name in (f"{slug}-problems.json", f"{slug}-testcases.json"):
        sidecar_path = coding_dir / sidecar_name
        if sidecar_path.exists():
            try:
                data = json.loads(sidecar_path.read_text(encoding="utf-8"))
                items = data.get("problems") or data.get("validated_problems") or []
                if isinstance(items, list) and len(items) > 0:
                    inferred_num_questions = len(items)
                    break
            except Exception:
                pass

    pkg_root = fs.coding_package_dir(subject, name)
    testcases_md = _read("testcases")
    if pkg_root.exists():
        disk_pub = _collect_test_cases_from_dir(pkg_root / "public", "public")
        disk_priv = _collect_test_cases_from_dir(pkg_root / "private", "private")
        if not disk_pub and (pkg_root / "problem-1").is_dir():
            disk_pub = _collect_test_cases_from_dir(pkg_root / "problem-1" / "public", "public")
            disk_priv = _collect_test_cases_from_dir(pkg_root / "problem-1" / "private", "private")
        if disk_pub or disk_priv:
            disk_lines = ["\n\n## On-Disk Test Case Files (`public/` & `private/`)"]
            for c in disk_pub + disk_priv:
                disk_lines.append(
                    f"### {c['label']}\n**Input (`input.txt`):**\n```text\n{c['input'].rstrip()}\n```\n"
                    f"**Expected Output (`output.txt`):**\n```text\n{c['expected_output'].rstrip()}\n```"
                )
            testcases_md = testcases_md + "\n" + "\n\n".join(disk_lines)

    inputs = {
        "subject": subject,
        "name": slug,
        "language": resolved_lang,
        "num_questions": inferred_num_questions,
        "problems_content": _read("problems"),
        "discrepancy_content": _read("coding-discrepancy-report"),
        "testcases_content": testcases_md,
        "final_summary_content": _read("final-summary"),
        "manual_instructions": notes or "(none - just fix what the automated review flagged)",
        **build_skill_prompt_inputs(subject),
    }
    
    import shutil

    with fs.crewai_storage_context(f".memories/{subject}/{slug}"):
        result = CodingRevisionCrew().crew().kickoff(inputs=inputs)

    # Synchronize revised companion files (.md and .json) to the active canonical files
    # so that problems, testcases, discrepancy-report, and final-summary are updated.
    for base, revised in [
        ("problems", "problems-revised"),
        ("testcases", "testcases-revised"),
        ("coding-discrepancy-report", "coding-discrepancy-report-revised"),
        ("final-summary", "final-summary-revised"),
    ]:
        for ext in (".md", ".json"):
            rev_path = coding_dir / f"{slug}-{revised}{ext}"
            base_path = coding_dir / f"{slug}-{base}{ext}"
            if rev_path.exists():
                shutil.copy2(rev_path, base_path)

    pkg_root = fs.coding_package_dir(subject, name)
    sidecar = coding_dir / f"{slug}-testcases.json"
    if max_rewrite_attempts > 0 and (pkg_root.exists() or sidecar.exists()):
        ok, diag = verify_on_disk_coding_package(subject, name, resolved_lang)
        attempt = 0
        while not ok and attempt < max_rewrite_attempts:
            attempt += 1
            rewrite_notes = (
                f"AUTOMATED TEST EXECUTION VERIFICATION FAILED AFTER REVISION (Attempt {attempt}/{max_rewrite_attempts}).\n"
                f"Rewrite the reference solution or fix the test cases and re-verify with RunCodeTool:\n{diag}"
            )
            result = run_coding_revision(
                subject=subject,
                name=name,
                language=resolved_lang,
                notes=rewrite_notes,
                max_rewrite_attempts=0,
            )
            ok, diag = verify_on_disk_coding_package(subject, name, resolved_lang)

    return result

