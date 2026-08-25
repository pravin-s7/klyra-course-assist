"""HTTP API for Klyra (production entry point).

Run with: `uvicorn klyra.api.main:app` (or `klyra serve`).

Routes call services/, exactly like cli.py does - see approach.md. This
package owns HTTP concerns only: request/response shapes (models/),
job bookkeeping (services/job_store.py), auth, and rate limiting
(dependencies.py).
"""
