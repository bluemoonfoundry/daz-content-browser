import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from api.app_state import APP_MODE
from api.contracts import (
    ChromaDbManagerProtocol,
    DazPgAnalyzerProtocol,
    SQLiteDbProtocol,
    UpdateTaskServiceProtocol,
)
from api.dependencies import (
    get_chroma_db_manager,
    get_daz_pg_analyzer,
    get_sqlite_db,
    get_update_task_service,
)
from api.schemas.models import UpdateRequest
from demo_data import get_demo_status as get_demo_status_mock

router = APIRouter()


@router.get("/api/v1/status")
def get_status(
    chroma_db_manager: ChromaDbManagerProtocol = Depends(get_chroma_db_manager),
    sqlite_db: SQLiteDbProtocol = Depends(get_sqlite_db),
    daz_pg_analyzer: DazPgAnalyzerProtocol | None = Depends(get_daz_pg_analyzer),
):
    if APP_MODE == "demo":
        return get_demo_status_mock()

    chromadb_count = chroma_db_manager.collection.count()
    sqlite_count = sqlite_db.count()
    postgres_count = daz_pg_analyzer.count_skus() if daz_pg_analyzer else -1
    new_products = max(0, postgres_count - chromadb_count)
    return {
        "status": "ok",
        "postgres_connected": postgres_count >= 0,
        "sqlite_connected": sqlite_count >= 0,
        "chromadb_connected": True,
        "postgres_count": postgres_count,
        "sqlite_count": sqlite_count,
        "chromadb_count": chromadb_count,
        "new_products": new_products,
        "update_available": new_products > 0,
    }


@router.get("/api/v1/update/status")
def get_update_status_global(
    update_task_service: UpdateTaskServiceProtocol = Depends(get_update_task_service),
):
    return update_task_service.get_current()


@router.get("/api/v1/update/status/{task_id}")
def get_update_status(
    task_id: str,
    update_task_service: UpdateTaskServiceProtocol = Depends(get_update_task_service),
):
    task = update_task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/api/v1/update", status_code=202)
def start_update(
    background_tasks: BackgroundTasks,
    body: UpdateRequest = UpdateRequest(),
    update_task_service: UpdateTaskServiceProtocol = Depends(get_update_task_service),
):
    if APP_MODE == "demo":
        raise HTTPException(status_code=403, detail="Update functionality is disabled in demo mode.")

    if update_task_service.is_running():
        return {"message": "Update already running.", "task_id": None}

    update_task_service.prune_old()
    task_id = str(uuid.uuid4())
    task_entry = update_task_service.create_task(task_id)

    def wrapped_run():
        from api_tasks import run_update_flow

        try:
            run_update_flow(task_entry, force=body.force)
        finally:
            update_task_service.finish_from_task_entry(task_entry)

    background_tasks.add_task(wrapped_run)
    return {"message": "Update process started.", "task_id": task_id}
