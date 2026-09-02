"""POST /api/v1/subjects/{subject}/generate - generation phase (create + review + solve)."""


from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from klyra.api.dependencies import DEFAULT_RATE_LIMIT, limiter, require_api_key
from klyra.celery_app import celery_app
from klyra.models.requests import GenerateRequest
from klyra.models.responses import JobAccepted
from klyra.services import job_store
from klyra.utils import fs

router = APIRouter(tags=["generation"])


@router.post(
    "/subjects/{subject}/generate",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def generate(
    subject: str, req: GenerateRequest, request: Request
) -> JobAccepted:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Fail fast with a 422 rather than letting the job start and immediately fail -
    # same check run_generation makes, just surfaced synchronously (see PRODUCTION.md).
    if not req.type_counts and not (req.types and req.num_questions):
        raise HTTPException(
            status_code=422,
            detail="generate needs either type_counts (e.g. 'MCQ:5,MSQ:3,NAT:2') or both types and num_questions.",
        )

    job_id = job_store.create_job("generate", subject=subject, qp_name=req.qp_name)
    celery_app.send_task(
        "klyra.worker.run_generation_task",
        kwargs=dict(
            job_id=job_id,
            subject=subject,
            qp_name=req.qp_name,
            scope=req.scope,
            difficulty=req.difficulty,
            source=req.source,
            pyq_similar=req.pyq_similar,
            concepts=req.concepts,
            type_counts=req.type_counts,
            types=req.types,
            num_questions=req.num_questions,
            notes=req.notes,
        )
    )
    return JobAccepted(job_id=job_id)
