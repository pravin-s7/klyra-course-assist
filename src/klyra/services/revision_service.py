"""Revision: fix a generated QP's flagged issues (plus optional manual edits)."""
from __future__ import annotations

from typing import Any


def run_revision(subject: str, qp_name: str, notes: str | None = None) -> Any:
    from klyra.crews.revision_crew.revision_crew import RevisionCrew
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
    }
    
    import os
    from unittest.mock import patch
    import crewai_core.paths
    
    mem_dir = os.path.abspath(f".memories/{subject}/{slug}")
    with patch("crewai_core.paths.db_storage_path", return_value=mem_dir):
        return RevisionCrew().crew().kickoff(inputs=inputs)
