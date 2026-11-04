"""Modular route subpackages for Klyra Web."""
from __future__ import annotations

from klyra.web.routes.files import router as files_router
from klyra.web.routes.sessions import router as sessions_router
from klyra.web.routes.ws_chat import router as ws_chat_router

__all__ = ["files_router", "sessions_router", "ws_chat_router"]

