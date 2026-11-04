"""Persistent store for background job records.

Backed by SQLite (not an in-memory dict) so a job's *record* - queued,
running, done, failed, plus its result/error - survives a process restart.
See PRODUCTION.md #2: a real multi-worker deployment still needs a real
queue (Celery/RQ + Redis) so an in-flight job itself resumes after a crash;
this only guarantees the *bookkeeping* survives, which is enough for a
single-instance deployment using FastAPI's BackgroundTasks.

Every route/service goes through this module, so swapping the backend later
(e.g. for Postgres, or wrapping a real queue) is a change here, not at every
call site - same principle as `vector_store.get_client()`.
"""
from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import Column, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

class JobRecord(Base):
    __tablename__ = "jobs"
    job_id = Column(String, primary_key=True)
    job_type = Column(String, nullable=False)
    status = Column(String, nullable=False)
    metadata_json = Column("metadata", String, nullable=False, default="{}")
    result = Column(String, nullable=True)
    error = Column(String, nullable=True)
    created_at = Column(String, nullable=False)
    started_at = Column(String, nullable=True)
    finished_at = Column(String, nullable=True)

import time

_engine = None
_SessionLocal = None
_engine_key: str | None = None


def _current_jobs_db_key() -> str:
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return f"url:{db_url}"
    db_path = Path(os.environ.get("KLYRA_JOBS_DB", ".klyra_jobs.sqlite3")).resolve()
    return f"sqlite:{db_path}"


def _get_engine():
    global _engine, _SessionLocal, _engine_key
    key = _current_jobs_db_key()
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
            db_path = Path(os.environ.get("KLYRA_JOBS_DB", ".klyra_jobs.sqlite3"))
            if db_path.parent != Path("."):
                db_path.parent.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        _SessionLocal = sessionmaker(bind=_engine)
        for attempt in range(15):
            try:
                Base.metadata.create_all(bind=_engine)
                break
            except Exception:
                if attempt == 14:
                    raise
                time.sleep(1.0)
        _engine_key = key
    return _SessionLocal

@contextmanager
def _get_session():
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


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _row_to_dict(job: JobRecord) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "status": job.status,
        "metadata": json.loads(job.metadata_json) if job.metadata_json else {},
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def create_job(job_type: str, **metadata: Any) -> str:
    job_id = str(uuid.uuid4())
    with _get_session() as session:
        job = JobRecord(
            job_id=job_id,
            job_type=job_type,
            status=JobStatus.QUEUED.value,
            metadata_json=json.dumps(metadata),
            created_at=_now(),
        )
        session.add(job)
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _get_session() as session:
        job = session.query(JobRecord).filter(JobRecord.job_id == job_id).first()
        return _row_to_dict(job) if job else None


def list_jobs(job_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with _get_session() as session:
        query = session.query(JobRecord)
        if job_type:
            query = query.filter(JobRecord.job_type == job_type)
        jobs = query.order_by(JobRecord.created_at.desc()).limit(limit).all()
        return [_row_to_dict(job) for job in jobs]


def _update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    with _get_session() as session:
        session.query(JobRecord).filter(JobRecord.job_id == job_id).update(fields)


def run_job(job_id: str, fn: Callable[[], Any]) -> None:
    """Runs `fn()` synchronously and records the outcome.

    Meant to be handed to FastAPI's `BackgroundTasks.add_task` (which runs it
    in a worker thread after the response is sent) - not called directly
    from a request handler, and not awaited.
    """
    _update_job(job_id, status=JobStatus.RUNNING.value, started_at=_now())
    try:
        result = fn()
        _update_job(
            job_id,
            status=JobStatus.DONE.value,
            result=str(result) if result is not None else "",
            finished_at=_now(),
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad: last line of defense in a background task
        _update_job(job_id, status=JobStatus.FAILED.value, error=str(exc), finished_at=_now())
