"""Reads back the markdown and structured JSON artifacts a generation/revision run produced."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from klyra.utils import fs

# Suffixes written by generation_crew / revision_crew, in a sensible display order.
_QP_SUFFIXES = (
    "qp",
    "discrepancy-report",
    "answer-key",
    "final-summary",
    "qp-revised",
    "discrepancy-report-revised",
    "answer-key-revised",
    "final-summary-revised",
)

# Suffixes written by coding_problem_crew / coding_revision_crew.
_CODING_SUFFIXES = (
    "problems",
    "coding-discrepancy-report",
    "testcases",
    "final-summary",
    "problems-revised",
    "coding-discrepancy-report-revised",
    "testcases-revised",
    "final-summary-revised",
)

# Suffixes written by teaching_material_crew / teaching_revision_crew.
_TEACHING_SUFFIXES = (
    "teaching-material",
    "teaching-answers",
    "teaching-material-revised",
    "teaching-answers-revised",
)



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


def _read_json_suffixes(base_dir: Path, slug: str, suffixes: tuple[str, ...]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for suffix in suffixes:
        path = base_dir / f"{slug}-{suffix}.json"
        if path.exists():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                data[suffix] = parsed
                data[path.name] = parsed
            except Exception:
                continue
    return data


def get_structured_outputs(
    subject: str,
    name: str | None = None,
    kind: str = "qp",
    category: str | None = None,
) -> dict[str, Any]:
    """Read structured `.json` sidecar artifacts for a QP (`kind='qp'`),
    coding problem set (`kind='coding'`), or teaching session (`kind='teaching'`).
    """
    fs.validate_subject(subject)
    eff_kind = (category or kind or "qp").strip().lower()
    if name:
        slug = fs.slugify(name)
        if eff_kind == "qp":
            return _read_json_suffixes(fs.generated_dir(subject), slug, _QP_SUFFIXES)
        if eff_kind == "coding":
            return _read_json_suffixes(fs.generated_dir(subject) / "coding", slug, _CODING_SUFFIXES)
        if eff_kind == "teaching":
            return _read_json_suffixes(fs.generated_dir(subject) / "teaching", slug, _TEACHING_SUFFIXES)
        raise ValueError(f"Unknown artifact kind '{eff_kind}'. Expected one of: qp, coding, teaching.")

    gen_dir = fs.generated_dir(subject)
    if not gen_dir.exists():
        return {}
    out: dict[str, Any] = {}
    for p in sorted(gen_dir.rglob("*.json")):
        try:
            out[p.name] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return out
