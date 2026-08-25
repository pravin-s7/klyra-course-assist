"""Coding-problem generation phase: create + review + independently
validate (by actually running code) a set of coding problems - and its
on-demand revision pass. Mirrors generation_service.py/revision_service.py.
"""
from __future__ import annotations

from typing import Any

from klyra.utils.parsing import parse_week_scope


class CodingInputError(ValueError):
    """Raised when a coding-problem request cannot be turned into crew inputs."""


def run_coding_generation(
    subject: str,
    name: str,
    language: str,
    scope: str,
    difficulty: str,
    num_questions: int,
    public_tests: int,
    private_tests: int,
    source: str | None = None,
    pyq_similar: int = 0,
    leetcode_similar: bool = False,
    include_reference: bool = False,
    concepts: str | None = None,
    notes: str | None = None,
) -> Any:
    if pyq_similar > num_questions:
        raise CodingInputError(
            f"pyq_similar ({pyq_similar}) cannot exceed num_questions ({num_questions})."
        )

    import os

    os.environ["CREWAI_STORAGE_DIR"] = f".db/{subject}"

    from klyra.crews.coding_problem_crew.coding_problem_crew import CodingProblemCrew
    from klyra.utils import fs

    (fs.generated_dir(subject) / "coding").mkdir(parents=True, exist_ok=True)
    inputs = {
        "subject": subject,
        "name": fs.slugify(name),
        "language": language.strip().lower(),
        "syllabus_scope": parse_week_scope(scope),
        "source_filter": source or "any",
        "difficulty": difficulty,
        "pyq_similar_count": pyq_similar,
        "leetcode_similar": "yes" if leetcode_similar else "no",
        "include_reference": "yes" if include_reference else "no",
        "concepts": concepts or "any",
        "num_questions": num_questions,
        "public_test_count": public_tests,
        "private_test_count": private_tests,
        "notes": notes or "(none)",
    }
    return CodingProblemCrew().crew().kickoff(inputs=inputs)


def run_coding_revision(subject: str, name: str, language: str, notes: str | None = None) -> Any:
    from klyra.crews.coding_revision_crew.coding_revision_crew import CodingRevisionCrew
    from klyra.utils import fs

    slug = fs.slugify(name)
    coding_dir = fs.generated_dir(subject) / "coding"

    def _read(suffix: str) -> str:
        path = coding_dir / f"{slug}-{suffix}.md"
        if not path.exists():
            return f"(no {suffix} file found at {path})"
        return path.read_text()

    inputs = {
        "subject": subject,
        "name": slug,
        "language": language.strip().lower(),
        "problems_content": _read("problems"),
        "discrepancy_content": _read("coding-discrepancy-report"),
        "testcases_content": _read("testcases"),
        "final_summary_content": _read("final-summary"),
        "manual_instructions": notes or "(none - just fix what the automated review flagged)",
    }
    
    import os
    from unittest.mock import patch
    import crewai_core.paths
    
    mem_dir = os.path.abspath(f".memories/{subject}/{slug}")
    with patch("crewai_core.paths.db_storage_path", return_value=mem_dir):
        return CodingRevisionCrew().crew().kickoff(inputs=inputs)
