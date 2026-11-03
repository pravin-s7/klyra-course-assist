"""Filesystem conventions for Klyra.

Fixed layout (see .cursor/rules/klyra.mdc):

    docs/<subject>/week_1/ ... week_12/
    docs/<subject>/additional/
    docs/<subject>/pyq/
    docs/<subject>/transcripts/
    docs/<subject>/generated/
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

DOCS_ROOT = Path("docs")
NUM_WEEKS = 12

ADDITIONAL_DIR_NAME = "additional"
PYQ_DIR_NAME = "pyq"
TRANSCRIPTS_DIR_NAME = "transcripts"
GENERATED_DIR_NAME = "generated"
ASSETS_DIR_NAME = ".assets"
MANIFEST_NAME = ".ingest_manifest.json"
SKILL_FILE_NAME = "skill.md"

# Office formats are converted to PDF (via LibreOffice) before multimodal
# parsing; plain-text formats are read directly; anything else supported is
# treated as code/text and read directly too.
OFFICE_SUFFIXES = {".ppt", ".pptx", ".doc", ".docx"}
PDF_SUFFIX = ".pdf"
PLAIN_TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}
CODE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".sql", ".sh", ".ipynb", ".json",
    ".yaml", ".yml", ".r", ".m", ".scala", ".kt",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SUPPORTED_SUFFIXES = OFFICE_SUFFIXES | {PDF_SUFFIX} | PLAIN_TEXT_SUFFIXES | CODE_SUFFIXES | IMAGE_SUFFIXES

def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug or "untitled"


_SUBJECT_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_subject(subject: str) -> str:
    """Subject becomes a filesystem path segment - reject anything that
    could path-traverse (`..`, `/`) instead of silently normalizing it, so
    callers get a clear error rather than writing/reading the wrong place.
    Enforced centrally in subject_dir() so every caller (CLI args, and any
    future API/agent-supplied value) goes through the same check."""
    if not _SUBJECT_RE.match(subject):
        raise ValueError(
            f"Invalid subject {subject!r}: must contain only letters, digits, "
            "'-' and '_' (it's used to build a filesystem path)."
        )
    return subject.lower()


def week_dir_name(week: int) -> str:
    if not 1 <= week <= NUM_WEEKS:
        raise ValueError(f"week must be between 1 and {NUM_WEEKS}, got {week}")
    return f"week_{week}"


def subject_dir(subject: str, root: Path = DOCS_ROOT) -> Path:
    return root / validate_subject(subject)


def check_subject_exists(subject: str, root: Path = DOCS_ROOT) -> None:
    path = subject_dir(subject, root)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Subject directory does not exist: {path}. Please create it manually before running tools.")


def week_dir(subject: str, week: int, root: Path = DOCS_ROOT) -> Path:
    return subject_dir(subject, root) / week_dir_name(week)


def additional_dir(subject: str, root: Path = DOCS_ROOT) -> Path:
    return subject_dir(subject, root) / ADDITIONAL_DIR_NAME


def pyq_dir(subject: str, root: Path = DOCS_ROOT) -> Path:
    return subject_dir(subject, root) / PYQ_DIR_NAME


def transcripts_dir(subject: str, root: Path = DOCS_ROOT) -> Path:
    return subject_dir(subject, root) / TRANSCRIPTS_DIR_NAME


def generated_dir(subject: str, root: Path = DOCS_ROOT) -> Path:
    return subject_dir(subject, root) / GENERATED_DIR_NAME


def coding_package_dir(subject: str, name: str, root: Path = DOCS_ROOT) -> Path:
    """Student-facing problem package: README, public/private test folders,
    evaluation_script. Lives next to the generated markdown reports."""
    return generated_dir(subject, root) / "coding" / slugify(name)


def assets_dir(subject: str, root: Path = DOCS_ROOT) -> Path:
    """Where extracted images (diagrams pulled out of source PDFs) live."""
    return subject_dir(subject, root) / ASSETS_DIR_NAME


def subject_skill_path(subject: str, root: Path = DOCS_ROOT) -> Path:
    """Per-subject skill configuration file (`docs/<subject>/skill.md`)."""
    return subject_dir(subject, root) / SKILL_FILE_NAME


def markdown_image_path(subject: str, image_path: Path, root: Path = DOCS_ROOT) -> str:
    """Relative path from a generated QP file's directory to an asset image,
    ready to paste directly into markdown image syntax."""
    rel = os.path.relpath(image_path, start=generated_dir(subject, root))
    return Path(rel).as_posix()


def is_supported_file(path: Path) -> bool:
    return (
        path.is_file()
        and not path.name.startswith(".")
        and path.name != SKILL_FILE_NAME
        and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


import contextlib
from typing import Iterator


@contextlib.contextmanager
def crewai_storage_context(storage_dir: str) -> Iterator[str]:
    """Thread/context-safe helper to scope `CREWAI_STORAGE_DIR` and CrewAI's
    `db_storage_path` without failing if `crewai_core` is absent."""
    abs_dir = os.path.abspath(storage_dir)
    prev = os.environ.get("CREWAI_STORAGE_DIR")
    os.environ["CREWAI_STORAGE_DIR"] = abs_dir
    try:
        from unittest.mock import patch

        stack = contextlib.ExitStack()
        for target in ("crewai_core.paths.db_storage_path", "crewai.utilities.paths.db_storage_path"):
            try:
                stack.enter_context(patch(target, return_value=abs_dir))
                break
            except Exception:
                pass
        with stack:
            yield abs_dir
    finally:
        if prev is None:
            os.environ.pop("CREWAI_STORAGE_DIR", None)
        else:
            os.environ["CREWAI_STORAGE_DIR"] = prev


@dataclass(frozen=True)
class SourceFile:
    path: Path
    subject: str
    week: int | None  # None for additional/, pyq/, transcripts/ (not week-tagged)
    category: str  # "week" | "additional" | "pyq" | "transcript"

    def sha256(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.path.read_bytes())
        return digest.hexdigest()


def missing_weeks(subject: str, root: Path = DOCS_ROOT) -> list[int]:
    gaps = []
    for week in range(1, NUM_WEEKS + 1):
        wdir = week_dir(subject, week, root)
        if not wdir.exists() or not any(is_supported_file(p) for p in wdir.rglob("*")):
            gaps.append(week)
    return gaps


def discover_subject_files(
    subject: str, 
    week: int | None = None, 
    categories: list[str] | None = None,
    file_path: str | None = None
) -> list[SourceFile]:
    """Find all ingestible documents for a subject, optionally filtered by week and categories."""
    base = subject_dir(subject)
    if not file_path and not base.exists():
        raise FileNotFoundError(f"Subject directory does not exist: {base}")
    sources = []

    if file_path:
        # User requested a specific file
        path = Path(file_path)
        if path.is_file():
            # Try to infer week and category from the path
            inferred_category = "week"
            inferred_week = None
            parts = path.parts
            
            if "pyq" in parts:
                inferred_category = "pyq"
            elif "transcripts" in parts:
                inferred_category = "transcript"
            elif "additional" in parts:
                inferred_category = "additional"
                
            for part in parts:
                if part.startswith("week_"):
                    try:
                        inferred_week = int(part.split("_")[1])
                    except ValueError:
                        pass

            sources.append(SourceFile(
                path=path,
                subject=subject,
                week=inferred_week,
                category=inferred_category
            ))
        return sources

    if not categories:
        categories = ["week", "pyq", "transcript", "additional"]

    if "week" in categories:
        for week_dir in base.glob("week_*"):
            w = int(week_dir.name.split("_")[1])
            if week is not None and w != week:
                continue
            for path in week_dir.rglob("*"):
                if is_supported_file(path):
                    sources.append(SourceFile(path=path, subject=subject, week=w, category="week"))

    if "pyq" in categories and (week is None):
        d = base / "pyq"
        if d.exists():
            for path in d.rglob("*"):
                if is_supported_file(path):
                    sources.append(SourceFile(path=path, subject=subject, week=None, category="pyq"))

    if "transcript" in categories and (week is None):
        d = base / "transcripts"
        if d.exists():
            for path in d.rglob("*"):
                if is_supported_file(path):
                    sources.append(SourceFile(path=path, subject=subject, week=None, category="transcript"))

    if "additional" in categories and (week is None):
        d = base / "additional"
        if d.exists():
            for path in d.rglob("*"):
                if is_supported_file(path):
                    sources.append(SourceFile(path=path, subject=subject, week=None, category="additional"))

    return sorted(sources, key=lambda s: str(s.path))


def remove_from_manifest(subject: str, file_path: str) -> None:
    manifest = load_manifest(subject)
    key = str(file_path)
    if key in manifest:
        del manifest[key]
        save_manifest(subject, manifest)


def manifest_path(subject: str, root: Path = DOCS_ROOT) -> Path:
    return subject_dir(subject, root) / MANIFEST_NAME


def load_manifest(subject: str, root: Path = DOCS_ROOT) -> dict:
    path = manifest_path(subject, root)
    if path.exists():
        text = path.read_text().strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}
    return {}


def save_manifest(subject: str, manifest: dict, root: Path = DOCS_ROOT) -> None:
    manifest_path(subject, root).write_text(json.dumps(manifest, indent=2, sort_keys=True))
