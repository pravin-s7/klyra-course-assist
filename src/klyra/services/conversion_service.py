"""Independent utility: convert generated documents between Markdown and LaTeX."""
from __future__ import annotations

from typing import Any


def run_conversion(subject: str, request: str) -> Any:
    import os

    os.environ["CREWAI_STORAGE_DIR"] = f".db/{subject}"

    from klyra.crews.format_converter_crew.format_converter_crew import FormatConverterCrew

    inputs = {"subject": subject, "conversion_request": request}
    return FormatConverterCrew().crew().kickoff(inputs=inputs)
