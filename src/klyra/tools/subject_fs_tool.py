"""Write files under docs/<subject>/generated/ only.

Used by `klyra code` / `revise-code` so agents can actually create the
student package (README, public/private test_case_N folders, evaluation
script) instead of only describing that layout in markdown. Paths cannot
escape the subject's generated/ directory.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from klyra.utils import fs

_ALLOWED_SCRIPT_NAMES = {
    "evaluation_script.sh",
    "evaluation_script.py",
    "evaluation_script",
}


def _generated_root(subject: str) -> Path:
    fs.validate_subject(subject)
    root = fs.generated_dir(subject).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_under_generated(subject: str, relative_path: str) -> Path:
    """Resolve relative_path inside docs/<subject>/generated/. Raises ValueError
    on empty paths, absolute paths, or `..` escapes."""
    rel = (relative_path or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or rel.startswith("~"):
        raise ValueError(f"Path must be relative to the subject's generated/ folder, got {relative_path!r}")
    parts = Path(rel).parts
    if ".." in parts:
        raise ValueError(f"Path must not contain '..': {relative_path!r}")
    root = _generated_root(subject)
    dest = (root / rel).resolve()
    dest.relative_to(root)
    return dest


def write_under_generated(subject: str, relative_path: str, content: str) -> Path:
    dest = resolve_under_generated(subject, relative_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content if content is not None else "")
    if dest.name in _ALLOWED_SCRIPT_NAMES or dest.suffix in {".sh", ".py", ".sed", ".awk"}:
        dest.chmod(dest.stat().st_mode | 0o111)
    return dest


class TestCaseFiles(BaseModel):
    input: str = Field(default="", description="Contents of input.txt")
    output: str = Field(default="", description="Contents of output.txt (expected stdout)")


def _write_test_bank(folder: Path, cases: list[TestCaseFiles]) -> list[str]:
    if folder.exists():
        shutil.rmtree(folder)
    written: list[str] = []
    for i, case in enumerate(cases):
        case_dir = folder / f"test_case_{i}"
        case_dir.mkdir(parents=True)
        (case_dir / "input.txt").write_text(case.input if case.input is not None else "")
        (case_dir / "output.txt").write_text(case.output if case.output is not None else "")
        written.append(str(case_dir / "input.txt"))
        written.append(str(case_dir / "output.txt"))
    return written


def _normalize_language_key(language: str) -> str:
    key = (language or "").strip().lower()
    aliases = {
        "c++": "cpp",
        "g++": "cpp",
        "cxx": "cpp",
        "py": "python",
        "python3": "python",
        "sh": "bash",
        "shell": "bash",
        "sed": "sed",
        "awk": "awk",
        "gawk": "gawk",
        "sqlite": "sql",
        "sqlite3": "sql",
        "postgres": "postgresql",
        "pgsql": "postgresql",
        "jdk": "java",
        "openjdk": "java",
    }
    return aliases.get(key, key)


def default_evaluation_script(language: str) -> tuple[str, str]:
    """Generic stdin/stdout runner. Agent should pass a custom script when
    the problem uses a named file (e.g. access.log) instead of stdin."""
    key = _normalize_language_key(language)
    if key == "python":
        return "evaluation_script.py", _PYTHON_EVAL
    if key == "python-db":
        return "evaluation_script.py", _PYTHON_DB_EVAL
    if key == "java":
        return "evaluation_script.sh", _JAVA_EVAL
    if key in {"c", "cpp"}:
        is_c = key == "c"
        return "evaluation_script.sh", _C_EVAL.format(
            compiler="gcc" if is_c else "g++ -std=c++17",
            ext="c" if is_c else "cpp",
        )
    if key == "sql":
        return "evaluation_script.sh", _SQL_EVAL
    if key == "postgresql":
        return "evaluation_script.sh", _POSTGRES_EVAL
    runner = key if key in {"sed", "awk", "gawk"} else "bash"
    return "evaluation_script.sh", _BASH_EVAL.replace("__DEFAULT_RUNNER__", runner)


_EVAL_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "eval"


def _load_eval_template(filename: str) -> str:
    return (_EVAL_TEMPLATES_DIR / filename).read_text(encoding="utf-8")


_BASH_EVAL = _load_eval_template("eval_bash.sh")
_PYTHON_EVAL = _load_eval_template("eval_python.py")
_PYTHON_DB_EVAL = _load_eval_template("eval_python_db.py")
_JAVA_EVAL = _load_eval_template("eval_java.sh")
_C_EVAL = _load_eval_template("eval_c_cpp.sh")
_SQL_EVAL = _load_eval_template("eval_sql.sh")
_POSTGRES_EVAL = _load_eval_template("eval_postgres.sh")


class WriteSubjectFileInput(BaseModel):
    subject: str
    relative_path: str = Field(
        ...,
        description="Path relative to docs/<subject>/generated/, e.g. "
        "coding/problem1/public/test_case_0/input.txt",
    )
    content: str = Field(default="", description="Full file contents to write (overwrites)")


class WriteSubjectFileTool(BaseTool):
    name: str = "write_subject_file"
    description: str = (
        "Create or overwrite ONE file under docs/<subject>/generated/ "
        "(creates parent folders). Use this to add/update a test case, "
        "README.md, or evaluation_script. Cannot write outside this "
        "subject's generated/ folder. For a full public/private package "
        "prefer write_coding_package."
    )
    args_schema: Type[BaseModel] = WriteSubjectFileInput

    def _run(self, subject: str, relative_path: str, content: str = "") -> str:
        rel = (relative_path or "").strip().replace("\\", "/")
        parts = [p for p in rel.split("/") if p]
        if len(parts) >= 4 and parts[0] == "coding" and parts[2] in {"problem-1", "problem1"}:
            try:
                pkg_root = fs.coding_package_dir(subject, parts[1])
                if _infer_single_problem_package(subject, parts[1], pkg_root):
                    rel = "/".join([parts[0], parts[1], *parts[3:]])
            except Exception:
                pass
        try:
            dest = write_under_generated(subject, rel, content)
        except (ValueError, OSError) as exc:
            return f"Write refused: {exc}"
        return f"Wrote {dest.as_posix()}"


class WriteCodingPackageInput(BaseModel):
    subject: str
    name: str = Field(..., description="Problem-set name (same as --name), e.g. problem1")
    language: str = Field(..., description="python, python-db, postgresql, sql, java, c, cpp, or bash")
    readme: str = Field(..., description="Student-facing README.md (statement, samples; no hidden tests)")
    public_tests: list[TestCaseFiles] = Field(default_factory=list)
    private_tests: list[TestCaseFiles] = Field(default_factory=list)
    evaluation_script: str | None = Field(
        default=None,
        description="Optional custom evaluation script contents; defaults to language-specific evaluation script.",
    )
    evaluation_script_filename: str | None = Field(
        default=None,
        description="Defaults to evaluation_script.sh (or .py for python/python-db)",
    )
    problem_subdir: str | None = Field(
        default=None,
        description="Optional subfolder under the package ONLY when the set has multiple problems (num_questions > 1), e.g. problem-1, problem-2. Leave None for single-problem sets.",
    )
    reference_solution: str | None = Field(
        default=None,
        description="The reference solution code. If provided, it will be saved as solution.{language} in the same folder.",
    )


def _infer_expected_problem_count(subject: str, name: str, pkg_root: Path) -> int | None:
    """Return the expected number of problems for `(subject, name)`, or None if unknown."""
    try:
        from klyra.crews.base_task import get_active_crew_inputs

        active_inputs = get_active_crew_inputs()
    except Exception:
        active_inputs = {}

    active_subj = fs.slugify(str(active_inputs.get("subject") or ""))
    active_name = fs.slugify(str(active_inputs.get("name") or ""))
    req_subj = fs.slugify(subject)
    req_name = fs.slugify(name)
    if (not active_subj or active_subj == req_subj) and (not active_name or active_name == req_name):
        raw_nq = active_inputs.get("num_questions")
        if raw_nq is not None and str(raw_nq).strip().isdigit():
            val = int(str(raw_nq).strip())
            if val > 0:
                return val

    slug = fs.slugify(name)
    coding_dir = fs.generated_dir(subject) / "coding"
    import json

    for candidate in (
        coding_dir / f"{slug}-problems-revised.json",
        coding_dir / f"{slug}-problems.json",
        coding_dir / f"{slug}-testcases-revised.json",
        coding_dir / f"{slug}-testcases.json",
    ):
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                items = data.get("problems") or data.get("validated_problems") or []
                if isinstance(items, list) and len(items) > 0:
                    return len(items)
            except Exception:
                pass

    if (pkg_root / "problem-2").exists():
        return max(2, len([p for p in pkg_root.glob("problem-*") if p.is_dir()]))

    # If root already has public/ or README.md and there is no problem-2, treat as single-problem
    if ((pkg_root / "public").exists() or (pkg_root / "README.md").exists()) and not (
        pkg_root / "problem-2"
    ).exists():
        return 1
    return None


def _infer_single_problem_package(subject: str, name: str, pkg_root: Path) -> bool:
    """Return True if the active crew / on-disk metadata indicates a 1-problem set."""
    return _infer_expected_problem_count(subject, name, pkg_root) == 1


def _infer_problem_subdir_from_readme(subject: str, name: str, readme: str) -> str:
    """Infer `problem-<N>` from the student README heading or sidecar problem titles."""
    import json
    import re

    m = re.search(r"#+\s*Problem\s*(\d+)\b", readme or "", re.IGNORECASE)
    if m:
        return f"problem-{int(m.group(1))}"

    slug = fs.slugify(name)
    coding_dir = fs.generated_dir(subject) / "coding"
    for candidate in (
        coding_dir / f"{slug}-problems-revised.json",
        coding_dir / f"{slug}-problems.json",
    ):
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                for idx, item in enumerate(data.get("problems") or [], start=1):
                    title = str(item.get("title") or "").strip()
                    pnum = int(item.get("problem_number") or idx)
                    if title and title.lower() in (readme or "").lower():
                        return f"problem-{pnum}"
            except Exception:
                pass
    return "problem-1"


class WriteCodingPackageTool(BaseTool):
    name: str = "write_coding_package"
    description: str = (
        "Write the student-facing coding package for this subject under "
        "docs/<subject>/generated/coding/<name>/ : README.md, "
        "public/test_case_N/input.txt + output.txt, "
        "private/test_case_N/input.txt + output.txt, and evaluation_script. "
        "Creates folders and overwrites public/private banks. Only writes "
        "inside this subject's generated/coding/<name>/ directory. Call this "
        "on create AND on revise-code when notes ask for this layout."
    )
    args_schema: Type[BaseModel] = WriteCodingPackageInput

    def _run(
        self,
        subject: str,
        name: str,
        language: str,
        readme: str,
        public_tests: list | None = None,
        private_tests: list | None = None,
        evaluation_script: str | None = None,
        evaluation_script_filename: str | None = None,
        problem_subdir: str | None = None,
        reference_solution: str | None = None,
    ) -> str:
        try:
            fs.validate_subject(subject)
        except ValueError as exc:
            return f"Write refused: {exc}"

        try:
            from klyra.crews.base_task import get_active_crew_inputs

            active_inputs = get_active_crew_inputs()
        except Exception:
            active_inputs = {}

        active_lang = _normalize_language_key(str(active_inputs.get("language") or ""))
        lang_key = _normalize_language_key(language or active_lang or "python")
        if lang_key == "python" and active_lang == "python-db":
            lang_key = "python-db"

        pkg_root = fs.coding_package_dir(subject, name)
        sub = fs.slugify(problem_subdir) if problem_subdir else ""
        expected_count = _infer_expected_problem_count(subject, name, pkg_root)
        is_single = expected_count == 1

        if sub in {"", "problem-1", "problem1"} and is_single:
            sub = ""
        elif not sub and expected_count is not None and expected_count > 1:
            sub = _infer_problem_subdir_from_readme(subject, name, readme)

        if sub:
            pkg = pkg_root / sub
            # Multi-problem package: clean up any stray root-level single-problem files
            for folder_name in ("public", "private"):
                stray_dir = pkg_root / folder_name
                if stray_dir.is_dir():
                    shutil.rmtree(stray_dir, ignore_errors=True)
            for stray_file in (
                pkg_root / "README.md",
                *[pkg_root / s for s in _ALLOWED_SCRIPT_NAMES],
                *list(pkg_root.glob("solution.*")),
            ):
                if stray_file.is_file():
                    stray_file.unlink(missing_ok=True)
        else:
            pkg = pkg_root
            # Single-problem package at root: remove any duplicate problem-1/ subdirectory
            if not (pkg_root / "problem-2").exists():
                for dup_sub in ("problem-1", "problem1"):
                    dup_dir = pkg_root / dup_sub
                    if dup_dir.is_dir():
                        shutil.rmtree(dup_dir, ignore_errors=True)

        try:
            pkg.resolve().relative_to(fs.generated_dir(subject).resolve())
        except ValueError:
            return "Write refused: package path escaped generated/"

        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "README.md").write_text(readme or "")

        effective_lang = lang_key
        if reference_solution:
            if lang_key in {"bash", "sed", "awk", "gawk"}:
                try:
                    from klyra.tools.code_execution_tool import detect_shell_script_dialect

                    effective_lang = detect_shell_script_dialect(reference_solution, lang_key)
                except Exception:
                    effective_lang = lang_key
                for stale_ext in (".sh", ".sed", ".awk", ".gawk"):
                    (pkg / f"solution{stale_ext}").unlink(missing_ok=True)
            ext_map = {
                "python": ".py",
                "python-db": ".py",
                "java": ".java",
                "c": ".c",
                "cpp": ".cpp",
                "bash": ".sh",
                "sed": ".sed",
                "awk": ".awk",
                "gawk": ".awk",
                "sql": ".sql",
                "sqlite": ".sql",
                "postgresql": ".sql",
                "postgres": ".sql",
            }
            ext = ext_map.get(effective_lang, ".txt")
            (pkg / f"solution{ext}").write_text(reference_solution)

        def _as_cases(raw: list | None) -> list[TestCaseFiles]:
            out: list[TestCaseFiles] = []
            for item in raw or []:
                if isinstance(item, TestCaseFiles):
                    out.append(item)
                elif isinstance(item, dict):
                    out.append(
                        TestCaseFiles(
                            input=item.get("input", item.get("stdin_input", "")),
                            output=item.get("output", item.get("expected_output", item.get("expected_stdout", ""))),
                        )
                    )
                else:
                    out.append(
                        TestCaseFiles(
                            input=getattr(item, "input", getattr(item, "stdin_input", "")),
                            output=getattr(item, "output", getattr(item, "expected_stdout", "")),
                        )
                    )
            return out

        public_written = _write_test_bank(pkg / "public", _as_cases(public_tests))
        private_written = _write_test_bank(pkg / "private", _as_cases(private_tests))

        default_name, default_body = default_evaluation_script(effective_lang)
        if evaluation_script is None:
            script_name = default_name
        else:
            first_line = evaluation_script.lstrip().splitlines()[0].lower() if evaluation_script.strip() else ""
            if first_line.startswith("#!") and "python" in first_line:
                script_name = "evaluation_script.py"
            elif first_line.startswith("#!") and ("sh" in first_line or "bash" in first_line):
                script_name = "evaluation_script.sh"
            else:
                script_name = Path(evaluation_script_filename or default_name).name
                if script_name not in _ALLOWED_SCRIPT_NAMES:
                    script_name = default_name

        # Remove any existing evaluation_script.* so .py and .sh never coexist
        for old_script in _ALLOWED_SCRIPT_NAMES:
            (pkg / old_script).unlink(missing_ok=True)

        script_path = pkg / Path(script_name).name
        script_path.write_text(evaluation_script if evaluation_script is not None else default_body)
        script_path.chmod(script_path.stat().st_mode | 0o111)

        return (
            f"Wrote coding package at {pkg.as_posix()} "
            f"({len(public_written)//2} public, {len(private_written)//2} private, "
            f"script={script_path.name})"
        )
