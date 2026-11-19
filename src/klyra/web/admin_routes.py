"""Admin console API for the Klyra web app.

All endpoints require the logged-in user to have role == "admin" (same
HMAC cookie auth as the rest of the app). These power the single-page
admin dashboard (static/admin.html):

- GET    /api/admin/stats                          → overview counters + recent activity
- GET    /api/admin/subjects                       → subjects on disk + skill.md status
- POST   /api/admin/subjects                       → create subject + default skill.md
- DELETE /api/admin/subjects/{name}                → delete subject folder
- GET    /api/admin/subjects/{name}/files          → files organized by folder
- DELETE /api/admin/subjects/{name}/files/{path}   → delete a file inside a subject
- GET    /api/admin/subjects/{name}/skill          → read/render subject skill.md
- PUT    /api/admin/subjects/{name}/skill          → save/initialize subject skill.md
- GET    /api/admin/config                         → read site configuration
- POST   /api/admin/config                         → update .env & runtime configuration
- GET    /api/admin/logs                           → tail application & background task logs
- GET    /api/admin/jobs                           → list recent background & Celery jobs
- GET    /api/admin/users/{uid}/sessions           → a user's chats
- GET    /api/admin/sessions/{sid}/history         → full chat transcript
- DELETE /api/admin/users/{uid}/sessions/{sid}     → delete one chat
- GET    /api/admin/users/{uid}/files              → a user's generated files
- DELETE /api/admin/users/{uid}/files/{rel_path}   → revoke file ownership
"""
from __future__ import annotations

import collections
import datetime
import logging
import os
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from klyra.auth.dependencies import get_current_user, require_admin
from klyra.storage import repo
from klyra.storage.models import UserRecord

router = APIRouter(tags=["admin"])

_ADMIN_LOG_BUFFER: collections.deque[str] = collections.deque(maxlen=500)
_LOGS_CLEARED_AT: datetime.datetime | None = None


def _parse_log_ts(raw: str | None) -> datetime.datetime | None:
    if not raw:
        return None
    s = str(raw).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except ValueError:
        return None


class _AdminRingLogHandler(logging.Handler):
    """Capture Klyra application warnings/errors/info into an in-memory ring buffer for `/api/admin/logs`."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.datetime.fromtimestamp(
                record.created, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            msg = record.getMessage()
            _ADMIN_LOG_BUFFER.append(
                f"{ts} [{record.levelname}] {record.name}: {msg}"
            )
        except Exception:
            pass


_ring_handler = _AdminRingLogHandler(level=logging.INFO)
for _logger_name in ("klyra", "uvicorn.error"):
    _lg = logging.getLogger(_logger_name)
    if not any(isinstance(h, _AdminRingLogHandler) for h in _lg.handlers):
        _lg.addHandler(_ring_handler)


def _docs_dir() -> Path:
    try:
        from klyra.web import server as _srv

        srv_docs = getattr(_srv, "_DOCS_DIR", None)
        if isinstance(srv_docs, Path) and srv_docs != (Path.cwd() / "docs"):
            return srv_docs
    except Exception:
        pass
    return Path(os.environ.get("KLYRA_DOCS_DIR", Path.cwd() / "docs"))


def _mask(value: str | None) -> str:
    """Mask secrets for the read-only config panel."""
    if not value:
        return "—"
    if len(value) <= 8:
        return "*" * len(value)
    return value[:3] + "…" + value[-3:]


def _json_loads(raw, fallback):
    import json

    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return fallback


def _normalize_session_history(record) -> list[dict]:
    """Normalize chat history events for a session record.

    Handles:
    - Live in-memory sessions (`ACTIVE_SESSIONS`) that may have newer events
    - `ui_history_json` with standard event dicts (`user_transcript`, `klyra_text`, `tool_done`, `file_link`)
    - Legacy tuple format `["user"|"klyra", text]`
    - Fallback to `gemini_history_json` when `ui_history_json` has no visible items
    - Backfilling `"timestamp"` on events using `record.created_at` / `record.modified_at`
    """
    raw_history = _json_loads(record.ui_history_json, [])
    try:
        from klyra.web.routes.sessions import ACTIVE_SESSIONS

        live = ACTIVE_SESSIONS.get(record.session_id)
        live_history = live.get("history") if isinstance(live, dict) else None
        if isinstance(live_history, list) and len(live_history) >= len(raw_history):
            raw_history = list(live_history)
    except Exception:
        pass

    default_user_ts = getattr(record, "created_at", "") or ""
    default_bot_ts = (
        getattr(record, "modified_at", "")
        or getattr(record, "created_at", "")
        or ""
    )

    visible: list[dict] = []
    for item in raw_history:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            role, text = item[0], item[1]
            ev_type = "user_transcript" if role == "user" else "klyra_text"
            ts = item[2] if len(item) >= 3 and item[2] else (
                default_user_ts if role == "user" else default_bot_ts
            )
            visible.append({"type": ev_type, "text": str(text), "timestamp": ts})
        elif isinstance(item, dict):
            ev_type = item.get("type", "")
            if ev_type in ("turn_complete", "tool_start"):
                continue
            ev_copy = dict(item)
            if ev_type == "klyra_transcript":
                ev_copy["type"] = "klyra_text"
                ev_type = "klyra_text"
            if not ev_copy.get("timestamp"):
                ev_copy["timestamp"] = (
                    default_user_ts
                    if ev_type == "user_transcript"
                    else default_bot_ts
                )
            if ev_type in ("user_transcript", "klyra_text", "tool_done", "file_link"):
                visible.append(ev_copy)

    # Fallback: if ui_history_json only had turn_complete or was empty, check gemini_history_json
    if not visible:
        gem = _json_loads(getattr(record, "gemini_history_json", "{}"), {})
        gem_hist = gem.get("history", []) if isinstance(gem, dict) else []
        for turn in gem_hist:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role", "")
            parts = turn.get("parts", [])
            texts = [
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and p.get("text")
            ]
            combined = "\n".join(t for t in texts if t.strip()).strip()
            if combined:
                visible.append(
                    {
                        "type": "user_transcript" if role == "user" else "klyra_text",
                        "text": combined,
                        "timestamp": default_user_ts if role == "user" else default_bot_ts,
                    }
                )

    return visible


@router.get("/api/admin/stats")
def admin_stats(user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    all_users = repo.list_users()
    users_by_id = {u.id: u for u in all_users}
    docs_root = _docs_dir()
    subjects = (
        [
            p.name
            for p in docs_root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ]
        if docs_root.exists()
        else []
    )

    all_sessions = repo.list_chat_sessions(owner_id=None)
    all_sessions_sorted = sorted(
        all_sessions,
        key=lambda r: (getattr(r, "modified_at", None) or r.created_at or ""),
        reverse=True,
    )
    recent_sessions = []
    for r in all_sessions_sorted[:10]:
        owner = users_by_id.get(r.owner_id)
        norm_hist = _normalize_session_history(r)
        recent_sessions.append(
            {
                "session_id": r.session_id,
                "title": r.title,
                "owner_id": r.owner_id,
                "owner_username": owner.username if owner else "unknown",
                "created_at": r.created_at,
                "modified_at": getattr(r, "modified_at", None) or r.created_at,
                "message_count": len(norm_hist),
            }
        )

    db_url = os.environ.get("DATABASE_URL", "").strip()
    qdrant_url = os.environ.get("QDRANT_URL", "").strip()
    has_gemini_key = bool(
        (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    )
    active_model = (
        os.environ.get("KLYRA_CHAT_MODEL")
        or os.environ.get("KLYRA_MODEL")
        or "gemini-3.5-flash-lite"
    )

    return {
        "users": len(all_users),
        "instructors": sum(1 for u in all_users if u.role != "admin"),
        "admins": sum(1 for u in all_users if u.role == "admin"),
        "chat_sessions": repo.count_chat_sessions(),
        "generated_files": repo.count_file_ownership(),
        "subjects": len(subjects),
        "subject_names": sorted(subjects),
        "recent_sessions": recent_sessions,
        "system_health": {
            "database": "PostgreSQL" if db_url else "SQLite (.db/klyra.db)",
            "vector_store": f"Qdrant ({qdrant_url})" if qdrant_url else "ChromaDB (.chroma)",
            "gemini_api_configured": has_gemini_key,
            "active_model": active_model,
        },
    }


class SubjectCreate(BaseModel):
    name: str
    init_skill: bool = True
    init_folders: bool = True


@router.post("/api/admin/subjects")
def create_subject(
    payload: SubjectCreate, user: UserRecord = Depends(get_current_user)
):
    require_admin(user)
    name = payload.name.strip().lower()
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise HTTPException(
            status_code=400,
            detail="Invalid subject name. Use alphanumeric characters, dashes, or underscores.",
        )

    docs_dir = _docs_dir()
    docs_dir.mkdir(parents=True, exist_ok=True)
    subj_dir = docs_dir / name

    if subj_dir.exists():
        raise HTTPException(status_code=400, detail="Subject already exists.")

    subj_dir.mkdir(parents=True, exist_ok=True)
    if payload.init_folders:
        for sub in ("week_1", "pyq", "transcripts", "additional", "generated"):
            (subj_dir / sub).mkdir(parents=True, exist_ok=True)

    if payload.init_skill:
        try:
            from klyra.services.skill_service import (
                default_subject_skill,
                render_subject_skill_markdown,
            )

            skill_path = subj_dir / "skill.md"
            if not skill_path.exists():
                skill_path.write_text(
                    render_subject_skill_markdown(default_subject_skill(name)),
                    encoding="utf-8",
                )
        except Exception:
            pass

    return {"message": f"Subject '{name}' created successfully", "name": name}


@router.delete("/api/admin/subjects/{name}")
def delete_subject(name: str, user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    docs_dir = _docs_dir().resolve()
    subj_dir = (docs_dir / name).resolve()
    if not str(subj_dir).startswith(str(docs_dir)) or subj_dir == docs_dir:
        raise HTTPException(status_code=400, detail="Invalid subject path")
    if not subj_dir.exists() or not subj_dir.is_dir():
        raise HTTPException(status_code=404, detail="Subject not found")

    shutil.rmtree(subj_dir)

    # Clean up vector index for this subject if present
    try:
        from klyra.storage import vector_store

        vector_store.delete_subject(name)
    except Exception:
        pass

    # Remove deleted subject from assigned users
    for u in repo.list_users():
        courses = repo.user_courses(u)
        if name.lower() in [c.lower() for c in courses]:
            updated_courses = [c for c in courses if c.lower() != name.lower()]
            repo.update_user(u.id, courses=updated_courses)

    return {"status": "deleted", "name": name}


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
        file_count = sum(
            len([f for f in files if not f.startswith(".")])
            for _, _, files in os.walk(p)
        )
        has_skill_md = (p / "skill.md").is_file()
        skill_summary = {
            "display_name": p.name.replace("_", " ").title(),
            "domain": "general",
            "allowed_question_types": ["MCQ", "MSQ", "NAT", "Descriptive"],
            "default_type_counts": {},
            "coding_languages": [],
            "diagram_style": "matplotlib",
            "qp_style": "standard_exam",
        }
        try:
            from klyra.services.skill_service import get_subject_skill

            cfg = get_subject_skill(p.name)
            skill_summary = {
                "display_name": cfg.display_name,
                "domain": cfg.domain,
                "allowed_question_types": list(cfg.allowed_question_types),
                "default_type_counts": dict(cfg.default_type_counts),
                "coding_languages": list(cfg.coding_languages),
                "diagram_style": cfg.diagram_style,
                "qp_style": cfg.output_formats.qp_style,
            }
        except Exception:
            pass

        instructors = []
        for u in all_users:
            if u.role != "admin" and p.name.lower() in [
                c.lower() for c in repo.user_courses(u)
            ]:
                instructors.append(
                    {
                        "id": u.id,
                        "username": u.username,
                        "full_name": u.full_name,
                        "email": u.email,
                    }
                )

        subjects.append(
            {
                "name": p.name,
                "folders": folders,
                "file_count": file_count,
                "has_skill_md": has_skill_md,
                "skill_summary": skill_summary,
                "instructors": instructors,
            }
        )
    return {"subjects": subjects}


@router.get("/api/admin/subjects/{name}/files")
def admin_subject_files(name: str, user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    docs_root = _docs_dir().resolve()
    subj_dir = (docs_root / name).resolve()
    if not str(subj_dir).startswith(str(docs_root)) or not subj_dir.is_dir():
        raise HTTPException(status_code=404, detail="Subject not found")

    folders: dict[str, list[str]] = {}
    file_details: dict[str, list[dict]] = {}
    users_by_id = {u.id: u for u in repo.list_users()}

    for root, _, files in os.walk(subj_dir):
        rel_root = Path(root).relative_to(subj_dir)
        # Skip hidden dirs other than .assets
        parts = rel_root.parts
        if any(
            part.startswith(".") and part != ".assets" for part in parts
        ):
            continue
        folder_name = rel_root.as_posix() if str(rel_root) != "." else "/"

        file_list = []
        detail_list = []
        for file in sorted(files):
            if file.startswith("."):
                continue
            fp = Path(root) / file
            rel_to_subj = fp.relative_to(subj_dir).as_posix()
            rel_path = f"{name}/{rel_to_subj}"
            file_list.append(file)
            try:
                st = fp.stat()
                size = st.st_size
            except OSError:
                size = 0
            owner_id = None
            owner_username = None
            if "/generated/" in f"/{rel_path}":
                owner_id = repo.get_file_owner(rel_path)
                if owner_id and owner_id in users_by_id:
                    owner_username = users_by_id[owner_id].username
            detail_list.append(
                {
                    "name": file,
                    "rel_path": rel_path,
                    "sub_path": rel_to_subj,
                    "size": size,
                    "owner_id": owner_id,
                    "owner_username": owner_username,
                }
            )

        if file_list:
            folders.setdefault(folder_name, []).extend(file_list)
            file_details.setdefault(folder_name, []).extend(detail_list)

    return {
        "subject": name,
        "folders": folders,
        "file_details": file_details,
    }


@router.delete("/api/admin/subjects/{name}/files/{sub_path:path}")
def admin_delete_subject_file(
    name: str, sub_path: str, user: UserRecord = Depends(get_current_user)
):
    require_admin(user)
    docs_root = _docs_dir().resolve()
    subj_dir = (docs_root / name).resolve()
    target = (subj_dir / sub_path.lstrip("/")).resolve()
    if not str(target).startswith(str(subj_dir)) or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    rel_to_docs = target.relative_to(docs_root).as_posix()
    file_name = target.name
    target.unlink()
    try:
        repo.clear_file_owner(rel_to_docs)
    except Exception:
        pass
    try:
        from klyra.storage import vector_store

        vector_store.delete_by_source(name, file_name)
    except Exception:
        pass
    return {"status": "deleted", "path": rel_to_docs}


class SkillUpdateRequest(BaseModel):
    content: str


@router.get("/api/admin/subjects/{name}/skill")
def admin_get_subject_skill(
    name: str,
    default: bool = False,
    user: UserRecord = Depends(get_current_user),
):
    require_admin(user)
    docs_root = _docs_dir().resolve()
    subj_dir = (docs_root / name).resolve()
    if not str(subj_dir).startswith(str(docs_root)) or not subj_dir.is_dir():
        raise HTTPException(status_code=404, detail="Subject not found")

    skill_path = subj_dir / "skill.md"
    exists = skill_path.is_file()
    if exists and not default:
        content = skill_path.read_text(encoding="utf-8")
    else:
        try:
            from klyra.services.skill_service import (
                default_subject_skill,
                render_subject_skill_markdown,
            )

            content = render_subject_skill_markdown(default_subject_skill(name))
        except Exception:
            content = f"# {name}\n"

    return {"subject": name, "exists": exists, "content": content}


@router.put("/api/admin/subjects/{name}/skill")
def admin_put_subject_skill(
    name: str, req: SkillUpdateRequest, user: UserRecord = Depends(get_current_user)
):
    require_admin(user)
    docs_root = _docs_dir().resolve()
    subj_dir = (docs_root / name).resolve()
    if not str(subj_dir).startswith(str(docs_root)) or not subj_dir.is_dir():
        raise HTTPException(status_code=404, detail="Subject not found")

    skill_path = subj_dir / "skill.md"
    skill_path.write_text(req.content, encoding="utf-8")

    # Validate syntax if possible
    validation_summary = "Saved skill.md"
    try:
        from klyra.services.skill_service import parse_subject_skill

        cfg = parse_subject_skill(skill_path)
        validation_summary = (
            f"Saved valid skill.md for '{cfg.display_name}' "
            f"(style={cfg.output_formats.qp_style}, languages={','.join(cfg.coding_languages)})"
        )
    except Exception as exc:
        validation_summary = f"Saved skill.md (warning: {exc})"

    return {"status": "ok", "message": validation_summary}


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
            "api_key": os.environ.get("KLYRA_API_KEY", ""),
            "google_client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "google_client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            "oauth_auto_provision": os.environ.get("KLYRA_OAUTH_AUTO_PROVISION", "1"),
        },
        "llm": {
            "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
            "model": os.environ.get("KLYRA_MODEL")
            or os.environ.get("KLYRA_LLM_MODEL", "gemini/gemini-3.1-flash-lite"),
            "chat_model": os.environ.get("KLYRA_CHAT_MODEL", "gemini-3.5-flash-lite"),
            "voice_model": os.environ.get(
                "KLYRA_VOICE_MODEL", "gemini-3.1-flash-live-preview"
            ),
            "voice_name": os.environ.get("KLYRA_VOICE_NAME", "Puck"),
            "embedding_provider": os.environ.get("EMBEDDING_PROVIDER", "google"),
            "embedding_model": os.environ.get(
                "GOOGLE_EMBEDDING_MODEL", "models/gemini-embedding-2-preview"
            ),
        },
        "infra": {
            "qdrant_url": os.environ.get("QDRANT_URL", "http://localhost:6333"),
            "celery_broker": os.environ.get(
                "CELERY_BROKER_URL", "redis://localhost:6379/0"
            ),
            "redis_key": os.environ.get("REDIS_PASSWORD", ""),
        },
    }


class ConfigUpdateRequest(BaseModel):
    settings: dict[str, str]


@router.post("/api/admin/config")
def update_admin_config(
    req: ConfigUpdateRequest, user: UserRecord = Depends(get_current_user)
):
    require_admin(user)
    import dotenv

    env_path = Path(os.environ.get("KLYRA_ENV_FILE", Path.cwd() / ".env"))
    if not env_path.exists():
        env_path.touch()

    for key, value in req.settings.items():
        if value is not None:
            os.environ[key] = value
            dotenv.set_key(str(env_path), key, value)
            if key == "KLYRA_MODEL":
                os.environ["KLYRA_LLM_MODEL"] = value
            elif key == "KLYRA_LLM_MODEL":
                os.environ["KLYRA_MODEL"] = value

    return {"status": "ok"}


@router.get("/api/admin/logs")
def get_admin_logs(
    lines: int = 200,
    q: str = "",
    username: str = "",
    level: str = "",
    start_time: str = "",
    end_time: str = "",
    user: UserRecord = Depends(get_current_user),
):
    require_admin(user)
    entries: list[dict] = []
    all_users = repo.list_users()
    users_by_id = {u.id: u.username for u in all_users}
    usernames_list = sorted({u.username for u in all_users if u.username})

    # 1. In-memory application ring-buffer logs
    for line in list(_ADMIN_LOG_BUFFER):
        first_tok = line.split(" ", 1)[0] if line else ""
        if len(first_tok) >= 19 and first_tok[4:5] == "-":
            ts = first_tok
        elif len(line) >= 19 and line[4:5] == "-":
            ts = line[:19]
        else:
            ts = ""
        lvl = "INFO"
        for candidate in ("ERROR", "WARNING", "INFO", "DEBUG"):
            if f"[{candidate}]" in line:
                lvl = candidate
                break
        entries.append(
            {
                "timestamp": ts,
                "username": "system",
                "level": lvl,
                "source": "server",
                "message": line,
            }
        )

    # 2. User chat activity logs (prompts, responses, tool runs)
    try:
        for sess in repo.list_chat_sessions(owner_id=None):
            uname = users_by_id.get(sess.owner_id, "unknown")
            norm_hist = _normalize_session_history(sess)
            if not norm_hist:
                entries.append(
                    {
                        "timestamp": sess.created_at or "",
                        "username": uname,
                        "level": "INFO",
                        "source": "chat",
                        "message": f"Created chat session '{sess.title}' ({sess.session_id[:8]})",
                    }
                )
            for ev in norm_hist:
                ev_type = ev.get("type", "")
                ts = ev.get("timestamp") or sess.modified_at or sess.created_at or ""
                if ev_type == "user_transcript":
                    txt = (ev.get("text") or "").replace("\n", " ")[:180]
                    entries.append(
                        {
                            "timestamp": ts,
                            "username": uname,
                            "level": "INFO",
                            "source": "prompt",
                            "message": f"Prompt in '{sess.title}': {txt}",
                        }
                    )
                elif ev_type == "klyra_text":
                    txt = (ev.get("text") or "").replace("\n", " ")[:180]
                    is_err = "⚠️" in txt or "error" in txt.lower()
                    entries.append(
                        {
                            "timestamp": ts,
                            "username": uname,
                            "level": "ERROR" if is_err else "INFO",
                            "source": "response",
                            "message": f"Response in '{sess.title}': {txt}",
                        }
                    )
                elif ev_type == "tool_done":
                    entries.append(
                        {
                            "timestamp": ts,
                            "username": uname,
                            "level": "INFO",
                            "source": "tool",
                            "message": f"Tool '{ev.get('name', 'tool')}': {(ev.get('summary') or '')[:160]}",
                        }
                    )
    except Exception:
        pass

    # 3. Generated file ownership activity
    try:
        for u in all_users:
            for f in repo.list_files_by_owner(u.id):
                entries.append(
                    {
                        "timestamp": f.created_at or "",
                        "username": u.username,
                        "level": "INFO",
                        "source": "file",
                        "message": f"Generated file '{f.rel_path}' in course '{f.subject}'",
                    }
                )
    except Exception:
        pass

    # 4. Web background task logs
    try:
        from klyra.web.routes.files import background_tasks_state

        for tid, info in list(background_tasks_state.items()):
            status = info.get("status", "unknown")
            subj = info.get("subject", "")
            cat = info.get("category", "")
            lvl = "ERROR" if status == "failed" else "INFO"
            entries.append(
                {
                    "timestamp": info.get("created_at", ""),
                    "username": info.get("username", "admin"),
                    "level": lvl,
                    "source": "ingest",
                    "message": f"[{tid[:8]}] status={status} subject={subj} category={cat} {info.get('logs', '').strip()}",
                }
            )
    except Exception:
        pass

    # 5. voice_tools.log on disk if present
    log_path = Path("voice_tools.log")
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for raw_line in f.readlines()[-lines:]:
                    ln = raw_line.strip()
                    if not ln:
                        continue
                    ts = ln[:19] if len(ln) >= 19 and ln[4:5] == "-" else ""
                    lvl = "ERROR" if "ERROR" in ln else ("WARNING" if "WARN" in ln else "INFO")
                    entries.append(
                        {
                            "timestamp": ts,
                            "username": "system",
                            "level": lvl,
                            "source": "voice_tools",
                            "message": ln,
                        }
                    )
        except Exception:
            pass

    # Sort newest first by timestamp string (ISO-like timestamps compare lexicographically)
    entries.sort(key=lambda e: (e.get("timestamp") or "").replace(" ", "T"), reverse=True)

    # Apply server-side filters (including clear-logs watermark)
    q_norm = q.strip().lower()
    user_norm = username.strip().lower()
    lvl_norm = level.strip().upper()
    start_norm = start_time.strip().replace(" ", "T")
    end_norm = end_time.strip().replace(" ", "T")

    filtered: list[dict] = []
    for e in entries:
        if _LOGS_CLEARED_AT is not None:
            entry_dt = _parse_log_ts(e.get("timestamp"))
            if entry_dt is None or entry_dt <= _LOGS_CLEARED_AT:
                continue
        if user_norm and e.get("username", "").lower() != user_norm:
            continue
        if lvl_norm and e.get("level", "").upper() != lvl_norm:
            continue
        ts_cmp = (e.get("timestamp") or "").replace(" ", "T")
        if start_norm and ts_cmp and ts_cmp < start_norm:
            continue
        if end_norm and ts_cmp and ts_cmp[: len(end_norm)] > end_norm:
            continue
        if q_norm:
            haystack = f"{e.get('timestamp', '')} {e.get('username', '')} {e.get('source', '')} {e.get('message', '')}".lower()
            if q_norm not in haystack:
                continue
        filtered.append(e)

    filtered = filtered[:lines]
    formatted_lines = [
        f"{e.get('timestamp') or '—'} [{e.get('level', 'INFO')}] @{e.get('username', 'system')} ({e.get('source', 'app')}): {e.get('message', '')}"
        for e in filtered
    ]
    logs_text = (
        "\n".join(formatted_lines)
        if formatted_lines
        else "No log entries match the current filters."
    )
    return {
        "logs": logs_text,
        "entries": filtered,
        "usernames": usernames_list,
    }


@router.delete("/api/admin/logs")
def clear_admin_logs(user: UserRecord = Depends(get_current_user)):
    global _LOGS_CLEARED_AT
    require_admin(user)
    _ADMIN_LOG_BUFFER.clear()
    _LOGS_CLEARED_AT = datetime.datetime.now(datetime.timezone.utc)
    try:
        from klyra.web.routes.files import background_tasks_state

        for tid in list(background_tasks_state.keys()):
            if background_tasks_state[tid].get("status") != "running":
                background_tasks_state.pop(tid, None)
    except Exception:
        pass
    log_path = Path("voice_tools.log")
    if log_path.exists():
        try:
            log_path.write_text("", encoding="utf-8")
        except Exception:
            pass
    return {"status": "cleared", "cleared_at": _LOGS_CLEARED_AT.isoformat()}


@router.get("/api/admin/jobs")
def get_admin_jobs(user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    jobs_list: list[dict] = []

    try:
        from klyra.web.routes.files import background_tasks_state

        for tid, info in list(background_tasks_state.items()):
            jobs_list.append(
                {
                    "id": tid,
                    "source": "web_upload",
                    "task_type": f"ingest ({info.get('category', 'files')})",
                    "subject": info.get("subject", "—"),
                    "status": info.get("status", "unknown"),
                    "files": info.get("files", []),
                    "error": info.get("error"),
                }
            )
    except Exception:
        pass

    try:
        from klyra.services import job_store

        for j in job_store.list_jobs(limit=20):
            meta = j.get("metadata") if isinstance(j.get("metadata"), dict) else {}
            jobs_list.append(
                {
                    "id": j.get("job_id", ""),
                    "source": "celery_api",
                    "task_type": j.get("job_type", "job"),
                    "subject": meta.get("subject", "—"),
                    "status": j.get("status", "unknown"),
                    "files": [],
                    "error": j.get("error"),
                }
            )
    except Exception:
        pass

    return {"jobs": jobs_list}


def _serialize_admin_session(record, owner: UserRecord | None = None) -> dict:
    """Serialize a chat session record with message, attachment, and generated-file counts."""
    norm_hist = _normalize_session_history(record)
    attachment_count = 0
    file_link_count = 0
    preview = ""
    for ev in norm_hist:
        if not isinstance(ev, dict):
            continue
        ev_type = ev.get("type", "")
        if ev_type == "user_transcript":
            atts = ev.get("attachments")
            if isinstance(atts, list):
                attachment_count += len(atts)
            if not preview:
                txt = str(ev.get("text") or "").strip()
                if txt:
                    preview = txt[:140]
        elif ev_type == "file_link":
            file_link_count += 1

    return {
        "session_id": record.session_id,
        "title": record.title or "Untitled Chat",
        "pinned": record.pinned,
        "owner_id": record.owner_id,
        "owner_username": owner.username if owner else "unknown",
        "owner_full_name": (owner.full_name or "") if owner else "",
        "owner_role": owner.role if owner else "",
        "created_at": record.created_at,
        "modified_at": getattr(record, "modified_at", None) or record.created_at,
        "message_count": len(norm_hist),
        "attachment_count": attachment_count,
        "file_link_count": file_link_count,
        "preview": preview,
    }


@router.get("/api/admin/sessions")
def admin_all_sessions(user: UserRecord = Depends(get_current_user)):
    """Return all chat sessions across all users for the Admin Chats view."""
    require_admin(user)
    all_users = repo.list_users()
    users_by_id = {u.id: u for u in all_users}
    counts_by_user: dict[str, int] = {u.id: 0 for u in all_users}

    sessions: list[dict] = []
    for r in repo.list_chat_sessions(owner_id=None):
        owner = users_by_id.get(r.owner_id)
        if r.owner_id in counts_by_user:
            counts_by_user[r.owner_id] += 1
        sessions.append(_serialize_admin_session(r, owner))

    sessions.sort(key=lambda s: s["modified_at"] or s["created_at"], reverse=True)
    users_summary = [
        {
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name or "",
            "role": u.role,
            "session_count": counts_by_user.get(u.id, 0),
        }
        for u in all_users
    ]
    return {
        "sessions": sessions,
        "users": users_summary,
        "total": len(sessions),
        "with_attachments_count": sum(1 for s in sessions if s["attachment_count"] > 0),
        "with_files_count": sum(1 for s in sessions if s["file_link_count"] > 0),
    }


@router.get("/api/admin/users/{user_id}/sessions")
def admin_user_sessions(user_id: str, user: UserRecord = Depends(get_current_user)):
    require_admin(user)
    target = repo.get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    sessions = [
        _serialize_admin_session(r, target)
        for r in repo.list_chat_sessions(owner_id=user_id)
    ]
    sessions.sort(key=lambda s: s["modified_at"] or s["created_at"], reverse=True)
    return {"sessions": sessions}


@router.get("/api/admin/sessions/{session_id}/history")
def admin_session_history(
    session_id: str, user: UserRecord = Depends(get_current_user)
):
    require_admin(user)
    record = repo.get_chat_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found")
    owner = repo.get_user_by_id(record.owner_id) if record.owner_id else None
    history = _normalize_session_history(record)
    return {
        "session_id": record.session_id,
        "title": record.title,
        "owner_id": record.owner_id,
        "owner_username": owner.username if owner else "unknown",
        "created_at": record.created_at,
        "modified_at": getattr(record, "modified_at", None) or record.created_at,
        "history": history,
    }


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
    from klyra.web.routes.files import (
        _CHAT_IMAGE_EXTS,
        _artifact_source_for_rel,
        _strip_upload_uuid_prefix,
    )

    require_admin(user)
    if repo.get_user_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    files = []
    for r in repo.list_files_by_owner(user_id):
        full_path = _docs_dir() / r.rel_path
        ext = full_path.suffix.lower()
        source = _artifact_source_for_rel(r.rel_path)
        display_name = (
            _strip_upload_uuid_prefix(full_path.name)
            if source == "uploaded"
            else full_path.name
        )
        files.append(
            {
                "name": display_name,
                "rel_path": r.rel_path,
                "url": f"/files/{r.rel_path}",
                "preview_url": f"/preview/{r.rel_path}",
                "download_url": f"/files/{r.rel_path}",
                "file_type": ext,
                "is_image": ext in _CHAT_IMAGE_EXTS,
                "source": source,
                "subject": "" if r.subject == "_general" else (r.subject or ""),
                "created_at": r.created_at,
                "timestamp": r.created_at,
                "owned": True,
                "exists": full_path.is_file(),
                "size": full_path.stat().st_size if full_path.is_file() else 0,
            }
        )
    return {"files": files}


@router.get("/api/admin/users/{user_id}/artifacts")
def admin_user_artifacts(user_id: str, user: UserRecord = Depends(get_current_user)):
    """Return all uploaded and generated artifacts belonging to a specific user."""
    from klyra.web.routes.files import collect_user_artifacts

    require_admin(user)
    target = repo.get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    return collect_user_artifacts(target, strict_owner_only=True)


@router.get("/api/admin/artifacts")
def admin_all_artifacts(user: UserRecord = Depends(get_current_user)):
    """Return all uploaded and generated artifacts across all users for the Admin Artifacts view."""
    from klyra.web.routes.files import collect_user_artifacts

    require_admin(user)
    all_users = repo.list_users()
    combined: list[dict] = []
    subjects_set: set[str] = set()
    users_summary: list[dict] = []

    for u in all_users:
        data = collect_user_artifacts(u, strict_owner_only=True)
        users_summary.append(
            {
                "id": u.id,
                "username": u.username,
                "full_name": u.full_name,
                "role": u.role,
                "uploaded_count": data.get("uploaded_count", 0),
                "generated_count": data.get("generated_count", 0),
            }
        )
        for subj in data.get("subjects") or []:
            if subj:
                subjects_set.add(subj)
        for item in data.get("artifacts") or []:
            enriched = dict(item)
            enriched["owner_id"] = u.id
            enriched["owner_username"] = u.username
            enriched["owner_full_name"] = u.full_name or ""
            combined.append(enriched)

    combined.sort(key=lambda a: a.get("timestamp") or "", reverse=True)
    uploaded_count = sum(1 for a in combined if a.get("source") == "uploaded")
    generated_count = sum(1 for a in combined if a.get("source") != "uploaded")
    return {
        "artifacts": combined,
        "users": users_summary,
        "subjects": sorted(subjects_set),
        "uploaded_count": uploaded_count,
        "generated_count": generated_count,
    }


class ReassignFileRequest(BaseModel):
    rel_path: str
    new_owner_id: str | None = None


@router.post("/api/admin/files/reassign")
def admin_reassign_file(
    req: ReassignFileRequest, user: UserRecord = Depends(get_current_user)
):
    require_admin(user)
    rel_path = req.rel_path.strip().lstrip("/")
    if not rel_path:
        raise HTTPException(status_code=400, detail="rel_path is required")
    parts = rel_path.split("/")
    subject = parts[0] if parts else ""
    if not req.new_owner_id:
        repo.clear_file_owner(rel_path)
        return {"status": "shared", "rel_path": rel_path, "owner_id": None}
    target_user = repo.get_user_by_id(req.new_owner_id)
    if target_user is None:
        raise HTTPException(status_code=404, detail="Target user not found")
    repo.reassign_file_owner(rel_path, req.new_owner_id, subject=subject)
    return {
        "status": "reassigned",
        "rel_path": rel_path,
        "owner_id": target_user.id,
        "owner_username": target_user.username,
    }


@router.delete("/api/admin/users/{user_id}/files/{rel_path:path}")
def admin_revoke_file(
    user_id: str, rel_path: str, user: UserRecord = Depends(get_current_user)
):
    require_admin(user)
    record_owner = repo.get_file_owner(rel_path)
    if record_owner is None:
        raise HTTPException(
            status_code=404, detail="No ownership record for that file"
        )
    if record_owner != user_id:
        raise HTTPException(
            status_code=400, detail="That file is not owned by the given user"
        )
    repo.clear_file_owner(rel_path)
    return {"status": "revoked", "rel_path": rel_path}
