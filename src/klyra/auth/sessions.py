"""HMAC-signed cookie session tokens (stdlib only, no itsdangerous).

Token format: "<user_id>.<expires_ts>.<signature>" where signature =
HMAC-SHA256(secret, f"{user_id}.{expires_ts}"). The secret comes from
KLYRA_SESSION_SECRET; if unset, one is generated per process (local dev
convenience — logins won't survive restarts, which is fine for dev but
means you MUST set KLYRA_SESSION_SECRET in production / multi-worker).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

SESSION_COOKIE = "klyra_session"
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days

_secret: bytes | None = None


def _get_secret() -> bytes:
    global _secret
    if _secret is None:
        env = os.environ.get("KLYRA_SESSION_SECRET")
        if env:
            _secret = env.encode("utf-8")
        else:
            _secret = secrets.token_bytes(32)
    return _secret


def _sign(payload: str) -> str:
    return hmac.new(_get_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def encode_session_token(user_id: str) -> str:
    expires = int(time.time()) + TOKEN_TTL_SECONDS
    payload = f"{user_id}.{expires}"
    return f"{payload}.{_sign(payload)}"


def decode_session_token(token: str) -> str | None:
    """Return the user_id if the token is valid and unexpired, else None."""
    try:
        user_id, expires_str, signature = token.split(".")
        payload = f"{user_id}.{expires_str}"
        if not hmac.compare_digest(signature, _sign(payload)):
            return None
        if int(expires_str) < time.time():
            return None
        return user_id
    except (ValueError, TypeError):
        return None