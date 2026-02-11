import os
import uuid
from typing import List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from demo_data import get_demo_search_results as search_mock
from demo_data import get_demo_stats as get_demo_stats_mock
from utilities import open_daz_product
from api_tasks import run_update_flow
from managers.managers import chroma_db_manager

import logging
logger = logging.getLogger(__name__)


APP_MODE = os.getenv("APP_MODE", "production")
app = FastAPI(
    title=f"Visual Asset Browser API ({APP_MODE.upper()} MODE)", version="1.0.0"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

update_tasks = {}


class QueryRequest(BaseModel):
    """Model for the query request payload."""
    prompt: str
    limit: int = 10
    offset: int = 0
    tags: Optional[List[str]] = None
    artists: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    compatible_figures: Optional[List[str]] = None
    score_threshold: float = 1.0
    sort_by: str = "relevance"
    sort_order: str = Field("descending", pattern="^(ascending|descending)$")

@app.get("/")
async def root():
    """Root endpoint to verify the server is running."""
    import logging
    logger = logging.getLogger(__name__)
    print("Hello from the simple FastAPI app!")
    return {"message": "Hello"}    

@app.post("/api/v1/update", status_code=202)
def start_update(background_tasks: BackgroundTasks):
    """Starts the update process in the background."""
    if APP_MODE == "demo":
        raise HTTPException(
            status_code=403, detail="Update functionality is disabled in demo mode."
        )
    task_id = str(uuid.uuid4())
    update_tasks[task_id] = {
        "task_id": task_id,
        "task_status": 1,
        "stage": "start",
        "progress": "Task has been queued.",
    }
    background_tasks.add_task(run_update_flow, update_tasks[task_id])
    return {"message": "Update process started.", "task_id": task_id}

@app.get("/api/v1/update/status/{task_id}")
def get_update_status(task_id: str):
    """Retrieves the status of an ongoing update task."""
    task = update_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@app.post("/api/v1/query")
def run_query(request: QueryRequest):
    """Handles search queries against the ChromaDB vector store.
    
    Args:
        request (QueryRequest): The search parameters.

    Returns:
        dict: The search results.
    """
    print(f"Received query request: {request}")
    print(f"model dump: {request.model_dump()}")
    result = (
        search_mock(**request.model_dump())
        if APP_MODE == "demo"
        else chroma_db_manager.search(**request.model_dump())
    )

    with open('test.json', 'w', encoding='utf-8') as f:
        f.write (f'RECV Result = {result}')
    return result


@app.get("/api/v1/browseproduct/{product_id}")
def browse_product(product_id: str):
    """Opens the specified product in DAZ Studio's Content Library pane.
    
    Args:
        product_id (str): The SKU or identifier of the product to open.
    
    """
    if APP_MODE == "demo":
        return
    open_daz_product(args=type("obj", (object,), {"product": product_id}))


@app.get("/api/v1/info")
def get_info():
    """ Runs the stats command and returns the result as a JSON document, including histograms for filterable fields.
    """


    # --- DEMO MODE CHECK ---
    if APP_MODE == "demo":
        print("--- DEMO MODE: Returning mock database stats. ---")
        # We need to update the demo_data file to return this new structure
        return get_demo_stats_mock()


    # --- PRODUCTION MODE ---
    stats = chroma_db_manager.get_db_stats()
    if stats is None:
        raise HTTPException(
            status_code=404, detail="Database collection not found or empty."
        )

    from managers.managers import daz_pg_analyzer, sqlite_db
    postgres_skus = daz_pg_analyzer.get_all_skus()
    sqlite_skus = sqlite_db.get_all_skus_from_sqlite()
    new_skus = list(set(postgres_skus) - set(sqlite_skus))

    stats["total_products_postgres"] = len(postgres_skus)
    stats["total_products_sqlite"] = len(sqlite_skus)
    stats["new_products"] = len(new_skus)

    # Convert all Counter objects in the histograms to regular dictionaries
    if "histograms" in stats and stats["histograms"]:
        for key, counter in stats["histograms"].items():
            stats["histograms"][key] = dict(counter)


    return stats
