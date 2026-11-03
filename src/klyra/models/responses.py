"""Response bodies for every endpoint."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class JobAccepted(BaseModel):
    job_id: str
    status: str = "queued"


class JobStatusResponse(BaseModel):
    job_id: str
    job_type: str
    status: str
    metadata: dict[str, Any]
    result: str | None = None
    error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class JobListResponse(BaseModel):
    jobs: list[JobStatusResponse]


class GeneratedFilesResponse(BaseModel):
    subject: str
    qp_name: str
    files: dict[str, str]


class CodingProblemFilesResponse(BaseModel):
    subject: str
    name: str
    files: dict[str, str]
    package_dir: str | None = None
    package_files: list[str] = []


class TeachingMaterialFilesResponse(BaseModel):
    subject: str
    session_name: str
    files: dict[str, str]


class TranscriptResponse(BaseModel):
    path: str


class HealthResponse(BaseModel):
    status: str = "ok"


class SubjectSkillResponse(BaseModel):
    subject: str
    config: dict[str, Any]
    markdown: str


class StructuredOutputsResponse(BaseModel):
    subject: str
    category: str | None = None
    name: str | None = None
    outputs: dict[str, Any]
