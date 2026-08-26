"""Generation phase: create + review + solve a question paper."""
from __future__ import annotations

from typing import Any


class GenerationInputError(ValueError):
    """Raised when the request doesn't have enough info to build a QP."""


def run_generation(
    subject: str,
    qp_name: str,
    scope: str,
    difficulty: str,
    source: str | None = None,
    pyq_similar: int = 0,
    concepts: str | None = None,
    type_counts: str | None = None,
    types: str | None = None,
    num_questions: int | None = None,
    notes: str | None = None,
) -> Any:
    import os

    os.environ["CREWAI_STORAGE_DIR"] = f".db/{subject}"

    from klyra.crews.generation_crew.generation_crew import GenerationCrew
    from klyra.utils import fs

    fs.check_subject_exists(subject)
    from klyra.utils.parsing import parse_type_counts, parse_week_scope

    if type_counts:
        counts = parse_type_counts(type_counts)
        question_types = ", ".join(f"{qtype} x{count}" for qtype, count in counts.items())
        resolved_num_questions = sum(counts.values())
    elif types and num_questions:
        question_types = types
        resolved_num_questions = num_questions
    else:
        raise GenerationInputError(
            "generate needs either type_counts (e.g. 'MCQ:5,MSQ:3,NAT:2') or both types and num_questions."
        )

    if not fs.subject_dir(subject).exists():
        raise GenerationInputError(f"Subject '{subject}' does not exist in docs/. Please run ingest first.")

    fs.generated_dir(subject).mkdir(parents=True, exist_ok=True)
    inputs = {
        "subject": subject,
        "qp_name": fs.slugify(qp_name),
        "syllabus_scope": parse_week_scope(scope),
        "source_filter": source or "any",
        "difficulty": difficulty,
        "pyq_similar_count": pyq_similar,
        "concepts": concepts or "any",
        "question_types": question_types,
        "num_questions": resolved_num_questions,
        "notes": notes or "(none)",
    }
    result = GenerationCrew().crew().kickoff(inputs=inputs)
    
    # Validation: Ensure the generation didn't fail due to empty context
    qp_file = fs.generated_dir(subject) / f"{fs.slugify(qp_name)}-qp.md"
    if qp_file.exists() and len(qp_file.read_text().strip()) < 150:
        raise GenerationInputError(
            f"Generation failed: The generated file is suspiciously short. "
            f"This usually means the vector store returned no context for the requested scope ({scope}). "
            f"Please ensure you have ingested content for this scope before generating."
        )
        
    return result
