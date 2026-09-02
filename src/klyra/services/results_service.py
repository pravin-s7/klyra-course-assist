"""Reads back the markdown artifacts a generation/revision run produced."""
from __future__ import annotations

from pathlib import Path

from klyra.utils import fs

# Suffixes written by generation_crew / revision_crew, in a sensible display order.
_QP_SUFFIXES = ("qp", "discrepancy-report", "answer-key", "final-summary", "qp-revised")

# Suffixes written by coding_problem_crew / coding_revision_crew.
_CODING_SUFFIXES = (
    "problems",
    "coding-discrepancy-report",
    "testcases",
    "final-summary",
    "problems-revised",
)

# Suffixes written by teaching_material_crew / teaching_revision_crew.
_TEACHING_SUFFIXES = ("teaching-material", "teaching-material-revised")


def _read_suffixes(base_dir: Path, slug: str, suffixes: tuple[str, ...]) -> dict[str, str]:
    files: dict[str, str] = {}
    for suffix in suffixes:
        path = base_dir / f"{slug}-{suffix}.md"
        if path.exists():
            files[suffix] = path.read_text()
    return files


def get_generated_files(subject: str, qp_name: str) -> dict[str, str]:
    """QP artifacts: docs/<subject>/generated/<qp_name>-*.md"""
    fs.validate_subject(subject)
    slug = fs.slugify(qp_name)
    return _read_suffixes(fs.generated_dir(subject), slug, _QP_SUFFIXES)


def get_coding_package_listing(subject: str, name: str) -> tuple[str | None, list[str]]:
    """On-disk student package: README, public/private test folders, evaluation_script."""
    fs.validate_subject(subject)
    pkg = fs.coding_package_dir(subject, name)
    if not pkg.exists():
        return None, []
    files = sorted(p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file())
    return pkg.as_posix(), files


def get_coding_problem_files(subject: str, name: str) -> dict[str, str]:
    """Coding-problem artifacts: docs/<subject>/generated/coding/<name>-*.md"""
    fs.validate_subject(subject)
    slug = fs.slugify(name)
    return _read_suffixes(fs.generated_dir(subject) / "coding", slug, _CODING_SUFFIXES)


def get_teaching_material(subject: str, session_name: str) -> dict[str, str]:
    """Teaching-material artifacts: docs/<subject>/generated/teaching/<session_name>-*.md"""
    fs.validate_subject(subject)
    slug = fs.slugify(session_name)
    return _read_suffixes(fs.generated_dir(subject) / "teaching", slug, _TEACHING_SUFFIXES)
