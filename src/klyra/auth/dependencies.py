"""FastAPI dependencies for cookie-based auth + RBAC checks."""
from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket, status

from klyra.auth.sessions import SESSION_COOKIE, decode_session_token
from klyra.storage import repo
from klyra.storage.models import UserRecord


def get_current_user(request: Request) -> UserRecord:
    """Raise 401 unless the request carries a valid session cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    user_id = decode_session_token(token) if token else None
    user = repo.get_user_by_id(user_id) if user_id else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def try_get_current_user(request: Request) -> UserRecord | None:
    """Non-raising variant (used for optional-auth endpoints)."""
    token = request.cookies.get(SESSION_COOKIE)
    user_id = decode_session_token(token) if token else None
    return repo.get_user_by_id(user_id) if user_id else None


def require_admin(user: UserRecord) -> UserRecord:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def subjects_for_user(user: UserRecord) -> set[str] | None:
    """None → all subjects (admin). Otherwise the user's assigned courses."""
    if user.role == "admin":
        return None
    return set(repo.user_courses(user))


def can_access_subject(user: UserRecord, subject: str) -> bool:
    allowed = subjects_for_user(user)
    return allowed is None or subject.lower() in allowed


def can_read_generated_file(user: UserRecord, rel_path: str) -> bool:
    """rel_path is relative to docs/, e.g. 'dbms/generated/midterm.md'.

    Admins read everything. Instructors need the subject assigned AND
    ownership of the generated file (unclaimed/legacy files are admin-only).
    """
    if user.role == "admin":
        return True
    parts = rel_path.split("/")
    if len(parts) < 2:
        return False
    subject = parts[0]
    if not can_access_subject(user, subject):
        return False
    if len(parts) > 2 and parts[1] == "generated":
        return repo.get_file_owner(rel_path) == user.id
    return True


def websocket_user(ws: WebSocket) -> UserRecord | None:
    """Resolve the user from the WebSocket handshake cookies (or None)."""
    token = ws.cookies.get(SESSION_COOKIE)
    user_id = decode_session_token(token) if token else None
    return repo.get_user_by_id(user_id) if user_id else None