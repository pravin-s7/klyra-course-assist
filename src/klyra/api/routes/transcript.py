"""POST /api/v1/subjects/{subject}/transcript - manual YouTube transcript fetch."""


from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from klyra.api.dependencies import DEFAULT_RATE_LIMIT, limiter, require_api_key
from klyra.celery_app import celery_app
from klyra.models.requests import TranscriptRequest
from klyra.models.responses import JobAccepted
from klyra.services import job_store
from klyra.utils import fs

router = APIRouter(tags=["transcript"])


@router.post(
    "/subjects/{subject}/transcript",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def transcript(
    subject: str, req: TranscriptRequest, request: Request
) -> JobAccepted:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = job_store.create_job("transcript", subject=subject, url=req.url)
    celery_app.send_task(
        "klyra.worker.run_transcript_fetch_task",
        kwargs=dict(job_id=job_id, url=req.url, subject=subject, week=req.week, title=req.title)
    )
    return JobAccepted(job_id=job_id)
