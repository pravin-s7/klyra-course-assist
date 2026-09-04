"""Admin console API for the Klyra web app.

All endpoints require the logged-in user to have role == "admin" (same
HMAC cookie auth as the rest of the app). These power the single-page
admin dashboard (static/admin.html):

- GET    /api/admin/stats                        → overview counters
- GET    /api/admin/subjects                     → subjects on disk
- GET    /api/admin/config                       → read-only site config
- GET    /api/admin/users/{uid}/sessions         → a user's chats
- DELETE /api/admin/users/{uid}/sessions/{sid}   → delete one chat
- GET    /api/admin/users/{uid}/files            → a user's generated files
- DELETE /api/admin/users/{uid}/files/{rel_path} → revoke file ownership
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from klyra.auth.dependencies import get_current_user, require_admin
from klyra.storage import repo
from klyra.storage.models import UserRecord

router = APIRouter(tags=["admin"])


def _docs_dir() -> Path:
    return Path(os.environ.get("KLYRA_DOCS_DIR", Path.cwd() / "docs"))


def _mask(value: str | None) -> str:
    """Mask secrets for the read-only config panel."""
    if not value:
        return "—"
    if len(value) <= 8:
        return "*" * len(value)
    return value[:3] + "…" + value[-3:]


def _json_loads(raw: str, fallback):
    import json

    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return fallback


@router.get("/api/admin/stats")
def admin_stats(user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    all_users = repo.list_users()
    subjects = [
        p.name
        for p in _docs_dir().iterdir()
        if p.is_dir() and not p.name.startswith(".")
    ] if _docs_dir().exists() else []
    return {
        "users": len(all_users),
        "instructors": sum(1 for u in all_users if u.role != "admin"),
        "admins": sum(1 for u in all_users if u.role == "admin"),
        "chat_sessions": repo.count_chat_sessions(),
        "generated_files": repo.count_file_ownership(),
        "subjects": len(subjects),
        "subject_names": sorted(subjects),
    }

class SubjectCreate(BaseModel):
    name: str

@router.post("/api/admin/subjects")
def create_subject(payload: SubjectCreate, user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    name = payload.name.strip()
    if not name or not name.isalnum() and not all(c.isalnum() or c in "-_" for c in name):
        raise HTTPException(status_code=400, detail="Invalid subject name. Use alphanumeric characters, dashes, or underscores.")
    
    docs_dir = _docs_dir()
    docs_dir.mkdir(parents=True, exist_ok=True)
    subj_dir = docs_dir / name
    
    if subj_dir.exists():
        raise HTTPException(status_code=400, detail="Subject already exists.")
        
    subj_dir.mkdir()
    return {"message": f"Subject '{name}' created successfully"}


@router.get("/api/admin/subjects")
def admin_subjects(user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    subjects: list[dict] = []
    if not _docs_dir().exists():
        return {"subjects": subjects}
        
    all_users = repo.list_users()
    
    for p in sorted(_docs_dir().iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue
        folders = sorted(
            d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")
        )
        file_count = sum(len(files) for _, _, files in os.walk(p))
        
        # Find instructors for this subject
        instructors = []
        for u in all_users:
            if u.role != "admin" and p.name in repo.user_courses(u):
                instructors.append({
                    "id": u.id,
                    "username": u.username,
                    "full_name": u.full_name,
                    "email": u.email
                })
                
        subjects.append(
            {
                "name": p.name,
                "folders": folders,
                "file_count": file_count,
                "instructors": instructors,
            }
        )
    return {"subjects": subjects}

@router.get("/api/admin/subjects/{name}/files")
def admin_subject_files(name: str, user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    subj_dir = _docs_dir() / name
    if not subj_dir.exists() or not subj_dir.is_dir():
        raise HTTPException(status_code=404, detail="Subject not found")
        
    folders = {}
    
    for root, _, files in os.walk(subj_dir):
        # Determine relative folder path
        rel_root = Path(root).relative_to(subj_dir)
        folder_name = str(rel_root) if str(rel_root) != "." else "/"
        
        file_list = []
        for file in files:
            if not file.startswith("."):
                file_list.append(file)
                
        if file_list:
            if folder_name not in folders:
                folders[folder_name] = []
            folders[folder_name].extend(sorted(file_list))
            
    return {"subject": name, "folders": folders}



@router.get("/api/admin/config")
def admin_config(user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    return {
        "storage": {
            "database": os.environ.get("DATABASE_URL", ""),
            "jobs_db": os.environ.get("KLYRA_JOBS_DB", ".klyra_jobs.sqlite3"),
            "klyra_db": os.environ.get("KLYRA_DB", ".db/klyra.db"),
        },
        "auth": {
            "session_secret": os.environ.get("KLYRA_SESSION_SECRET", ""),
            "google_client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "google_client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            "oauth_auto_provision": os.environ.get("KLYRA_OAUTH_AUTO_PROVISION", "1"),
        },
        "llm": {
            "model": os.environ.get("KLYRA_LLM_MODEL", "gemini/gemini-3.1-flash-lite"),
            "chat_model": os.environ.get("KLYRA_CHAT_MODEL", "gemini-3.5-flash-lite"),
            "voice_model": os.environ.get("KLYRA_VOICE_MODEL", "gemini-2.0-flash-exp"),
        },
        "infra": {
            "qdrant_url": os.environ.get("QDRANT_URL", "http://localhost:6333"),
            "celery_broker": os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"),
            "redis_key": os.environ.get("REDIS_PASSWORD", ""),
        },
    }

class ConfigUpdateRequest(BaseModel):
    settings: dict[str, str]

@router.post("/api/admin/config")
def update_admin_config(req: ConfigUpdateRequest, user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    import dotenv
    env_path = Path("/home/pravin/klyra/.env")
    if not env_path.exists():
        env_path.touch()
    
    for key, value in req.settings.items():
        if value is not None:
            # Update memory
            os.environ[key] = value
            # Update file
            dotenv.set_key(env_path, key, value)
            
    return {"status": "ok"}


@router.get("/api/admin/logs")
def get_admin_logs(lines: int = 100, user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    logs = ""
    log_path = Path("voice_tools.log")
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                # Read last N lines using standard approach
                all_lines = f.readlines()
                logs = "".join(all_lines[-lines:])
        except Exception as e:
            logs = f"Error reading log: {e}"
    else:
        logs = "No log file found."
    return {"logs": logs}


@router.get("/api/admin/users/{user_id}/sessions")
def admin_user_sessions(user_id: str, user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    if repo.get_user_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    sessions = [
        {
            "session_id": r.session_id,
            "title": r.title,
            "pinned": r.pinned,
            "created_at": r.created_at,
            "message_count": len(_json_loads(r.ui_history_json, [])),
        }
        for r in repo.list_chat_sessions(owner_id=user_id)
    ]
    sessions.sort(key=lambda s: s["created_at"], reverse=True)
    return {"sessions": sessions}


@router.get("/api/admin/sessions/{session_id}/history")
def admin_session_history(session_id: str, user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    record = repo.get_chat_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found")
    history = _json_loads(record.ui_history_json, [])
    return {"history": history}


@router.delete("/api/admin/users/{user_id}/sessions/{session_id}")
def admin_delete_user_session(
    user_id: str, session_id: str, user: UserRecord = Depends(get_current_user)
):
    require_admin(user)
    record = repo.get_chat_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if record.owner_id != user_id:
        raise HTTPException(
            status_code=400, detail="Session does not belong to that user"
        )
    repo.delete_chat_session(session_id)
    return {"status": "deleted"}


@router.get("/api/admin/users/{user_id}/files")
def admin_user_files(user_id: str, user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    if repo.get_user_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    files = []
    for r in repo.list_files_by_owner(user_id):
        full_path = _docs_dir() / r.rel_path
        files.append(
            {
                "rel_path": r.rel_path,
                "subject": r.subject,
                "created_at": r.created_at,
                "exists": full_path.is_file(),
                "size": full_path.stat().st_size if full_path.is_file() else 0,
            }
        )
    return {"files": files}


@router.delete("/api/admin/users/{user_id}/files/{rel_path:path}")
def admin_revoke_file(
    user_id: str, rel_path: str, user: UserRecord = Depends(get_current_user)
):
    require_admin(user)
    record_owner = repo.get_file_owner(rel_path)
    if record_owner is None:
        raise HTTPException(status_code=404, detail="No ownership record for that file")
    if record_owner != user_id:
        raise HTTPException(
            status_code=400, detail="That file is not owned by the given user"
        )
    repo.clear_file_owner(rel_path)
    return {"status": "revoked", "rel_path": rel_path}