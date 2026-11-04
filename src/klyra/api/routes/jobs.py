"""GET endpoints for polling job status and fetching finished artifacts.

Deliberately not rate-limited or auth-gated by default like the POST
routes - these are cheap reads a frontend polls every few seconds while a
job runs (see PRODUCTION.md's polling pattern). Put them behind
`require_api_key` too if job results are sensitive in your deployment.
"""


from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from klyra.api.dependencies import require_api_key
from klyra.models.responses import GeneratedFilesResponse, JobListResponse, JobStatusResponse
from klyra.services import job_store, results_service
from klyra.utils import fs

router = APIRouter(tags=["jobs"])


def _to_response(job: dict[str, Any]) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=job["job_id"],
        job_type=job["job_type"],
        status=job["status"],
        metadata=job["metadata"],
        result=job.get("result"),
        error=job.get("error"),
        created_at=job["created_at"],
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
    )


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str) -> JobStatusResponse:
    job = job_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    return _to_response(job)


@router.get("/jobs", response_model=JobListResponse)
def list_jobs(job_type: str | None = None, limit: int = 50) -> JobListResponse:
    jobs = job_store.list_jobs(job_type=job_type, limit=limit)
    return JobListResponse(jobs=[_to_response(job) for job in jobs])


@router.get("/subjects/{subject}/generated/{qp_name}", response_model=GeneratedFilesResponse)
def get_generated(subject: str, qp_name: str) -> GeneratedFilesResponse:
    try:
        files = results_service.get_generated_files(subject, qp_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not files:
        raise HTTPException(status_code=404, detail="no generated files for this qp_name yet")
    return GeneratedFilesResponse(subject=subject, qp_name=fs.slugify(qp_name), files=files)
