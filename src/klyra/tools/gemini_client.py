"""Direct Gemini calls for multimodal document understanding.

Deliberately independent of whichever LLM powers a CrewAI agent's own
reasoning loop: this module is called directly by document_parser_tool so
parsing quality never depends on how the orchestrating agent is configured.
Per .cursor/rules/klyra.mdc, Gemini 3.1 Flash Lite is the recommended
default: it reads PDFs/images natively (no separate OCR step), and on a real
free-tier account it got 15 requests/minute vs. just 5 RPM for every full
"Flash" model (2.5/3/3.5/3.7) - "Lite" variants are worth preferring for
exactly this reason.

Even 15 RPM is tight once you're parsing several files back to back plus
whatever the orchestrating agent itself calls, so `_throttle()` +
`_call_with_retry()` keep this tool usable rather than 429ing immediately.
Raise KLYRA_PARSER_MIN_INTERVAL_SECONDS (or set it to 0) if your account
has a higher tier.
"""
from __future__ import annotations

import os
import re
import threading
import time
from functools import lru_cache

_MIN_INTERVAL_SECONDS = float(os.environ.get("KLYRA_PARSER_MIN_INTERVAL_SECONDS", "5"))
_MAX_RETRIES = int(os.environ.get("KLYRA_PARSER_MAX_RETRIES", "4"))
_RETRYABLE_MARKERS = ("RESOURCE_EXHAUSTED", "429", "UNAVAILABLE", "503")
_RETRY_DELAY_RE = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)

_throttle_lock = threading.Lock()
_last_call_at = 0.0


class GeminiNotConfiguredError(RuntimeError):
    """Raised when GEMINI_API_KEY / GOOGLE_API_KEY is not set."""


@lru_cache(maxsize=1)
def _client():
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise GeminiNotConfiguredError(
            "Set GEMINI_API_KEY (or GOOGLE_API_KEY) to parse pdf/ppt/doc/docx "
            "files with Gemini's multimodal understanding."
        )
    # Keep the SDK's own internal retry budget small (our _call_with_retry
    # above is the outer retry loop that actually respects Google's
    # suggested backoff) so one file can't silently eat minutes before we
    # even get a chance to throttle/retry it ourselves.
    timeout_ms = int(float(os.environ.get("KLYRA_LLM_TIMEOUT_SECONDS", "30")) * 1000)
    retry_attempts = int(os.environ.get("KLYRA_LLM_RETRY_ATTEMPTS", "2"))
    return genai.Client(
        api_key=api_key,
        http_options={
            "timeout": timeout_ms,
            "retry_options": {"attempts": retry_attempts, "initial_delay": 1.0, "max_delay": 10.0},
        },
    )


def _throttle() -> None:
    """Block just long enough to keep our own call rate under
    KLYRA_PARSER_MIN_INTERVAL_SECONDS between consecutive Gemini calls."""
    if _MIN_INTERVAL_SECONDS <= 0:
        return
    global _last_call_at
    with _throttle_lock:
        wait = _last_call_at + _MIN_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _is_retryable(exc: Exception) -> bool:
    return any(marker in str(exc) for marker in _RETRYABLE_MARKERS)


def _suggested_delay(exc: Exception, attempt: int) -> float:
    match = _RETRY_DELAY_RE.search(str(exc))
    if match:
        return float(match.group(1)) + 1.0  # small buffer past what Google suggests
    return min(60.0, 2.0 ** attempt * 3.0)  # exponential backoff fallback


def _call_with_retry(fn):
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        _throttle()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below if not retryable
            last_exc = exc
            if not _is_retryable(exc) or attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(_suggested_delay(exc, attempt))
    raise last_exc  # pragma: no cover - unreachable, loop always returns or raises


def describe_document(file_bytes: bytes, mime_type: str, source_name: str) -> str:
    """Send a PDF to Gemini and return normalized text, with faithful inline
    descriptions of any images/diagrams/charts it contains. Retries on
    transient 429 (quota) / 503 (overloaded) errors with backoff."""
    from google.genai import types

    model = os.environ.get("KLYRA_PARSER_MODEL", "gemini-3.1-flash-lite")
    prompt = (
        f"This is a course document ('{source_name}'). Extract all text "
        "content verbatim, in reading order. For every image, diagram, "
        "chart, or figure, insert a faithful plain-text description of what "
        "it shows, written as '[Figure: ...]', inline where it appears. "
        "Never summarize, omit, or alter technical content."
    )

    def _call():
        return _client().models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                prompt,
            ],
            config={"max_output_tokens": 65536},
        )

    response = _call_with_retry(_call)
    return response.text or ""
