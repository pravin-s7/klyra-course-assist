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
    if dest.name in _ALLOWED_SCRIPT_NAMES or dest.suffix in {".sh", ".py"}:
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


def default_evaluation_script(language: str) -> tuple[str, str]:
    """Generic stdin/stdout runner. Agent should pass a custom script when
    the problem uses a named file (e.g. access.log) instead of stdin."""
    key = language.strip().lower()
    if key == "python":
        return "evaluation_script.py", _PYTHON_EVAL
    if key == "python-db":
        return "evaluation_script.py", _PYTHON_DB_EVAL
    if key == "java":
        return "evaluation_script.sh", _JAVA_EVAL
    if key in {"c", "cpp"}:
        return "evaluation_script.sh", _C_EVAL.format(compiler="gcc" if key == "c" else "g++")
    if key in {"sql", "sqlite"}:
        return "evaluation_script.sh", _SQL_EVAL
    if key in {"postgresql", "postgres"}:
        return "evaluation_script.sh", _POSTGRES_EVAL
    return "evaluation_script.sh", _BASH_EVAL


_BASH_EVAL = r"""#!/usr/bin/env bash
# Usage: ./evaluation_script.sh [path/to/student_solution.sh]
set -uo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
SOLUTION="$ROOT/solution.sh"
for arg in "$@"; do
  if [[ "$arg" != --* ]]; then
    SOLUTION="$arg"
    break
  fi
done
fail=0
passed=0
total=0
run_bank() {
  local kind=$1
  local dir="$ROOT/$kind"
  [[ -d "$dir" ]] || return 0
  for tc in "$dir"/test_case_*; do
    [[ -d "$tc" ]] || continue
    total=$((total + 1))
    local got
    got=$(bash "$SOLUTION" < "$tc/input.txt" 2>/dev/null || true)
    local exp
    exp=$(cat "$tc/output.txt")
    if [[ "$(printf '%s' "$got" | sed 's/[[:space:]]*$//')" == "$(printf '%s' "$exp" | sed 's/[[:space:]]*$//')" ]]; then
      echo "PASS $(basename "$tc") ($kind)"
      passed=$((passed + 1))
    else
      echo "FAIL $(basename "$tc") ($kind)"
      fail=1
    fi
  done
}
run_bank public
run_bank private
if [[ $fail -eq 0 ]]; then
  echo "ALL PASSED ($passed/$total)"
fi
exit $fail
"""

_PYTHON_EVAL = r'''#!/usr/bin/env python3
"""Usage: python3 evaluation_script.py [path/to/student_solution.py]"""
import subprocess, sys
from pathlib import Path

root = Path(__file__).resolve().parent
pos_args = [a for a in sys.argv[1:] if not a.startswith("-")]
solution = pos_args[0] if pos_args else str(root / "solution.py")
fail = 0
passed = 0
total = 0
for kind in ("public", "private"):
    bank = root / kind
    if not bank.is_dir():
        continue
    for tc in sorted(bank.glob("test_case_*")):
        if not tc.is_dir():
            continue
        total += 1
        inp = (tc / "input.txt").read_text()
        exp = (tc / "output.txt").read_text().rstrip()
        proc = subprocess.run([sys.executable, solution], input=inp, text=True, capture_output=True, cwd=str(root))
        got = (proc.stdout or "").rstrip()
        status = "PASS" if got == exp and proc.returncode == 0 else "FAIL"
        if status == "FAIL":
            fail = 1
        else:
            passed += 1
        print(f"{status} {tc.name} ({kind})")
if fail == 0:
    print(f"ALL PASSED ({passed}/{total})")
sys.exit(fail)
'''

_PYTHON_DB_EVAL = r'''#!/usr/bin/env python3
"""Usage: python3 evaluation_script.py [path/to/student_solution.py]"""
import os, shutil, subprocess, sys
from pathlib import Path

root = Path(__file__).resolve().parent
pos_args = [a for a in sys.argv[1:] if not a.startswith("-")]
solution = pos_args[0] if pos_args else str(root / "solution.py")
psql_bin = shutil.which("psql")
fail = 0
passed = 0
total = 0
for kind in ("public", "private"):
    bank = root / kind
    if not bank.is_dir():
        continue
    for tc in sorted(bank.glob("test_case_*")):
        if not tc.is_dir():
            continue
        total += 1
        in_file = tc / "input.txt"
        inp = in_file.read_text()
        exp = (tc / "output.txt").read_text().rstrip()
        if psql_bin and any(k in inp.upper() for k in ("CREATE TABLE", "INSERT INTO", "DROP TABLE", "\\COPY")):
            subprocess.run([psql_bin, "-U", os.environ.get("PGUSER", "postgres"), "-d", "postgres", "-q", "-f", str(in_file)], cwd=str(root), capture_output=True)
        env = dict(os.environ)
        env.setdefault("PGUSER", "postgres")
        env.setdefault("PGPASSWORD", "postgres")
        env.setdefault("PGHOST", "127.0.0.1")
        env.setdefault("PGPORT", "5432")
        env.setdefault("PGDATABASE", "postgres")
        proc = subprocess.run([sys.executable, solution], input=inp, text=True, capture_output=True, cwd=str(root), env=env)
        got = (proc.stdout or "").rstrip()
        status = "PASS" if got == exp and proc.returncode == 0 else "FAIL"
        if status == "FAIL":
            fail = 1
        else:
            passed += 1
        print(f"{status} {tc.name} ({kind})")
if fail == 0:
    print(f"ALL PASSED ({passed}/{total})")
sys.exit(fail)
'''

_JAVA_EVAL = r"""#!/usr/bin/env bash
set -uo pipefail
SOLUTION=${1:?usage: evaluation_script.sh Solution.java}
ROOT=$(cd "$(dirname "$0")" && pwd)
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
cp "$SOLUTION" "$WORKDIR/Solution.java"
(cd "$WORKDIR" && javac Solution.java) || exit 2
fail=0
run_bank() {
  local kind=$1 dir="$ROOT/$kind"
  [[ -d "$dir" ]] || return 0
  for tc in "$dir"/test_case_*; do
    [[ -d "$tc" ]] || continue
    got=$(cd "$WORKDIR" && java Solution < "$tc/input.txt" 2>/dev/null || true)
    exp=$(cat "$tc/output.txt")
    if [[ "$(printf '%s' "$got" | sed 's/[[:space:]]*$//')" == "$(printf '%s' "$exp" | sed 's/[[:space:]]*$//')" ]]; then
      echo "PASS $(basename "$tc") ($kind)"
    else
      echo "FAIL $(basename "$tc") ($kind)"
      fail=1
    fi
  done
}
run_bank public
run_bank private
exit $fail
"""

_C_EVAL = r"""#!/usr/bin/env bash
set -uo pipefail
SOLUTION=${{1:?usage: evaluation_script.sh solution.c}}
ROOT=$(cd "$(dirname "$0")" && pwd)
BIN=$(mktemp)
trap 'rm -f "$BIN"' EXIT
{compiler} -O2 -o "$BIN" "$SOLUTION" -lm || exit 2
fail=0
run_bank() {{
  local kind=$1 dir="$ROOT/$kind"
  [[ -d "$dir" ]] || return 0
  for tc in "$dir"/test_case_*; do
    [[ -d "$tc" ]] || continue
    got=$("$BIN" < "$tc/input.txt" 2>/dev/null || true)
    exp=$(cat "$tc/output.txt")
    if [[ "$(printf '%s' "$got" | sed 's/[[:space:]]*$//')" == "$(printf '%s' "$exp" | sed 's/[[:space:]]*$//')" ]]; then
      echo "PASS $(basename "$tc") ($kind)"
    else
      echo "FAIL $(basename "$tc") ($kind)"
      fail=1
    fi
  done
}}
run_bank public
run_bank private
exit $fail
"""

_SQL_EVAL = r"""#!/usr/bin/env bash
set -uo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
SOLUTION="$ROOT/solution.sql"
for arg in "$@"; do
  if [[ "$arg" != --* ]]; then
    SOLUTION="$arg"
    break
  fi
done
fail=0
passed=0
total=0
run_sql() {
  local tc_input=$1 sol=$2
  if command -v sqlite3 >/dev/null 2>&1; then
    (cat "$tc_input"; echo ""; cat "$sol") | sqlite3 -batch :memory: 2>/dev/null || true
  else
    python3 - "$tc_input" "$sol" << 'PYEOF'
import sqlite3, sys
from pathlib import Path
conn = sqlite3.connect(":memory:")
setup_sql = Path(sys.argv[1]).read_text()
query_sql = Path(sys.argv[2]).read_text()
if setup_sql.strip():
    conn.executescript(setup_sql)
cur = conn.cursor()
for stmt in [s.strip() for s in query_sql.split(";") if s.strip()]:
    cur.execute(stmt)
    rows = cur.fetchall()
    for r in rows:
        print("|".join("" if v is None else str(v) for v in r))
PYEOF
  fi
}
run_bank() {
  local kind=$1
  local dir="$ROOT/$kind"
  [[ -d "$dir" ]] || return 0
  for tc in "$dir"/test_case_*; do
    [[ -d "$tc" ]] || continue
    total=$((total + 1))
    got=$(run_sql "$tc/input.txt" "$SOLUTION")
    exp=$(cat "$tc/output.txt")
    if [[ "$(printf '%s' "$got" | sed 's/[[:space:]]*$//')" == "$(printf '%s' "$exp" | sed 's/[[:space:]]*$//')" ]]; then
      echo "PASS $(basename "$tc") ($kind)"
      passed=$((passed + 1))
    else
      echo "FAIL $(basename "$tc") ($kind)"
      fail=1
    fi
  done
}
run_bank public
run_bank private
if [[ $fail -eq 0 ]]; then
  echo "ALL PASSED ($passed/$total)"
fi
exit $fail
"""

_POSTGRES_EVAL = r"""#!/usr/bin/env bash
set -uo pipefail
SOLUTION=${1:?usage: evaluation_script.sh solution.sql}
ROOT=$(cd "$(dirname "$0")" && pwd)
fail=0
run_bank() {
  local kind=$1 dir="$ROOT/$kind"
  [[ -d "$dir" ]] || return 0
  for tc in "$dir"/test_case_*; do
    [[ -d "$tc" ]] || continue
    got=$( (cat "$tc/input.txt"; echo ""; cat "$SOLUTION") | psql -U postgres -X -q -A -t 2>/dev/null || true )
    exp=$(cat "$tc/output.txt")
    if [[ "$(printf '%s' "$got" | sed 's/[[:space:]]*$//')" == "$(printf '%s' "$exp" | sed 's/[[:space:]]*$//')" ]]; then
      echo "PASS $(basename "$tc") ($kind)"
    else
      echo "FAIL $(basename "$tc") ($kind)"
      fail=1
    fi
  done
}
run_bank public
run_bank private
exit $fail
"""


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


def _infer_single_problem_package(subject: str, name: str, pkg_root: Path) -> bool:
    """Return True if the active crew / on-disk metadata indicates a 1-problem set."""
    try:
        from klyra.crews.base_task import get_active_crew_inputs

        active_inputs = get_active_crew_inputs()
    except Exception:
        active_inputs = {}

    raw_nq = active_inputs.get("num_questions")
    if raw_nq is not None and str(raw_nq).strip().isdigit():
        return int(str(raw_nq).strip()) == 1

    if (pkg_root / "problem-2").exists():
        return False

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
                    return len(items) == 1
            except Exception:
                pass

    # If root already has public/ or README.md and there is no problem-2, treat as single-problem
    if ((pkg_root / "public").exists() or (pkg_root / "README.md").exists()) and not (
        pkg_root / "problem-2"
    ).exists():
        return True
    return False


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

        active_lang = str(active_inputs.get("language") or "").strip().lower()
        lang_key = (language or active_lang or "python").strip().lower()
        if lang_key == "python" and active_lang == "python-db":
            lang_key = "python-db"

        pkg_root = fs.coding_package_dir(subject, name)
        sub = fs.slugify(problem_subdir) if problem_subdir else ""
        is_single = _infer_single_problem_package(subject, name, pkg_root)

        if sub in {"", "problem-1", "problem1"} and is_single:
            sub = ""

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

        if reference_solution:
            ext_map = {
                "python": ".py",
                "python-db": ".py",
                "java": ".java",
                "c": ".c",
                "cpp": ".cpp",
                "bash": ".sh",
                "sql": ".sql",
                "sqlite": ".sql",
                "postgresql": ".sql",
                "postgres": ".sql",
            }
            ext = ext_map.get(lang_key, ".txt")
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

        default_name, default_body = default_evaluation_script(lang_key)
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
