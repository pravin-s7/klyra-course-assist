"""Data-access helpers for the storage layer.

All call sites (web server, auth routes, bootstrap) go through these
functions so the schema is never touched directly outside this module.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from klyra.storage.db import get_session_factory
from klyra.storage.models import ChatSessionRecord, FileOwnershipRecord, UserRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_json_native(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return json.loads(json.dumps(value, default=str))


# ── Users ──────────────────────────────────────────────────────────

def create_user(username: str, password_hash: str, role: str, courses: list[str], full_name: str | None = None, email: str | None = None) -> UserRecord:
    with get_session_factory() as session:
        user = UserRecord(
            id=uuid.uuid4().hex,
            username=username,
            full_name=full_name,
            email=email,
            password_hash=password_hash,
            role=role,
            courses_json=_to_json_native(courses, []),
            created_at=_now(),
        )
        session.add(user)
        return user


def get_user_by_username(username: str) -> UserRecord | None:
    with get_session_factory() as session:
        return session.query(UserRecord).filter(UserRecord.username == username).first()


def get_user_by_id(user_id: str) -> UserRecord | None:
    with get_session_factory() as session:
        return session.query(UserRecord).filter(UserRecord.id == user_id).first()


def list_users() -> list[UserRecord]:
    with get_session_factory() as session:
        return session.query(UserRecord).order_by(UserRecord.created_at).all()


def count_users() -> int:
    with get_session_factory() as session:
        return session.query(UserRecord).count()


def update_user(user_id: str, **fields) -> UserRecord | None:
    """Updatable fields: password_hash, role, courses, full_name, email."""
    with get_session_factory() as session:
        user = session.query(UserRecord).filter(UserRecord.id == user_id).first()
        if user is None:
            return None
        if "password_hash" in fields:
            user.password_hash = fields["password_hash"]
        if "role" in fields:
            user.role = fields["role"]
        if "courses" in fields:
            user.courses_json = _to_json_native(fields["courses"], [])
        if "full_name" in fields:
            user.full_name = fields["full_name"]
        if "email" in fields:
            user.email = fields["email"]
        session.add(user)
        return user


def delete_user(user_id: str) -> bool:
    with get_session_factory() as session:
        user = session.query(UserRecord).filter(UserRecord.id == user_id).first()
        if user is None:
            return False
        session.delete(user)
        return True


def user_courses(user: UserRecord) -> list[str]:
    val = user.courses_json
    if isinstance(val, list):
        return [str(x) for x in val]
    try:
        parsed = json.loads(val or "[]")
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


# ── Chat sessions ──────────────────────────────────────────────────

def get_chat_session(session_id: str) -> ChatSessionRecord | None:
    with get_session_factory() as session:
        return (
            session.query(ChatSessionRecord)
            .filter(ChatSessionRecord.session_id == session_id)
            .first()
        )


def list_chat_sessions(owner_id: str | None = None) -> list[ChatSessionRecord]:
    """owner_id=None → all sessions (admin view)."""
    with get_session_factory() as session:
        query = session.query(ChatSessionRecord)
        if owner_id is not None:
            query = query.filter(ChatSessionRecord.owner_id == owner_id)
        return query.all()


def save_chat_session(
    session_id: str,
    owner_id: str,
    title: str,
    ui_history: list,
    gemini_history: dict,
) -> None:
    """Insert-or-update a chat session from chat activity.

    Title is adopted only while the stored title is still the default (so an
    auto-title from the first message can land, but a user rename is never
    clobbered — use rename_chat_session for explicit renames). Pinned state
    is preserved for existing records (only set_chat_session_pinned changes
    it), so periodic auto-saves can't accidentally unpin a chat.
    """
    with get_session_factory() as session:
        record = (
            session.query(ChatSessionRecord)
            .filter(ChatSessionRecord.session_id == session_id)
            .first()
        )
        if record is None:
            record = ChatSessionRecord(
                session_id=session_id,
                owner_id=owner_id,
                title=title or "New Chat",
                pinned=False,
                created_at=_now(),
            )
        else:
            record.owner_id = owner_id
            if record.title == "New Chat" and title and title != "New Chat":
                record.title = title
        record.ui_history_json = _to_json_native(ui_history, [])
        record.gemini_history_json = _to_json_native(gemini_history, {})
        session.add(record)


def rename_chat_session(session_id: str, title: str) -> bool:
    with get_session_factory() as session:
        record = (
            session.query(ChatSessionRecord)
            .filter(ChatSessionRecord.session_id == session_id)
            .first()
        )
        if record is None:
            return False
        record.title = title or "New Chat"
        session.add(record)
        return True


def set_chat_session_pinned(session_id: str, pinned: bool) -> bool:
    with get_session_factory() as session:
        record = (
            session.query(ChatSessionRecord)
            .filter(ChatSessionRecord.session_id == session_id)
            .first()
        )
        if record is None:
            return False
        record.pinned = pinned
        session.add(record)
        return True


def delete_chat_session(session_id: str) -> bool:
    with get_session_factory() as session:
        record = (
            session.query(ChatSessionRecord)
            .filter(ChatSessionRecord.session_id == session_id)
            .first()
        )
        if record is None:
            return False
        session.delete(record)
        return True


# ── File ownership ─────────────────────────────────────────────────

def set_file_owner(rel_path: str, owner_id: str, subject: str) -> None:
    """Record (or overwrite) ownership of a generated file."""
    with get_session_factory() as session:
        record = (
            session.query(FileOwnershipRecord)
            .filter(FileOwnershipRecord.rel_path == rel_path)
            .first()
        )
        if record is None:
            record = FileOwnershipRecord(
                rel_path=rel_path, owner_id=owner_id, subject=subject, created_at=_now()
            )
        else:
            record.owner_id = owner_id
            record.subject = subject
        session.add(record)


def get_file_owner(rel_path: str) -> str | None:
    with get_session_factory() as session:
        record = (
            session.query(FileOwnershipRecord)
            .filter(FileOwnershipRecord.rel_path == rel_path)
            .first()
        )
        return record.owner_id if record else None


def any_files_owned_by_other(paths: list[str], owner_id: str) -> bool:
    """True if any of *paths* is owned by a different user, or has no
    ownership record (unclaimed/legacy content is admin-only, i.e. "not
    the requester's" until an admin reassigns it)."""
    if not paths:
        return False
    with get_session_factory() as session:
        records = (
            session.query(FileOwnershipRecord)
            .filter(FileOwnershipRecord.rel_path.in_(paths))
            .all()
        )
        by_path = {record.rel_path: record.owner_id for record in records}
    for path in paths:
        if by_path.get(path) != owner_id:
            return True
    return False


# ── Admin dashboard helpers ────────────────────────────────────────

def list_files_by_owner(owner_id: str) -> list[FileOwnershipRecord]:
    """All generated files recorded as owned by *owner_id*."""
    with get_session_factory() as session:
        return (
            session.query(FileOwnershipRecord)
            .filter(FileOwnershipRecord.owner_id == owner_id)
            .order_by(FileOwnershipRecord.created_at.desc())
            .all()
        )


def count_file_ownership() -> int:
    with get_session_factory() as session:
        return session.query(FileOwnershipRecord).count()


def clear_file_owner(rel_path: str) -> bool:
    """Remove an ownership record (revokes non-admin read access to the file).

    The file stays on disk but becomes admin-only until reassigned.
    """
    with get_session_factory() as session:
        record = (
            session.query(FileOwnershipRecord)
            .filter(FileOwnershipRecord.rel_path == rel_path)
            .first()
        )
        if record is None:
            return False
        session.delete(record)
        return True


def reassign_file_owner(rel_path: str, owner_id: str, subject: str) -> None:
    """Point an existing ownership record at a different user (admin tool)."""
    with get_session_factory() as session:
        record = (
            session.query(FileOwnershipRecord)
            .filter(FileOwnershipRecord.rel_path == rel_path)
            .first()
        )
        if record is None:
            record = FileOwnershipRecord(
                rel_path=rel_path, owner_id=owner_id, subject=subject, created_at=_now()
            )
        else:
            record.owner_id = owner_id
            record.subject = subject
        session.add(record)


def list_owners_by_subject(subject: str) -> list[FileOwnershipRecord]:
    with get_session_factory() as session:
        return (
            session.query(FileOwnershipRecord)
            .filter(FileOwnershipRecord.subject == subject)
            .all()
        )


def count_chat_sessions() -> int:
    with get_session_factory() as session:
        return session.query(ChatSessionRecord).count()
