"""Klyra Domain Crews (`ExamCrew`, `CodingCrew`, `TeachingCrew`, `IngestionCrew`, `FormatConverterCrew`)."""
from __future__ import annotations

from klyra.crews.coding_crew import CodingCrew, CodingProblemCrew, CodingRevisionCrew
from klyra.crews.exam_crew import ExamCrew, GenerationCrew, RevisionCrew
from klyra.crews.format_converter_crew.format_converter_crew import FormatConverterCrew
from klyra.crews.ingestion_crew.ingestion_crew import IngestionCrew
from klyra.crews.teaching_crew import (
    TeachingCrew,
    TeachingMaterialCrew,
    TeachingRevisionCrew,
)

__all__ = [
    "ExamCrew",
    "CodingCrew",
    "TeachingCrew",
    "IngestionCrew",
    "FormatConverterCrew",
    "GenerationCrew",
    "RevisionCrew",
    "CodingProblemCrew",
    "CodingRevisionCrew",
    "TeachingMaterialCrew",
    "TeachingRevisionCrew",
]

