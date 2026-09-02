"""Manual YouTube transcript generation.

Per .cursor/rules/klyra.mdc this is on-demand only: a human (or an agent
explicitly handed a URL) calls this for one specific video. Nothing in this
project crawls or searches YouTube on its own.

Two backends, tried in order:
  1. youtube-transcript-api - simplest, no extra binary, but its watch-page
     scrape is aggressively IP-blocked by YouTube from datacenter/cloud IPs
     (confirmed empirically while building this).
  2. yt-dlp - downloads the caption track only (skip_download=True), more
     resilient to that blocking - but YouTube's default "web" client now
     requires a proof-of-origin token yt-dlp can't produce, which surfaces
     as "Sign in to confirm you're not a bot" (also confirmed empirically).
     Working around this means impersonating a different player client;
     which one currently works shifts over time as YouTube adjusts trust
     (`android` worked at the time this was written), so several are tried
     in order rather than hardcoding just one.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from klyra.utils import fs

_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")
_VTT_TIMESTAMP_LINE = re.compile(r"\d{2}:\d{2}:\d{2}\.\d{3}\s*-->")
_VTT_INLINE_TAG = re.compile(r"<[^>]+>")


def extract_video_id(url_or_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_id):
        return url_or_id
    match = _ID_RE.search(url_or_id)
    if not match:
        raise ValueError(f"Could not extract a YouTube video ID from: {url_or_id!r}")
    return match.group(1)


def _parse_vtt(vtt_text: str) -> str:
    lines_out: list[str] = []
    prev = None
    for raw_line in vtt_text.splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE", "STYLE")):
            continue
        if _VTT_TIMESTAMP_LINE.search(line) or line.isdigit():
            continue
        line = _VTT_INLINE_TAG.sub("", line).strip()
        if not line or line == prev:  # auto-captions repeat the rolling line across cues
            continue
        lines_out.append(line)
        prev = line
    return " ".join(lines_out)


def _fetch_via_transcript_api(video_id: str, languages: list[str]) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    fetched = api.fetch(video_id, languages=languages)
    return " ".join(snippet.text for snippet in fetched)


_YTDLP_PLAYER_CLIENTS = [
    ["android"],
    ["ios"],
    ["tv", "web_safari"],
    None,  # yt-dlp's own default, in case a future version no longer needs a workaround
]


def _fetch_via_ytdlp(video_id: str, languages: list[str]) -> str:
    import glob
    import os
    import tempfile

    import yt_dlp

    last_exc: Exception | None = None
    for player_client in _YTDLP_PLAYER_CLIENTS:
        with tempfile.TemporaryDirectory() as tmp:
            opts = {
                "writesubtitles": True,
                "writeautomaticsub": True,
                "skip_download": True,
                "subtitleslangs": languages,
                "subtitlesformat": "vtt",
                "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
            }
            if player_client is not None:
                opts["extractor_args"] = {"youtube": {"player_client": player_client}}

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
                vtt_files = sorted(glob.glob(os.path.join(tmp, "*.vtt")))
                if vtt_files:
                    return _parse_vtt(Path(vtt_files[0]).read_text(errors="ignore"))
                last_exc = RuntimeError(f"no subtitle file produced (player_client={player_client})")
            except Exception as exc:  # noqa: BLE001 - try the next client
                last_exc = exc

    raise last_exc or RuntimeError("yt-dlp failed for an unknown reason")


def fetch_youtube_transcript(url_or_id: str, languages: list[str] | None = None) -> str:
    video_id = extract_video_id(url_or_id)
    langs = languages or ["en"]
    try:
        return _fetch_via_transcript_api(video_id, langs)
    except Exception as primary_exc:  # noqa: BLE001 - deliberately broad, we fall back
        try:
            return _fetch_via_ytdlp(video_id, langs)
        except Exception as fallback_exc:  # noqa: BLE001
            raise RuntimeError(
                f"Could not fetch a transcript for '{video_id}': "
                f"youtube-transcript-api failed ({primary_exc}); "
                f"yt-dlp fallback also failed ({fallback_exc})."
            ) from fallback_exc


def save_youtube_transcript(
    url_or_id: str, subject: str, week: int | None = None, title: str | None = None
) -> Path:
    text = fetch_youtube_transcript(url_or_id)
    video_id = extract_video_id(url_or_id)
    name = fs.slugify(title) if title else video_id

    out_dir = fs.week_dir(subject, week) if week is not None else fs.transcripts_dir(subject)
    out_dir.mkdir(parents=True, exist_ok=True)

    path = out_dir / f"{name}-transcript.md"
    header = f"# Transcript: {title or video_id}\n\nSource: https://youtu.be/{video_id}\n\n"
    path.write_text(header + text)
    return path


class GenerateYoutubeTranscriptInput(BaseModel):
    url: str = Field(..., description="YouTube video URL or bare video ID")
    subject: str = Field(..., description="Subject slug, e.g. 'data-structures'")
    week: int | None = Field(
        default=None, description="Week 1-12 this lecture belongs to, if known. Omit if unknown."
    )
    title: str | None = Field(default=None, description="Optional short title for the file name")


class GenerateYoutubeTranscriptTool(BaseTool):
    name: str = "generate_youtube_transcript"
    description: str = (
        "Manually fetch the caption transcript for a single YouTube video and "
        "save it as source material for a subject. Only call this when a "
        "human has explicitly provided a YouTube URL - never search for or "
        "crawl videos on your own. Saves into week_N/ if week is given, else "
        "into transcripts/ for later manual sorting."
    )
    args_schema: Type[BaseModel] = GenerateYoutubeTranscriptInput

    def _run(self, url: str, subject: str, week: int | None = None, title: str | None = None) -> str:
        path = save_youtube_transcript(url, subject, week=week, title=title)
        return f"Saved transcript to {path}"
