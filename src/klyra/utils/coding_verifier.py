"""Coding problem disk synchronization and sandboxed test execution verifier."""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

from klyra.crews.base_task import get_active_crew_inputs
from klyra.models.subject_skill import SubjectSkillConfig
from klyra.models.task_outputs import (
    CodingProblemSetOutput,
    CodingProblemSpec,
    CodingTestCase,
    ValidatedProblemResult,
)
from klyra.tools.code_execution_tool import (
    DockerNotAvailableError,
    docker_available as _default_docker_available,
    run_against_test_cases as _default_run_against_test_cases,
)
from klyra.tools.subject_fs_tool import TestCaseFiles, WriteCodingPackageTool
from klyra.utils.format_renderer import render_coding_readme_markdown
from klyra.utils.fs import generated_dir


def _get_sandbox_fns() -> tuple[Any, Any]:
    """Return `(docker_available, run_against_test_cases)` honoring monkeypatches on `task_guardrails`."""
    try:
        from klyra.utils import task_guardrails as tg

        return (
            getattr(tg, "docker_available", _default_docker_available),
            getattr(tg, "run_against_test_cases", _default_run_against_test_cases),
        )
    except Exception:
        return _default_docker_available, _default_run_against_test_cases


def _read_on_disk_test_cases(base_dir: Path, visibility: str) -> list[CodingTestCase]:
    """Read `test_case_*/input.txt` and `output.txt` from `base_dir / visibility`."""
    vis_dir = base_dir / visibility
    if not vis_dir.is_dir():
        return []

    def _sort_key(p: Path) -> tuple[int, str]:
        suffix = p.name.rsplit("_", 1)[-1]
        return (int(suffix) if suffix.isdigit() else 999999, p.name)

    cases: list[CodingTestCase] = []
    for idx, tc_dir in enumerate(sorted(vis_dir.glob("test_case_*"), key=_sort_key), start=1):
        if not tc_dir.is_dir():
            continue
        in_file = tc_dir / "input.txt"
        out_file = tc_dir / "output.txt"
        if in_file.exists() and out_file.exists():
            cases.append(
                CodingTestCase(
                    index=idx,
                    visibility="private" if visibility == "private" else "public",
                    stdin_input=in_file.read_text(encoding="utf-8"),
                    expected_stdout=out_file.read_text(encoding="utf-8"),
                )
            )
    return cases


def _discover_disk_test_cases_for_problem(
    subject: str, pkg_name: str, problem_number: int, total_problems: int
) -> tuple[list[CodingTestCase], list[CodingTestCase]]:
    """Locate on-disk public and private test cases for `problem_number`."""
    if not pkg_name:
        return [], []
    from klyra.utils import fs

    pkg_root = fs.coding_package_dir(subject, pkg_name)
    if not pkg_root.exists():
        return [], []

    candidates: list[Path] = []
    sub_dir = pkg_root / f"problem-{problem_number}"
    if total_problems > 1:
        if sub_dir.is_dir():
            candidates.append(sub_dir)
        if problem_number == 1:
            candidates.append(pkg_root)
    else:
        candidates.append(pkg_root)
        if sub_dir.is_dir():
            candidates.append(sub_dir)

    best_pub: list[CodingTestCase] = []
    best_priv: list[CodingTestCase] = []
    for cand in candidates:
        pub = _read_on_disk_test_cases(cand, "public")
        priv = _read_on_disk_test_cases(cand, "private")
        if pub and (
            not best_pub
            or sum(len(x.stdin_input) for x in pub) > sum(len(x.stdin_input) for x in best_pub)
        ):
            best_pub = pub
        if priv and (
            not best_priv
            or sum(len(x.stdin_input) for x in priv) > sum(len(x.stdin_input) for x in best_priv)
        ):
            best_priv = priv
    return best_pub, best_priv


def _backfill_problem_tests_from_disk(
    prob: CodingProblemSpec, subject: str, pkg_name: str, total_problems: int
) -> None:
    """Ensure `prob.sample_tests` has no private tests and backfill `prob.public_tests` /
    `prob.private_tests` from on-disk `write_coding_package` files when available."""
    if prob.sample_tests:
        leaked_priv = [tc for tc in prob.sample_tests if tc.visibility == "private"]
        if leaked_priv:
            prob.sample_tests = [
                tc for tc in prob.sample_tests if tc.visibility != "private"
            ]
            if not prob.private_tests:
                prob.private_tests = leaked_priv

    disk_pub, disk_priv = _discover_disk_test_cases_for_problem(
        subject, pkg_name, prob.problem_number, total_problems
    )
    if disk_pub:
        if not prob.public_tests or (
            sum(len(x.stdin_input) for x in disk_pub)
            > sum(len(x.stdin_input) for x in prob.public_tests)
        ):
            prob.public_tests = disk_pub
    elif not prob.public_tests and prob.sample_tests:
        prob.public_tests = [
            CodingTestCase(
                index=idx,
                visibility="public",
                stdin_input=tc.stdin_input,
                expected_stdout=tc.expected_stdout,
                explanation=tc.explanation,
            )
            for idx, tc in enumerate(prob.sample_tests, start=1)
        ]

    if disk_priv:
        if not prob.private_tests or (
            sum(len(x.stdin_input) for x in disk_priv)
            > sum(len(x.stdin_input) for x in prob.private_tests)
        ):
            prob.private_tests = disk_priv


def _sanitize_student_readme(readme_text: str) -> str:
    """Remove any accidentally included Reference Solution or Private Test Cases sections from README.md."""
    cleaned = re.split(
        r"\n##+\s+(?:Reference\s+Solution|Full\s+Draft\s+Test\s+Suite|Private\s+Test\s+Cases)\b",
        readme_text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    cleaned = re.split(
        r"\n###+\s+Private\s+Test\b",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return cleaned.strip() + "\n"


def _load_on_disk_problem_specs(subject: str, pkg_name: str) -> list[CodingProblemSpec]:
    """Load `CodingProblemSpec` entries from `{slug}-problems-revised.json` or `{slug}-problems.json` if present."""
    if not pkg_name:
        return []
    from klyra.utils import fs

    slug = fs.slugify(pkg_name)
    coding_dir = generated_dir(subject) / "coding"
    for candidate_name in (f"{slug}-problems-revised.json", f"{slug}-problems.json"):
        candidate = coding_dir / candidate_name
        if candidate.exists():
            try:
                parsed = CodingProblemSetOutput.model_validate_json(
                    candidate.read_text(encoding="utf-8")
                )
                if parsed.problems:
                    return list(parsed.problems)
            except Exception:
                pass
    return []


def _sync_problem_spec_to_disk(
    prob: CodingProblemSpec,
    subject: str,
    package_name: str,
    total_problems: int,
    skill: SubjectSkillConfig | None = None,
) -> None:
    """Deterministically materialize `prob` into `docs/<subject>/generated/coding/<package_name>/`
    (using `problem-<N>` subdirectory when `total_problems > 1`)."""
    if not package_name or not prob.reference_solution.strip():
        return
    pub_tests = prob.public_tests or prob.sample_tests
    priv_tests = prob.private_tests
    if not pub_tests:
        return

    active_lang = str(get_active_crew_inputs().get("language") or "").strip().lower()
    prob_lang = (prob.language or "").strip().lower()
    if active_lang and (not prob_lang or (prob_lang == "python" and active_lang != "python")):
        lang = active_lang
    else:
        lang = prob_lang or active_lang or "python"
    if lang == "python" and re.search(r"\b(?:psycopg2|pg8000)\b", prob.reference_solution):
        lang = "python-db"
    prob.language = lang

    readme_text = _sanitize_student_readme(render_coding_readme_markdown(prob, skill))
    subdir = f"problem-{prob.problem_number}" if total_problems > 1 else None
    pub_files = [
        TestCaseFiles(input=tc.stdin_input, output=tc.expected_stdout)
        for tc in pub_tests
    ]
    priv_files = [
        TestCaseFiles(input=tc.stdin_input, output=tc.expected_stdout)
        for tc in priv_tests
    ]
    writer = WriteCodingPackageTool()
    writer._run(
        subject=subject,
        name=package_name,
        language=lang,
        readme=readme_text,
        public_tests=pub_files,
        private_tests=priv_files,
        problem_subdir=subdir,
        reference_solution=prob.reference_solution,
    )


def verify_and_sync_validated_problem(
    vp: ValidatedProblemResult,
    subject: str,
    package_name: str,
    total_problems: int,
    skill: SubjectSkillConfig | None = None,
    sync_to_disk: bool = True,
) -> tuple[bool, str]:
    """Execute `vp.reference_solution` against every public & private test case in the
    sandbox (when available) and sync the verified coding package to disk.

    Returns `(True, summary_str)` when all tests pass, or `(False, detailed_error_report)`
    when any test case fails compilation, crashes, times out, or produces wrong stdout.
    """
    if not vp.reference_solution.strip():
        return (
            False,
            f"Problem {vp.problem_number} ('{vp.title}') has an empty `reference_solution`.",
        )
    if not vp.public_tests:
        return (
            False,
            f"Problem {vp.problem_number} ('{vp.title}') must include at least 1 public test case in `public_tests`.",
        )
    if not vp.private_tests:
        return (
            False,
            f"Problem {vp.problem_number} ('{vp.title}') must include at least 1 private test case in `private_tests`.",
        )

    active_lang = str(get_active_crew_inputs().get("language") or "").strip().lower()
    vp_lang = (vp.language or "").strip().lower()
    if active_lang and (not vp_lang or (vp_lang == "python" and active_lang != "python")):
        lang = active_lang
    else:
        lang = vp_lang or active_lang or "python"
    if lang == "python" and re.search(r"\b(?:psycopg2|pg8000)\b", vp.reference_solution):
        lang = "python-db"
    vp.language = lang

    # If the validator stripped out SQL seed setup (`CREATE TABLE` / `INSERT INTO` / `\copy`)
    # that the creator/reviser wrote to disk, recover the full SQL input from disk before running.
    if lang in ("python-db", "postgresql", "sql") and package_name:
        disk_pub, disk_priv = _discover_disk_test_cases_for_problem(
            subject, package_name, vp.problem_number, total_problems
        )
        sql_kw_re = re.compile(
            r"\b(?:CREATE\s+TABLE|INSERT\s+INTO|DROP\s+TABLE)\b|\\copy\b",
            re.IGNORECASE,
        )
        for idx_p, tc in enumerate(vp.public_tests):
            if (
                not sql_kw_re.search(tc.stdin_input)
                and idx_p < len(disk_pub)
                and sql_kw_re.search(disk_pub[idx_p].stdin_input)
            ):
                tc.stdin_input = disk_pub[idx_p].stdin_input
        for idx_p, tc in enumerate(vp.private_tests):
            if (
                not sql_kw_re.search(tc.stdin_input)
                and idx_p < len(disk_priv)
                and sql_kw_re.search(disk_priv[idx_p].stdin_input)
            ):
                tc.stdin_input = disk_priv[idx_p].stdin_input

    docker_available_fn, run_against_test_cases_fn = _get_sandbox_fns()
    sandbox_ok = docker_available_fn()
    vp.sandbox_available = sandbox_ok

    if sandbox_ok:
        all_specs: list[dict[str, str]] = []
        test_labels: list[tuple[str, int, CodingTestCase]] = []
        for idx, tc in enumerate(vp.public_tests, start=1):
            all_specs.append(
                {"input": tc.stdin_input, "expected_output": tc.expected_stdout}
            )
            test_labels.append(("public", idx, tc))
        for idx, tc in enumerate(vp.private_tests, start=1):
            all_specs.append(
                {"input": tc.stdin_input, "expected_output": tc.expected_stdout}
            )
            test_labels.append(("private", idx, tc))

        run_kwargs: dict[str, Any] = {
            "language": lang,
            "source_code": vp.reference_solution,
            "test_cases": all_specs,
        }
        try:
            if "batch" in inspect.signature(run_against_test_cases_fn).parameters:
                run_kwargs["batch"] = True
        except (ValueError, TypeError):
            pass

        try:
            exec_result = run_against_test_cases_fn(**run_kwargs)
        except DockerNotAvailableError as exc:
            exec_result = None
            sandbox_ok = False
            vp.sandbox_available = False
            vp.execution_verified = False
            vp.execution_summary = (
                f"UNVERIFIED (Docker sandbox unavailable for `{lang}`: {exc}); "
                f"{len(vp.public_tests)} public and {len(vp.private_tests)} private test cases recorded."
            )

        if exec_result is not None:
            if not exec_result.compiled or exec_result.compile_error:
                return (
                    False,
                    f"Problem {vp.problem_number} ('{vp.title}') FAILED compilation in `{lang}` sandbox:\n"
                    f"```text\n{exec_result.compile_error}\n```\n"
                    "Rewrite the `reference_solution` so it compiles cleanly without errors, re-run `execute_code_in_sandbox`, and return the updated result.",
                )

            total_cases = len(all_specs)
            failed_details: list[str] = []
            for (visibility, idx, tc), res in zip(test_labels, exec_result.results):
                if not res.passed:
                    reason = (
                        "TIMED OUT"
                        if res.timed_out
                        else f"exit_code={res.exit_code}"
                        if res.exit_code != 0
                        else "STDOUT MISMATCH"
                    )
                    failed_details.append(
                        f"  - [{visibility.upper()} test #{idx}] {reason}:\n"
                        f"    stdin_input:     {tc.stdin_input!r}\n"
                        f"    expected_stdout: {res.expected!r}\n"
                        f"    actual_stdout:   {res.stdout!r}\n"
                        f"    stderr:          {res.stderr!r}"
                    )

            if failed_details:
                return (
                    False,
                    f"Problem {vp.problem_number} ('{vp.title}') failed {len(failed_details)}/{total_cases} test cases in `{lang}` sandbox:\n"
                    + "\n".join(failed_details)
                    + "\n\nACTION REQUIRED: Inspect whether the `reference_solution` has a bug or a test case's `expected_stdout` was miscalculated. "
                    "Rewrite the broken `reference_solution` or test case, verify 100% pass rate with `execute_code_in_sandbox`, update the package with `write_coding_package`, and return the corrected output.",
                )

            vp.execution_verified = True
            vp.execution_summary = (
                f"Verified {exec_result.passed_count}/{total_cases} test cases passed in `{lang}` sandbox "
                f"({len(vp.public_tests)} public, {len(vp.private_tests)} private)."
            )
    else:
        vp.execution_verified = False
        vp.execution_summary = (
            f"UNVERIFIED (Docker sandbox unavailable for `{lang}`); "
            f"{len(vp.public_tests)} public and {len(vp.private_tests)} private test cases recorded."
        )

    if sync_to_disk and package_name:
        readme_text = vp.readme_markdown.strip()
        if not readme_text:
            from klyra.utils import fs

            pkg_root = fs.coding_package_dir(subject, package_name)
            sub_readme = pkg_root / f"problem-{vp.problem_number}" / "README.md"
            root_readme = pkg_root / "README.md"
            if total_problems > 1 and sub_readme.exists():
                readme_text = sub_readme.read_text(encoding="utf-8").strip()
            elif total_problems == 1 and root_readme.exists():
                readme_text = root_readme.read_text(encoding="utf-8").strip()
            elif sub_readme.exists():
                readme_text = sub_readme.read_text(encoding="utf-8").strip()

        if not readme_text:
            matched_spec: CodingProblemSpec | None = None
            for spec_cand in _load_on_disk_problem_specs(subject, package_name):
                if spec_cand.problem_number == vp.problem_number or (
                    vp.title and spec_cand.title.strip().lower() == vp.title.strip().lower()
                ):
                    matched_spec = spec_cand
                    break
            if matched_spec is not None:
                readme_text = render_coding_readme_markdown(matched_spec, skill)
            else:
                spec = CodingProblemSpec(
                    problem_number=vp.problem_number,
                    title=vp.title or f"Problem {vp.problem_number}",
                    language=lang,
                    problem_statement=f"Solve {vp.title or f'Problem {vp.problem_number}'}.",
                    input_format="See sample test cases.",
                    output_format="See sample test cases.",
                    sample_tests=vp.public_tests[:2],
                    reference_solution=vp.reference_solution,
                )
                readme_text = render_coding_readme_markdown(spec, skill)
        readme_text = _sanitize_student_readme(readme_text)
        vp.readme_markdown = readme_text

        subdir = f"problem-{vp.problem_number}" if total_problems > 1 else None
        pub_files = [
            TestCaseFiles(input=tc.stdin_input, output=tc.expected_stdout)
            for tc in vp.public_tests
        ]
        priv_files = [
            TestCaseFiles(input=tc.stdin_input, output=tc.expected_stdout)
            for tc in vp.private_tests
        ]
        writer = WriteCodingPackageTool()
        msg = writer._run(
            subject=subject,
            name=package_name,
            language=lang,
            readme=readme_text,
            public_tests=pub_files,
            private_tests=priv_files,
            problem_subdir=subdir,
            reference_solution=vp.reference_solution,
        )
        vp.package_files_written = [msg]

    return True, vp.execution_summary

