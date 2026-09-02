"""Klyra Web Server — FastAPI with WebSocket for chat + voice.

Run with ``klyra web`` or ``uvicorn klyra.web.server:app``.
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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
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

app = FastAPI(title="Klyra Web", version="0.1.0")

_STATIC_DIR = Path(__file__).parent / "static"
_DOCS_DIR = Path.cwd() / "docs"
SESSIONS_DIR = Path.cwd() / ".db" / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


# ── Static file serving ────────────────────────────────────────────

@app.get("/")
async def index():
    """Serve the main chat UI."""
    html_path = _STATIC_DIR / "index.html"
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


@app.get("/files/{path:path}")
async def serve_file(path: str, preview: bool = False):
    """Serve generated files for download or preview."""
    file_path = (_DOCS_DIR / path).resolve()
    if not str(file_path).startswith(str(_DOCS_DIR.resolve())):
        return Response(status_code=403)
    if not file_path.exists() or not file_path.is_file():
        return Response(status_code=404)

    if preview:
        try:
            content = file_path.read_text(encoding="utf-8")
            return Response(content=content, media_type="text/plain; charset=utf-8")
        except Exception:
            return Response(status_code=400, content="Cannot preview binary file")

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )


@app.post("/api/upload/{subject}/{category}")
async def upload_files(subject: str, category: str, background_tasks: BackgroundTasks, files: list[UploadFile] = File(...)):
    """Upload files and trigger background ingestion."""
    from klyra.services.ingestion_service import run_ingestion
    from klyra.utils.fs import validate_subject

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
async def get_task_status(task_id: str):
    return background_tasks_state.get(task_id, {"status": "unknown"})

class DeleteRequest(BaseModel):
    type: str
    items: list[str]

@app.get("/api/subjects")
def get_subjects():
    """Return a list of available subjects in the docs directory."""
    subjects = []
    if _DOCS_DIR.exists():
        for p in _DOCS_DIR.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                subjects.append(p.name)
    return {"subjects": sorted(subjects)}

@app.get("/api/sessions")
def get_sessions():
    """Return a list of saved sessions."""
    sessions = []
    if SESSIONS_DIR.exists():
        for f in SESSIONS_DIR.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as file:
                    data = json.load(file)
                    sessions.append({
                        "id": data.get("session_id"),
                        "title": data.get("title", "New Chat"),
                        "created_at": data.get("created_at", 0),
                        "pinned": data.get("pinned", False)
                    })
            except Exception:
                pass
    # Sort by pinned (True first), then created_at descending
    sessions.sort(key=lambda x: (not x.get("pinned", False), -x["created_at"]))
    return {"sessions": sessions}

class SessionUpdateRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None

@app.put("/api/sessions/{session_id}")
def update_session(session_id: str, req: SessionUpdateRequest):
    """Update a saved session (rename or pin)."""
    p = SESSIONS_DIR / f"{session_id}.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as file:
            data = json.load(file)
        
        if req.title is not None:
            data["title"] = req.title
        if req.pinned is not None:
            data["pinned"] = req.pinned
            
        with open(p, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            
        return {"status": "success", "session": data}
    return Response(status_code=404, content="Session not found")

@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    """Delete a saved session."""
    p = SESSIONS_DIR / f"{session_id}.json"
    if p.exists():
        p.unlink()
    if session_id in ACTIVE_SESSIONS:
        del ACTIVE_SESSIONS[session_id]
    return {"status": "success"}

@app.get("/api/index/{subject}")
async def list_index(subject: str):
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
async def delete_index(subject: str, req: DeleteRequest, background_tasks: BackgroundTasks):
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
async def download_folder(subject: str, folder: str, background_tasks: BackgroundTasks):
    """Zip and download a folder, or download a specific file."""
    target_path = (_DOCS_DIR / subject / folder).resolve()
    
    # Security check to prevent directory traversal
    if not str(target_path).startswith(str(_DOCS_DIR.resolve())):
        return Response(status_code=403, content="Invalid path")
        
    if not target_path.exists():
        return Response(status_code=404, content="File or folder not found")

    if target_path.is_file():
        # If it's a file, just serve it directly
        return FileResponse(
            target_path, 
            filename=target_path.name,
            media_type="application/octet-stream"
        )

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

def _save_session_to_disk(sid: str, title: str, orchestrator, history: list):
    p = SESSIONS_DIR / f"{sid}.json"
    existing_data = {}
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        except Exception:
            pass

    # Preserve custom title if it was changed from "New Chat" and current title isn't explicitly passed as something else
    # Wait, the frontend will just let the backend decide title initially, then use PUT to rename.
    # We should only overwrite title if existing title is missing or "New Chat".
    final_title = existing_data.get("title", "New Chat")
    if final_title == "New Chat" and title != "New Chat":
        final_title = title
        
    data = {
        "session_id": sid,
        "title": final_title,
        "created_at": existing_data.get("created_at", time.time()),
        "pinned": existing_data.get("pinned", False),
        "ui_history": history,
        "gemini_history": orchestrator.to_dict()
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _load_session_from_disk(sid: str) -> dict | None:
    p = SESSIONS_DIR / f"{sid}.json"
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load session {sid}: {e}")
    return None

@app.websocket("/ws")
async def websocket_chat(ws: WebSocket, session_id: str = None):
    """Handle a single chat/voice session over WebSocket."""
    await ws.accept()
    logger.info("WebSocket connected")

    # Import here to avoid circular / heavy imports at module level
    from klyra.chat import ChatOrchestrator

    chat_history_for_voice: list[dict] = []
    voice_session_task = None
    voice_session_ref = {"session": None, "active": False}
    
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
            _save_session_to_disk(session_id, title, orchestrator, chat_history_for_voice)

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

    # Load from memory or disk
    loaded_from_disk = False
    if session_id:
        if session_id in ACTIVE_SESSIONS:
            session_data = ACTIVE_SESSIONS[session_id]
            orchestrator = session_data["orchestrator"]
            chat_history_for_voice = session_data["history"]
        else:
            disk_data = _load_session_from_disk(session_id)
            if disk_data:
                chat_history_for_voice = disk_data.get("ui_history", [])
                orchestrator = ChatOrchestrator(
                    on_text=on_text,
                    on_tool_start=on_tool_start,
                    on_tool_done=on_tool_done,
                    on_file_link=on_file_link,
                    on_tool_log=on_tool_log,
                )
                await orchestrator.initialize()
                orchestrator.load_from_dict(disk_data.get("gemini_history", {}))
                ACTIVE_SESSIONS[session_id] = {
                    "orchestrator": orchestrator,
                    "history": chat_history_for_voice
                }
                loaded_from_disk = True

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
                            _run_voice_session(ws, voice_session_ref, chat_history_for_voice, orchestrator, session_id)
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


async def _run_voice_session(ws: WebSocket, ref: dict, chat_history: list, orchestrator, session_id: str):
    """Run a Gemini Live API voice session, forwarding audio/events to the WebSocket."""
    from google import genai
    from google.genai import types
    from klyra.voice.tool_declarations import get_all_declarations
    from klyra.voice.tool_executor import ToolExecutor
    from klyra.core import SYSTEM_INSTRUCTION

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("KLYRA_VOICE_MODEL", "gemini-2.0-flash-exp")
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
    if orchestrator and hasattr(orchestrator, "_history") and orchestrator._history:
        history_lines = []
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
                    for fc in response.tool_call.function_calls:
                        # Append tool call to history so it's available if we switch back to text chat
                        orchestrator._history.append(
                            types.Content(role="model", parts=[types.Part(
                                function_call=types.FunctionCall(name=fc.name, args=fc.args)
                            )])
                        )

                        start_event = {"type": "tool_start", "tool": fc.name, "args": _sanitize(dict(fc.args))}
                        chat_history.append(start_event)
                        await safe_send_json(start_event)
                        
                        result = await executor.execute(fc.name, dict(fc.args))
                        status = result.get("status", "error")
                        msg = result.get("message", "")
                        
                        done_event = {"type": "tool_done", "tool": fc.name, "status": status, "message": msg[:500]}
                        chat_history.append(done_event)
                        await safe_send_json(done_event)
                        
                        # Append tool response to history so it's available if we switch back to text chat
                        orchestrator._history.append(
                            types.Content(role="user", parts=[types.Part(
                                function_response=types.FunctionResponse(name=fc.name, response=result)
                            )])
                        )

                    # Send tool responses back to Gemini
                    responses = [
                        types.FunctionResponse(name=fc.name, id=fc.id, response=result)
                        for fc in response.tool_call.function_calls
                    ]
                    await session.send_tool_response(function_responses=responses)

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
                                first_msg = chat_history[0][1]
                                title = (first_msg[:30] + '...') if len(first_msg) > 30 else first_msg
                            _save_session_to_disk(session_id, title, orchestrator, chat_history)

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
