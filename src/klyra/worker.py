from klyra.celery_app import celery_app
from klyra.services import job_store
from klyra.services.ingestion_service import run_ingestion
from klyra.services.generation_service import run_generation
from klyra.services.revision_service import run_revision
from klyra.services.conversion_service import run_conversion
from klyra.services.coding_problem_service import run_coding_generation, run_coding_revision
from klyra.services.teaching_material_service import run_teaching_material, run_teaching_revision
from klyra.services.transcript_service import run_transcript

@celery_app.task
def run_ingestion_task(job_id: str, **kwargs):
    job_store.run_job(job_id, lambda: run_ingestion(**kwargs))

@celery_app.task
def run_generation_task(job_id: str, **kwargs):
    job_store.run_job(job_id, lambda: run_generation(**kwargs))

@celery_app.task
def run_revision_task(job_id: str, **kwargs):
    job_store.run_job(job_id, lambda: run_revision(**kwargs))

@celery_app.task
def run_conversion_task(job_id: str, **kwargs):
    job_store.run_job(job_id, lambda: run_conversion(**kwargs))

@celery_app.task
def run_coding_generation_task(job_id: str, **kwargs):
    job_store.run_job(job_id, lambda: run_coding_generation(**kwargs))

@celery_app.task
def run_coding_revision_task(job_id: str, **kwargs):
    job_store.run_job(job_id, lambda: run_coding_revision(**kwargs))

@celery_app.task
def run_teaching_material_task(job_id: str, **kwargs):
    job_store.run_job(job_id, lambda: run_teaching_material(**kwargs))

@celery_app.task
def run_teaching_revision_task(job_id: str, **kwargs):
    job_store.run_job(job_id, lambda: run_teaching_revision(**kwargs))

@celery_app.task
def run_transcript_fetch_task(job_id: str, **kwargs):
    job_store.run_job(job_id, lambda: run_transcript(**kwargs))
