"""Teaching material (independent agent): generate for a live session,
revise on demand, and fetch finished artifacts.
"""


from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from klyra.api.dependencies import DEFAULT_RATE_LIMIT, limiter, require_api_key
from klyra.celery_app import dispatch_job
from klyra.models.requests import ReviseTeachRequest, TeachRequest
from klyra.models.responses import JobAccepted, TeachingMaterialFilesResponse
from klyra.services import job_store, results_service
from klyra.services.teaching_material_service import (
    run_teaching_material,
    run_teaching_material as _orig_run_teaching_material,
    run_teaching_revision,
    run_teaching_revision as _orig_run_teaching_revision,
)
from klyra.utils import fs

router = APIRouter(tags=["teaching-material"])


@router.post(
    "/subjects/{subject}/teach",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def teach(subject: str, req: TeachRequest, request: Request) -> JobAccepted:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = job_store.create_job("teach", subject=subject, session_name=req.session_name)
    dispatch_job(
        "klyra.worker.run_teaching_material_task",
        job_id,
        run_teaching_material,
        _orig_run_teaching_material,
        subject=subject,
        session_name=req.session_name,
        scope=req.scope,
        concepts=req.concepts,
        notes=req.notes,
    )
    return JobAccepted(job_id=job_id)


@router.post(
    "/subjects/{subject}/teach/{session_name}/revise",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def revise_teach(
    subject: str,
    session_name: str,
    req: ReviseTeachRequest,
    request: Request,
) -> JobAccepted:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = job_store.create_job("revise-teach", subject=subject, session_name=session_name)
    dispatch_job(
        "klyra.worker.run_teaching_revision_task",
        job_id,
        run_teaching_revision,
        _orig_run_teaching_revision,
        subject=subject,
        session_name=session_name,
        notes=req.notes,
    )
    return JobAccepted(job_id=job_id)


@router.get("/subjects/{subject}/teach/{session_name}", response_model=TeachingMaterialFilesResponse)
def get_teaching_material(subject: str, session_name: str) -> TeachingMaterialFilesResponse:
    try:
        files = results_service.get_teaching_material(subject, session_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not files:
        raise HTTPException(status_code=404, detail="no teaching material found for this session_name yet")
    return TeachingMaterialFilesResponse(subject=subject, session_name=fs.slugify(session_name), files=files)
