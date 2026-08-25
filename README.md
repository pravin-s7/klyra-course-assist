# Klyra

Klyra is a multi-agent AI system built on CrewAI that automates course preparation, parsing course materials to generate highly structured question papers, coding problems, and teaching materials.

This repository runs a FastAPI backend with a Celery worker for handling asynchronous AI tasks, backed by PostgreSQL and Qdrant.

## Architecture

Klyra is designed as an asynchronous, event-driven system to handle long-running AI tasks reliably.

```text
                  FastAPI (klyra serve)
                     │
                     ▼
              Job Repository (SQLite/PostgreSQL)
                     │
                     ▼
                Redis Queue (Broker & Backend)
                     │
                     ▼
              Celery Worker Process
                     │
                     ▼
             CrewAI Multi-Agent System
```

1. **Client App / User** sends a request to the FastAPI endpoints.
2. The API creates a job record in the **Job Repository** and dispatches a task to **Redis**.
3. The API immediately returns a `202 Accepted` with a `job_id`.
4. The background **Celery Worker** picks up the task and kicks off the heavy **CrewAI** generation process.
5. Clients can poll the job status or fetch the generated files securely once the task is complete.

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

Klyra also comes with a rich local CLI (`klyra`). You can use this to execute tasks synchronously without starting the API server!

```bash
# Ingest course materials
klyra ingest --subject python-basics --week 1

# Generate a question paper synchronously
klyra generate --subject python-basics --qp-name midterm --scope "week_1-week_4" --difficulty medium --num-questions 10

# Delete accidentally ingested files from Qdrant
klyra delete-index --subject python-basics --source-file "/absolute/path/to/docs/python-basics/week_1/bad_file.pdf"
```
