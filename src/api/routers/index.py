"""Status, health, info and index jobs."""
from fastapi import APIRouter, HTTPException, Query

from api.deps import AUTH, READ, ServicesDep
from api.schemas import (CancelResult, HealthResponse, InfoResponse, JobList, StatusResponse,
                         UpdateRequest, UpdateStarted, UpdateStatus)

router = APIRouter(prefix="/api/v1")


@router.get("/status", dependencies=READ, response_model=StatusResponse)
def get_status(services: ServicesDep):
    """Index counts plus connection health. Polled frequently by the UI.

    The legacy top-level keys are kept (deprecated); ``index`` carries the
    reconciliation-backed detail (see catalog.status.index_status).
    """
    return services.status.status()


@router.get("/health", dependencies=READ, response_model=HealthResponse)
def get_health(services: ServicesDep):
    """Per-component health with repair guidance. `status` is "ok" or "degraded"; the API
    keeps serving what it can (browse, settings, jobs) while a component is down."""
    return services.status.health()


@router.get("/info", dependencies=READ, response_model=InfoResponse)
def get_info(services: ServicesDep):
    """Totals and filter values (for MCP/dashboard use)."""
    return services.status.info()


@router.get("/update/status", dependencies=READ, response_model=UpdateStatus)
def get_update_status_global(services: ServicesDep):
    """Status of the active index job, or else the most recent one. Polled by the UI."""
    return services.jobs.update_status()


@router.get("/update/status/{task_id}", dependencies=READ, response_model=UpdateStatus)
def get_update_status(task_id: str, services: ServicesDep):
    """Status of a specific index job."""
    view = services.jobs.task(task_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return view


@router.get("/jobs", dependencies=READ, response_model=JobList)
def list_jobs(services: ServicesDep, limit: int = Query(20, ge=1, le=200)):
    """Recent index jobs, newest first, including failed and cancelled ones."""
    return {"jobs": services.jobs.recent(limit)}


@router.post("/update", status_code=202, dependencies=AUTH, response_model=UpdateStarted)
def start_update(services: ServicesDep, body: UpdateRequest = UpdateRequest()):
    """Queues an index job and starts a worker for it. One job runs at a time across the
    API and the CLI; while one is active this returns its id instead of starting another."""
    return services.jobs.start(body.mode, body.force)


@router.delete("/update", dependencies=AUTH, response_model=CancelResult)
def cancel_update(services: ServicesDep):
    """Cancels the active index job: at once if still queued, else at the worker's next checkpoint."""
    return services.jobs.cancel()
