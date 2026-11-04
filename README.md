<div align="center">

# 🎓 Klyra

**Multi-Agent Academic Content Generation, Automated Code Verification & Conversational Teaching Studio**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![CrewAI](https://img.shields.io/badge/Multi--Agent-CrewAI-FF5A5F)](https://www.crewai.com/)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Gemini API](https://img.shields.io/badge/AI-Gemini%20Live%20%26%20Vision-4285F4?logo=google&logoColor=white)](https://ai.google.dev/)
[![Qdrant](https://img.shields.io/badge/Vector%20DB-Qdrant-DC244C)](https://qdrant.tech/)
[![Docker Sandbox](https://img.shields.io/badge/Sandbox-Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)

[Features](#-key-features) •
[Architecture](#-architecture) •
[Project Structure](#-project-structure) •
[Quickstart](#-quickstart) •
[CLI Reference](#-cli-reference) •
[Web Studio & API](#-web-studio--rest-api) •
[Configuration](#️-configuration--environment-variables) •
[Testing](#-testing) •
[Contributing](#-contributing)

</div>

---

## 📖 Overview

**Klyra** is an open-source, multi-agent AI platform built on **CrewAI**, **Google Gemini**, **FastAPI**, and **Qdrant** that automates academic course preparation for university educators and teaching assistants.

By ingesting lecture slides, syllabi, transcripts, and past-year question papers (PYQs), Klyra generates syllabus-grounded **question papers**, **Docker-verified coding problem sets**, **self-healing LaTeX/TikZ diagrams**, and **live teaching session plans**—accessible through a unified **Web Studio**, **Gemini Live Voice Assistant**, **Interactive Terminal Chat**, **Local CLI**, and **Async REST API**.

---

## Key Features

- **Multi-Agent Question Paper Generation & Revision**
  - Orchestrates a 4-stage CrewAI pipeline: **QP Creator** → parallel **Discrepancy Reviewer** & independent **Solver/Verifier** → **Finalizer**.
  - Enforces strict Pydantic schemas, mark distribution guardrails, option-count checks, and syllabus/PYQ grounding.
  - Persists dual structured outputs (`.json` + `.md`) and converts deterministically to **Overleaf-ready LaTeX (`.tex`)**, **PDF**, or **Word (`.docx`)**.
- **Sandboxed Coding Problem Generation & Verification**
  - Creates complete coding problem packages (`README.md`, reference `solution.*`, `evaluation_script.*`, and public/private test suites).
  - Independently compiles and executes reference solutions against all generated test cases inside **network-disabled Docker containers** (`--network none`, memory/CPU/PID capped).
  - Supports **Python**, **C**, **C++**, **Java**, **Bash/Awk/Sed**, **SQL (SQLite)**, **PostgreSQL**, and **Python + Database** tracks.
- **Self-Healing LaTeX/TikZ Diagram Engine**
  - Automatically renders graphs, trees, automata, ER diagrams, and circuits via `pdflatex` → PNG/SVG.
  - Runs a **two-layer quality gate**: deterministic 2D geometric collision/clipping detection followed by **Gemini Vision** semantic verification (checking arrow visibility, edge labels, and course notation fidelity) with automatic self-repair loops.
- **🎙️ Four Unified Interfaces (Single-Source Tool Registry)**
  - **Web Studio (`klyra web`)**: Dark-mode chat & real-time voice UI with inline Markdown/MathJax rendering, interactive diagram lightbox, live streaming agent logs, one-click `.zip` bundle downloads, RBAC (`admin` / `instructor`), and Google OAuth 2.0.
  - **Voice Assistant (`klyra voice`)**: Hands-free, real-time bidirectional voice conversation powered by the **Gemini Live API**.
  - **Terminal Chat (`klyra chat`)**: Interactive conversational CLI with tool execution.
  - **CLI & REST API (`klyra <cmd>` / `/api/v1/*`)**: Direct synchronous CLI commands and Celery-backed asynchronous HTTP endpoints.
- **🧭 Per-Subject `skill.md` Profiles**
  - Each course (`docs/<subject>/skill.md`) declaratively configures notation rules, allowed programming languages, evaluation templates, and pedagogical style.

---

## 🏗️ Architecture

```text
┌───────────────────────────────────────────────────────────────────────────────┐
│                                 ENTRY POINTS                                  │
│                                                                               │
│   klyra web                klyra voice / chat         klyra <cmd>             │
│   (FastAPI Web Studio      (Gemini Live Audio &       (Synchronous CLI &      │
│    + /api/v1 REST API)      Terminal Chat Loops)       Scripting Interface)   │
│            │                         │                          │             │
│            └─────────────────────────┼──────────────────────────┘             │
│                                      ▼                                        │
├───────────────────────────────────────────────────────────────────────────────┤
│                   SINGLE-SOURCE TOOL REGISTRY & SERVICES                      │
│             (klyra/voice/tool_registry.py  •  klyra/services/*.py)            │
│                                                                               │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐  │
│  │ Ingestion    │ │ Generation & │ │ Coding       │ │ Teaching, Conversion │  │
│  │ Service      │ │ Revision Svc │ │ Problem Svc  │ │ & Transcript Svcs    │  │
│  └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └──────────┬───────────┘  │
│         │                │                │                    │              │
├─────────┼────────────────┼────────────────┼────────────────────┼──────────────┤
│         ▼                ▼                ▼                    ▼              │
│                     CONSOLIDATED DOMAIN CREWS (CrewAI)                        │
│                                                                               │
│  ┌────────────────────────────────────┬────────────────────────────────────┐  │
│  │ ExamCrew (generate + revise)       │ CodingCrew (generate + revise)     │  │
│  │                                    │                                    │  │
│  │   QP Creator ────────┐             │   Problem Writer ────────┐         │  │
│  │                      ▼             │                          ▼         │  │
│  │   Discrepancy ◄─── (QP) ───► Solver│   Docker Sandbox ◄── (Problems)    │  │
│  │   Reviewer     ║            Verifier│   Code Validator  ║    Discrepancy│  │
│  │                ╚═════╦═══════╝     │                    ╚═════╦═════════│  │
│  │                      ▼             │                          ▼         │  │
│  │              Finalizer Agent       │                  Finalizer Agent   │  │
│  └────────────────────────────────────┴────────────────────────────────────┘  │
│  + TeachingCrew (prepare + revise)    + IngestionCrew (parse + index)         │
│                                                                               │
├───────────────────────────────────────────────────────────────────────────────┤
│                        TOOLS, DIAGRAMS & STORAGE LAYER                        │
│                                                                               │
│  • Vector Store (Qdrant / Chroma) + Gemini Embedding 2                        │
│  • Diagram Engine (klyra/diagrams/: compiler.py + verifier.py + tool.py)      │
│  • Docker Code Sandbox (klyra/tools/code_execution_tool.py + templates/eval/) │
│  • Format Renderer & Guardrails (dual .json + .md persistence, Pandoc/LaTeX)  │
│  • SQLAlchemy Storage (PostgreSQL / SQLite: Users, Sessions, File Ownership)  │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## 📂 Project Structure

```text
src/klyra/
├── cli.py                        # Unified CLI entrypoint (ingest, generate, code, teach, web, voice, chat, ...)
├── celery_app.py                 # Celery broker & backend configuration
├── worker.py                     # Background Celery worker tasks for /api/v1/*
├── core/                         # Shared system prompt, LLM factory, subject skill.md loader
├── models/                       # Pydantic schemas (QuestionPaper, CodingProblemSet, TeachingPlan, API responses)
├── services/                     # Explicit business logic layer
│   ├── ingestion_service.py      # Multimodal document parsing & vector indexing
│   ├── generation_service.py     # Question paper generation
│   ├── revision_service.py       # Question paper revision
│   ├── coding_problem_service.py # Sandboxed coding problem generation & revision
│   ├── teaching_material_service.py # Live session teaching material generation & revision
│   ├── conversion_service.py     # Deterministic Markdown ↔ LaTeX/PDF/DOCX conversion + LLM fallback
│   ├── transcript_service.py     # YouTube lecture transcript downloader
│   └── job_store.py              # Async job state store
├── crews/                        # Consolidated CrewAI domain crews
│   ├── exam_crew.py              # ExamCrew (question paper generation & revision)
│   ├── coding_crew.py            # CodingCrew (coding problem generation & revision)
│   ├── teaching_crew.py          # TeachingCrew (teaching material generation & revision)
│   ├── ingestion_crew/           # Document parsing & Qdrant indexing crew
│   └── base_task.py              # KlyraTask with dual .json + .md output persistence
├── diagrams/                     # Self-healing LaTeX/TikZ diagram subsystem
│   ├── compiler.py               # TikZ sanitization, pdflatex compilation, PNG/SVG conversion
│   ├── verifier.py               # 2D geometric collision analysis + Gemini Vision semantic gate
│   └── tool.py                   # CrewAI GenerateDiagramTool & standalone diagram generator
├── templates/                    # Static evaluation scripts & LaTeX preambles
│   ├── eval/                     # Runner scripts (eval_python.py, eval_bash.sh, eval_java.sh, eval_c_cpp.sh, eval_sql.sh, ...)
│   └── latex/exam_preamble.tex   # Exam paper LaTeX preamble template
├── tools/                        # Agent tools (vector_store, code_execution_tool, format_conversion, subject_fs_tool, ...)
├── utils/                        # Guardrails & rendering utilities
│   ├── task_guardrails.py        # CrewAI output validation callbacks
│   ├── markdown_parsers.py       # Fallback Markdown-to-Pydantic structured parsers
│   ├── coding_verifier.py        # Independent Docker test-suite verification & spec sync
│   └── format_renderer.py        # Pydantic → Markdown renderer & save_dual_output()
├── voice/                        # Gemini Live & Chat tool layer
│   ├── tool_registry.py          # Single-source @register_tool definitions & Pydantic input schemas
│   ├── tool_executor.py          # Async tool dispatcher with log streaming & RBAC checks
│   ├── orchestrator.py           # Gemini Live bidirectional audio session manager
│   └── transport.py              # Audio transport abstraction (PyAudio CLI transport)
├── auth/                         # Cookie sessions, RBAC dependencies, password hashing, Google OAuth 2.0
├── storage/                      # SQLAlchemy models & repository (Users, ChatSessions, FileOwnership)
├── api/                          # REST /api/v1/* routers (mounted on klyra.web.server:app)
└── web/                          # FastAPI Web Studio
    ├── server.py                 # Unified FastAPI app entrypoint
    ├── admin_routes.py           # User & course assignment administration
    ├── routes/                   # Modular routes (files.py, sessions.py, ws_chat.py, ws_voice.py)
    └── static/                   # Frontend UI (index.html, app.js, markdown.js, admin.js, style.css)
```

---

## Quickstart

### Option 1: Docker Compose (Recommended for Full Stack)

Docker Compose starts the **Web Studio & REST API**, **Celery Worker**, **PostgreSQL**, **Redis**, and **Qdrant** vector database in one command.

1. **Create your `.env` file**
   ```bash
   cp .env.example .env   # or create .env with the variables below
   ```
   Minimum required keys in `.env`:
   ```env
   GEMINI_API_KEY=your-gemini-api-key
   KLYRA_SESSION_SECRET=change-me-to-a-long-random-secret
   KLYRA_ADMIN_PASSWORD=change-me-admin-password
   ```

2. **Build the coding sandbox images (optional, for coding problem verification)**
   ```bash
   docker build -f Dockerfile.sandbox-python-db -t klyra-sandbox-python-db:latest .
   docker build -f Dockerfile.sandbox-postgres  -t klyra-sandbox-postgres:latest .
   ```

3. **Start the stack**
   ```bash
   docker compose up -d --build
   ```

4. **Open Klyra Web Studio**
   - **Web UI (Chat + Voice + File Manager)**: [http://localhost:8765](http://localhost:8765)
   - **Admin Dashboard**: [http://localhost:8765/admin](http://localhost:8765/admin)
   - **OpenAPI / Swagger Docs**: [http://localhost:8765/docs](http://localhost:8765/docs)

---

### Option 2: Local Development Setup

1. **System Prerequisites**
   - Python **3.10+**
   - Docker (for Qdrant and sandboxed code verification)
   - `texlive-latex-base`, `texlive-pictures`, `texlive-latex-extra`, `poppler-utils`, `pandoc` (for LaTeX diagram compilation & PDF/DOCX export)
   - `portaudio19-dev` (optional, for `klyra voice` microphone/speaker I/O)

2. **Start Qdrant (and optional Redis/Postgres)**
   ```bash
   docker run -d --name klyra-qdrant -p 6333:6333 qdrant/qdrant:latest
   ```

3. **Create a virtual environment & install Klyra**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev,voice]"
   ```

4. **Launch Klyra**
   ```bash
   # Launch the Web Studio at http://127.0.0.1:8765
   klyra web

   # Or start an interactive terminal chat session
   klyra chat

   # Or start a hands-free Gemini Live voice session
   klyra voice
   ```

---

## CLI Reference

Klyra provides a full suite of synchronous CLI commands via `klyra <subcommand>`:

### 1. Ingest & Manage Course Materials
```bash
# Ingest all lecture materials for a subject
klyra ingest --subject dbms

# Ingest a specific week along with PYQs and transcripts
klyra ingest --subject ct --week 3 --pyq --transcript

# Ingest a single file or force re-parse
klyra ingest --subject python --file docs/python/week_1/lecture1.pdf --force

# Remove a specific file from the vector index
klyra delete-index --subject python --source-file docs/python/week_1/old.pdf
```

### 2. Generate & Revise Question Papers
```bash
# Generate a question paper with parallel review and answer key verification
klyra generate \
  --subject ct \
  --qp-name endterm-graph-qp \
  --scope "all" \
  --difficulty hard \
  --type-counts "MCQ:5,MSQ:3,NAT:2" \
  --pyq-similar 4 \
  --concepts "directed graphs, PageRank, shortest paths" \
  --notes "Include TikZ graph diagrams where appropriate"

# Revise an existing question paper using automated review feedback or custom notes
klyra revise \
  --subject ct \
  --qp-name endterm-graph-qp \
  --notes "Make Question 3's edge weights unambiguous"
```

### 3. Generate & Revise Coding Problems
```bash
# Generate Docker-verified coding problems with public & private test cases
klyra code \
  --subject python \
  --name matrix-lab \
  --language python \
  --scope "all" \
  --difficulty hard \
  --num-questions 2 \
  --public-tests 2 \
  --private-tests 3 \
  --concepts "2D matrices, simulation"

# Revise a coding problem set
klyra revise-code \
  --subject python \
  --name matrix-lab \
  --language python \
  --notes "Clarify input bounds for negative integers in Problem 2"

# Re-verify an on-disk coding package against its test suite
klyra skill --subject python --verify-code matrix-lab
```

### 4. Teaching Materials, Format Conversion & Utilities
```bash
# Prepare a live teaching session plan
klyra teach \
  --subject dbms \
  --session-name week4-normalization \
  --scope "week_4" \
  --notes "60 min live session, intuition before BCNF formalism, 2 worked examples"

# Revise a teaching session plan
klyra revise-teach \
  --subject dbms \
  --session-name week4-normalization \
  --notes "Add a 3NF vs BCNF comparison table"

# Convert generated Markdown to LaTeX (.tex), PDF (.pdf), or Word (.docx)
klyra convert \
  --subject ct \
  --input docs/ct/generated/endterm-graph-qp-qp.md \
  --to tex

# Download a YouTube lecture transcript into docs/<subject>/transcripts/
klyra transcript \
  --subject dbms \
  --url "https://www.youtube.com/watch?v=..." \
  --name week4-lecture

# Inspect, validate, or initialize a subject's skill.md configuration
klyra skill --subject ct --show
klyra skill --subject ct --validate
klyra skill --subject new-course --init
```

---

## 🌐 Web Studio & REST API

### Web Studio (`klyra web`)
- **Chat & Voice**: Converse with Klyra by text or live microphone streaming (`/ws`).
- **Inline Previews & Lightbox**: View generated Markdown, LaTeX, and math formulas (via MathJax), click any generated TikZ diagram to zoom, or toggle raw Markdown.
- **One-Click ZIP Bundles**: Download complete coding problem packages (including `problem-N/` folders, test cases, solutions, evaluation scripts, and raw `.md`/`.json` specs) or Question Paper bundles via `/api/download/<subject>/<name>`.
- **Role-Based Access Control (RBAC)**:
  - **Admins** have access to all courses, user management (`/admin`), and all generated files.
  - **Instructors** are scoped to their assigned courses and own the files they generate.

### REST API (`/api/v1/*`)
All long-running generation tasks are also exposed as asynchronous REST endpoints (authenticated via `X-API-Key: <KLYRA_API_KEY>`):

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Health check for Qdrant, Redis, and server status |
| `POST` | `/api/v1/ingest` | Enqueue document ingestion job |
| `POST` | `/api/v1/generate` | Enqueue question paper generation job |
| `POST` | `/api/v1/revise` | Enqueue question paper revision job |
| `POST` | `/api/v1/coding-problems` | Enqueue coding problem set generation job |
| `POST` | `/api/v1/coding-problems/revise` | Enqueue coding problem revision job |
| `POST` | `/api/v1/teaching-material` | Enqueue teaching material generation job |
| `POST` | `/api/v1/teaching-material/revise` | Enqueue teaching material revision job |
| `POST` | `/api/v1/convert` | Enqueue format conversion job |
| `POST` | `/api/v1/transcript` | Enqueue YouTube transcript fetch job |
| `GET` | `/api/v1/jobs/{job_id}` | Poll async job status, result, or error |

---

## Subject Directory Convention (`docs/<subject>/`)

Each course lives under `docs/<subject>/`:

```text
docs/<subject>/
├── skill.md                  # Subject-specific pedagogical, notation & language configuration
├── week_1/ ... week_12/      # Weekly lecture slides, notes, PDFs, or code samples
├── pyq/                      # Past-year question papers
├── transcripts/              # Lecture transcripts
├── additional/               # Reference sheets or supplementary materials
├── .assets/
│   ├── extracted/            # Diagrams extracted from source PDFs during ingestion
│   └── generated/            # Self-verified TikZ diagrams generated for QPs & problems
└── generated/
    ├── <qp>-qp.md / .json              # Generated Question Paper
    ├── <qp>-answer-key.md / .json      # Independently verified Answer Key
    ├── <qp>-discrepancy-report.md      # Reviewer Discrepancy Report
    ├── <qp>-final-summary.md           # Final Sign-Off Summary
    ├── teaching/                       # Generated teaching session plans
    └── coding/
        ├── <pkg>-problems.md / .json   # Student-facing problem descriptions
        ├── <pkg>-testcases.md / .json  # Full test suite & reference solutions
        └── <pkg>/                      # Executable package directory (problem-1/, problem-2/, ...)
```

---

## Configuration & Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | *(Required)* | Google Gemini API key for LLM, Vision, Live API, and Embeddings |
| `KLYRA_MODEL` | `gemini/gemini-3-flash-preview` | Primary LiteLLM model identifier used by CrewAI agents |
| `KLYRA_CHAT_MODEL` | `gemini-3.5-flash-lite` | Model used by `ChatOrchestrator` in web & terminal chat |
| `KLYRA_VOICE_MODEL` | `gemini-3.1-flash-live-preview` | Gemini Live model for real-time voice sessions |
| `KLYRA_VOICE_NAME` | `Puck` | Voice persona (`Puck`, `Charon`, `Kore`, `Fenrir`, `Aoede`, `Leda`, `Orus`, `Zephyr`) |
| `EMBEDDING_PROVIDER` | `google` | Embedding provider (`google` or `openai`) |
| `GOOGLE_EMBEDDING_MODEL` | `models/gemini-embedding-2-preview` | Gemini embedding model for Qdrant vector indexing |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant server URL (falls back to local `.qdrant/` if unset) |
| `DATABASE_URL` | `sqlite:///.db/klyra.db` | SQLAlchemy database URL (PostgreSQL in Docker, SQLite locally) |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Redis URL for Celery task queue |
| `KLYRA_SESSION_SECRET` | *(Auto-generated in dev)* | HMAC secret for signing web session cookies (set in production) |
| `KLYRA_ADMIN_USERNAME` | `admin` | Bootstrap admin username |
| `KLYRA_ADMIN_PASSWORD` | *(Printed on first boot)* | Bootstrap admin password |
| `KLYRA_API_KEY` | *(Optional)* | API key for `/api/v1/*` (`X-API-Key` header) |
| `GOOGLE_CLIENT_ID` | *(Optional)* | Google OAuth 2.0 Client ID for "Sign in with Google" |
| `GOOGLE_CLIENT_SECRET` | *(Optional)* | Google OAuth 2.0 Client Secret |

---

## Contributing

Contributions, bug reports, and feature requests are welcome!

1. Fork the repository and create your feature branch (`git checkout -b feat/amazing-feature`).
2. Install development dependencies (`pip install -e ".[dev]"`).
3. Ensure all tests pass (`pytest -v`) and lint checks succeed (`ruff check src/ tests/`).
4. Open a Pull Request describing your changes.

