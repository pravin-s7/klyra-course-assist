"""Google OAuth2 (Authorization Code) login for the Klyra web app.

Flow:
1. GET /api/auth/google/login → redirect to Google's consent screen with a
   CSRF-protected state (short-lived HttpOnly cookie).
2. Google redirects back to GET /api/auth/google/callback?code&state.
3. We exchange the code for tokens, fetch the user's email/profile, then:
   - existing user with that username (email) → log them in;
   - new user → auto-provision as an instructor with no courses
     (disable with KLYRA_OAUTH_AUTO_PROVISION=0), or reject.

Config (see .env.example):
- GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET   (required to enable)
- KLYRA_OAUTH_REDIRECT_URI                  (optional; derived from the
  request's base URL when unset)
- KLYRA_OAUTH_AUTO_PROVISION                (default "1")
"""
from __future__ import annotations

import logging
import os
import secrets

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from klyra.auth.passwords import hash_password
from klyra.auth.sessions import SESSION_COOKIE, encode_session_token
from klyra.storage import repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth-google"])

STATE_COOKIE = "klyra_oauth_state"
STATE_TTL_SECONDS = 600

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"


def _client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "")


def _client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "")


def _auto_provision() -> bool:
    return os.environ.get("KLYRA_OAUTH_AUTO_PROVISION", "1") != "0"


def google_enabled() -> bool:
    return bool(_client_id() and _client_secret())


def _redirect_uri(request: Request) -> str:
    configured = os.environ.get("KLYRA_OAUTH_REDIRECT_URI", "")
    if configured:
        return configured
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/auth/google/callback"


@router.get("/api/auth/google/status")
def google_status():
    """Frontend uses this to decide whether to render the Google button."""
    return {"enabled": google_enabled()}


@router.get("/api/auth/google/login")
def google_login(request: Request):
    if not google_enabled():
        return HTMLResponse(
            "<h3>Google login is not configured.</h3>"
            "<p>Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env and restart.</p>"
            '<p><a href="/">Back to login</a></p>',
            status_code=503,
        )

    state = secrets.token_urlsafe(32)
    params = (
        f"client_id={_client_id()}"
        f"&redirect_uri={_redirect_uri(request)}"
        "&response_type=code"
        "&scope=openid%20email%20profile"
        f"&state={state}"
        "&prompt=select_account"
    )
    response = RedirectResponse(f"{_AUTH_ENDPOINT}?{params}", status_code=302)
    response.set_cookie(
        STATE_COOKIE, state, httponly=True, samesite="lax", max_age=STATE_TTL_SECONDS
    )
    return response


def _error_page(message: str, status_code: int = 400):
    return HTMLResponse(
        f"<h3>Google login failed</h3><p>{message}</p>"
        '<p><a href="/">Back to login</a></p>',
        status_code=status_code,
    )


@router.get("/api/auth/google/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None):
    if not google_enabled():
        return _error_page("Google login is not configured.", 503)

    # CSRF check: state must match the cookie we set on /login
    cookie_state = request.cookies.get(STATE_COOKIE)
    if not state or not cookie_state or state != cookie_state:
        return _error_page("Invalid or expired OAuth state. Please try again.")
    if not code:
        return _error_page("Google did not return an authorization code.")

    try:
        token_resp = httpx.post(
            _TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "redirect_uri": _redirect_uri(request),
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get("access_token")
        if not access_token:
            return _error_page("Token exchange failed.")

        userinfo_resp = httpx.get(
            _USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        userinfo_resp.raise_for_status()
        info = userinfo_resp.json()
    except httpx.HTTPError as exc:
        logger.warning("Google OAuth exchange failed: %s", exc)
        return _error_page("Could not verify your Google account. Please try again.")

    email = (info.get("email") or "").lower()
    if not email or not info.get("email_verified", False):
        return _error_page("Your Google account has no verified email address.")

    user = repo.get_user_by_username(email)
    if user is None:
        if not _auto_provision():
            return _error_page(
                f"No Klyra account exists for {email}. Ask an admin to create one.",
                403,
            )
        user = repo.create_user(
            username=email,
            # Random unusable password for OAuth-only accounts (password
            # login never matches; verify_password fails safely).
            password_hash=hash_password(secrets.token_urlsafe(32)),
            role="instructor",
            courses=[],
        )
        logger.info("Auto-provisioned Google user %s (instructor, no courses)", email)

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        encode_session_token(user.id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
    )
    response.delete_cookie(STATE_COOKIE)
    return response


