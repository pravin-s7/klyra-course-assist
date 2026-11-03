"""Revision: fix a generated QP's flagged issues (plus optional manual edits)."""
from __future__ import annotations

from typing import Any


def run_revision(subject: str, qp_name: str, notes: str | None = None) -> Any:
    from klyra.crews.revision_crew.revision_crew import RevisionCrew
    from klyra.services.skill_service import build_skill_prompt_inputs
    from klyra.utils import fs

    fs.check_subject_exists(subject)

    slug = fs.slugify(qp_name)
    generated = fs.generated_dir(subject)

    def _read(suffix: str) -> str:
        path = generated / f"{slug}-{suffix}.md"
        if not path.exists():
            return f"(no {suffix} file found at {path})"
        return path.read_text()

    inputs = {
        "subject": subject,
        "qp_name": slug,
        "qp_content": _read("qp"),
        "discrepancy_content": _read("discrepancy-report"),
        "answer_key_content": _read("answer-key"),
        "final_summary_content": _read("final-summary"),
        "manual_instructions": notes or "(none - just fix what the automated review flagged)",
        **build_skill_prompt_inputs(subject),
    }
    
    import shutil

    with fs.crewai_storage_context(f".memories/{subject}/{slug}"):
        result = RevisionCrew().crew().kickoff(inputs=inputs)

    # Synchronize revised companion files (.md and .json) to the active canonical files
    # so that answer-key, discrepancy-report, and final-summary are updated.
    for base, revised in [
        ("qp", "qp-revised"),
        ("answer-key", "answer-key-revised"),
        ("discrepancy-report", "discrepancy-report-revised"),
        ("final-summary", "final-summary-revised"),
    ]:
        for ext in (".md", ".json"):
            rev_path = generated / f"{slug}-{revised}{ext}"
            base_path = generated / f"{slug}-{base}{ext}"
            if rev_path.exists():
                shutil.copy2(rev_path, base_path)

    return result

