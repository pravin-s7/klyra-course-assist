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
    if key == "java":
        return "evaluation_script.sh", _JAVA_EVAL
    if key in {"c", "cpp"}:
        return "evaluation_script.sh", _C_EVAL.format(compiler="gcc" if key == "c" else "g++")
    return "evaluation_script.sh", _BASH_EVAL


_BASH_EVAL = r"""#!/usr/bin/env bash
# Usage: ./evaluation_script.sh path/to/student_solution.sh
set -uo pipefail
SOLUTION=${1:?usage: evaluation_script.sh <student_solution>}
ROOT=$(cd "$(dirname "$0")" && pwd)
fail=0
run_bank() {
  local kind=$1
  local dir="$ROOT/$kind"
  [[ -d "$dir" ]] || return 0
  for tc in "$dir"/test_case_*; do
    [[ -d "$tc" ]] || continue
    local got
    got=$(bash "$SOLUTION" < "$tc/input.txt" 2>/dev/null || true)
    local exp
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

_PYTHON_EVAL = r'''#!/usr/bin/env python3
"""Usage: python3 evaluation_script.py path/to/student_solution.py"""
import subprocess, sys
from pathlib import Path

root = Path(__file__).resolve().parent
solution = sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: evaluation_script.py <student_solution.py>")
fail = 0
for kind in ("public", "private"):
    bank = root / kind
    if not bank.is_dir():
        continue
    for tc in sorted(bank.glob("test_case_*")):
        if not tc.is_dir():
            continue
        inp = (tc / "input.txt").read_text()
        exp = (tc / "output.txt").read_text().rstrip()
        proc = subprocess.run([sys.executable, solution], input=inp, text=True, capture_output=True)
        got = (proc.stdout or "").rstrip()
        status = "PASS" if got == exp and proc.returncode == 0 else "FAIL"
        if status == "FAIL":
            fail = 1
        print(f"{status} {tc.name} ({kind})")
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
SOLUTION=${1:?usage: evaluation_script.sh solution.c}
ROOT=$(cd "$(dirname "$0")" && pwd)
BIN=$(mktemp)
trap 'rm -f "$BIN"' EXIT
{compiler} -O2 -o "$BIN" "$SOLUTION" || exit 2
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
        try:
            dest = write_under_generated(subject, relative_path, content)
        except (ValueError, OSError) as exc:
            return f"Write refused: {exc}"
        return f"Wrote {dest.as_posix()}"


class WriteCodingPackageInput(BaseModel):
    subject: str
    name: str = Field(..., description="Problem-set name (same as --name), e.g. problem1")
    language: str = Field(..., description="python, java, c, cpp, or bash")
    readme: str = Field(..., description="Student-facing README.md (statement, samples; no hidden tests)")
    public_tests: list[TestCaseFiles] = Field(default_factory=list)
    private_tests: list[TestCaseFiles] = Field(default_factory=list)
    evaluation_script_filename: str | None = Field(
        default=None,
        description="Defaults to evaluation_script.sh (or .py for python)",
    )
    problem_subdir: str | None = Field(
        default=None,
        description="Optional subfolder under the package when the set has multiple problems, e.g. problem_1",
    )


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
        evaluation_script_filename: str | None = None,
        problem_subdir: str | None = None,
    ) -> str:
        try:
            fs.validate_subject(subject)
        except ValueError as exc:
            return f"Write refused: {exc}"

        pkg = fs.coding_package_dir(subject, name)
        if problem_subdir:
            sub = fs.slugify(problem_subdir)
            pkg = pkg / sub
        try:
            pkg.resolve().relative_to(fs.generated_dir(subject).resolve())
        except ValueError:
            return "Write refused: package path escaped generated/"

        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "README.md").write_text(readme or "")

        def _as_cases(raw: list | None) -> list[TestCaseFiles]:
            out: list[TestCaseFiles] = []
            for item in raw or []:
                if isinstance(item, TestCaseFiles):
                    out.append(item)
                elif isinstance(item, dict):
                    out.append(
                        TestCaseFiles(
                            input=item.get("input", ""),
                            output=item.get("output", item.get("expected_output", "")),
                        )
                    )
                else:
                    out.append(TestCaseFiles(input=getattr(item, "input", ""), output=getattr(item, "output", "")))
            return out

        public_written = _write_test_bank(pkg / "public", _as_cases(public_tests))
        private_written = _write_test_bank(pkg / "private", _as_cases(private_tests))

        default_name, default_body = default_evaluation_script(language)
        script_name = Path(evaluation_script_filename or default_name).name
        if script_name not in _ALLOWED_SCRIPT_NAMES:
            script_name = default_name
        script_path = pkg / Path(script_name).name
        script_path.write_text(default_body)
        script_path.chmod(script_path.stat().st_mode | 0o111)

        return (
            f"Wrote coding package at {pkg.as_posix()} "
            f"({len(public_written)//2} public, {len(private_written)//2} private, "
            f"script={script_path.name})"
        )
