# Production image for the Klyra HTTP API.
#
# Build:  docker build -t klyra .
# Run:    docker run -p 8000:8000 --env-file .env \
#           -v klyra-docs:/app/docs -v klyra-chroma:/app/.chroma \
#           -v klyra-jobs:/app/state klyra
#
# docs/, .chroma/, and the jobs DB are runtime state - mount them as volumes
# (or point KLYRA_CHROMA_DIR/KLYRA_JOBS_DB at object storage/a real DB)
# rather than baking them into the image. See PRODUCTION.md.
#
# Deliberately does NOT install the `docker` CLI or mount the host's Docker
# socket - the coding-problem crew's code-execution tool needs that to
# validate test cases, but giving a containerized API the ability to launch
# sibling containers is a real security tradeoff (see PRODUCTION.md
# section 5) that should be an explicit choice, not a default.
FROM python:3.11-slim

# Full-fidelity .ppt/.doc parsing shells out to LibreOffice; .pptx/.docx and
# everything else work without it (see tools/document_parser_tool.py
# fallbacks). pandoc + a minimal TeX Live are needed for the `convert` and
# `teach`/diagram-rendering agents (pandoc, pdflatex via GenerateDiagramTool).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice \
        pandoc \
        texlive-latex-base \
        texlive-pictures \
        texlive-latex-extra \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for standard security practice
RUN useradd -m -u 1000 klyra
WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

# Use a standard install (not editable) to prevent runtime source code mutations
RUN pip install --no-cache-dir .

# Runtime state directories - see the volume mounts in the docstring above.
RUN mkdir -p /app/docs /app/state && chown -R klyra:klyra /app

# Drop privileges to the non-root user
USER klyra

ENV KLYRA_CHROMA_DIR=/app/.chroma
ENV KLYRA_JOBS_DB=/app/state/jobs.sqlite3

EXPOSE 8765
ENTRYPOINT ["uvicorn", "klyra.web.server:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
