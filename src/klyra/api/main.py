"""FastAPI app: run with `uvicorn klyra.api.main:app` or `klyra serve`.

Every route delegates to services/ (see approach.md) - this module only
wires up routers, CORS, rate limiting, and the health check.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Must run before dependencies.py reads KLYRA_API_KEY/KLYRA_RATE_LIMIT and
# before any route module below is imported, in case this app is started
# directly (`uvicorn klyra.api.main:app`) rather than via `klyra serve`
# (which already loads .env itself).
load_dotenv(Path.cwd() / ".env")

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402

from klyra.api.dependencies import limiter  # noqa: E402
from klyra.api.routes import (  # noqa: E402
    coding_problem,
    conversion,
    files,
    generation,
    ingestion,
    jobs,
    revision,
    teaching_material,
    transcript,
)
from klyra.models.responses import HealthResponse  # noqa: E402

app = FastAPI(
    title="Klyra API",
    version="0.1.0",
    description="HTTP API for Klyra, the multi-agent system - ingestion, generation, revision, "
    "format conversion, coding problems, and teaching-material preparation, each as an async job.",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Comma-separated list, e.g. "https://app.example.com,https://staging.example.com".
# Deliberately empty (not "*") by default - see PRODUCTION.md "Auth and abuse protection":
# these endpoints kick off billed LLM calls, don't make it easy for an arbitrary site to
# trigger them via a logged-in user's browser.
_origins = [origin.strip() for origin in os.environ.get("KLYRA_CORS_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi import Depends
from klyra.api.dependencies import require_api_key

_auth = [Depends(require_api_key)]

app.include_router(files.router, prefix="/api/v1", dependencies=_auth)
app.include_router(ingestion.router, prefix="/api/v1", dependencies=_auth)
app.include_router(generation.router, prefix="/api/v1", dependencies=_auth)
app.include_router(revision.router, prefix="/api/v1", dependencies=_auth)
app.include_router(conversion.router, prefix="/api/v1", dependencies=_auth)
app.include_router(teaching_material.router, prefix="/api/v1", dependencies=_auth)
app.include_router(coding_problem.router, prefix="/api/v1", dependencies=_auth)
app.include_router(transcript.router, prefix="/api/v1", dependencies=_auth)
app.include_router(jobs.router, prefix="/api/v1", dependencies=_auth)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()
