"""Shared retry/resilience primitives for all Klyra components.

Consolidates the retry logic previously duplicated across gemini_client,
vector_store, and llm_config into a single module.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# ── Retryable error markers ─────────────────────────────────────────
# Any exception whose str() contains one of these is treated as transient.
RETRYABLE_MARKERS = (
    "RESOURCE_EXHAUSTED",
    "429",
    "UNAVAILABLE",
    "503",
    "504",
    "DEADLINE_EXCEEDED",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "TimeoutError",
    "Temporary failure in name resolution",
    "Connection reset",
    "RemoteDisconnected",
    "network is unreachable",
)

_RETRY_DELAY_RE = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)


def is_retryable(exc: Exception) -> bool:
    """Return True if *exc* looks like a transient Google API error."""
    return any(marker in str(exc) for marker in RETRYABLE_MARKERS)


def suggested_delay(exc: Exception, attempt: int) -> float:
    """Return a backoff delay in seconds.

    Uses Google's suggested delay if present in the error message,
    otherwise falls back to exponential backoff (3s × 2^attempt, max 60s).
    """
    match = _RETRY_DELAY_RE.search(str(exc))
    if match:
        return float(match.group(1)) + 1.0  # small buffer past what Google suggests
    return min(60.0, 2.0 ** attempt * 3.0)


def call_with_retry(
    fn: Callable[[], T],
    *,
    max_retries: int | None = None,
    throttle_fn: Callable[[], None] | None = None,
) -> T:
    """Call *fn()* with automatic retry on transient API errors.

    Parameters
    ----------
    fn : callable
        Zero-argument callable to invoke.
    max_retries : int | None
        Maximum number of attempts.  Defaults to ``KLYRA_PARSER_MAX_RETRIES``
        env var (default 4).
    throttle_fn : callable | None
        Optional rate-limiter called before each attempt.
    """
    if max_retries is None:
        max_retries = int(os.environ.get("KLYRA_PARSER_MAX_RETRIES", "4"))

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        if throttle_fn is not None:
            throttle_fn()
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not is_retryable(exc) or attempt == max_retries - 1:
                raise
            time.sleep(suggested_delay(exc, attempt))
    assert last_exc is not None  # pragma: no cover – unreachable
    raise last_exc  # pragma: no cover


# ── SDK retry options dict (for CrewAI LLM / genai.Client) ──────────

def sdk_retry_options(
    *,
    timeout_seconds: float | None = None,
    retry_attempts: int | None = None,
) -> dict[str, Any]:
    """Return an ``http_options`` dict suitable for ``genai.Client()`` or
    CrewAI ``LLM(client_params=...)``.

    Reads defaults from env vars ``KLYRA_LLM_TIMEOUT_SECONDS`` and
    ``KLYRA_LLM_RETRY_ATTEMPTS``.
    """
    if timeout_seconds is None:
        timeout_seconds = float(os.environ.get("KLYRA_LLM_TIMEOUT_SECONDS", "30"))
    if retry_attempts is None:
        retry_attempts = int(os.environ.get("KLYRA_LLM_RETRY_ATTEMPTS", "10"))

    return {
        "timeout": int(timeout_seconds * 1000),
        "retry_options": {
            "attempts": retry_attempts,
            "initial_delay": 2.0,
            "max_delay": 65.0,
        },
    }


# ── Thread-safe throttle ────────────────────────────────────────────

class Throttle:
    """Simple thread-safe rate limiter.

    Ensures at least *min_interval* seconds elapse between calls to
    ``wait()``.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_call_at = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._last_call_at + self._min_interval - now
            if wait > 0:
                time.sleep(wait)
            self._last_call_at = time.monotonic()
