"""Manual utility: fetch and save a YouTube transcript into a subject's docs tree."""
from __future__ import annotations

from pathlib import Path


def run_transcript(url: str, subject: str, week: int | None = None, title: str | None = None) -> Path:
    from klyra.tools.youtube_transcript_tool import save_youtube_transcript
    from klyra.utils import fs

    fs.check_subject_exists(subject)

    return save_youtube_transcript(url_or_id=url, subject=subject, week=week, title=title)
