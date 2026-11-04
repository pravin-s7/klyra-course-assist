# Production image for the Klyra HTTP API.
#
# Build:  docker build -t klyra .
# Run:    docker run -p 8000:8000 --env-file .env \
#           -v klyra-docs:/app/docs -v klyra-chroma:/app/.chroma \
#           -v klyra-jobs:/app/state klyra

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    libreoffice \
    pandoc \
    poppler-utils \
    texlive-latex-base \
    texlive-pictures \
    texlive-latex-extra \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user
RUN useradd -m -u 1000 klyra
WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY docker/ docker/

# Install package + dev dependencies (pytest, httpx) so test suites can run inside the container
RUN pip install --no-cache-dir ".[dev]"

# Runtime state directories - see the volume mounts in the docstring above.
RUN mkdir -p /app/docs /app/state /app/.db /app/.qdrant && chown -R klyra:klyra /app

ENV HOME=/tmp
ENV CREWAI_STORAGE_DIR=/tmp/crewai_storage
ENV KLYRA_CHROMA_DIR=/app/.chroma
ENV KLYRA_QDRANT_DIR=/app/.qdrant
ENV KLYRA_JOBS_DB=/app/state/jobs.sqlite3

EXPOSE 8765
ENTRYPOINT ["uvicorn", "klyra.web.server:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
