"""POST /api/v1/subjects/{subject}/transcript - manual YouTube transcript fetch."""


from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from klyra.api.dependencies import DEFAULT_RATE_LIMIT, limiter, require_api_key
from klyra.celery_app import dispatch_job
from klyra.models.requests import TranscriptRequest
from klyra.models.responses import JobAccepted
from klyra.services import job_store
from klyra.services.transcript_service import run_transcript, run_transcript as _orig_run_transcript
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
    dispatch_job(
        "klyra.worker.run_transcript_fetch_task",
        job_id,
        run_transcript,
        _orig_run_transcript,
        url=req.url,
        subject=subject,
        week=req.week,
        title=req.title,
    )
    return JobAccepted(job_id=job_id)
