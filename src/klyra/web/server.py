"""Klyra Web Server — FastAPI with WebSocket for chat + voice.

Run with ``klyra web`` or ``uvicorn klyra.web.server:app``.

RBAC (Phase 1):
- All /api/* routes and /ws require a logged-in user (session cookie).
- Chat sessions are owned by users: users see/edit/delete only their own
  chats; admins see all.
- Subject (course) gating: uploads, index listing/deletion, downloads, and
  /api/subjects are limited to the user's assigned courses (admin: all).
- Generated-file access (/files/..., /api/download/...) additionally checks
  the file-ownership registry for files under {subject}/generated/.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import socket
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, Response
from pydantic import BaseModel
from fastapi.background import BackgroundTasks

load_dotenv(Path.cwd() / ".env")

logger = logging.getLogger(__name__)

# Fix IPv6 websocket handshake timeout on some Linux/WSL systems
_old_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(*args, **kwargs):
    responses = _old_getaddrinfo(*args, **kwargs)
    return [r for r in responses if r[0] == socket.AF_INET] or responses
socket.getaddrinfo = _ipv4_only_getaddrinfo

from klyra.auth.bootstrap import ensure_bootstrap
from klyra.auth.dependencies import (
    can_access_subject,
    can_read_generated_file,
    get_current_user,
    websocket_user,
)
from klyra.auth.google_oauth import router as google_oauth_router
from klyra.auth.routes import router as auth_router
from klyra.storage import repo
from klyra.storage.models import UserRecord
from klyra.web.admin_routes import router as admin_router

# First-run: create tables, seed admin, migrate legacy JSON sessions.
_STATIC_DIR = Path(__file__).parent / "static"
_DOCS_DIR = Path.cwd() / "docs"
_LEGACY_SESSIONS_DIR = Path.cwd() / ".db" / "sessions"
ensure_bootstrap(_LEGACY_SESSIONS_DIR)

app = FastAPI(title="Klyra Web", version="0.1.0")
app.include_router(auth_router)
app.include_router(google_oauth_router)
app.include_router(admin_router)


# ── Static file serving ────────────────────────────────────────────

@app.get("/")
async def index():
    """Serve the main chat UI."""
    html_path = _STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/admin")
async def admin_page():
    """Serve the user-management admin panel (API itself enforces admin)."""
    html_path = _STATIC_DIR / "admin.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/static/{filename}")
async def static_file(filename: str):
    """Serve CSS/JS assets."""
    file_path = (_STATIC_DIR / filename).resolve()
    if not str(file_path).startswith(str(_STATIC_DIR.resolve())):
        return Response(status_code=403)
    if not file_path.exists():
        return Response(status_code=404)

    content_types = {
        ".css": "text/css",
        ".js": "application/javascript",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }
    ct = content_types.get(file_path.suffix, "application/octet-stream")
    return Response(content=file_path.read_bytes(), media_type=ct)


_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".tex": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
}


def _resolve_docs_file(path: str, user: UserRecord) -> tuple[Path | None, str]:
    """Resolve a path relative to _DOCS_DIR, handling 'assets/' vs '.assets/' and bare '.assets/'."""
    clean_path = path.lstrip("/")
    if clean_path.startswith("docs/"):
        clean_path = clean_path[len("docs/"):]

    candidates: list[str] = [clean_path]
    # Handle missing dot in .assets (e.g. 'ct/assets/generated/foo.png' -> 'ct/.assets/generated/foo.png')
    if "/assets/" in clean_path and "/.assets/" not in clean_path:
        candidates.append(clean_path.replace("/assets/", "/.assets/", 1))

    # Handle bare '.assets/...' or 'assets/...' without subject prefix
    if clean_path.startswith(".assets/") or clean_path.startswith("assets/"):
        rest = clean_path.split("/", 1)[1] if "/" in clean_path else ""
        if rest and _DOCS_DIR.exists():
            for subj_dir in sorted(_DOCS_DIR.iterdir()):
                if subj_dir.is_dir() and not subj_dir.name.startswith("."):
                    if can_access_subject(user, subj_dir.name):
                        candidates.append(f"{subj_dir.name}/.assets/{rest}")

    docs_root = _DOCS_DIR.resolve()
    for cand in candidates:
        fp = (_DOCS_DIR / cand).resolve()
        if str(fp).startswith(str(docs_root)) and fp.exists() and fp.is_file():
            rel = fp.relative_to(docs_root).as_posix()
            return fp, rel

    # Fallback: return initial resolved path for standard 403/404 handling
    fp = (_DOCS_DIR / clean_path).resolve()
    return fp, clean_path


@app.get("/.assets/{rest:path}")
@app.get("/assets/{rest:path}")
async def serve_bare_asset(rest: str, user: UserRecord = Depends(get_current_user)):
    """Fallback route when Markdown contains relative '../.assets/...' links resolved against '/'."""
    return await serve_file(f".assets/{rest}", preview=False, download=False, user=user)


@app.get("/files/{path:path}")
async def serve_file(
    path: str,
    preview: bool = False,
    download: bool = False,
    user: UserRecord = Depends(get_current_user),
):
    """Serve generated files or assets for inline display, preview, or download (owner/course gated)."""
    file_path, rel_path = _resolve_docs_file(path, user)
    if file_path is None or not str(file_path).startswith(str(_DOCS_DIR.resolve())):
        return Response(status_code=403)

    # Access check BEFORE existence so we don't leak whether another
    # user's generated file exists.
    if not can_read_generated_file(user, rel_path):
        return Response(status_code=403, content="You do not have access to this file")

    if not file_path.exists() or not file_path.is_file():
        return Response(status_code=404)

    suffix = file_path.suffix.lower()
    is_image = suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
    media_type = _MIME_TYPES.get(suffix, "application/octet-stream")

    if preview and not is_image:
        try:
            content = file_path.read_text(encoding="utf-8")
            return Response(content=content, media_type="text/plain; charset=utf-8")
        except Exception:
            return Response(status_code=400, content="Cannot preview binary file")

    if download:
        return FileResponse(
            path=str(file_path),
            filename=file_path.name,
            media_type="application/octet-stream",
        )

    # Serve images and inline assets without forcing Content-Disposition: attachment
    return FileResponse(
        path=str(file_path),
        media_type=media_type,
    )


@app.post("/api/upload/{subject}/{category}")
async def upload_files(
    subject: str,
    category: str,
    background_tasks: BackgroundTasks,
    user: UserRecord = Depends(get_current_user),
    files: list[UploadFile] = File(...),
):
    """Upload files and trigger background ingestion (course-gated)."""
    from klyra.services.ingestion_service import run_ingestion
    from klyra.utils.fs import validate_subject

    if not can_access_subject(user, subject):
        return Response(status_code=403, content="You are not assigned to this course")

    try:
        subject = validate_subject(subject)
    except ValueError as e:
        return Response(status_code=400, content=str(e))

    # Map category to actual directory path inside subject
    cat_path_map = {
        "pyq": "pyq",
        "transcripts": "transcripts",
        "additional": "additional",
    }

    dir_name = cat_path_map.get(category, category) # handles week_X
    target_dir = _DOCS_DIR / subject / dir_name
    target_dir.mkdir(parents=True, exist_ok=True)

    task_id = str(uuid.uuid4())
    background_tasks_state[task_id] = {"status": "running", "progress": 0, "message": "Starting ingestion...", "logs": ""}

    files_copied = []
    try:
        for upload_file in files:
            if upload_file.filename:
                file_path = target_dir / upload_file.filename
                with open(file_path, "wb") as buffer:
                    shutil.copyfileobj(upload_file.file, buffer)
                files_copied.append(file_path)
    except Exception as e:
        return Response(status_code=500, content=f"Upload failed: {e}")

    # Trigger indexing in background
    def ingest_background():
        with background_logger(task_id, background_tasks_state):
            try:
                # We pass the category as a list. If it's a week, we extract the number.
                kwargs = {}
                if category.startswith("week_"):
                    kwargs["week"] = int(category.split("_")[1])
                else:
                    kwargs["categories"] = [category]
                run_ingestion(subject, **kwargs)
                background_tasks_state[task_id] = {"status": "completed", "progress": 100, "message": "Indexing complete", "logs": background_tasks_state[task_id].get("logs", "")}
            except Exception as e:
                logger.error(f"Ingestion failed for {subject}/{category}: {e}")
                background_tasks_state[task_id] = {"status": "error", "progress": 0, "message": f"Ingestion failed: {e}", "logs": background_tasks_state[task_id].get("logs", "")}

                # Rollback uploaded files
                print(f"Rolling back {len(files_copied)} physical files due to ingestion error...")
                for f in files_copied:
                    try:
                        f.unlink(missing_ok=True)
                    except Exception as ex:
                        print(f"Failed to delete {f} during rollback: {ex}")

                # Cleanup empty parent dir
                if not any(target_dir.iterdir()):
                    try:
                        target_dir.rmdir()
                    except:
                        pass

    background_tasks.add_task(ingest_background)

    return {"message": "Files uploaded successfully. Indexing started.", "task_id": task_id}


import uuid
import io
import sys
import contextlib
import logging

background_tasks_state = {}

class BackgroundLogCapture(io.StringIO):
    def __init__(self, task_id: str, state_dict: dict):
        super().__init__()
        self.task_id = task_id
        self.state_dict = state_dict
        if "logs" not in self.state_dict[self.task_id]:
            self.state_dict[self.task_id]["logs"] = ""

    def write(self, string: str):
        sys.__stdout__.write(string)
        sys.__stdout__.flush()
        self.state_dict[self.task_id]["logs"] += string

@contextlib.contextmanager
def background_logger(task_id: str, state_dict: dict):
    capture = BackgroundLogCapture(task_id, state_dict)
    log_handler = logging.StreamHandler(capture)
    log_handler.setLevel(logging.INFO)
    log_handler.setFormatter(logging.Formatter('%(message)s'))
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)
    try:
        with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            yield
    finally:
        root_logger.removeHandler(log_handler)

@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str, user: UserRecord = Depends(get_current_user)):
    return background_tasks_state.get(task_id, {"status": "unknown"})

class DeleteRequest(BaseModel):
    type: str
    items: list[str]

@app.get("/api/subjects")
def get_subjects(user: UserRecord = Depends(get_current_user)):
    """Return the subjects the user may work with (admin: all on disk)."""
    if user.role == "admin":
        subjects = []
        if _DOCS_DIR.exists():
            for p in _DOCS_DIR.iterdir():
                if p.is_dir() and not p.name.startswith("."):
                    subjects.append(p.name)
        return {"subjects": sorted(subjects)}
    return {"subjects": sorted(repo.user_courses(user))}

@app.get("/api/sessions")
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
    # Pinned first, then newest first (created_at is an ISO-8601 string,
    # so lexicographic ordering == chronological ordering). Two stable
    # sorts: newest-first, then pinned-first.
    sessions.sort(key=lambda x: x["created_at"], reverse=True)
    sessions.sort(key=lambda x: x.get("pinned", False), reverse=True)
    return {"sessions": sessions}

class SessionUpdateRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None

def _own_session_or_404(session_id: str, user: UserRecord):
    record = repo.get_chat_session(session_id)
    if record is None:
        return None, Response(status_code=404, content="Session not found")
    if record.owner_id != user.id and user.role != "admin":
        return None, Response(status_code=403, content="You do not own this chat")
    return record, None

@app.put("/api/sessions/{session_id}")
def update_session(session_id: str, req: SessionUpdateRequest, user: UserRecord = Depends(get_current_user)):
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
        } if updated else {"id": session_id},
    }

@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, user: UserRecord = Depends(get_current_user)):
    """Delete one of the user's sessions."""
    record, err = _own_session_or_404(session_id, user)
    if err is not None:
        return err
    repo.delete_chat_session(session_id)
    if session_id in ACTIVE_SESSIONS:
        del ACTIVE_SESSIONS[session_id]
    return {"status": "success"}

@app.get("/api/index/{subject}")
async def list_index(subject: str, user: UserRecord = Depends(get_current_user)):
    if not can_access_subject(user, subject):
        return Response(status_code=403, content="You are not assigned to this course")
    from klyra.utils.fs import discover_subject_files
    try:
        sources = discover_subject_files(subject)
    except FileNotFoundError:
        return {"folders": [], "files": []}

    folders = set()
    files = []

    source_dir = Path("docs") / subject
    for s in sources:
        files.append(str(s.path.resolve()))
        try:
            rel = s.path.relative_to(source_dir)
            if len(rel.parts) > 1:
                folders.add(rel.parts[0])
        except ValueError as e:
            logger.error(f"ValueError relative_to. s.path={s.path}, source_dir={source_dir}")

    return {"folders": sorted(list(folders)), "files": sorted(files)}

@app.post("/api/index/{subject}/delete")
async def delete_index(
    subject: str,
    req: DeleteRequest,
    background_tasks: BackgroundTasks,
    user: UserRecord = Depends(get_current_user),
):
    if not can_access_subject(user, subject):
        return Response(status_code=403, content="You are not assigned to this course")
    from klyra.utils.fs import discover_subject_files
    from klyra.services.ingestion_service import run_ingestion

    files_to_delete = []
    if req.type == "folders":
        try:
            sources = discover_subject_files(subject)
            source_dir = Path("docs") / subject
            for s in sources:
                try:
                    rel = s.path.relative_to(source_dir)
                    if len(rel.parts) > 0 and rel.parts[0] in req.items:
                        files_to_delete.append(str(s.path.resolve()))
                except ValueError:
                    pass
        except FileNotFoundError:
            pass
    else:
        files_to_delete = req.items

    if not files_to_delete:
        return {"message": "No files found to delete"}

    task_id = str(uuid.uuid4())
    total_files = len(files_to_delete)
    background_tasks_state[task_id] = {"status": "running", "progress": 0, "message": "Starting deletion...", "logs": ""}

    def delete_background():
        with background_logger(task_id, background_tasks_state):
            for i, file_path in enumerate(files_to_delete):
                try:
                    run_ingestion(subject, delete_file=file_path)

                    # Only delete physically if index deletion succeeds
                    try:
                        p = Path(file_path)
                        if p.exists() and str(p.resolve()).startswith(str(_DOCS_DIR.resolve())):
                            p.unlink()
                            logger.info(f"Deleted physical file {file_path}")
                            parent = p.parent
                            # Clean up empty parent directories up to subject dir
                            subject_dir_resolved = (_DOCS_DIR / subject).resolve()
                            while parent != subject_dir_resolved:
                                if not any(parent.iterdir()):
                                    parent.rmdir()
                                    parent = parent.parent
                                else:
                                    break
                    except Exception as e:
                        logger.error(f"Failed to delete physical file {file_path}: {e}")

                except Exception as e:
                    logger.error(f"Failed to delete {file_path} from index: {e}")

                # Update progress
                pct = int(((i + 1) / total_files) * 100)
                background_tasks_state[task_id] = {"status": "running", "progress": pct, "message": f"Deleting {i+1}/{total_files}...", "logs": background_tasks_state[task_id].get("logs", "")}

            background_tasks_state[task_id] = {"status": "completed", "progress": 100, "message": "Deletion complete", "logs": background_tasks_state[task_id].get("logs", "")}

    background_tasks.add_task(delete_background)
    return {"message": f"Started deletion of {len(files_to_delete)} files", "task_id": task_id}

@app.get("/api/download/{subject}/{folder:path}")
async def download_folder(
    subject: str,
    folder: str,
    background_tasks: BackgroundTasks,
    user: UserRecord = Depends(get_current_user),
):
    """Zip and download a folder, or download a specific file (course + ownership gated)."""
    if not can_access_subject(user, subject):
        return Response(status_code=403, content="You are not assigned to this course")

    target_path = (_DOCS_DIR / subject / folder).resolve()

    # Security check to prevent directory traversal
    if not str(target_path).startswith(str(_DOCS_DIR.resolve())):
        return Response(status_code=403, content="Invalid path")

    if not target_path.exists():
        return Response(status_code=404, content="File or folder not found")

    if target_path.is_file():
        rel_path = target_path.relative_to(_DOCS_DIR).as_posix()
        if not can_read_generated_file(user, rel_path):
            return Response(status_code=403, content="You do not have access to this file")
        return FileResponse(
            target_path,
            filename=target_path.name,
            media_type="application/octet-stream"
        )

    # Zipping a folder: block if it contains generated files the user can't read
    rel_target = target_path.relative_to(_DOCS_DIR).as_posix()
    generated_root = (_DOCS_DIR / subject / "generated").resolve()
    if str(generated_root) in str(target_path) and user.role != "admin":
        for f in target_path.rglob("*"):
            if f.is_file():
                rel = f.relative_to(_DOCS_DIR).as_posix()
                if not can_read_generated_file(user, rel):
                    return Response(status_code=403, content="Folder contains files you cannot access")

    # It's a directory, so zip it
    fd, temp_zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)

    # Clean up the .zip string because make_archive appends .zip automatically
    base_name = temp_zip_path[:-4]
    shutil.make_archive(base_name, 'zip', target_path)

    def cleanup():
        try:
            os.remove(temp_zip_path)
        except OSError:
            pass

    background_tasks.add_task(cleanup)

    return FileResponse(
        path=temp_zip_path,
        filename=f"{subject}_{folder}.zip",
        media_type="application/zip"
    )


# ── WebSocket chat handler ─────────────────────────────────────────

ACTIVE_SESSIONS: dict[str, dict] = {}

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
        ui_hist = json.loads(raw_ui) if isinstance(raw_ui, str) else (raw_ui if isinstance(raw_ui, list) else [])
        raw_gem = record.gemini_history_json
        gem_hist = json.loads(raw_gem) if isinstance(raw_gem, str) else (raw_gem if isinstance(raw_gem, dict) else {})
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

@app.websocket("/ws")
async def websocket_chat(ws: WebSocket, session_id: str = None):
    """Handle a single chat/voice session over WebSocket (auth required)."""
    user = websocket_user(ws)
    if user is None:
        await ws.close(code=4401)
        return

    await ws.accept()
    logger.info("WebSocket connected (user=%s)", user.username)

    # Import here to avoid circular / heavy imports at module level
    from klyra.chat import ChatOrchestrator

    # Ownership check: existing session must belong to this user (admin exempt)
    if session_id:
        existing = repo.get_chat_session(session_id)
        if existing is not None and existing.owner_id != user.id and user.role != "admin":
            await ws.close(code=4403)
            return

    chat_history_for_voice: list[dict] = []
    voice_session_task = None
    voice_session_ref = {"session": None, "active": False}
    allowed_subjects = (
        None if user.role == "admin" else set(repo.user_courses(user))
    )

    def _sync_save():
        if session_id:
            title = "New Chat"
            if len(chat_history_for_voice) > 0:
                for e in chat_history_for_voice:
                    if isinstance(e, dict) and e.get("type") == "user_transcript":
                        title = e.get("text", "")
                        break
                    elif isinstance(e, list) or isinstance(e, tuple):
                        title = e[1]
                        break
            title = (title[:30] + '...') if len(title) > 30 else title
            _save_session_to_db(session_id, user.id, title, orchestrator, chat_history_for_voice)

    # ── Chat orchestrator callbacks ──

    def _sanitize(d): return d
    async def on_text(text: str):
        event = {"type": "klyra_text", "text": text}
        chat_history_for_voice.append(event)
        await ws.send_json(event)

    async def on_tool_start(name: str, args: dict):
        event = {"type": "tool_start", "tool": name, "args": _sanitize(args)}
        chat_history_for_voice.append(event)
        await ws.send_json(event)
        _sync_save()

    async def on_tool_done(name: str, status: str, message: str):
        event = {"type": "tool_done", "tool": name, "status": status, "message": message[:500]}
        chat_history_for_voice.append(event)
        await ws.send_json(event)
        _sync_save()

    async def on_file_link(name: str, url: str, file_type: str):
        event = {"type": "file_link", "name": name, "url": url, "file_type": file_type}
        chat_history_for_voice.append(event)
        await ws.send_json(event)
        _sync_save()

    async def on_tool_log(name: str, text: str):
        try:
            await ws.send_json({"type": "tool_log", "tool": name, "text": text})
        except Exception as e:
            logger.debug(f"Failed to send tool log: {e}")

    # Load from memory or DB
    if session_id:
        if session_id in ACTIVE_SESSIONS:
            session_data = ACTIVE_SESSIONS[session_id]
            orchestrator = session_data["orchestrator"]
            chat_history_for_voice = session_data["history"]
        else:
            db_data = _load_session_from_db(session_id)
            if db_data:
                chat_history_for_voice = db_data.get("ui_history", [])
                orchestrator = ChatOrchestrator(
                    on_text=on_text,
                    on_tool_start=on_tool_start,
                    on_tool_done=on_tool_done,
                    on_file_link=on_file_link,
                    on_tool_log=on_tool_log,
                    allowed_subjects=allowed_subjects,
                    user_id=user.id,
                )
                await orchestrator.initialize()
                orchestrator.load_from_dict(db_data.get("gemini_history", {}))
                ACTIVE_SESSIONS[session_id] = {
                    "orchestrator": orchestrator,
                    "history": chat_history_for_voice
                }

    if session_id and session_id in ACTIVE_SESSIONS:
        # Reattach callbacks
        orchestrator._on_text = on_text
        orchestrator._on_tool_start = on_tool_start
        orchestrator._on_tool_done = on_tool_done
        orchestrator._on_file_link = on_file_link
        orchestrator._on_tool_log = on_tool_log
        if orchestrator._executor:
            orchestrator._executor.on_log_callback = on_tool_log

        # Rehydrate client UI
        for event in chat_history_for_voice:
            # Handle legacy string tuples (if old sessions exist)
            if isinstance(event, list) or isinstance(event, tuple):
                role, text = event
                if role == "user":
                    await ws.send_json({"type": "user_transcript", "text": text, "final": True})
                elif role == "model":
                    await ws.send_json({"type": "klyra_text", "text": text})
                    await ws.send_json({"type": "turn_complete"})
            else:
                # Replay exactly the saved websocket event
                await ws.send_json(event)
    else:
        orchestrator = ChatOrchestrator(
            on_text=on_text,
            on_tool_start=on_tool_start,
            on_tool_done=on_tool_done,
            on_file_link=on_file_link,
            on_tool_log=on_tool_log,
            allowed_subjects=allowed_subjects,
            user_id=user.id,
        )
        await orchestrator.initialize()
        if session_id:
            ACTIVE_SESSIONS[session_id] = {
                "orchestrator": orchestrator,
                "history": chat_history_for_voice
            }
    text_session_task = None

    try:
        while True:
            raw = await ws.receive()

            # Binary frame = voice audio
            if "bytes" in raw and raw["bytes"]:
                audio_data = raw["bytes"]
                if voice_session_ref["active"] and voice_session_ref["session"]:
                    try:
                        # Log length periodically and check volume
                        if not hasattr(ws, "_audio_bytes"):
                            ws._audio_bytes = 0
                            ws._audio_sum = 0
                        ws._audio_bytes += len(audio_data)
                        # quick and dirty sum of absolute 16-bit values
                        import struct
                        shorts = struct.unpack(f"<{len(audio_data)//2}h", audio_data)
                        ws._audio_sum += sum(abs(x) for x in shorts)
                        
                        if ws._audio_bytes > 16000 * 2: # log every 1 second of audio
                            avg = ws._audio_sum / (ws._audio_bytes / 2)
                            logger.info(f"Received {ws._audio_bytes} bytes from web. Avg Volume: {avg:.2f}")
                            if avg < 5.0:
                                logger.warning("Web audio is SILENT (mic not picking up sound or browser not sending it)")
                            ws._audio_bytes = 0
                            ws._audio_sum = 0

                        await voice_session_ref["session"].send_realtime_input(
                            audio={"data": audio_data, "mime_type": "audio/pcm;rate=16000"}
                        )
                    except Exception as e:
                        logger.debug("Failed to send audio: %s", e)
                continue

            # JSON text frame
            if "text" in raw and raw["text"]:
                try:
                    msg = json.loads(raw["text"])
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "chat":
                    # Text chat message
                    user_text = msg.get("text", "").strip()
                    if not user_text:
                        continue

                    # If voice is active, stop it first
                    if voice_session_ref["active"]:
                        await _stop_voice(voice_session_task, voice_session_ref)

                    # Append user message to history first to maintain chronological order
                    user_event = {"type": "user_transcript", "text": user_text, "final": True}
                    chat_history_for_voice.append(user_event)
                    _sync_save()

                    async def process_chat(text):
                        try:
                            response = await orchestrator.send_message(text)
                        except asyncio.CancelledError:
                            logger.info("Chat task cancelled by user")
                            await ws.send_json({"type": "tool_log", "tool": "System", "text": "Generation stopped by user."})
                        except Exception as e:
                            logger.error("Error in chat task: %s", e)
                        finally:
                            chat_history_for_voice.append({"type": "turn_complete"})
                            try:
                                await ws.send_json({"type": "turn_complete"})
                            except Exception:
                                pass
                            _sync_save()

                    text_session_task = asyncio.create_task(process_chat(user_text))

                elif msg_type == "chat_stop":
                    if text_session_task and not text_session_task.done():
                        text_session_task.cancel()
                        text_session_task = None

                elif msg_type == "voice_start":
                    # Start voice session
                    if not voice_session_ref["active"]:
                        voice_session_task = asyncio.create_task(
                            _run_voice_session(ws, voice_session_ref, chat_history_for_voice, orchestrator, session_id, user, _sync_save, allowed_subjects)
                        )

                elif msg_type == "voice_stop":
                    # Stop voice session
                    await _stop_voice(voice_session_task, voice_session_ref)
                    voice_session_task = None

                elif msg_type == "clear_chat":
                    # Clear chat history for a new session
                    orchestrator._history.clear()
                    chat_history_for_voice.clear()
                    if session_id and session_id in ACTIVE_SESSIONS:
                        del ACTIVE_SESSIONS[session_id]
                    if voice_session_ref["active"]:
                        await _stop_voice(voice_session_task, voice_session_ref)
                        voice_session_task = None

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except RuntimeError as e:
        if "disconnect message has been received" in str(e):
            logger.info("WebSocket disconnected (RuntimeError)")
        else:
            logger.error("WebSocket error: %s", e, exc_info=True)
    except Exception as e:
        logger.error("WebSocket error: %s", e, exc_info=True)
    finally:
        await _stop_voice(voice_session_task, voice_session_ref)


async def _run_voice_session(ws: WebSocket, ref: dict, chat_history: list, orchestrator, session_id: str, user, sync_save, allowed_subjects: set | None = None):
    """Run a Gemini Live API voice session, forwarding audio/events to the WebSocket."""
    from google import genai
    from google.genai import types
    from klyra.voice.tool_declarations import get_all_declarations
    from klyra.voice.tool_executor import ToolExecutor
    from klyra.core import SYSTEM_INSTRUCTION

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("KLYRA_VOICE_MODEL", "gemini-3.8-live")
    voice_name = os.environ.get("KLYRA_VOICE_NAME", "Zephyr")
    client = genai.Client(api_key=api_key)

    ws_lock = asyncio.Lock()

    async def safe_send_json(data):
        async with ws_lock:
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def safe_send_bytes(data):
        async with ws_lock:
            try:
                await ws.send_bytes(data)
            except Exception:
                pass

    async def on_tool_log(name: str, text: str):
        # We don't save raw logs to history to save space
        await safe_send_json({"type": "tool_log", "tool": name, "text": text})

    executor = ToolExecutor(on_log_callback=on_tool_log)

    # Build system instruction with full conversation and tool history
    sys_inst = SYSTEM_INSTRUCTION
    history_lines: list[str] = []
    if orchestrator and hasattr(orchestrator, "_history") and orchestrator._history:
        # Take the last 20 messages to ensure tool context is captured
        for msg in orchestrator._history[-20:]:
            if not hasattr(msg, "role") or not hasattr(msg, "parts"):
                continue
            role = msg.role.upper()
            for part in msg.parts:
                if part.text:
                    history_lines.append(f"{role}: {part.text}")
                elif getattr(part, "function_call", None):
                    history_lines.append(f"{role} (TOOL CALL): {part.function_call.name}")
                elif getattr(part, "function_response", None):
                    # Keep tool responses concise but present
                    resp_str = str(part.function_response.response)
                    if len(resp_str) > 1500:
                        resp_str = resp_str[:1500] + "... (truncated)"
                    history_lines.append(f"{role} (TOOL RESULT for {part.function_response.name}): {resp_str}")

    if history_lines:
        history_text = "\n".join(history_lines)
        sys_inst += f"\n\n### RECENT CONVERSATION & TOOL HISTORY (for context):\n{history_text}"

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
        system_instruction=types.Content(parts=[types.Part(text=sys_inst)]),
        tools=[
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(**decl) for decl in get_all_declarations()
                ]
            )
        ],
    )
    loop = asyncio.get_running_loop()
    if not hasattr(loop, "_ipv4_patched"):
        orig_getaddrinfo = loop.getaddrinfo
        async def _ipv4_getaddrinfo(*args, **kwargs):
            import socket
            res = await orig_getaddrinfo(*args, **kwargs)
            return [r for r in res if r[0] == socket.AF_INET] or res
        loop.getaddrinfo = _ipv4_getaddrinfo
        loop._ipv4_patched = True

    try:
        async with client.aio.live.connect(model=model, config=config) as session:
            ref["session"] = session
            ref["active"] = True
            await safe_send_json({"type": "voice_started"})

            current_model_text = ""

            async for response in session.receive():
                # Tool calls
                if response.tool_call and response.tool_call.function_calls:
                    tool_responses = []
                    for fc in response.tool_call.function_calls:
                        # Enforce subject RBAC: reject tool calls on unassigned subjects
                        fc_args = dict(fc.args) if fc.args else {}
                        if allowed_subjects is not None:
                            subj = str(fc_args.get("subject", "")).lower()
                            if subj and subj not in allowed_subjects:
                                deny = {
                                    "status": "error",
                                    "message": f"Access denied: you are not assigned to the course '{subj}'.",
                                    "tool": fc.name,
                                }
                                tool_responses.append(
                                    types.FunctionResponse(name=fc.name, id=fc.id, response=deny)
                                )
                                await safe_send_json({"type": "tool_done", "tool": fc.name, "status": "error", "message": deny["message"][:500]})
                                continue

                        # Append tool call to history so it's available if we switch back to text chat
                        orchestrator._history.append(
                            types.Content(role="model", parts=[types.Part(
                                function_call=types.FunctionCall(name=fc.name, args=fc_args)
                            )])
                        )

                        start_event = {"type": "tool_start", "tool": fc.name, "args": _sanitize(fc_args)}
                        chat_history.append(start_event)
                        await safe_send_json(start_event)

                        tool_start_time = time.time()
                        result = await executor.execute(fc.name, fc_args)
                        status = result.get("status", "error")
                        msg = result.get("message", "")

                        done_event = {"type": "tool_done", "tool": fc.name, "status": status, "message": msg[:500]}
                        chat_history.append(done_event)
                        await safe_send_json(done_event)

                        # Register ownership and emit file links (including generated diagrams)
                        if status == "success" and orchestrator is not None:
                            try:
                                await orchestrator._detect_files(
                                    fc.name, fc_args, msg, since_time=tool_start_time
                                )
                            except Exception as e:
                                logger.debug("file detection/ownership tagging failed: %s", e)

                        # Append tool response to history so it's available if we switch back to text chat
                        orchestrator._history.append(
                            types.Content(role="user", parts=[types.Part(
                                function_response=types.FunctionResponse(name=fc.name, response=result)
                            )])
                        )
                        tool_responses.append(
                            types.FunctionResponse(name=fc.name, id=fc.id, response=result)
                        )

                    # Send tool responses back to Gemini
                    if tool_responses:
                        await session.send_tool_response(function_responses=tool_responses)

                # Audio + transcriptions
                if response.server_content is not None:
                    sc = response.server_content

                    if sc.input_transcription:
                        text = sc.input_transcription.text
                        user_event = {"type": "user_transcript", "text": text, "final": True}
                        await safe_send_json(user_event)
                        chat_history.append(user_event)
                        orchestrator._history.append(types.Content(role="user", parts=[types.Part(text=text)]))

                    if sc.output_transcription:
                        text = sc.output_transcription.text
                        await safe_send_json({"type": "klyra_transcript", "text": text})
                        current_model_text += text

                    if sc.model_turn is not None:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                await safe_send_bytes(part.inline_data.data)

                    if sc.turn_complete:
                        if current_model_text:
                            model_event = {"type": "klyra_text", "text": current_model_text}
                            chat_history.append(model_event)
                            orchestrator._history.append(types.Content(role="model", parts=[types.Part(text=current_model_text)]))
                            current_model_text = ""

                        turn_event = {"type": "turn_complete"}
                        chat_history.append(turn_event)
                        await safe_send_json(turn_event)

                        if session_id:
                            title = "New Chat"
                            if len(chat_history) > 0:
                                for e in chat_history:
                                    if isinstance(e, dict) and e.get("type") == "user_transcript":
                                        title = e.get("text", "")
                                        break
                                    elif isinstance(e, list) or isinstance(e, tuple):
                                        title = e[1]
                                        break
                            title = (title[:30] + '...') if len(title) > 30 else title
                            sync_save()

    except asyncio.CancelledError:
        pass
    except Exception as e:
        e_str = str(e).lower()
        # Suppress harmless normal closures or unhelpful generic exceptions from the SDK on disconnect
        if e_str == "1" or "close" in e_str or "disconnect" in e_str or "cancelled" in e_str:
            pass
        else:
            logger.error("Voice session error: %r", e)
            try:
                await ws.send_json({"type": "voice_error", "message": str(e)})
            except Exception:
                pass
    finally:
        ref["session"] = None
        ref["active"] = False
        try:
            await ws.send_json({"type": "voice_stopped"})
        except Exception:
            pass


async def _stop_voice(task, ref):
    """Cancel a running voice session task."""
    ref["active"] = False
    ref["session"] = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def _sanitize(args: dict) -> dict:
    """Remove confirmation tokens and limit arg display size."""
    clean = {k: v for k, v in args.items() if k != "confirmation_token"}
    return clean