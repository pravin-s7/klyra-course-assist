"""Chat session CRUD routes and session persistence helpers for Klyra Web."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel

from klyra.auth.dependencies import get_current_user
from klyra.storage import repo
from klyra.storage.models import UserRecord

logger = logging.getLogger(__name__)

router = APIRouter()

ACTIVE_SESSIONS: dict[str, dict] = {}


class SessionUpdateRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None


def _save_session_to_db(sid: str, owner_id: str, title: str, orchestrator, history: list):
    repo.save_chat_session(
        session_id=sid,
        owner_id=owner_id,
        title=title,
        ui_history=history,
        gemini_history=orchestrator.to_dict(),
    )


def _load_session_from_db(sid: str):
    record = repo.get_chat_session(sid)
    if record is None:
        return None
    try:
        raw_ui = record.ui_history_json
        ui_hist = (
            json.loads(raw_ui)
            if isinstance(raw_ui, str)
            else (raw_ui if isinstance(raw_ui, list) else [])
        )
        raw_gem = record.gemini_history_json
        gem_hist = (
            json.loads(raw_gem)
            if isinstance(raw_gem, str)
            else (raw_gem if isinstance(raw_gem, dict) else {})
        )
        return {
            "owner_id": record.owner_id,
            "title": record.title,
            "pinned": record.pinned,
            "ui_history": ui_hist,
            "gemini_history": gem_hist,
        }
    except Exception as e:
        logger.error(f"Failed to load session {sid}: {e}")
    return None


def _own_session_or_404(session_id: str, user: UserRecord):
    record = repo.get_chat_session(session_id)
    if record is None:
        return None, Response(status_code=404, content="Session not found")
    if record.owner_id != user.id and user.role != "admin":
        return None, Response(status_code=403, content="You do not own this chat")
    return record, None


@router.get("/api/sessions")
def get_sessions(user: UserRecord = Depends(get_current_user)):
    """Return the user's own chat sessions (admin uses /api/admin/ for others')."""
    records = repo.list_chat_sessions(owner_id=user.id)
    sessions = [
        {
            "id": r.session_id,
            "title": r.title,
            "created_at": r.created_at,
            "pinned": r.pinned,
        }
        for r in records
    ]
    sessions.sort(key=lambda x: x["created_at"], reverse=True)
    sessions.sort(key=lambda x: x.get("pinned", False), reverse=True)
    return {"sessions": sessions}


@router.put("/api/sessions/{session_id}")
def update_session(
    session_id: str,
    req: SessionUpdateRequest,
    user: UserRecord = Depends(get_current_user),
):
    """Update one of the user's sessions (rename or pin)."""
    record, err = _own_session_or_404(session_id, user)
    if err is not None:
        return err

    if req.title is not None:
        repo.rename_chat_session(session_id, req.title)
    if req.pinned is not None:
        repo.set_chat_session_pinned(session_id, req.pinned)

    updated = repo.get_chat_session(session_id)
    return {
        "status": "success",
        "session": {
            "id": updated.session_id,
            "title": updated.title,
            "pinned": updated.pinned,
            "created_at": updated.created_at,
        }
        if updated
        else {"id": session_id},
    }


@router.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, user: UserRecord = Depends(get_current_user)):
    """Delete one of the user's sessions."""
    record, err = _own_session_or_404(session_id, user)
    if err is not None:
        return err
    repo.delete_chat_session(session_id)
    if session_id in ACTIVE_SESSIONS:
        del ACTIVE_SESSIONS[session_id]
    return {"status": "success"}

