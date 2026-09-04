"""Shared storage layer for Klyra (users, chat sessions, file ownership).

Uses the same DATABASE_URL resolution as services/job_store.py:
- DATABASE_URL set (e.g. Postgres in docker-compose) → that DB
- otherwise → local SQLite at KLYRA_DB (default .db/klyra.db)
"""
from klyra.storage.db import get_session_factory, init_db  # noqa: F401
from klyra.storage.models import (  # noqa: F401
    ChatSessionRecord,
    FileOwnershipRecord,
    UserRecord,
)