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

import re
import shlex
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
COMPILE_TIMEOUT_SECONDS = 120
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
        image="eclipse-temurin:21-jdk",
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
    "sed": LanguageRunner(
        image=BASH_RUNNER_IMAGE,
        filename="solution.sed",
        compile_cmd=None,
        run_cmd=["sed", "-Ef", "solution.sed"],
    ),
    "awk": LanguageRunner(
        image=BASH_RUNNER_IMAGE,
        filename="solution.awk",
        compile_cmd=None,
        run_cmd=["awk", "-f", "solution.awk"],
    ),
    "gawk": LanguageRunner(
        image=BASH_RUNNER_IMAGE,
        filename="solution.awk",
        compile_cmd=None,
        run_cmd=["gawk", "-f", "solution.awk"],
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
        run_cmd=[
            "sh",
            "-c",
            "mkdir -p /tmp/pgdata /run/postgresql && chown postgres:postgres /tmp/pgdata /run/postgresql && su-exec postgres initdb -D /tmp/pgdata >/dev/null 2>&1 && su-exec postgres pg_ctl -D /tmp/pgdata -w start >/dev/null 2>&1 && cat - solution.sql | su-exec postgres psql -q -d postgres",
        ],
    ),
    "python-db": LanguageRunner(
        image="klyra-python-postgres-runner:latest",
        filename="solution.py",
        compile_cmd=None,
        run_cmd=[
            "sh",
            "-c",
            "mkdir -p /tmp/pgdata /run/postgresql && chown postgres:postgres /tmp/pgdata /run/postgresql && su-exec postgres initdb -D /tmp/pgdata >/dev/null 2>&1 && su-exec postgres pg_ctl -D /tmp/pgdata -w start >/dev/null 2>&1 && cat - > schema.sql && su-exec postgres psql -q -d postgres -f schema.sql >/dev/null 2>&1 && for db in lis flis $(grep -oE \"(database|dbname)[[:space:]]*=[[:space:]]*['\\\"][a-zA-Z0-9_]+['\\\"]\" solution.py 2>/dev/null | sed -E \"s/.*['\\\"]([a-zA-Z0-9_]+)['\\\"].*/\\1/\"); do if [ \"$db\" != \"postgres\" ] && [ \"$db\" != \"template1\" ]; then su-exec postgres psql -q -d template1 -c \"DROP DATABASE IF EXISTS \\\"$db\\\" WITH (FORCE);\" -c \"CREATE DATABASE \\\"$db\\\" TEMPLATE postgres;\" >/dev/null 2>&1 || true; fi; done && for tf in $(grep -oE \"['\\\"][a-zA-Z0-9_.-]+\\.txt['\\\"]\" solution.py 2>/dev/null | tr -d \"'\\\"\"); do if [ ! -f \"$tf\" ]; then grep -vE \"^[[:space:]]*(CREATE|INSERT|DROP|ALTER|BEGIN|COMMIT|--|\\\\\\\\copy|SELECT|VALUES|\\)|;|$)\" schema.sql > \"$tf\" 2>/dev/null || cp schema.sql \"$tf\"; fi; done && PGUSER=postgres PGPASSWORD=postgres PGHOST=127.0.0.1 PGPORT=5432 PGDATABASE=postgres python3 solution.py < schema.sql",
        ],
    ),
}


_LANGUAGE_ALIASES: dict[str, str] = {
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


def detect_shell_script_dialect(source_code: str, default_lang: str = "bash") -> str:
    """Detect whether a Linux/shell script is pure `sed` (`sed -Ef`), pure
    `gawk` (`gawk -f`), pure `awk` (`awk -f`), or `bash` (`bash`)."""
    raw_lang = (default_lang or "bash").strip().lower()
    if raw_lang in ("gawk", "awk", "sed"):
        norm_default = raw_lang
    else:
        norm_default = "bash"

    code = (source_code or "").strip()
    if not code:
        return norm_default

    if code.startswith("```"):
        lines = code.splitlines()
        fence_lang = lines[0][3:].strip().lower()
        if fence_lang in ("gawk", "awk", "sed"):
            norm_default = fence_lang
        if len(lines) >= 2 and lines[-1].strip() == "```":
            code = "\n".join(lines[1:-1]).strip()

    lines = [ln.strip() for ln in code.splitlines() if ln.strip()]
    if not lines:
        return norm_default

    first = lines[0].lower()
    if first.startswith("#!"):
        if "gawk" in first:
            return "gawk"
        if "awk" in first:
            return "awk"
        if "sed" in first:
            return "sed"
        if "bash" in first or "/sh" in first or "env sh" in first:
            return "bash"

    non_comment = [ln for ln in lines if not ln.startswith("#")]
    if not non_comment:
        return norm_default

    body = "\n".join(non_comment)

    # Unmistakable bash syntax: shell invocations of sed/awk/grep or bash keywords `then`/`fi`/`do`/`done`/`esac`
    bash_syntax_re = re.compile(
        r"(?:^\s*(?:sed|g?awk|grep|egrep|fgrep|cat|echo|read|find|sort|uniq|cut|tr|paste|wc|head|tail|chmod|mkdir|rm|cp|mv)\s+)"
        r"|\b(?:then|fi|do|done|esac)\b",
        re.MULTILINE,
    )
    has_bash_syntax = bool(bash_syntax_re.search(body))

    # GNU Awk (`gawk`) specific extensions (`gensub`, `asort`, `asorti`, `patsplit`, `systime`, `strftime`, `mktime`, `PROCINFO`, `IGNORECASE`, `FPAT`, `BEGINFILE`, `ENDFILE`)
    gawk_ext_re = re.compile(
        r"\b(?:gensub|asort|asorti|patsplit|systime|strftime|mktime|and|or|xor|lshift|rshift)\s*\("
        r"|\b(?:PROCINFO|IGNORECASE|FIELDWIDTHS|FPAT|RT|BEGINFILE|ENDFILE)\b"
    )
    # Standard AWK markers (`BEGIN`, `END`, `NR`, `NF`, `FS`, `OFS`, etc.)
    awk_marker_re = re.compile(
        r"\b(?:BEGIN|END)\s*\{|\b(?:NR|FNR|NF|FS|OFS|RS|ORS|FILENAME|ENVIRON|ARGC|ARGV)\b|\b(?:gsub|sub|substr|split|tolower|toupper)\s*\(",
    )
    if not has_bash_syntax and "{" in body and "}" in body and (
        awk_marker_re.search(body) or gawk_ext_re.search(body) or norm_default in ("awk", "gawk")
    ):
        if gawk_ext_re.search(body) or norm_default == "gawk":
            return "gawk"
        return "awk"

    # Pure SED detection: every non-comment line is a sed command (`s/.../.../`, `/pat/d`, `y/.../.../`, etc.)
    sed_line_re = re.compile(
        r"^(?:[0-9]+|\$|/(?:\\.|[^/])+/|[0-9]+\s*,\s*(?:[0-9]+|\$|/(?:\\.|[^/])+/)?"
        r"|/(?:\\.|[^/])*/\s*,\s*/(?:\\.|[^/])*/)?\s*!?\s*"
        r"(?:[sy](.).*\1.*\1[gip0-9Ims]*|[daicpqn=btThHgGxNPl]|r\s+\S+|w\s+\S+|\{|\}|:.*)\s*;?\s*$"
    )
    if not has_bash_syntax and (
        all(sed_line_re.match(ln) for ln in non_comment)
        or (
            norm_default == "sed"
            and any(
                ln.startswith(("s/", "s|", "s#", "y/")) or ln.endswith(("/d", "/p"))
                for ln in non_comment
            )
        )
    ):
        return "sed"

    if norm_default in ("sed", "awk", "gawk") and has_bash_syntax:
        return "bash"

    return norm_default


def _resolve_runner_language(language: str, source_code: str) -> str:
    """Normalize language aliases, auto-upgrade `python` to `python-db` when `psycopg2`
    is imported, and auto-detect pure `sed` / `awk` / `gawk` scripts vs `bash`."""
    raw = (language or "").strip().lower()
    key = _LANGUAGE_ALIASES.get(raw, raw)
    if key == "python" and source_code and ("psycopg2" in source_code or "pg8000" in source_code):
        return "python-db"
    if key in ("bash", "sed", "awk", "gawk") and source_code:
        return detect_shell_script_dialect(source_code, default_lang=key)
    return key


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
    
    if language in ("bash", "sed", "awk", "gawk", "sql"):
        image_name = BASH_RUNNER_IMAGE
        dockerfile_func = _bash_runner_dockerfile
        dockerfile_name = "docker/bash-runner.Dockerfile"
    elif language == "python-db":
        image_name = PYTHON_POSTGRES_RUNNER_IMAGE
        dockerfile_func = _python_postgres_runner_dockerfile
        dockerfile_name = "docker/python-postgres-runner.Dockerfile"
    else:
        return None

    if _image_exists(image_name):
        return None

    dockerfile = dockerfile_func()
    if not dockerfile:
        return f"Cannot build {image_name}: {dockerfile_name} not found."

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
    __test__ = False

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


def _clean_source_code(source_code: str, language: str) -> str:
    """Sanitize LLM-generated source code before writing to the sandbox file.

    1. Strips wrapping markdown code fences (```bash ... ```).
    2. Detects and unwraps heredoc wrappers (e.g. `cat << 'EOF' > solution.sh ... EOF`),
       which LLMs sometimes generate when trying to emulate an interactive shell session.
    """
    code = source_code.strip()

    # 1. Strip markdown code fences if wrapped
    if code.startswith("```"):
        lines = code.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            code = "\n".join(lines[1:-1]).strip()

    # 2. Extract inner code if wrapped in a heredoc redirection
    # e.g., cat << 'EOF' > solution.sh \n <code> \n EOF
    heredoc_match = re.search(
        r"cat\s*<<\s*['\"]?(\w+)['\"]?\s*>\s*[^\n]+\n(.*?)\n\1",
        code,
        re.DOTALL,
    )
    if heredoc_match:
        code = heredoc_match.group(2).strip()

    # 3. For Java: Solution.java requires `public class Solution`. If the LLM named
    # the sole public class `Main` or another identifier, rename it to `Solution`.
    if language == "java" and not re.search(r"\bclass\s+Solution\b", code):
        code = re.sub(r"\bpublic\s+class\s+[A-Za-z_][A-Za-z0-9_]*\b", "public class Solution", code, count=1)

    return code


def _build_batch_runner_script(
    key: str,
    runner: LanguageRunner,
    num_cases: int,
    timeout_seconds: int,
) -> str:
    """Build a self-contained POSIX shell script that compiles once (if needed),
    initializes any stateful services once (e.g. PostgreSQL), and runs all
    test cases sequentially inside a single pooled Docker container."""
    lines = [
        "#!/bin/sh",
        "cd /sandbox || exit 101",
    ]
    if runner.compile_cmd:
        compile_str = shlex.join(runner.compile_cmd)
        lines.extend(
            [
                f"if ! {compile_str} >/sandbox/_klyra_batch/compile.err 2>&1; then",
                "    exit 100",
                "fi",
            ]
        )

    if key in ("postgresql", "python-db"):
        lines.extend(
            [
                "mkdir -p /tmp/pgdata /run/postgresql && chown postgres:postgres /tmp/pgdata /run/postgresql",
                "su-exec postgres initdb -D /tmp/pgdata >/dev/null 2>&1",
                "su-exec postgres pg_ctl -D /tmp/pgdata -w start >/dev/null 2>&1",
            ]
        )

    for i in range(num_cases):
        in_file = f"/sandbox/_klyra_batch/input_{i}.txt"
        out_file = f"/sandbox/_klyra_batch/stdout_{i}.txt"
        err_file = f"/sandbox/_klyra_batch/stderr_{i}.txt"
        exit_file = f"/sandbox/_klyra_batch/exit_{i}.txt"

        if key == "sql":
            lines.append(
                f"rm -f /tmp/klyra_test_{i}.db && "
                f"timeout {timeout_seconds}s sh -c 'cat {in_file} solution.sql | sqlite3 /tmp/klyra_test_{i}.db' "
                f">{out_file} 2>{err_file}"
            )
        elif key == "postgresql":
            lines.extend(
                [
                    "su-exec postgres dropdb --if-exists klyra_test >/dev/null 2>&1 || true",
                    "su-exec postgres createdb klyra_test >/dev/null 2>&1 || true",
                    f"timeout {timeout_seconds}s sh -c 'cat {in_file} solution.sql | su-exec postgres psql -q -d klyra_test' "
                    f">{out_file} 2>{err_file}",
                ]
            )
        elif key == "python-db":
            lines.extend(
                [
                    "rm -f /sandbox/*.txt /tmp/klyra_seed.sql",
                    'su-exec postgres psql -q -d template1 -c "DROP DATABASE IF EXISTS postgres WITH (FORCE);" -c "CREATE DATABASE postgres;" >/dev/null 2>&1 || true',
                    f'grep -vE "^[[:space:]]*(INIT_SCHEMA|INSERT_INPUT|SCHEMA:|INPUT:)[[:space:]]*$" {in_file} | grep -E "(;|^[[:space:]]*\\\\\\\\copy)" > /tmp/klyra_seed.sql 2>/dev/null || cp {in_file} /tmp/klyra_seed.sql',
                    "su-exec postgres psql -q -d postgres -f /tmp/klyra_seed.sql >/dev/null 2>&1 || true",
                    'for db in lis flis $(grep -oE "(database|dbname)[[:space:]]*=[[:space:]]*[\'\\"][a-zA-Z0-9_]+[\'\\"]" /sandbox/solution.py 2>/dev/null | sed -E "s/.*[\'\\"]([a-zA-Z0-9_]+)[\'\\"].*/\\1/"); do if [ "$db" != "postgres" ] && [ "$db" != "template1" ]; then su-exec postgres psql -q -d template1 -c "DROP DATABASE IF EXISTS \\"$db\\" WITH (FORCE);" -c "CREATE DATABASE \\"$db\\" TEMPLATE postgres;" >/dev/null 2>&1 || true; fi; done',
                    f'for tf in $(grep -oE "[\'\\"][a-zA-Z0-9_.-]+\\.txt[\'\\"]" /sandbox/solution.py 2>/dev/null | tr -d "\'\\""); do if [ ! -f "/sandbox/$tf" ]; then grep -vE "^[[:space:]]*(INIT_SCHEMA|INSERT_INPUT|SCHEMA:|INPUT:|CREATE|INSERT|DROP|ALTER|BEGIN|COMMIT|--|\\\\\\\\copy|SELECT|VALUES|\\)|;|$)" {in_file} > "/sandbox/$tf" 2>/dev/null || cp {in_file} "/sandbox/$tf"; fi; done',
                    f"timeout {timeout_seconds}s env PGUSER=postgres PGPASSWORD=postgres PGHOST=127.0.0.1 PGPORT=5432 PGDATABASE=postgres python3 solution.py <{in_file} >{out_file} 2>{err_file}",
                ]
            )
        else:
            run_str = shlex.join(runner.run_cmd)
            lines.append(
                f"timeout {timeout_seconds}s {run_str} <{in_file} >{out_file} 2>{err_file}"
            )
        lines.append(f"echo $? >{exit_file}")

    if key in ("postgresql", "python-db"):
        lines.append("su-exec postgres pg_ctl -D /tmp/pgdata -m fast stop >/dev/null 2>&1 || true")

    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def run_against_test_cases_batch(
    language: str,
    source_code: str,
    test_cases: list[dict],
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ExecutionReport:
    """Compile `source_code` (if needed) and execute all `test_cases` inside a
    single pooled Docker container lifecycle, eliminating N-container startup
    overhead and repeated PostgreSQL cluster initialization."""
    key = _resolve_runner_language(language, source_code)
    if key not in LANGUAGE_RUNNERS:
        raise ValueError(f"Unsupported language {language!r}. Supported: {sorted(LANGUAGE_RUNNERS)}")
    runner = LANGUAGE_RUNNERS[key]
    image_error = ensure_language_image(key)
    if image_error:
        raise DockerNotAvailableError(image_error)

    clean_code = _clean_source_code(source_code, key)
    with tempfile.TemporaryDirectory(prefix="klyra-batch-") as tmp:
        workdir = Path(tmp)
        workdir.chmod(0o777)
        (workdir / runner.filename).write_text(clean_code, encoding="utf-8")

        batch_dir = workdir / "_klyra_batch"
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_dir.chmod(0o777)

        for i, case in enumerate(test_cases):
            in_path = batch_dir / f"input_{i}.txt"
            in_path.write_text(case.get("input", ""), encoding="utf-8")
            in_path.chmod(0o666)

        script_path = batch_dir / "run_batch.sh"
        script_path.write_text(
            _build_batch_runner_script(key, runner, len(test_cases), timeout_seconds),
            encoding="utf-8",
        )
        script_path.chmod(0o755)

        total_timeout = (COMPILE_TIMEOUT_SECONDS if runner.compile_cmd else 5) + max(
            1, len(test_cases)
        ) * (timeout_seconds + 2)

        batch_run = _docker_run(
            image=runner.image,
            workdir=workdir,
            cmd=["sh", "/sandbox/_klyra_batch/run_batch.sh"],
            stdin_text=None,
            timeout_seconds=total_timeout,
        )

        if batch_run.exit_code == 125 or (
            batch_run.timed_out
            and any(
                marker in (batch_run.stderr or "")
                for marker in ("Unable to find image", "Pulling from", "Downloading", "Extracting")
            )
        ):
            err_msg = (
                batch_run.stderr.strip()
                or batch_run.stdout.strip()
                or f"Docker failed to start container for image {runner.image!r} (exit 125)."
            )
            raise DockerNotAvailableError(err_msg)

        compile_err_path = batch_dir / "compile.err"
        if batch_run.exit_code == 100:
            err_text = (
                compile_err_path.read_text(encoding="utf-8", errors="replace").strip()
                if compile_err_path.exists()
                else ""
            )
            if not err_text:
                err_text = batch_run.stderr.strip() or batch_run.stdout.strip() or "Compilation failed."
            return ExecutionReport(compiled=False, compile_error=err_text, results=[])

        if (
            batch_run.timed_out
            and runner.compile_cmd
            and not (batch_dir / "exit_0.txt").exists()
        ):
            return ExecutionReport(compiled=False, compile_error="Compilation timed out.", results=[])

        results: list[TestCaseResult] = []
        for i, case in enumerate(test_cases):
            stdin_text = case.get("input", "")
            expected = case.get("expected_output", "")
            out_path = batch_dir / f"stdout_{i}.txt"
            err_path = batch_dir / f"stderr_{i}.txt"
            exit_path = batch_dir / f"exit_{i}.txt"

            if exit_path.exists():
                raw_exit = exit_path.read_text(encoding="utf-8", errors="replace").strip()
                code_val = int(raw_exit) if raw_exit.lstrip("-").isdigit() else batch_run.exit_code
                stdout_val = (
                    out_path.read_text(encoding="utf-8", errors="replace")
                    if out_path.exists()
                    else ""
                )
                stderr_val = (
                    err_path.read_text(encoding="utf-8", errors="replace")
                    if err_path.exists()
                    else ""
                )
                timed_out = code_val == 124
                exit_code = None if timed_out else code_val
            elif batch_run.timed_out:
                stdout_val = (
                    out_path.read_text(encoding="utf-8", errors="replace")
                    if out_path.exists()
                    else batch_run.stdout
                )
                stderr_val = (
                    err_path.read_text(encoding="utf-8", errors="replace")
                    if err_path.exists()
                    else batch_run.stderr
                )
                timed_out = True
                exit_code = None
            else:
                # Fallback when subprocess.run is mocked without writing per-case batch files
                stdout_val = batch_run.stdout
                stderr_val = batch_run.stderr
                timed_out = False
                exit_code = batch_run.exit_code

            passed = (
                not timed_out
                and exit_code == 0
                and _normalize(stdout_val) == _normalize(expected)
            )
            results.append(
                TestCaseResult(
                    index=i,
                    passed=passed,
                    stdin=stdin_text,
                    expected=expected,
                    stdout=stdout_val,
                    stderr=stderr_val,
                    timed_out=timed_out,
                    exit_code=exit_code,
                )
            )

        return ExecutionReport(compiled=True, compile_error=None, results=results)


def run_against_test_cases(
    language: str,
    source_code: str,
    test_cases: list[dict],
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    *,
    batch: bool = False,
) -> ExecutionReport:
    """Compiles `source_code` (if the language needs it) once, then runs it
    against `test_cases` (`{"input": str, "expected_output": str}`), comparing
    normalized stdout. When `batch=True`, all test cases execute inside a single
    pooled container instead of spawning one container per test case."""
    if batch:
        return run_against_test_cases_batch(
            language=language,
            source_code=source_code,
            test_cases=test_cases,
            timeout_seconds=timeout_seconds,
        )
    key = _resolve_runner_language(language, source_code)
    if key not in LANGUAGE_RUNNERS:
        raise ValueError(f"Unsupported language {language!r}. Supported: {sorted(LANGUAGE_RUNNERS)}")
    runner = LANGUAGE_RUNNERS[key]
    image_error = ensure_language_image(key)
    if image_error:
        raise DockerNotAvailableError(image_error)

    clean_code = _clean_source_code(source_code, key)
    with tempfile.TemporaryDirectory(prefix="klyra-run-") as tmp:
        workdir = Path(tmp)
        workdir.chmod(0o777)  # container may run as a non-root/different uid than this process
        (workdir / runner.filename).write_text(clean_code)

        if runner.compile_cmd:
            compile_result = _docker_run(
                image=runner.image,
                workdir=workdir,
                cmd=runner.compile_cmd,
                stdin_text=None,
                timeout_seconds=COMPILE_TIMEOUT_SECONDS,
            )
            if compile_result.exit_code == 125:
                err_msg = (
                    compile_result.stderr.strip()
                    or compile_result.stdout.strip()
                    or f"Docker failed to start container for image {runner.image!r} (exit 125)."
                )
                raise DockerNotAvailableError(err_msg)
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
            if run_result.exit_code == 125:
                err_msg = (
                    run_result.stderr.strip()
                    or run_result.stdout.strip()
                    or f"Docker failed to start container for image {runner.image!r} (exit 125)."
                )
                raise DockerNotAvailableError(err_msg)
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
    batch: bool = Field(
        default=False,
        description="When true, compile and execute all test cases inside a single pooled Docker container.",
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
        batch: bool = False,
    ) -> str:
        key = _resolve_runner_language(language, source_code)
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
            if batch:
                report = run_against_test_cases(
                    key, source_code, normalized_cases, timeout_seconds, batch=True
                )
            else:
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
            fail_line = (
                f"  [FAIL] test case {result.index + 1} ({status})\n"
                f"    input: {result.stdin!r}\n"
                f"    expected: {result.expected!r}\n"
                f"    actual stdout: {result.stdout!r}\n"
                f"    stderr: {result.stderr[-500:]!r}"
            )
            if key == "bash" and "python3: command not found" in result.stderr:
                fail_line += "\n    hint: python3 is not available in the bash runner. Write standard bash/grep/sed/awk code."
            elif not result.stdout and result.expected and result.exit_code == 0:
                fail_line += "\n    hint: Program produced empty stdout. Check if your filters/logic excluded all input lines, or if the expected output contradicts problem constraints."
            lines.append(fail_line)
        return "\n".join(lines)
