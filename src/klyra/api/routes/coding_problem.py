"""Coding problems: generate (with independent, execution-based test-case
validation), revise on demand, and fetch finished artifacts.
"""


from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from klyra.api.dependencies import DEFAULT_RATE_LIMIT, limiter, require_api_key
from klyra.celery_app import dispatch_job
from klyra.models.requests import CodeRequest, ReviseCodeRequest
from klyra.models.responses import CodingProblemFilesResponse, JobAccepted
from klyra.services import job_store, results_service
from klyra.services.coding_problem_service import (
    run_coding_generation,
    run_coding_generation as _orig_run_coding_generation,
    run_coding_revision,
    run_coding_revision as _orig_run_coding_revision,
)
from klyra.utils import fs

router = APIRouter(tags=["coding-problems"])


@router.post(
    "/subjects/{subject}/code",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def generate_code(subject: str, req: CodeRequest, request: Request) -> JobAccepted:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if req.pyq_similar > req.num_questions:
        raise HTTPException(
            status_code=400,
            detail=f"pyq_similar ({req.pyq_similar}) cannot exceed num_questions ({req.num_questions}).",
        )

    job_id = job_store.create_job("code", subject=subject, name=req.name, language=req.language)
    dispatch_job(
        "klyra.worker.run_coding_generation_task",
        job_id,
        run_coding_generation,
        _orig_run_coding_generation,
        subject=subject,
        name=req.name,
        language=req.language,
        scope=req.scope,
        difficulty=req.difficulty,
        num_questions=req.num_questions,
        public_tests=req.public_tests,
        private_tests=req.private_tests,
        source=req.source,
        pyq_similar=req.pyq_similar,
        leetcode_similar=req.leetcode_similar,
        include_reference=req.include_reference,
        concepts=req.concepts,
        notes=req.notes,
    )
    return JobAccepted(job_id=job_id)


@router.post(
    "/subjects/{subject}/code/{name}/revise",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def revise_code(
    subject: str, name: str, req: ReviseCodeRequest, request: Request
) -> JobAccepted:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = job_store.create_job("revise-code", subject=subject, name=name, language=req.language)
    dispatch_job(
        "klyra.worker.run_coding_revision_task",
        job_id,
        run_coding_revision,
        _orig_run_coding_revision,
        subject=subject,
        name=name,
        language=req.language,
        notes=req.notes,
    )
    return JobAccepted(job_id=job_id)


@router.get("/subjects/{subject}/code/{name}", response_model=CodingProblemFilesResponse)
def get_coding_problems(subject: str, name: str) -> CodingProblemFilesResponse:
    try:
        files = results_service.get_coding_problem_files(subject, name)
        package_dir, package_files = results_service.get_coding_package_listing(subject, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not files and not package_files:
        raise HTTPException(status_code=404, detail="no coding problems found for this name yet")
    return CodingProblemFilesResponse(
        subject=subject,
        name=fs.slugify(name),
        files=files,
        package_dir=package_dir,
        package_files=package_files,
    )
