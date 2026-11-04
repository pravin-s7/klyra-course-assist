"""Backward-compatible re-export of `GenerationCrew` from `klyra.crews.exam_crew`."""
from __future__ import annotations

from klyra.crews.exam_crew import ExamCrew, GenerationCrew

__all__ = ["GenerationCrew", "ExamCrew"]
