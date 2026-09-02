"""Actually compiles and runs untrusted, LLM-generated code against test
cases - inside an ephemeral, network-disabled Docker container per
language - rather than having an LLM read code and guess whether it's
correct. Same "reliable tool over generative guess" principle as
latex_diagram_tool.py/format_conversion.py: an LLM cannot reliably verify
code correctness without running it.

This is why the Test-Case Validator agent (coding_problem_crew) - not the
Creator agent - owns this tool: it independently re-derives and *executes*
a solution rather than trusting the Creator's claimed test cases, mirroring
how Solve & Verify Agent never trusts agent 3's embedded answer key.

Requires a running Docker daemon (`docker` on PATH + a reachable daemon) -
same "optional, degrade with a clear message" pattern as the LibreOffice/
pandoc/pdflatex dependencies elsewhere in this project. Every execution is:
  - `--network none` (no exfiltration, no downloading extra payloads)
  - memory/CPU/pids-limited (no resource-exhaustion/fork-bomb risk)
  - `--rm` (auto-cleanup), plus an explicit `docker kill` on timeout, since
    killing the *client* `docker run` CLI process does not by itself stop
    the container running server-side
  - run as an unprivileged container user where the base image allows it

The "bash" language needs a small image (see docker/bash-runner.Dockerfile)
because minimal base images can't be guaranteed to have gawk/sed
preinstalled, and installing packages at container-*run* time would need
network access we deliberately disable. `klyra-bash-runner:latest` is not
on Docker Hub; the tool builds it from that Dockerfile the first time bash
is used, so `docker run` never tries (and fails) to pull the name.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

DEFAULT_TIMEOUT_SECONDS = 5
COMPILE_TIMEOUT_SECONDS = 30
MEMORY_LIMIT = "256m"
CPU_LIMIT = "1"
PIDS_LIMIT = "64"

BASH_RUNNER_IMAGE = "klyra-bash-runner:latest"
PYTHON_POSTGRES_RUNNER_IMAGE = "klyra-python-postgres-runner:latest"


class DockerNotAvailableError(RuntimeError):
    """Raised when the `docker` CLI is missing or the daemon isn't reachable."""


@dataclass(frozen=True)
class LanguageRunner:
    image: str
    filename: str
    compile_cmd: list[str] | None  # argv, run inside /sandbox; None = interpreted, no compile step
    run_cmd: list[str]


LANGUAGE_RUNNERS: dict[str, LanguageRunner] = {
    "python": LanguageRunner(
        image="python:3.12-slim",
        filename="solution.py",
        compile_cmd=None,
        run_cmd=["python3", "solution.py"],
    ),
    "java": LanguageRunner(
        image="eclipse-temurin:21-jdk-slim",
        filename="Solution.java",
        compile_cmd=["javac", "Solution.java"],
        run_cmd=["java", "Solution"],
    ),
    "c": LanguageRunner(
        image="gcc:13-bookworm",
        filename="solution.c",
        compile_cmd=["gcc", "-O2", "-o", "solution", "solution.c", "-lm"],
        run_cmd=["./solution"],
    ),
    "cpp": LanguageRunner(
        image="gcc:13-bookworm",
        filename="solution.cpp",
        compile_cmd=["g++", "-O2", "-std=c++17", "-o", "solution", "solution.cpp", "-lm"],
        run_cmd=["./solution"],
    ),
    "bash": LanguageRunner(
        image=BASH_RUNNER_IMAGE,
        filename="solution.sh",
        compile_cmd=None,
        run_cmd=["bash", "solution.sh"],
    ),
    "sql": LanguageRunner(
        image=BASH_RUNNER_IMAGE,
        filename="solution.sql",
        compile_cmd=None,
        run_cmd=["sh", "-c", "cat - solution.sql | sqlite3 test.db"],
    ),
    "postgresql": LanguageRunner(
        image="postgres:16-alpine",
        filename="solution.sql",
        compile_cmd=None,
        run_cmd=["sh", "-c", "mkdir -p /tmp/pgdata && chown postgres:postgres /tmp/pgdata && su-exec postgres initdb -D /tmp/pgdata >/dev/null 2>&1 && su-exec postgres pg_ctl -D /tmp/pgdata -w start >/dev/null 2>&1 && cat - solution.sql | su-exec postgres psql -q -d postgres"],
    ),
    "python-db": LanguageRunner(
        image="klyra-python-postgres-runner:latest",
        filename="solution.py",
        compile_cmd=None,
        run_cmd=["sh", "-c", "mkdir -p /tmp/pgdata && chown postgres:postgres /tmp/pgdata && su-exec postgres initdb -D /tmp/pgdata >/dev/null 2>&1 && su-exec postgres pg_ctl -D /tmp/pgdata -w start >/dev/null 2>&1 && cat - > schema.sql && su-exec postgres psql -q -d postgres -f schema.sql >/dev/null 2>&1 && python3 solution.py"],
    ),
}


def _docker_path() -> str:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        raise DockerNotAvailableError(
            "docker is not installed (or not on PATH) - required to validate code by "
            "actually running it. Install Docker to enable coding-problem validation."
        )
    return docker_bin


def docker_available() -> bool:
    try:
        docker_bin = _docker_path()
    except DockerNotAvailableError:
        return False
    try:
        result = subprocess.run([docker_bin, "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _bash_runner_dockerfile() -> Path | None:
    """Locate docker/bash-runner.Dockerfile next to the repo (editable install)
    or under the current working directory."""
    candidates = [
        Path(__file__).resolve().parents[3] / "docker" / "bash-runner.Dockerfile",
        Path.cwd() / "docker" / "bash-runner.Dockerfile",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _python_postgres_runner_dockerfile() -> Path | None:
    candidates = [
        Path(__file__).resolve().parents[3] / "docker" / "python-postgres-runner.Dockerfile",
        Path.cwd() / "docker" / "python-postgres-runner.Dockerfile",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _image_exists(image: str) -> bool:
    docker_bin = _docker_path()
    try:
        result = subprocess.run(
            [docker_bin, "image", "inspect", image],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def ensure_language_image(language: str) -> str | None:
    """Make sure a locally-built runner image exists. Returns an error
    message if it can't, or None if the language uses a public image / the
    local image is already present / was just built.
    """
    language = language.strip().lower()
    
    if language in ("bash", "sql"):
        image_name = BASH_RUNNER_IMAGE
        dockerfile_func = _bash_runner_dockerfile
    elif language == "python-db":
        image_name = PYTHON_POSTGRES_RUNNER_IMAGE
        dockerfile_func = _python_postgres_runner_dockerfile
    else:
        return None

    if _image_exists(image_name):
        return None

    dockerfile = dockerfile_func()
    if not dockerfile:
        return f"Cannot build {image_name}: Dockerfile not found."

    docker_bin = _docker_path()
    try:
        build_result = subprocess.run(
            [
                docker_bin,
                "build",
                "-t",
                image_name,
                "-f",
                str(dockerfile),
                str(dockerfile.parent),
            ],
            capture_output=True,
            timeout=120,
        )
        if build_result.returncode != 0:
            err = build_result.stderr.decode(errors="replace")
            return f"Failed to build {image_name}. Docker build error:\n{err}"
        return None
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"Failed to run docker build for {image_name}: {e}"


@dataclass
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


def _docker_run(
    *, image: str, workdir: Path, cmd: list[str], stdin_text: str | None, timeout_seconds: int
) -> RunResult:
    """Runs `cmd` inside a fresh, network-disabled, resource-limited
    container with `workdir` bind-mounted at /sandbox (also its cwd)."""
    docker_bin = _docker_path()
    container_name = f"klyra-run-{uuid.uuid4().hex[:12]}"
    
    full_cmd = [
        docker_bin, "run", "--rm", "--name", container_name,
        "--network", "none",
        f"--memory={MEMORY_LIMIT}", f"--memory-swap={MEMORY_LIMIT}",
        f"--cpus={CPU_LIMIT}", f"--pids-limit={PIDS_LIMIT}",
        "-v", f"{workdir}:/sandbox",
        "-w", "/sandbox",
        "-i",
        image, *cmd,
    ]
    try:
        result = subprocess.run(
            full_cmd,
            input=stdin_text or "",
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return RunResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr, timed_out=False)
    except subprocess.TimeoutExpired as exc:
        # Killing the *client* docker CLI process (what subprocess.run just did) does not
        # stop the container running server-side - it must be killed explicitly.
        subprocess.run([docker_bin, "kill", container_name], capture_output=True, timeout=10)
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="ignore")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="ignore")
        return RunResult(exit_code=None, stdout=stdout, stderr=stderr, timed_out=True)


def _normalize(text: str) -> str:
    """Trailing-whitespace/blank-line-insensitive comparison - the same
    normalization real online judges use, so a program isn't marked wrong
    for a harmless trailing newline."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    return "\n".join(lines)


@dataclass
class TestCaseResult:
    index: int
    passed: bool
    stdin: str
    expected: str
    stdout: str
    stderr: str
    timed_out: bool
    exit_code: int | None


@dataclass
class ExecutionReport:
    compiled: bool
    compile_error: str | None
    results: list[TestCaseResult]

    @property
    def all_passed(self) -> bool:
        return self.compiled and all(r.passed for r in self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)


def run_against_test_cases(
    language: str,
    source_code: str,
    test_cases: list[dict],
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ExecutionReport:
    """Compiles `source_code` (if the language needs it) once, then runs it
    once per test case (`{"input": str, "expected_output": str}`), each in
    its own fresh container, comparing normalized stdout."""
    key = language.strip().lower()
    if key not in LANGUAGE_RUNNERS:
        raise ValueError(f"Unsupported language {language!r}. Supported: {sorted(LANGUAGE_RUNNERS)}")
    runner = LANGUAGE_RUNNERS[key]
    image_error = ensure_language_image(key)
    if image_error:
        raise DockerNotAvailableError(image_error)

    with tempfile.TemporaryDirectory(prefix="klyra-run-") as tmp:
        workdir = Path(tmp)
        workdir.chmod(0o777)  # container may run as a non-root/different uid than this process
        (workdir / runner.filename).write_text(source_code)

        if runner.compile_cmd:
            compile_result = _docker_run(
                image=runner.image,
                workdir=workdir,
                cmd=runner.compile_cmd,
                stdin_text=None,
                timeout_seconds=COMPILE_TIMEOUT_SECONDS,
            )
            if compile_result.timed_out:
                return ExecutionReport(compiled=False, compile_error="Compilation timed out.", results=[])
            if compile_result.exit_code != 0:
                error = compile_result.stderr.strip() or compile_result.stdout.strip()
                return ExecutionReport(compiled=False, compile_error=error, results=[])

        results: list[TestCaseResult] = []
        for i, case in enumerate(test_cases):
            stdin_text = case.get("input", "")
            expected = case.get("expected_output", "")
            run_result = _docker_run(
                image=runner.image,
                workdir=workdir,
                cmd=runner.run_cmd,
                stdin_text=stdin_text,
                timeout_seconds=timeout_seconds,
            )
            passed = (
                not run_result.timed_out
                and run_result.exit_code == 0
                and _normalize(run_result.stdout) == _normalize(expected)
            )
            results.append(
                TestCaseResult(
                    index=i,
                    passed=passed,
                    stdin=stdin_text,
                    expected=expected,
                    stdout=run_result.stdout,
                    stderr=run_result.stderr,
                    timed_out=run_result.timed_out,
                    exit_code=run_result.exit_code,
                )
            )

        return ExecutionReport(compiled=True, compile_error=None, results=results)


class TestCaseSpec(BaseModel):
    input: str = Field(default="", description="Exact stdin fed to the program")
    expected_output: str = Field(..., description="Exact expected stdout (compared whitespace-normalized)")


class RunCodeInput(BaseModel):
    language: str = Field(..., description=f"One of: {', '.join(sorted(LANGUAGE_RUNNERS))}")
    source_code: str = Field(
        ...,
        description=(
            "Full program source, reading input from stdin and writing the answer to stdout. "
            "For java: must define exactly `public class Solution` with "
            "`public static void main(String[] args)`."
        ),
    )
    test_cases: list[TestCaseSpec] = Field(..., description="One entry per test case to run against")
    timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS, ge=1, le=30, description="Per-test-case execution timeout"
    )


class RunCodeTool(BaseTool):
    name: str = "run_code"
    description: str = (
        "Actually COMPILE (if needed) and RUN a program against test cases inside an "
        "isolated, network-disabled Docker container - the only reliable way to confirm "
        "code is correct. Never claim a solution or test case is correct without calling "
        "this and seeing it pass. Returns per-test-case pass/fail with actual vs expected "
        f"stdout, or a compile error. Supported languages: {', '.join(sorted(LANGUAGE_RUNNERS))}. "
        "Requires Docker running on the host - if it reports unavailable, say so plainly in "
        "your output rather than assuming the code is correct. For bash, the local "
        "klyra-bash-runner image is built automatically from docker/bash-runner.Dockerfile "
        "on first use (it is not pulled from Docker Hub)."
    )
    args_schema: Type[BaseModel] = RunCodeInput

    def _run(
        self,
        language: str,
        source_code: str,
        test_cases: list,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        key = language.strip().lower()
        if key not in LANGUAGE_RUNNERS:
            return f"Unsupported language {language!r}. Supported: {', '.join(sorted(LANGUAGE_RUNNERS))}"
        if not docker_available():
            return (
                "Docker is not available (not installed, or the daemon isn't running) - "
                "cannot validate this code by execution. Report this plainly rather than "
                "assuming the code/test cases are correct."
            )

        normalized_cases = [tc if isinstance(tc, dict) else tc.model_dump() for tc in test_cases]
        try:
            report = run_against_test_cases(key, source_code, normalized_cases, timeout_seconds)
        except DockerNotAvailableError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - must never crash the crew; surface the failure instead
            return f"Execution failed unexpectedly: {exc}"

        if report.compile_error is not None:
            return f"COMPILE FAILED:\n{report.compile_error[-2000:]}"

        lines = [f"{report.passed_count}/{len(report.results)} test cases passed."]
        for result in report.results:
            if result.passed:
                lines.append(f"  [PASS] test case {result.index + 1}")
                continue
            status = "TIMED OUT" if result.timed_out else f"exit_code={result.exit_code}"
            lines.append(
                f"  [FAIL] test case {result.index + 1} ({status})\n"
                f"    input: {result.stdin!r}\n"
                f"    expected: {result.expected!r}\n"
                f"    actual stdout: {result.stdout!r}\n"
                f"    stderr: {result.stderr[-500:]!r}"
            )
        return "\n".join(lines)
