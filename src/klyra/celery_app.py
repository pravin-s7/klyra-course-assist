import os
from collections.abc import Callable
from typing import Any
from dotenv import load_dotenv
load_dotenv()

from celery import Celery

broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

celery_app = Celery(
    "klyra_tasks",
    broker=broker_url,
    backend=result_backend,
    include=["klyra.worker"]
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
)


def dispatch_job(
    task_name: str,
    job_id: str,
    fn: Callable[..., Any],
    original_fn: Callable[..., Any],
    **kwargs: Any,
) -> None:
    """Send `task_name` to the Celery worker via Redis in production/Docker Compose,
    or run `fn(**kwargs)` directly via `job_store.run_job` when `fn` is monkeypatched
    in tests, `CELERY_TASK_ALWAYS_EAGER` is enabled, or Redis is unreachable."""
    from klyra.services import job_store

    always_eager = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if always_eager or fn is not original_fn:
        job_store.run_job(job_id, lambda: fn(**kwargs))
        return

    try:
        celery_app.send_task(task_name, kwargs=dict(job_id=job_id, **kwargs))
    except Exception:
        job_store.run_job(job_id, lambda: fn(**kwargs))
