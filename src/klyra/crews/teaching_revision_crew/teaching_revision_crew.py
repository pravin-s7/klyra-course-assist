"""Backward-compatible re-export of `TeachingRevisionCrew` from `klyra.crews.teaching_crew`."""
from __future__ import annotations

from klyra.crews.teaching_crew import TeachingCrew, TeachingRevisionCrew

__all__ = ["TeachingRevisionCrew", "TeachingCrew"]
