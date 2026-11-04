"""POST /api/v1/subjects/{subject}/revise - fix a generated QP's flagged issues."""


from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from klyra.api.dependencies import DEFAULT_RATE_LIMIT, limiter, require_api_key
from klyra.celery_app import dispatch_job
from klyra.models.requests import ReviseRequest
from klyra.models.responses import JobAccepted
from klyra.services import job_store
from klyra.services.revision_service import run_revision, run_revision as _orig_run_revision
from klyra.utils import fs

router = APIRouter(tags=["revision"])


@router.post(
    "/subjects/{subject}/revise",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def revise(subject: str, req: ReviseRequest, request: Request) -> JobAccepted:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = job_store.create_job("revise", subject=subject, qp_name=req.qp_name)
    dispatch_job(
        "klyra.worker.run_revision_task",
        job_id,
        run_revision,
        _orig_run_revision,
        subject=subject,
        qp_name=req.qp_name,
        notes=req.notes,
    )
    return JobAccepted(job_id=job_id)
