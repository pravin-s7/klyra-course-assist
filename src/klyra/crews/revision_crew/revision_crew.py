"""Backward-compatible re-export of `RevisionCrew` from `klyra.crews.exam_crew`."""
from __future__ import annotations

from klyra.crews.exam_crew import ExamCrew, RevisionCrew

__all__ = ["RevisionCrew", "ExamCrew"]
