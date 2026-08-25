"""POST /api/v1/subjects/{subject}/convert - Markdown <-> LaTeX (independent agent)."""


from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from klyra.api.dependencies import DEFAULT_RATE_LIMIT, limiter, require_api_key
from klyra.celery_app import celery_app
from klyra.models.requests import ConvertRequest
from klyra.models.responses import JobAccepted
from klyra.services import job_store
from klyra.utils import fs

router = APIRouter(tags=["conversion"])


@router.post(
    "/subjects/{subject}/convert",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def convert(subject: str, req: ConvertRequest, request: Request) -> JobAccepted:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = job_store.create_job("convert", subject=subject)
    celery_app.send_task(
        "klyra.worker.run_conversion_task",
        kwargs=dict(job_id=job_id, subject=subject, request=req.request)
    )
    return JobAccepted(job_id=job_id)
