"""Shared LLM factory for all Klyra agents.

All agents default to ``KLYRA_MODEL`` (env var, default
``gemini-3.5-flash-lite``).  Individual overrides are still possible
via ``KLYRA_CODING_LLM_MODEL`` etc., but the common case is one knob.

Retry/timeout configuration is centralised in ``klyra.core.retry``.
"""
from __future__ import annotations

import os

from klyra.core.retry import sdk_retry_options


def _make_llm(env_var: str, *, prefix: str = "gemini/"):
    """Create a CrewAI LLM using the model name from *env_var*.

    Falls back to ``KLYRA_MODEL`` → ``gemini-3.5-flash-lite``.
    *prefix* is prepended if the value doesn't already contain a '/'.
    """
    from crewai import LLM

    base_default = os.environ.get("KLYRA_MODEL", "gemini-3.5-flash-lite")
    raw = os.environ.get(env_var) or base_default
    model = raw if "/" in raw else f"{prefix}{raw}"
    return LLM(
        model=model,
        client_params={"http_options": sdk_retry_options()},
    )


def default_llm():
    """LLM for most agents (syllabus indexer, QP creator, reviewer, …)."""
    return _make_llm("KLYRA_LLM_MODEL")


def coding_llm():
    """LLM for coding-problem agents (may use a stronger model)."""
    return _make_llm("KLYRA_CODING_LLM_MODEL")


def default_embedder():
    try:
        from google.api_core import client_options

        if not hasattr(client_options.ClientOptions, "headers"):
            setattr(client_options.ClientOptions, "headers", None)
    except Exception:
        pass

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
    os.environ["CHROMA_GOOGLE_GENAI_API_KEY"] = api_key
    from klyra.tools.vector_store import _GoogleGenAIEmbeddingFunction

    return {
        "provider": "custom",
        "config": {
            "embedding_callable": _GoogleGenAIEmbeddingFunction,
        },
    }
