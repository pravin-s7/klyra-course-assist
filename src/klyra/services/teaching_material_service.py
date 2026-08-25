"""Independent utility: prepare teaching material for a live session, and
its on-demand revision pass.
"""
from __future__ import annotations

from typing import Any


def run_teaching_material(
    subject: str,
    session_name: str,
    scope: str,
    concepts: str | None = None,
    notes: str | None = None,
) -> Any:
    import os

    os.environ["CREWAI_STORAGE_DIR"] = f".db/{subject}"

    from klyra.crews.teaching_material_crew.teaching_material_crew import TeachingMaterialCrew
    from klyra.utils import fs
    from klyra.utils.parsing import parse_week_scope

    (fs.generated_dir(subject) / "teaching").mkdir(parents=True, exist_ok=True)
    inputs = {
        "subject": subject,
        "session_name": fs.slugify(session_name),
        "syllabus_scope": parse_week_scope(scope),
        "concepts": concepts or "any",
        "notes": notes or "(none - use your judgement on structure and depth)",
    }
    return TeachingMaterialCrew().crew().kickoff(inputs=inputs)


def run_teaching_revision(subject: str, session_name: str, notes: str) -> Any:
    """Unlike QP/coding revision, `notes` is required here: teaching
    material has no automated review to fall back on, so without
    instructions there is nothing to revise."""
    from klyra.crews.teaching_revision_crew.teaching_revision_crew import TeachingRevisionCrew
    from klyra.utils import fs

    slug = fs.slugify(session_name)
    path = fs.generated_dir(subject) / "teaching" / f"{slug}-teaching-material.md"
    existing = path.read_text() if path.exists() else f"(no existing teaching material found at {path})"

    inputs = {
        "subject": subject,
        "session_name": slug,
        "existing_material_content": existing,
        "manual_instructions": notes,
    }
    
    import os
    from unittest.mock import patch
    import crewai_core.paths
    
    mem_dir = os.path.abspath(f".memories/{subject}/{slug}")
    with patch("crewai_core.paths.db_storage_path", return_value=mem_dir):
        return TeachingRevisionCrew().crew().kickoff(inputs=inputs)
