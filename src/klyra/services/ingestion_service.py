"""Ingestion phase: parse + index a subject's source documents."""
from __future__ import annotations

from typing import Any


def run_ingestion(
    subject: str, 
    week: int | None = None, 
    categories: list[str] | None = None, 
    file_path: str | None = None,
    delete_file: str | None = None,
    force: bool = False
) -> Any:
    import os
    from klyra.tools import vector_store
    from klyra.utils import fs

    fs.check_subject_exists(subject)

    if delete_file:
        print(f"Deleting {delete_file} from index...")
        # Assume it could be in parsed or pyq collections
        vector_store.delete_by_source_file(vector_store.parsed_collection_name(subject), delete_file)
        vector_store.delete_by_source_file(vector_store.pyq_collection_name(subject), delete_file)
        fs.remove_from_manifest(subject, delete_file)
        print("Deletion complete.")
        return

    os.environ["CREWAI_STORAGE_DIR"] = f".db/{subject}"

    from klyra.crews.ingestion_crew.ingestion_crew import IngestionCrew
    from klyra.services.skill_service import ensure_subject_skill

    ensure_subject_skill(subject)
    categories_str = ",".join(categories) if categories else ""

    crew = IngestionCrew().crew()
    print(f"Starting ingestion for subject={subject}...")
    try:
        return crew.kickoff(
            inputs={
                "subject": subject,
                "week_filter": week,
                "categories_filter": categories_str,
                "file_path": file_path if file_path else "",
                "force": force,
            }
        )
    finally:
        vector_store.close_client()
