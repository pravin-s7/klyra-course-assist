"""Klyra Web Server — FastAPI entrypoint wiring static UI, auth, modular web routes,
WebSocket chat/voice, and the `/api/v1` Celery-backed REST endpoints."""
from __future__ import annotations

import logging
import socket
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

load_dotenv(Path.cwd() / ".env")

logger = logging.getLogger(__name__)

# Fix IPv6 websocket handshake timeout on some Linux/WSL systems
_old_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(*args, **kwargs):
    responses = _old_getaddrinfo(*args, **kwargs)
    return [r for r in responses if r[0] == socket.AF_INET] or responses


socket.getaddrinfo = _ipv4_only_getaddrinfo

from klyra.api.dependencies import limiter  # noqa: E402
from klyra.api.routes import (  # noqa: E402
    coding_problem as api_coding_problem,
    conversion as api_conversion,
    files as api_files,
    generation as api_generation,
    ingestion as api_ingestion,
    jobs as api_jobs,
    revision as api_revision,
    teaching_material as api_teaching_material,
    transcript as api_transcript,
)
from klyra.auth.bootstrap import ensure_bootstrap  # noqa: E402
from klyra.auth.google_oauth import router as google_oauth_router  # noqa: E402
from klyra.auth.routes import router as auth_router  # noqa: E402
from klyra.models.responses import HealthResponse  # noqa: E402
from klyra.web.admin_routes import router as admin_router  # noqa: E402
from klyra.web.routes import files_router, sessions_router, ws_chat_router  # noqa: E402
from klyra.web.routes.files import (  # noqa: E402
    BackgroundLogCapture,
    DeleteRequest,
    _docs_dir,
    _resolve_docs_file,
    _resolve_download_target,
    background_logger,
    background_tasks_state,
    delete_index,
    download_folder,
    get_subjects,
    get_task_status,
    list_index,
    serve_bare_asset,
    serve_file,
    upload_files,
)
from klyra.web.routes.sessions import (  # noqa: E402
    ACTIVE_SESSIONS,
    SessionUpdateRequest,
    _load_session_from_db,
    _own_session_or_404,
    _save_session_to_db,
    delete_session,
    get_sessions,
    update_session,
)
from klyra.web.routes.ws_chat import websocket_chat  # noqa: E402
from klyra.web.routes.ws_voice import (  # noqa: E402
    _run_voice_session,
    _sanitize,
    _stop_voice,
)

_STATIC_DIR = Path(__file__).parent / "static"
_DOCS_DIR = Path.cwd() / "docs"
_LEGACY_SESSIONS_DIR = Path.cwd() / ".db" / "sessions"
ensure_bootstrap(_LEGACY_SESSIONS_DIR)

app = FastAPI(title="Klyra Web", version="0.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Auth & Admin routers
app.include_router(auth_router)
app.include_router(google_oauth_router)
app.include_router(admin_router)

# Web UI & WebSocket routers
app.include_router(files_router)
app.include_router(sessions_router)
app.include_router(ws_chat_router)

# Unified REST v1 API routers (Celery / background job API)
app.include_router(api_files.router, prefix="/api/v1")
app.include_router(api_ingestion.router, prefix="/api/v1")
app.include_router(api_generation.router, prefix="/api/v1")
app.include_router(api_revision.router, prefix="/api/v1")
app.include_router(api_conversion.router, prefix="/api/v1")
app.include_router(api_teaching_material.router, prefix="/api/v1")
app.include_router(api_coding_problem.router, prefix="/api/v1")
app.include_router(api_transcript.router, prefix="/api/v1")
app.include_router(api_jobs.router, prefix="/api/v1")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


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
