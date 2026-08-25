"""Ingestion phase: parse + index a subject's source documents."""
from __future__ import annotations

from typing import Any


def run_ingestion(subject: str, week: int | None = None, force: bool = False) -> Any:
    import os

    os.environ["CREWAI_STORAGE_DIR"] = f".db/{subject}"

    from klyra.crews.ingestion_crew.ingestion_crew import IngestionCrew

    inputs = {
        "subject": subject,
        "week_filter": week if week is not None else "None",
        "force": force,
    }
    return IngestionCrew().crew().kickoff(inputs=inputs)
