"""Shared FastAPI dependencies: optional API-key auth + rate limiting.

Both are intentionally minimal, env-driven, and off by default (so local
dev/tests need zero setup) - see PRODUCTION.md "Auth and abuse protection".
Swap `require_api_key` for real OAuth2/JWT and `limiter` for a
Redis-backed slowapi storage before running more than one API process,
since both are process-local as written.
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException
from slowapi import Limiter
from slowapi.util import get_remote_address

#: Applied to every expensive (crew-kicking-off) POST route. Cheap GET routes
#: (job status, health) are deliberately not rate-limited.
DEFAULT_RATE_LIMIT = os.environ.get("KLYRA_RATE_LIMIT", "10/minute")

limiter = Limiter(key_func=get_remote_address)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """No-op unless KLYRA_API_KEY is set, so local/dev use needs no auth.

    Set KLYRA_API_KEY in production and have every client send it back as
    the `X-API-Key` header.
    """
    expected = os.environ.get("KLYRA_API_KEY")
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")
