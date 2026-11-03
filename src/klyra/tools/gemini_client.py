"""Direct Gemini calls for multimodal document understanding.

Deliberately independent of whichever LLM powers a CrewAI agent's own
reasoning loop: this module is called directly by document_parser_tool so
parsing quality never depends on how the orchestrating agent is configured.

Rate-limiting and retry logic is provided by ``klyra.core.retry``.
"""
from __future__ import annotations

import os
from functools import lru_cache

from klyra.core.retry import Throttle, call_with_retry, sdk_retry_options


class GeminiNotConfiguredError(RuntimeError):
    """Raised when GEMINI_API_KEY / GOOGLE_API_KEY is not set."""


# Module-level throttle — shared across all callers in this process.
_throttle = Throttle(
    float(os.environ.get("KLYRA_PARSER_MIN_INTERVAL_SECONDS", "5"))
)


@lru_cache(maxsize=1)
def _client():
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise GeminiNotConfiguredError(
            "Set GEMINI_API_KEY (or GOOGLE_API_KEY) to parse pdf/ppt/doc/docx "
            "files with Gemini's multimodal understanding."
        )
    # Keep the SDK's own internal retry budget small (our call_with_retry
    # is the outer retry loop that actually respects Google's suggested
    # backoff) so one file can't silently eat minutes before we even get
    # a chance to throttle/retry it ourselves.
    http_opts = sdk_retry_options(retry_attempts=2)
    http_opts["retry_options"]["initial_delay"] = 1.0
    http_opts["retry_options"]["max_delay"] = 10.0
    return genai.Client(
        api_key=api_key,
        http_options=http_opts,
    )


def describe_document(file_bytes: bytes, mime_type: str, source_name: str) -> str:
    """Send a PDF to Gemini and return normalized text, with faithful inline
    descriptions of any images/diagrams/charts it contains. Retries on
    transient 429 (quota) / 503 (overloaded) errors with backoff."""
    from google.genai import types

    model = os.environ.get("KLYRA_PARSER_MODEL") or os.environ.get("KLYRA_MODEL", "gemini-3.5-flash-lite")
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

    response = call_with_retry(_call, throttle_fn=_throttle.wait)
    return response.text or ""
