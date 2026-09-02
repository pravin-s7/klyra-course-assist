# Klyra

Klyra is a multi-agent AI system built on CrewAI that automates course preparation — parsing course materials to generate highly structured question papers, coding problems, and teaching materials. It offers three interfaces: a **local CLI**, an **HTTP API** (FastAPI + Celery), and a **conversational voice CLI** powered by the Gemini Live API.

## Architecture Overview

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                          ENTRY POINTS                                   │
│                                                                         │
│   klyra <cmd>            klyra serve             klyra voice            │
│   (CLI — sync)           (HTTP API — async)       (Voice — real-time)   │
│       │                      │                        │                 │
│       │                      ▼                        ▼                 │
│       │               ┌─────────────┐          ┌──────────────┐         │
│       │               │  FastAPI    │          │ Voice        │         │
│       │               │  Server    │          │ Orchestrator │         │
│       │               │  + CORS    │          │              │         │
│       │               │  + Auth    │          │ ┌──────────┐ │         │
│       │               │  + Rate    │          │ │ PyAudio  │ │         │
│       │               │    Limit   │          │ │Transport │ │         │
│       │               └─────┬──────┘          │ └──────────┘ │         │
│       │                     │                 │ ┌──────────┐ │         │
│       │                     ▼                 │ │ Gemini   │ │         │
│       │               ┌──────────┐            │ │ Live WS  │ │         │
│       │               │ Job Store│            │ └────┬─────┘ │         │
│       │               │ (SQLite/ │            │      │       │         │
│       │               │  Postgres)│           │  Tool Calls  │         │
│       │               └─────┬────┘            └──────┼───────┘         │
│       │                     ▼                        │                 │
│       │               ┌──────────┐                   │                 │
│       │               │ Celery   │                   │                 │
│       │               │ Worker   │                   │                 │
│       │               │ (Redis)  │                   │                 │
│       │               └─────┬────┘                   │                 │
│       │                     │                        │                 │
│       └─────────────────────┼────────────────────────┘                 │
│                             ▼                                           │
├─────────────────────────────────────────────────────────────────────────┤
│                        SERVICE LAYER                                     │
│                   (klyra/services/*.py)                                   │
│                                                                         │
│  ┌──────────┐ ┌───────────┐ ┌─────────┐ ┌──────────┐ ┌─────────────┐  │
│  │ Ingest   │ │ Generate  │ │ Revise  │ │ Teach    │ │ Code        │  │
│  │ Service  │ │ Service   │ │ Service │ │ Service  │ │ Service     │  │
│  └────┬─────┘ └─────┬─────┘ └────┬────┘ └────┬─────┘ └──────┬──────┘  │
│       │             │            │           │              │          │
│  + Convert Service  │  + Revise-Teach Service  │  + Revise-Code      │
│  + Transcript Svc   │  + Delete-Index          │                      │
│                     │                          │                      │
├─────────────────────┼──────────────────────────┼──────────────────────┤
│                     ▼                          ▼                      │
│                    CREWAI MULTI-AGENT SYSTEM                          │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Generation Crew          │  Coding Problem Crew               │  │
│  │                           │                                    │  │
│  │  QP Creator ──────┐       │  Problem Writer ──────┐            │  │
│  │                   ▼       │                       ▼            │  │
│  │  Discrepancy ◄── (QP) ──►│  Code Validator ◄── (Problems) ──►│  │
│  │  Reviewer   ║             │  (runs in Docker) ║               │  │
│  │             ║  Solver &   │                   ║  Discrepancy  │  │
│  │             ╚══ Verifier  │                   ╚══ Reviewer    │  │
│  │                   │       │                       │            │  │
│  │             ┌─────┘       │                 ┌─────┘            │  │
│  │             ▼             │                 ▼                  │  │
│  │  Finalizer (merges both)  │  Finalizer (merges both)          │  │
│  └───────────────────────────┴────────────────────────────────────┘  │
│                                                                       │
│  + Ingestion Crew        + Teaching Material Crew                    │
│  + Revision Crew         + Teaching Revision Crew                    │
│  + Coding Revision Crew  + Format Converter Crew                     │
│                                                                       │
├───────────────────────────────────────────────────────────────────────┤
│                         TOOL LAYER                                     │
│                                                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                │
│  │ RAG Tools    │  │ Parsing      │  │ Execution    │                │
│  │              │  │ Tools        │  │ Tools        │                │
│  │ • Syllabus   │  │              │  │              │                │
│  │   Search     │  │ • Document   │  │ • Code       │                │
│  │ • PYQ Search │  │   Parser     │  │   Executor   │                │
│  │ • PYQ Chunks │  │   (Gemini    │  │   (Docker    │                │
│  │ • Image      │  │    multimodal│  │    sandbox)  │                │
│  │   Search     │  │    PDF/PPT)  │  │              │                │
│  │ • Source     │  │ • YouTube    │  │ • Format     │                │
│  │   Files List │  │   Transcript │  │   Converter  │                │
│  │ • LeetCode   │  │ • Image      │  │   (Pandoc)   │                │
│  │   Search     │  │   Extraction │  │              │                │
│  │ • Coding     │  │              │  │ • LaTeX      │                │
│  │   Source     │  │              │  │   Diagrams   │                │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                │
│         │                 │                 │                         │
├─────────┼─────────────────┼─────────────────┼─────────────────────────┤
│         ▼                 ▼                 ▼                         │
│                       STORAGE LAYER                                   │
│                                                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                │
│  │ Qdrant       │  │ Gemini LLM   │  │ Filesystem   │                │
│  │ Vector Store │  │ (via LiteLLM)│  │              │                │
│  │              │  │              │  │ docs/<subj>/ │                │
│  │ Collections: │  │ Default:     │  │  week_1-12/  │                │
│  │ • <subj>-    │  │ gemini-3.1-  │  │  additional/ │                │
│  │   syllabus   │  │ flash-lite   │  │  pyq/        │                │
│  │ • <subj>-pyq │  │              │  │  transcripts/│                │
│  │ • <subj>-    │  │ Embeddings:  │  │  generated/  │                │
│  │   parsed     │  │ gemini-      │  │   ├─ *.md    │                │
│  │ • <subj>-    │  │ embedding-2  │  │   └─ coding/ │                │
│  │   images     │  │              │  │              │                │
│  └──────────────┘  └──────────────┘  └──────────────┘                │
│                                                                       │
│  + SQLite / PostgreSQL (Job Store)                                    │
│  + Redis (Celery broker & result backend)                            │
│  + Docker (sandboxed code execution)                                  │
└───────────────────────────────────────────────────────────────────────┘
```

### Layer-by-Layer Breakdown

#### 1. Entry Points

Klyra has three interfaces that **all converge on the same service layer** — no logic duplication.

| Entry Point | How it works | When to use |
|---|---|---|
| **`klyra <cmd>`** (CLI) | Calls `services/*.py` directly, blocks until done | Local dev, scripting, one-off tasks |
| **`klyra serve`** (HTTP API) | FastAPI receives request → creates Job record → dispatches to Celery → returns `202 Accepted` immediately. Celery worker picks up the task and calls `services/*.py` in the background. Client polls `/api/v1/jobs/{id}` for status. | Production deployment, frontend integration |
| **`klyra voice`** (Voice CLI) | Opens a bidirectional WebSocket to Gemini Live API. User speaks → Gemini asks follow-up questions → gathers parameters → emits `function_call` → `ToolExecutor` calls `services/*.py` → result sent back to Gemini for verbal summary. | Hands-free use, faster than memorising CLI flags |

#### 2. Service Layer (`klyra/services/`)

The **single source of truth** for business logic. Every entry point calls these functions — they never talk to crews directly.

| Service | What it does |
|---|---|
| `ingestion_service` | Parses docs (PDF, PPT, DOCX, Markdown) via Gemini multimodal, indexes into Qdrant |
| `generation_service` | Creates question papers — validates inputs, builds crew parameters, kicks off `GenerationCrew` |
| `revision_service` | Revises generated QPs using their own review + answer key |
| `coding_problem_service` | Generates coding problems with Docker-validated test cases |
| `teaching_material_service` | Prepares/revises structured teaching session plans |
| `conversion_service` | Converts between Markdown ↔ LaTeX via an agent with Pandoc |
| `transcript_service` | Downloads YouTube transcripts for ingestion |
| `job_store` | Persistent job tracking (SQLite or PostgreSQL) for the async API |

#### 3. CrewAI Multi-Agent System (`klyra/crews/`)

Each service delegates to a **crew** — a team of AI agents with specialised roles. Agents run sequentially or in parallel depending on the workflow.

**Generation Crew** (question paper creation):

```text
QP Creator Agent
    │  (generates the paper using RAG tools + syllabus search)
    ▼
┌───────────────────────┬──────────────────────┐
│ Discrepancy Reviewer  │  Solver & Verifier   │  ← run in PARALLEL
│ (checks accuracy,     │  (independently      │
│  syllabus coverage,   │   solves every Q,    │
│  ambiguity, overlap)  │   verifies answers)  │
└───────────┬───────────┴──────────┬───────────┘
            └──────────┬───────────┘
                       ▼
              Finalizer Agent
              (cross-checks both reviews, produces final summary)
```

**Coding Problem Crew**: Same pattern, but the Validator Agent actually **compiles and runs** reference solutions against generated test cases inside a network-disabled Docker container (Python, Java, C, C++, Bash).

#### 4. Tool Layer (`klyra/tools/`)

Agents use these tools to interact with the outside world:

| Tool | Purpose |
|---|---|
| `SyllabusSearchTool` | Semantic search over indexed syllabus content (Qdrant) |
| `PyqSearchTool` | Semantic search over past-year questions (Qdrant) |
| `FetchPyqChunksTool` | Retrieve full PYQ content by metadata |
| `SearchImagesTool` | Find extracted diagrams from source documents |
| `ListSourceFilesTool` | List available source files for a subject |
| `LeetCodeSearchTool` | Find related LeetCode problems for coding sets |
| `CodingSourceTool` | Search coding-relevant content |
| `DocumentParserTool` | Gemini multimodal parsing (PDF, slides → structured text) |
| `YouTubeTranscriptTool` | Download video transcripts |
| `ImageExtractionTool` | Extract diagrams from PDFs |
| `CodeExecutionTool` | Run code in sandboxed Docker containers |
| `FormatConversionTool` | Markdown ↔ LaTeX via Pandoc |
| `GenerateDiagramTool` | Generate LaTeX/TikZ diagrams |

#### 5. Storage Layer

| Store | Technology | What it holds |
|---|---|---|
| **Vector Store** | Qdrant | 4 collections per subject: `syllabus`, `pyq`, `parsed`, `images` |
| **LLM** | Gemini (via LiteLLM) | Default: `gemini-3.1-flash-lite` (configurable via `KLYRA_LLM_MODEL`) |
| **Embeddings** | Gemini Embedding | `gemini-embedding-2` (or OpenAI `text-embedding-3-small`) |
| **Job Store** | SQLite / PostgreSQL | Job records: status, result, error, timestamps |
| **Chat Sessions** | `.db` (JSON) / PostgreSQL | Local chat history is stored in lightweight `.db/sessions/` JSON files. In production, this shifts to PostgreSQL to handle concurrent users (see SaaS section). |
| **Queue** | Redis | Celery broker + result backend (API mode only) |
| **Filesystem** | `docs/<subject>/` | Source materials (`week_1/`–`week_12/`, `pyq/`, `additional/`) and generated outputs (`generated/`) |
| **Code Sandbox** | Docker | Network-disabled containers for code validation |

### Voice Architecture

```text
klyra voice
    │
    ├─ AudioTransport (abstract interface)
    │   └─ PyAudioTransport (CLI — mic/speaker via PortAudio)
    │       ├─ Mic input:     16 kHz, 16-bit PCM, mono
    │       └─ Speaker output: 24 kHz, 16-bit PCM, mono
    │
    ├─ VoiceOrchestrator (transport-agnostic core)
    │   ├─ google-genai SDK → client.aio.live.connect() [WebSocket]
    │   ├─ System instruction with Klyra persona
    │   ├─ 10 function declarations (all CLI features)
    │   ├─ 3 concurrent async tasks:
    │   │   ├─ _stream_input()  — mic → Gemini
    │   │   ├─ _stream_output() — Gemini → speaker
    │   │   └─ _handle_tools()  — function calls → service layer
    │   └─ Auto-reconnect on session timeout (~10-15 min)
    │
    └─ ToolExecutor
        ├─ Receives function_call from Gemini
        ├─ Runs in asyncio.to_thread() (CrewAI is synchronous)
        └─ Returns result for Gemini to summarise verbally
```

The transport abstraction means the same orchestrator can later be plugged into a WebSocket or LiveKit transport for a web UI — no changes to tool logic or session management needed.

### Project Structure

```text
src/klyra/
├── cli.py                    # CLI entry point (argparse, 11 subcommands)
├── celery_app.py             # Celery configuration
├── worker.py                 # Celery task definitions
├── llm_config.py             # LLM + embedder factory
├── api/
│   ├── main.py               # FastAPI app (CORS, auth, rate limiting)
│   ├── dependencies.py       # API key auth + rate limiter
│   └── routes/               # 9 route modules (one per feature)
├── services/
│   ├── ingestion_service.py
│   ├── generation_service.py
│   ├── revision_service.py
│   ├── coding_problem_service.py
│   ├── teaching_material_service.py
│   ├── conversion_service.py
│   ├── transcript_service.py
│   ├── job_store.py          # Persistent job tracking (SQLAlchemy)
│   └── results_service.py
├── crews/
│   ├── generation_crew/      # QP creation (4 agents, parallel review)
│   ├── revision_crew/        # QP revision
│   ├── coding_problem_crew/  # Coding problems (Docker-validated)
│   ├── coding_revision_crew/ # Coding revision
│   ├── ingestion_crew/       # Document parsing + indexing
│   ├── teaching_material_crew/
│   ├── teaching_revision_crew/
│   └── format_converter_crew/
├── tools/                    # 13 agent tools (RAG, parsing, execution)
├── models/                   # Pydantic request/response models
├── utils/
│   ├── fs.py                 # Filesystem conventions + validation
│   └── parsing.py            # CLI argument parsing utilities
└── voice/
    ├── orchestrator.py       # Gemini Live session manager
    ├── transport.py          # Abstract transport + PyAudio impl
    ├── tool_declarations.py  # Function schemas (all 10 features)
    └── tool_executor.py      # Function call → service dispatch
```

## Setup & Execution Guide

You can run Klyra either natively on your local machine, or inside Docker containers.

---

### Option 1: Running with Docker (Recommended for Production)

Docker Compose is the easiest way to spin up the entire cluster, complete with the API, background worker, Redis broker, PostgreSQL database, and Qdrant vector store.

1. **Configure Environment**
   Create a `.env` file in the root directory (you can copy `.env.example` if it exists) and fill in your API keys:
   ```env
   GEMINI_API_KEY=your-api-key
   OPENAI_API_KEY=your-api-key # Optional, if you use OpenAI for embeddings
   KLYRA_API_KEY=your-secret-auth-token
   ```

2. **Start the Cluster**
   ```bash
   docker compose up -d --build
   ```
   This command pulls the necessary databases and builds the Python environments for the API and worker. 
   
   *(Note: The first time you run this, it will take several minutes to download heavy AI dependencies like `crewai` and `langchain`. Future builds are almost instant thanks to layer caching!)*

3. **Check the API**
   Once running, you can view the interactive API documentation at:
   - http://localhost:8000/docs
   
4. **View Logs**
   To see what the background AI workers are doing:
   ```bash
   docker compose logs -f worker
   ```

---

### Option 2: Running Locally (Without Docker)

If you prefer to run the Python processes directly on your host machine (great for active development), you can run Klyra natively. **You still need Redis, Postgres, and Qdrant running somewhere (or via Docker) for the backend to function fully.**

1. **Start the backing services**
   Even without dockerizing the Python app, you should start the databases:
   ```bash
   docker run -d -p 6379:6379 redis:7-alpine
   docker run -d -p 6333:6333 qdrant/qdrant:latest
   docker run -d -p 5432:5432 -e POSTGRES_USER=klyra -e POSTGRES_PASSWORD=klyrapass -e POSTGRES_DB=klyradb postgres:15-alpine
   ```

2. **Install Python Dependencies**
   Ensure you have Python 3.10 or 3.11 installed. Create a virtual environment and install the package:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

3. **Run the FastAPI Server**
   In your first terminal window, start the API:
   ```bash
   klyra serve
   # or
   uvicorn klyra.api.main:app --host 127.0.0.1 --port 8000 --reload
   ```

4. **Run the Celery Worker**
   In a *second* terminal window, start the background worker that actually executes the AI tasks:
   ```bash
   source .venv/bin/activate
   celery -A klyra.celery_app worker --loglevel=info
   ```

## Local CLI

Klyra comes with a rich local CLI (`klyra`). You can use this to execute tasks synchronously without starting the API server!

```bash
# Ingest course materials
klyra ingest --subject python-basics --week 1

# Generate a question paper synchronously
klyra generate --subject python-basics --qp-name midterm \
    --scope "week_1-week_4" --difficulty medium --type-counts "MCQ:5,MSQ:3,NAT:2"

# Generate coding problems
klyra code --subject python-basics --name lab-3 --language python \
    --scope week_3 --difficulty medium --num-questions 5 \
    --public-tests 3 --private-tests 5

# Prepare teaching material
klyra teach --subject python-basics --session-name week-4-live \
    --scope week_4 --notes "45 min, focus on intuition"

# Delete accidentally ingested files from Qdrant
klyra delete-index --subject python-basics \
    --source-file "/absolute/path/to/docs/python-basics/week_1/bad_file.pdf"
```

## Voice CLI

Klyra includes a conversational voice interface powered by the **Gemini Live API**. Instead of memorising CLI flags, you speak naturally and Klyra asks follow-up questions to gather the information it needs.

### Setup

```bash
# 1. Install PortAudio system library
sudo apt install portaudio19-dev          # Debian/Ubuntu
# brew install portaudio                  # macOS

# 2. Install Python voice dependencies
pip install -e ".[voice]"

# 3. Add voice config to .env (optional — defaults shown)
KLYRA_VOICE_MODEL=gemini-3-flash-live     # Gemini Live model
KLYRA_VOICE_NAME=Puck                     # Voice: Puck, Charon, Kore, Fenrir, Aoede, Leda, Orus, Zephyr
```

### Usage

```bash
klyra voice                    # Start with default voice
klyra voice --voice Kore       # Use a specific voice
```

### Example Conversation

```
$ klyra voice

  🎙  Klyra voice ready.  (model=gemini-3-flash-live, voice=Puck)
  Speak naturally. Press Ctrl+C to exit.

You:   "Create a question paper for DBMS."
Klyra: "Sure! What should I name this paper?"
You:   "Midterm."
Klyra: "Which weeks should I cover?"
You:   "Weeks one through four."
Klyra: "What difficulty level?"
You:   "Medium."
Klyra: "What types of questions and how many? For example, MCQ 5, MSQ 3."
You:   "Five MCQs and five MSQs."
Klyra: "Got it. I'll generate a medium-difficulty midterm for DBMS covering
        weeks 1-4 with 5 MCQs and 5 MSQs. Shall I proceed?"
You:   "Yes."

  ⚙  Executing: generate_question_paper...
  ✓  generate_question_paper completed.

Klyra: "Done! The question paper has been generated."
```

### Available Voice Commands

All CLI features are available through voice:

| Voice intent | What it does |
|---|---|
| "Ingest materials for data structures" | `klyra ingest --subject data-structures` |
| "Create a question paper" | `klyra generate ...` |
| "Revise the midterm paper" | `klyra revise ...` |
| "Convert to LaTeX" | `klyra convert ...` |
| "Prepare teaching material" | `klyra teach ...` |
| "Revise the teaching material" | `klyra revise-teach ...` |
| "Get a transcript from YouTube" | `klyra transcript ...` |
| "Generate coding problems" | `klyra code ...` |
| "Revise the coding problems" | `klyra revise-code ...` |
| "Delete index entries" | `klyra delete-index ...` |

### WSL Note

On WSL2, audio requires PulseAudio/PipeWire forwarding from the Windows host. Windows 11 with WSLg should work out of the box — run `pactl info` to check. If audio isn't working, `klyra voice` will print detailed setup instructions.

## Roadmap: SaaS API Monetization

If you plan to transition Klyra from a single-tenant tool to a **paid SaaS product**, you will need to replace the current single-admin authentication (`KLYRA_API_KEY`) and IP-based rate limiting (`slowapi`) with a robust, multi-tenant API key and quota system.

Below is the architectural plan to implement user-based API keys with per-service usage limits.

### 1. Database Schema Updates & Storage Choices (Postgres vs NoSQL)
Move beyond the current `jobs` table and local `.db` JSON session files by adding user identity, billing tracking, and session tracking.

**Why PostgreSQL for Production?**
While NoSQL databases (like MongoDB) are often praised for chat applications due to their document-oriented flexibility, **PostgreSQL is heavily recommended for Klyra in production.**
*   **JSONB Support:** Postgres offers robust `JSONB` columns, giving you the exact same schema-less flexibility as NoSQL for storing varying Gemini message histories.
*   **Relational Integrity:** Unlike pure NoSQL, Postgres allows you to enforce strict relational integrity for your SaaS elements (Users, Subscriptions, Workspaces, Quotas, and API Keys).
*   **Simplified Stack:** Since Klyra already uses Postgres/SQLite for its Job queue via SQLAlchemy, adding a `sessions` table to Postgres keeps your infrastructure footprint small and highly cohesive.

**Proposed Tables:**
- **`users`**: `id`, `email`, `stripe_customer_id`, `subscription_tier` (e.g., Free, Pro, Enterprise).
- **`api_keys`**: `id`, `user_id`, `key_prefix`, `hashed_key`.
- **`sessions`**: `id`, `user_id`, `title`, `history` (JSONB column containing the `ChatOrchestrator` interaction history).

### 2. Redis Quota Tracking (Token Bucket)
Since Klyra already uses Redis for Celery, leverage it to track API limits quickly without overwhelming Postgres.

- **Storage Strategy**: Use Redis counters with monthly expirations. 
  - Key format: `quota:{user_id}:generate_qp:2026-08`
- **Tier Limits**: Define limits per tier in code or DB.
  - *Free*: 5 Question Papers/mo, 2 Coding Sets/mo.
  - *Pro*: 50 Question Papers/mo, 50 Coding Sets/mo.

### 3. FastAPI Dependency Changes (`dependencies.py`)
Replace the current `require_api_key` and `slowapi` setup with a custom middleware/dependency pipeline:

1. **Authentication**: 
   - Extract `X-API-Key` from the header.
   - Hash the key and look it up in the `api_keys` table.
   - Attach the resulting `User` object to `request.state.user`.
2. **Quota Verification**:
   - Create a new dependency: `def require_quota(service: str, cost: int = 1)`
   - Before a route (e.g., `/api/v1/generate`) executes, check the user's tier limits against their current Redis usage counter.
   - If `usage + cost > limit`, raise `HTTP 429 Too Many Requests`.
3. **Deduction**:
   - If the quota check passes, increment the Redis counter.

### 4. Billing Integration (Stripe / LemonSqueezy)
- **Checkout**: When a user subscribes, generate their API key and show it to them *once*.
- **Webhooks**: Expose a `/webhooks/stripe` endpoint. When a `customer.subscription.updated` event arrives, update the user's `subscription_tier` in the database. The Redis quota checks will immediately reflect the new, higher limits.

### 5. Securing the Job Queue
Currently, jobs don't track ownership. Update the `JobRecord` schema to include a `user_id`. When users poll the `/api/v1/jobs/{job_id}` endpoint or attempt to fetch generated files, verify that `request.state.user.id == job.user_id` to prevent users from accessing others' generated materials.
