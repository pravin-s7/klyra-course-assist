"""SQLAlchemy engine shared by all Klyra storage tables.

Mirrors services/job_store.py's DATABASE_URL handling so the web app
(sessions/users) and the API app (jobs) land on the same database in
production (Postgres via docker-compose) and the same local SQLite file
in development. One schema, one migration story.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import time

_engine = None
_SessionLocal = None
_engine_key: str | None = None


def _current_db_key() -> str:
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return f"url:{db_url}"
    db_path = Path(os.environ.get("KLYRA_DB", ".db/klyra.db")).resolve()
    return f"sqlite:{db_path}"


def _get_engine():
    global _engine, _SessionLocal, _engine_key
    key = _current_db_key()
    if _engine is None or _engine_key != key:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:
                pass
        db_url = os.environ.get("DATABASE_URL")
        if db_url:
            if db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://", "postgresql://", 1)
            _engine = create_engine(db_url, pool_size=10, max_overflow=20, pool_pre_ping=True)
        else:
            db_path = Path(os.environ.get("KLYRA_DB", ".db/klyra.db"))
            db_path.parent.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(
                f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
            )
        # expire_on_commit=False so repo helpers can return ORM objects
        # (e.g. UserRecord.id) after the session context closes.
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
        _engine_key = key
    return _SessionLocal


def init_db() -> None:
    """Create all tables (idempotent). Safe to call from any entry point."""
    from klyra.storage.models import Base  # local import avoids cycles

    Session = _get_engine()
    bind = Session.kw["bind"]

    # Wait briefly if PostgreSQL container is still starting up
    for attempt in range(15):
        try:
            Base.metadata.create_all(bind=bind)
            break
        except Exception:
            if attempt == 14:
                raise
            time.sleep(1.0)

    # Run idempotent schema migrations for pre-existing tables
    db_url = os.environ.get("DATABASE_URL", "")
    is_postgres = db_url.startswith("postgres")

    from sqlalchemy import text

    with Session() as session:
        # Full Name and Email
        for col in ["full_name", "email"]:
            try:
                session.execute(text(f"ALTER TABLE users ADD COLUMN {col} VARCHAR;"))
                session.commit()
            except Exception:
                session.rollback()

        # Modified At
        for table in ["users", "chat_sessions", "file_ownership"]:
            try:
                session.execute(text(f"ALTER TABLE {table} ADD COLUMN modified_at VARCHAR;"))
                session.commit()
            except Exception:
                session.rollback()

        # JSONB Migrations (Postgres only)
        if is_postgres:
            try:
                session.execute(text("ALTER TABLE users ALTER COLUMN courses TYPE JSONB USING courses::JSONB;"))
                session.commit()
            except Exception:
                session.rollback()
            try:
                session.execute(text("ALTER TABLE chat_sessions ALTER COLUMN ui_history_json TYPE JSONB USING ui_history_json::JSONB;"))
                session.commit()
            except Exception:
                session.rollback()
            try:
                session.execute(text("ALTER TABLE chat_sessions ALTER COLUMN gemini_history_json TYPE JSONB USING gemini_history_json::JSONB;"))
                session.commit()
            except Exception:
                session.rollback()


@contextmanager
def get_session_factory():
    """Yield a SQLAlchemy session with commit/rollback semantics."""
    Session = _get_engine()
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()