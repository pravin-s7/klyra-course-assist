"""POST /api/v1/subjects/{subject}/files - upload files.
DELETE /api/v1/subjects/{subject}/files - delete files and purge index."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile

from klyra.api.dependencies import DEFAULT_RATE_LIMIT, limiter, require_api_key
from klyra.celery_app import celery_app
from klyra.models.requests import SkillUpdateRequest
from klyra.models.responses import (
    JobAccepted,
    StructuredOutputsResponse,
    SubjectSkillResponse,
)
from klyra.services import job_store, results_service, skill_service
from klyra.tools import vector_store
from klyra.utils import fs
from fastapi.responses import FileResponse

router = APIRouter(tags=["files"])


@router.post(
    "/subjects/{subject}/files",
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def upload_file(
    request: Request,
    subject: str,
    file: UploadFile,
    category: Annotated[str, Form()],
    week: Annotated[int | None, Form()] = None,
) -> JobAccepted:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if category not in ("week", "additional", "pyq", "transcript"):
        raise HTTPException(status_code=400, detail=f"Invalid category: {category}")

    if category == "week":
        if week is None:
            raise HTTPException(status_code=400, detail="week must be provided when category is 'week'")
        target_dir = fs.week_dir(subject, week)
    elif category == "additional":
        target_dir = fs.additional_dir(subject)
    elif category == "pyq":
        target_dir = fs.pyq_dir(subject)
    else:
        target_dir = fs.transcripts_dir(subject)

    target_dir.mkdir(parents=True, exist_ok=True)
    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file has no filename")
        
    file_path = target_dir / file.filename

    # Save to disk
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Automatically dispatch ingestion task for just this week/category
    # if it's 'pyq' or 'additional', we run ingestion for the whole subject (week=None)
    # but the task handles delta parsing so it's fast.
    job_week = week if category == "week" else None
    
    job_id = job_store.create_job("ingest", subject=subject, week=job_week, force=False)
    celery_app.send_task(
        "klyra.worker.run_ingestion_task",
        kwargs=dict(job_id=job_id, subject=subject, week=job_week, force=False)
    )
    
    # We return the job ID so the user can track when ingestion is done
    return JobAccepted(job_id=job_id)


@router.delete(
    "/subjects/{subject}/files",
    status_code=200,
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def delete_file(
    request: Request,
    subject: str,
    source_file: str,
) -> dict[str, str]:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sdir = fs.subject_dir(subject).resolve()
    target_path = Path(source_file).resolve()
    
    if not target_path.is_relative_to(sdir):
        raise HTTPException(status_code=403, detail="Path traversal attempt or file outside subject directory")
        
    if target_path.exists():
        if target_path.is_dir():
            shutil.rmtree(target_path)
        else:
            target_path.unlink()
            
    # Always attempt vector store deletion regardless of local existence 
    # (it might have been deleted locally already)
    where = {"source_file": source_file}
    for name_func in [
        vector_store.parsed_collection_name,
        vector_store.syllabus_collection_name,
        vector_store.pyq_collection_name,
        vector_store.images_collection_name,
    ]:
        vector_store.delete_by_metadata(name_func(subject), where)
        
    return {"status": "deleted", "source_file": source_file}


@router.get(
    "/subjects/{subject}/files/{file_path:path}",
    dependencies=[Depends(require_api_key)],
)
@limiter.limit(DEFAULT_RATE_LIMIT)
def download_file(
    request: Request,
    subject: str,
    file_path: str,
) -> FileResponse:
    try:
        fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sdir = fs.subject_dir(subject).resolve()
    target_path = (sdir / file_path).resolve()
    
    if not target_path.is_relative_to(sdir):
        raise HTTPException(status_code=403, detail="Path traversal attempt or file outside subject directory")
        
    if not target_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
        
    return FileResponse(target_path)


@router.get(
    "/subjects/{subject}/skill",
    response_model=SubjectSkillResponse,
    dependencies=[Depends(require_api_key)],
)
def get_subject_skill(subject: str) -> SubjectSkillResponse:
    try:
        subject = fs.validate_subject(subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cfg = skill_service.load_subject_skill(subject)
    path = fs.subject_skill_path(subject)
    markdown = (
        path.read_text(encoding="utf-8")
        if path.exists()
        else skill_service.render_subject_skill_markdown(cfg)
    )
    return SubjectSkillResponse(
        subject=subject,
        config=cfg.model_dump(mode="json"),
        markdown=markdown,
    )


@router.put(
    "/subjects/{subject}/skill",
    response_model=SubjectSkillResponse,
    dependencies=[Depends(require_api_key)],
)
def update_subject_skill(
    subject: str,
    req: SkillUpdateRequest,
) -> SubjectSkillResponse:
    try:
        subject = fs.validate_subject(subject)
        cfg = skill_service.parse_subject_skill(req.markdown, fallback_subject=subject)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    path = fs.subject_skill_path(subject)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = skill_service.render_subject_skill_markdown(cfg)
    path.write_text(rendered, encoding="utf-8")
    return SubjectSkillResponse(
        subject=subject,
        config=cfg.model_dump(mode="json"),
        markdown=rendered,
    )


@router.get(
    "/subjects/{subject}/structured",
    response_model=StructuredOutputsResponse,
    dependencies=[Depends(require_api_key)],
)
def get_subject_structured_outputs(
    subject: str,
    category: str | None = None,
    name: str | None = None,
) -> StructuredOutputsResponse:
    try:
        subject = fs.validate_subject(subject)
        outputs = results_service.get_structured_outputs(
            subject, category=category, name=name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return StructuredOutputsResponse(
        subject=subject,
        category=category,
        name=name,
        outputs=outputs,
    )
