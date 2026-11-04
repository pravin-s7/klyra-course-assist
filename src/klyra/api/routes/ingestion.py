"""POST /api/v1/subjects/{subject}/ingest - ingestion phase (parse + index)."""


from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from klyra.api.dependencies import DEFAULT_RATE_LIMIT, limiter, require_api_key
from klyra.celery_app import dispatch_job
from klyra.models.requests import IngestRequest
from klyra.models.responses import JobAccepted
from klyra.services import job_store
from klyra.services.ingestion_service import run_ingestion, run_ingestion as _orig_run_ingestion
from klyra.utils import fs

router = APIRouter(tags=["ingestion"])


@router.post(
    "/subjects/{subject}/ingest",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def ingest(subject: str, req: IngestRequest, request: Request) -> JobAccepted:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = job_store.create_job("ingest", subject=subject, week=req.week, force=req.force)
    dispatch_job(
        "klyra.worker.run_ingestion_task",
        job_id,
        run_ingestion,
        _orig_run_ingestion,
        subject=subject,
        week=req.week,
        force=req.force,
    )
    return JobAccepted(job_id=job_id)
