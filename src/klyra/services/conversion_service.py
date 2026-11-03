"""Independent utility: convert generated documents between Markdown and LaTeX."""
from __future__ import annotations

from typing import Any


def run_conversion(subject: str, request: str) -> Any:
    import os

    os.environ["CREWAI_STORAGE_DIR"] = f".db/{subject}"

    from klyra.crews.format_converter_crew.format_converter_crew import FormatConverterCrew
    from klyra.services.skill_service import build_skill_prompt_inputs
    from klyra.utils import fs

    fs.check_subject_exists(subject)

    inputs = {
        "subject": subject,
        "conversion_request": request,
        **build_skill_prompt_inputs(subject),
    }
    return FormatConverterCrew().crew().kickoff(inputs=inputs)
