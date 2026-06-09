import json
import math
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from embedding_utils import load_embedding_model
from demo_data import (
    get_demo_search_results as search_mock,
    get_demo_stats as get_demo_stats_mock,
    get_demo_status as get_demo_status_mock,
    get_demo_product as get_demo_product_mock,
    get_demo_filters as get_demo_filters_mock,
)
from utilities import open_daz_product
from api_tasks import run_update_flow
from managers.managers import chroma_db_manager, daz_pg_analyzer, sqlite_db, daz_script_server

import logging
logger = logging.getLogger(__name__)


APP_MODE = os.getenv("APP_MODE", "production")
_SETTINGS_PATH = Path(__file__).parent.parent / "settings.json"
_DIST_PATH = (
    Path(__file__).parent / "ui_dist"                   # installed wheel / PyInstaller
    if (Path(__file__).parent / "ui_dist").exists()
    else Path(__file__).parent.parent / "ui" / "dist"   # development
)

# ── Global update task (single active task; UI polls this) ────────────────────
_current_task: dict = {
    "running": False,
    "progress": "",
    "stage": "",
    "error": None,
    "last_run": None,
}


# ── Settings helpers ──────────────────────────────────────────────────────────

_SETTINGS_DEFAULTS: dict = {
    "cms_host": os.getenv("DB_HOST", "localhost"),
    "cms_port": int(os.getenv("DB_PORT", "17237")),
    "cms_db": os.getenv("DB_NAME", "dzcms"),
    "cms_user": os.getenv("DB_USER", ""),
    "cms_password": os.getenv("DB_PASS", ""),
    "cms_schema": os.getenv("DB_SCHEMA", "dzcontent"),
    "embedding_model": os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5"),
    "query_model": os.getenv("QUERY_MODEL", "BAAI/bge-large-en-v1.5"),
    "daz_script_server_url": os.getenv("DAZ_SCRIPT_SERVER_URL", "http://localhost:18811"),
    "daz_script_server_enabled": os.getenv("DAZ_SCRIPT_SERVER_ENABLED", "false").lower() == "true",
}


def _load_settings() -> dict:
    base = dict(_SETTINGS_DEFAULTS)
    if _SETTINGS_PATH.exists():
        try:
            overrides = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            base.update(overrides)
        except Exception as e:
            logger.warning(f"Could not load settings.json: {e}")
    return base


def _save_settings(data: dict) -> None:
    _SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── Format helpers ─────────────────────────────────────────────────────────────

def _format_product(row: dict) -> dict:
    """Strips internal fields, parses comma-separated fields to lists, normalises shape.

    Safe to call on both full SQLite rows and partial ChromaDB metadata dicts.
    """
    result = {k: v for k, v in row.items() if k not in ("embedding_text",)}
    for field in ("compatible_figures", "subcategories", "tags"):
        raw = result.get(field) or ""
        if not isinstance(raw, list):
            result[field] = [x.strip() for x in raw.split(",") if x.strip()]
    raw_artist = result.get("artist") or ""
    if not isinstance(raw_artist, list):
        result["artist"] = [x.strip() for x in raw_artist.split(",") if x.strip()]
    result["store_url"] = result.pop("url", None) or ""
    result["install_date"] = result.get("enriched_at")
    result["is_installed"] = True
    result["asset_count"] = result.get("asset_count") or 0
    return result


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    if APP_MODE != "demo" and daz_pg_analyzer is None:
        raise RuntimeError(
            "Server is in production mode but PostgreSQL credentials are missing. "
            "Set DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT in your .env file, "
            "or start the server with --demo."
        )
    if APP_MODE == "demo":
        logger.info("Server running in DEMO mode — PostgreSQL not required.")
    load_embedding_model()
    yield


app = FastAPI(
    title=f"Visual Asset Browser API ({APP_MODE.upper()} MODE)",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request models ─────────────────────────────────────────────────────────────

class UpdateRequest(BaseModel):
    force: bool = False


class SearchFilters(BaseModel):
    category: Optional[str] = None
    artist: Optional[str] = None
    compatible_figures: Optional[str] = None
    is_installed: Optional[bool] = None


class UISearchRequest(BaseModel):
    query: str
    filters: Optional[SearchFilters] = None
    max_results: int = 500
    min_relevance: float = 0.0


class QueryRequest(BaseModel):
    """MCP-oriented query model (prompt-based, multi-filter)."""
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


class SettingsPayload(BaseModel):
    cms_host: Optional[str] = None
    cms_port: Optional[int] = None
    cms_db: Optional[str] = None
    cms_user: Optional[str] = None
    cms_password: Optional[str] = None
    cms_schema: Optional[str] = None
    embedding_model: Optional[str] = None
    query_model: Optional[str] = None
    daz_script_server_url: Optional[str] = None
    daz_script_server_enabled: Optional[bool] = None


# ── Utility ────────────────────────────────────────────────────────────────────

update_tasks = {}


def _prune_old_tasks(max_age: timedelta = timedelta(hours=1)) -> None:
    cutoff = datetime.now(timezone.utc) - max_age
    done_statuses = {0, -1}
    stale = [
        tid for tid, t in update_tasks.items()
        if t.get("task_status") in done_statuses
        and datetime.fromisoformat(t["created_at"]) < cutoff
    ]
    for tid in stale:
        del update_tasks[tid]


# ── Status & Update ────────────────────────────────────────────────────────────

@app.get("/api/v1/status")
def get_status():
    """Index counts plus connection health. Polled frequently by the UI."""
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


@app.get("/api/v1/update/status")
def get_update_status_global():
    """Returns the status of the most recently triggered update."""
    return _current_task


@app.get("/api/v1/update/status/{task_id}")
def get_update_status(task_id: str):
    """Returns status for a specific background task (legacy)."""
    task = update_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/api/v1/update", status_code=202)
def start_update(body: UpdateRequest = UpdateRequest(), background_tasks: BackgroundTasks = None):
    """Triggers an incremental (or forced full) re-index."""
    if APP_MODE == "demo":
        raise HTTPException(status_code=403, detail="Update functionality is disabled in demo mode.")

    if _current_task.get("running"):
        return {"message": "Update already running.", "task_id": None}

    _prune_old_tasks()
    task_id = str(uuid.uuid4())
    task_entry = {
        "task_id": task_id,
        "task_status": 1,
        "stage": "start",
        "progress": "Task has been queued.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    update_tasks[task_id] = task_entry

    _current_task.update({
        "running": True,
        "progress": "Starting…",
        "stage": "start",
        "error": None,
        "last_run": datetime.now(timezone.utc).isoformat(),
    })

    def wrapped_run():
        try:
            run_update_flow(_current_task, force=body.force)
        finally:
            _current_task.update({
                "running": False,
                "error": _current_task.get("progress", "") if _current_task.get("task_status") == -1 else None,
            })
            task_entry.update({
                "task_status": _current_task.get("task_status", 0),
                "stage": _current_task.get("stage", ""),
                "progress": _current_task.get("progress", ""),
            })

    background_tasks.add_task(wrapped_run)
    return {"message": "Update process started.", "task_id": task_id}


# ── Browse products ────────────────────────────────────────────────────────────

@app.get("/api/v1/products")
def list_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=10000),
    category: Optional[str] = None,
    artist: Optional[str] = None,
    compatible_figure: Optional[str] = None,
    name_query: Optional[str] = None,
    sort_by: str = "name",
    sort_dir: str = "asc",
):
    """Paginated product listing with optional filters and sort."""
    if APP_MODE == "demo":
        from demo_data import DUMMY_PRODUCTS
        all_products = DUMMY_PRODUCTS
        if category:
            all_products = [p for p in all_products if p["metadata"].get("category") == category]
        if artist:
            all_products = [p for p in all_products if p["metadata"].get("artist") == artist]
        if compatible_figure:
            all_products = [p for p in all_products if compatible_figure in p["metadata"].get("compatible_figures", "")]
        if name_query:
            nq = name_query.lower()
            all_products = [p for p in all_products if nq in p["metadata"].get("name", "").lower()]
        total = len(all_products)
        start = (page - 1) * page_size
        page_products = all_products[start: start + page_size]
        products = [
            {
                "sku": p["id"],
                "name": p["metadata"].get("name"),
                "artist": [p["metadata"].get("artist")] if p["metadata"].get("artist") else [],
                "category": p["metadata"].get("category"),
                "subcategories": [],
                "compatible_figures": [f.strip() for f in p["metadata"].get("compatible_figures", "").split(",") if f.strip()],
                "tags": [t.strip() for t in p["metadata"].get("tags", "").split(",") if t.strip()],
                "store_url": p["metadata"].get("url", ""),
                "image_url": None,
                "is_installed": True,
                "install_date": p["metadata"].get("last_updated"),
                "asset_count": p["metadata"].get("asset_count") or 0,
            }
            for p in page_products
        ]
        import math
        return {"products": products, "total": total, "page": page, "page_size": page_size, "total_pages": max(1, math.ceil(total / page_size))}

    result = sqlite_db.get_products(
        page=page,
        page_size=page_size,
        category=category,
        artist=artist,
        compatible_figure=compatible_figure,
        name_query=name_query,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    result["products"] = [_format_product(r) for r in result["products"]]
    return result


@app.get("/api/v1/products/{sku}")
def get_product(sku: str):
    """Full details for a single product."""
    if APP_MODE == "demo":
        product = get_demo_product_mock(sku)
        if not product:
            raise HTTPException(status_code=404, detail=f"Product '{sku}' not found.")
        return product

    row = sqlite_db.get_sku_row(sku)
    if not row:
        raise HTTPException(status_code=404, detail=f"Product '{sku}' not found in local index.")
    return _format_product(row)


@app.get("/api/v1/products/{sku}/assets")
def get_product_assets(sku: str):
    """Asset files for a product."""
    return get_assets(sku)


@app.post("/api/v1/products/{sku}/open", status_code=200)
def open_product(sku: str):
    """Navigates the DAZ Studio Content Library to the product.

    Uses the DAZ Script Server plugin when available; falls back to subprocess launch.
    """
    row = sqlite_db.get_sku_row(sku)
    if not row:
        raise HTTPException(status_code=404, detail=f"Product '{sku}' not found.")
    product_name = row.get("name") or sku

    if APP_MODE != "demo" and daz_script_server.is_available():
        try:
            result = daz_script_server.browse_to_product(product_name)
            return {"success": True, "message": f"Opened '{product_name}' via DAZ Script Server.", "via": "plugin", "detail": result}
        except Exception as e:
            logger.warning(f"DAZ Script Server browse failed, falling back to subprocess: {e}")

    # Subprocess fallback
    success = open_daz_product(args=type("obj", (object,), {"product": product_name}))
    if not success:
        raise HTTPException(status_code=500, detail="Failed to open product in DAZ Studio.")
    return {"success": True, "message": f"Opened '{product_name}' via subprocess.", "via": "subprocess"}


# ── Filters ────────────────────────────────────────────────────────────────────

@app.get("/api/v1/filters")
def get_filters():
    """All valid filter values (categories, artists, compatible figures)."""
    if APP_MODE == "demo":
        return get_demo_filters_mock()
    return sqlite_db.get_filter_values()


# ── Search ─────────────────────────────────────────────────────────────────────

@app.post("/api/v1/search")
def run_search(request: UISearchRequest):
    """UI/vector semantic search. Accepts { query, filters, max_results, min_relevance }.

    Returns all matching results up to max_results in a single response. Pagination is
    left to the client — this avoids the unstable total_hits that occurs when the candidate
    pool grows with each page request.
    """
    logger.info(f"Search: query={request.query!r} filters={request.filters}")
    if APP_MODE == "demo":
        raw = search_mock(prompt=request.query, limit=request.max_results)
        results = [
            {
                "sku": r["id"],
                "name": r["metadata"].get("name"),
                "artist": [r["metadata"].get("artist")] if r["metadata"].get("artist") else [],
                "category": r["metadata"].get("category"),
                "subcategories": [],
                "compatible_figures": [f.strip() for f in r["metadata"].get("compatible_figures", "").split(",") if f.strip()],
                "tags": [t.strip() for t in r["metadata"].get("tags", "").split(",") if t.strip()],
                "store_url": r["metadata"].get("url", ""),
                "image_url": None,
                "is_installed": True,
                "install_date": r["metadata"].get("last_updated"),
                "last_updated": r["metadata"].get("last_updated"),
                "relevance_score": r.get("relevance_score"),
                "asset_count": r["metadata"].get("asset_count") or 0,
            }
            for r in raw.get("results", [])
        ]
        return {"results": results, "total": len(results), "query": request.query, "took_ms": 0}

    f = request.filters or SearchFilters()
    raw = chroma_db_manager.search(
        prompt=request.query,
        limit=request.max_results,
        offset=0,
        max_results=request.max_results,
        categories=[f.category] if f.category else None,
        artists=[f.artist] if f.artist else None,
        compatible_figures=[f.compatible_figures] if f.compatible_figures else None,
        score_threshold=1.0,
        sort_by="relevance",
        sort_order="descending",
    )
    results = [
        _format_product({**r.get("metadata", {}), "relevance_score": r.get("relevance_score")})
        for r in raw.get("results", [])
    ]
    if request.min_relevance > 0:
        results = [r for r in results if r.get("relevance_score", 0) >= request.min_relevance]
    return {"results": results, "total": len(results), "query": request.query, "took_ms": raw.get("took_ms", 0)}


@app.post("/api/v1/query")
def run_query(request: QueryRequest):
    """MCP-oriented semantic search (prompt-based, full filter set)."""
    logger.info(f"Query: {request}")
    result = (
        search_mock(**request.model_dump())
        if APP_MODE == "demo"
        else chroma_db_manager.search(**request.model_dump())
    )
    return result


# ── Settings ───────────────────────────────────────────────────────────────────

@app.get("/api/v1/settings")
def get_settings():
    """Returns the current application settings (env + settings.json overlay)."""
    s = _load_settings()
    s.pop("cms_password", None)
    return s


@app.put("/api/v1/settings")
def save_settings(payload: SettingsPayload):
    """Saves settings to settings.json, overlaying the env defaults."""
    existing = {}
    if _SETTINGS_PATH.exists():
        try:
            existing = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    existing.update(updates)
    _save_settings(existing)
    return _load_settings()


@app.post("/api/v1/settings/test-db")
def test_db_connection(payload: SettingsPayload):
    """Tests a PostgreSQL connection with the provided credentials."""
    try:
        import psycopg2
        s = _load_settings()
        s.update({k: v for k, v in payload.model_dump().items() if v is not None})
        t0 = datetime.now()
        conn = psycopg2.connect(
            host=s["cms_host"],
            port=s["cms_port"],
            dbname=s["cms_db"],
            user=s["cms_user"],
            password=s.get("cms_password", ""),
            connect_timeout=5,
        )
        conn.close()
        ms = int((datetime.now() - t0).total_seconds() * 1000)
        return {"success": True, "message": f"Connected in {ms}ms", "latency_ms": ms}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ── DAZ Studio plugin status ────────────────────────────────────────────────────

@app.get("/api/v1/daz-studio/status")
def get_daz_studio_status():
    """Probes the DAZ Script Server plugin to check if it's running."""
    if APP_MODE == "demo":
        return {"plugin_detected": False, "plugin_url": None, "version": None}
    return daz_script_server.status()


@app.get("/api/v1/daz-studio/content-dirs")
def get_daz_content_dirs(refresh: bool = False):
    """Returns content library directories from the running DAZ Studio instance."""
    if APP_MODE == "demo":
        return {"dirs": [], "source": "demo"}
    if not daz_script_server.is_available():
        raise HTTPException(status_code=503, detail="DAZ Script Server plugin is not running.")
    dirs = daz_script_server.get_content_directories(force=refresh)
    return {"dirs": dirs, "count": len(dirs)}


class LoadAssetRequest(BaseModel):
    path: str


@app.post("/api/v1/scene/load")
def load_asset_into_scene(body: LoadAssetRequest):
    """Loads an asset file into the current DAZ Studio scene via the DAZ Script Server plugin."""
    if APP_MODE == "demo":
        return {"success": True, "message": "Demo mode — no action taken."}
    if not daz_script_server.is_available():
        raise HTTPException(status_code=503, detail="DAZ Script Server plugin is not running.")
    try:
        result = daz_script_server.load_asset(body.path)
        return {"success": True, "message": f"Loaded '{body.path}' into scene.", "detail": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Legacy / info ──────────────────────────────────────────────────────────────

@app.get("/api/v1/content-roots")
def get_content_roots():
    if APP_MODE == "demo":
        return {"content_roots": []}
    roots = daz_pg_analyzer.get_content_roots()
    return {"content_roots": roots}


@app.get("/api/v1/assets/{sku}")
def get_assets(sku: str):
    """Asset file paths for a SKU, with resolved absolute paths where available."""
    if APP_MODE == "demo":
        return {"sku": sku, "files": []}

    files = daz_pg_analyzer.get_asset_files_by_sku(sku)
    if files is None:
        raise HTTPException(status_code=500, detail="Database error fetching asset files.")

    pg_roots = daz_pg_analyzer.get_content_roots()
    plugin_roots = daz_script_server.get_content_directories() if daz_script_server.is_available() else []
    # Deduplicate while preserving order: PostgreSQL roots first, then any new plugin roots
    seen: set[str] = set()
    all_roots: list[str] = []
    for r in pg_roots + plugin_roots:
        if r and r not in seen:
            seen.add(r)
            all_roots.append(r)

    for f in files:
        relative = Path(f["path"].lstrip("/")) / f["filename"]
        f["resolved_path"] = None
        for root in all_roots:
            candidate = Path(root) / relative
            if candidate.exists():
                f["resolved_path"] = candidate.as_posix()
                break

    return {"sku": sku, "files": files}


@app.get("/api/v1/browseproduct/{product_id}")
def browse_product(product_id: str):
    if APP_MODE == "demo":
        return {"status": "ok"}
    row = sqlite_db.get_sku_row(product_id)
    product_name = (row.get("name") if row else None) or product_id
    success = open_daz_product(args=type("obj", (object,), {"product": product_name}))
    if not success:
        raise HTTPException(status_code=500, detail="Failed to open product in DAZ Studio.")
    return {"success": True, "message": f"Opened '{product_name}' in Content Library."}


@app.get("/api/v1/info")
def get_info():
    """Full stats including filter lists (for MCP/dashboard use)."""
    if APP_MODE == "demo":
        logger.info("Demo mode: returning mock database stats.")
        return get_demo_stats_mock()

    total_docs = chroma_db_manager.collection.count()
    if total_docs == 0:
        raise HTTPException(status_code=404, detail="Database collection not found or empty.")

    filter_values = sqlite_db.get_filter_values()
    last_update = sqlite_db.get_last_updated()
    postgres_count = daz_pg_analyzer.count_skus()
    sqlite_count = sqlite_db.count()

    logger.info(
        f"/info: postgres={postgres_count}, sqlite={sqlite_count}, "
        f"chromadb={total_docs}, "
        f"new_products={max(0, postgres_count - total_docs)}"
    )

    return {
        "total_docs": total_docs,
        "last_update": last_update,
        "histograms": {
            "categories": filter_values["categories"],
            "artists": filter_values["artists"],
            "compatible_figures": filter_values["compatible_figures"],
        },
        "total_products_postgres": postgres_count,
        "total_products_sqlite": sqlite_count,
        "new_products": max(0, postgres_count - total_docs),
    }


# ── Asset file helpers ─────────────────────────────────────────────────────────

@app.get("/api/v1/assets/thumbnail")
def get_asset_thumbnail(path: str):
    """Serves the companion thumbnail PNG for a DAZ asset file.

    DAZ Studio places thumbnails alongside content files with the same stem
    and a .png extension (e.g. 'FN Ethan.duf' → 'FN Ethan.png').
    Returns 404 when no thumbnail is found so the UI can show its placeholder.
    """
    asset = Path(path)
    candidates = [
        asset.with_suffix(".png"),
        asset.parent / (asset.name + ".png"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return FileResponse(str(candidate), media_type="image/png")
    raise HTTPException(status_code=404, detail="No thumbnail found for this asset.")


class RevealRequest(BaseModel):
    path: str


@app.post("/api/v1/files/reveal")
def reveal_in_explorer(body: RevealRequest):
    """Opens the system file explorer with the given file selected."""
    target = Path(body.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {body.path}")
    try:
        if sys.platform == "win32":
            # shell=True so Windows parses /select,<path> as a single argument;
            # quoting the path handles spaces without confusing Explorer's parser.
            subprocess.Popen(f'explorer /select,"{target}"', shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Serve built UI (production) ────────────────────────────────────────────────
if _DIST_PATH.exists():
    app.mount("/", StaticFiles(directory=str(_DIST_PATH), html=True), name="ui")
