"""Shared LLM factory for all Klyra agents.

Per .cursor/rules/klyra.mdc, Gemini 3.1 Flash Lite is the recommended
default: natively multimodal (text/image/video/audio/pdf) and, on a real
free-tier account, "Flash Lite" variants were observed getting 10-15
requests/minute vs. just 5 RPM for the full "Flash" models (2.5/3/3.5/3.7) -
2-3x more headroom for the same cost tier. Overridable via KLYRA_LLM_MODEL
- e.g. set it to "gemini/gemini-pro-latest" if you have billing set up and
want a stronger model (Pro-class models have zero free-tier quota).
"""
from __future__ import annotations

import os


def default_llm():
    from crewai import LLM

    model = os.environ.get("KLYRA_LLM_MODEL", "gemini/gemini-3.1-flash-lite")
    # The plain `timeout=` kwarg on LLM(...) is silently dropped for the
    # native Gemini provider (GeminiCompletion has no such field) - the
    # google-genai client's own http_options must be reached via
    # client_params instead, which GeminiCompletion._initialize_client
    # merges straight into genai.Client(...). Without this, the SDK's own
    # internal 5xx/429 retry can compound with CrewAI's per-task retry into
    # multi-minute hangs during a demand spike (observed directly).
    timeout_ms = int(float(os.environ.get("KLYRA_LLM_TIMEOUT_SECONDS", "30")) * 1000)
    retry_attempts = int(os.environ.get("KLYRA_LLM_RETRY_ATTEMPTS", "10"))
    return LLM(
        model=model,
        client_params={
            "http_options": {
                "timeout": timeout_ms,
                "retry_options": {
                    "attempts": retry_attempts,
                    "initial_delay": 2.0,
                    "max_delay": 65.0,
                },
            }
        },
    )


def coding_llm():
    from crewai import LLM

    model = os.environ.get("KLYRA_CODING_LLM_MODEL", "gemini/gemini-3.5-flash-lite")
    timeout_ms = int(float(os.environ.get("KLYRA_LLM_TIMEOUT_SECONDS", "30")) * 1000)
    retry_attempts = int(os.environ.get("KLYRA_LLM_RETRY_ATTEMPTS", "10"))
    
    return LLM(
        model=model,
        client_params={
            "http_options": {
                "timeout": timeout_ms,
                "retry_options": {
                    "attempts": retry_attempts,
                    "initial_delay": 2.0,
                    "max_delay": 65.0,
                },
            }
        },
    )


def default_embedder():
    import os

    provider = os.environ.get("EMBEDDING_PROVIDER", "default")
    if provider == "openai":
        return {
            "provider": "openai",
            "config": {
                "api_key": os.environ.get("OPENAI_API_KEY"),
                "model": "text-embedding-3-small",
            },
        }

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    # CrewAI's default Chroma storage for Knowledge strictly requires this env var
    # when using the google-generativeai provider
    os.environ["CHROMA_GOOGLE_GENAI_API_KEY"] = api_key
    
    return {
        "provider": "google-generativeai",
        "config": {
            "api_key": api_key,
            "model": "models/gemini-embedding-2",
        },
    }
