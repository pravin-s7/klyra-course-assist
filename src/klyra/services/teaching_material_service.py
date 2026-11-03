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
    from klyra.services.skill_service import build_skill_prompt_inputs
    from klyra.utils import fs
    from klyra.utils.parsing import parse_week_scope

    fs.check_subject_exists(subject)

    if not fs.subject_dir(subject).exists():
        raise ValueError(f"Subject '{subject}' does not exist in docs/. Please run ingest first.")

    (fs.generated_dir(subject) / "teaching").mkdir(parents=True, exist_ok=True)
    inputs = {
        "subject": subject,
        "session_name": fs.slugify(session_name),
        "syllabus_scope": parse_week_scope(scope),
        "concepts": concepts or "any",
        "notes": notes or "(none - use your judgement on structure and depth)",
        **build_skill_prompt_inputs(subject),
    }
    return TeachingMaterialCrew().crew().kickoff(inputs=inputs)


def run_teaching_revision(subject: str, session_name: str, notes: str) -> Any:
    """Unlike QP/coding revision, `notes` is required here: teaching
    material has no automated review to fall back on, so without
    instructions there is nothing to revise."""
    from klyra.crews.teaching_revision_crew.teaching_revision_crew import TeachingRevisionCrew
    from klyra.services.skill_service import build_skill_prompt_inputs
    from klyra.utils import fs

    fs.check_subject_exists(subject)

    slug = fs.slugify(session_name)
    teaching_dir = fs.generated_dir(subject) / "teaching"
    path = teaching_dir / f"{slug}-teaching-material.md"
    existing = path.read_text() if path.exists() else f"(no existing teaching material found at {path})"

    answers_path = teaching_dir / f"{slug}-teaching-answers.md"
    existing_answers = answers_path.read_text() if answers_path.exists() else f"(no existing teaching answers found at {answers_path})"

    inputs = {
        "subject": subject,
        "session_name": slug,
        "existing_material_content": existing,
        "existing_answers_content": existing_answers,
        "manual_instructions": notes,
        **build_skill_prompt_inputs(subject),
    }
    
    import shutil

    with fs.crewai_storage_context(f".memories/{subject}/{slug}"):
        result = TeachingRevisionCrew().crew().kickoff(inputs=inputs)

    # Synchronize revised companion files (.md and .json) to the active canonical files
    for base, revised in [
        ("teaching-material", "teaching-material-revised"),
        ("teaching-answers", "teaching-answers-revised"),
    ]:
        for ext in (".md", ".json"):
            rev_path = teaching_dir / f"{slug}-{revised}{ext}"
            base_path = teaching_dir / f"{slug}-{base}{ext}"
            if rev_path.exists():
                shutil.copy2(rev_path, base_path)

    return result

