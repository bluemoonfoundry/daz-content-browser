import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional

from contextlib import asynccontextmanager, contextmanager
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import embedding_utils
from catalog.active_index import SearchUnavailable, mcp_response
from catalog.model_config import DEFAULT_MODEL_ID, SETTINGS_PATH, configured_model_id
from catalog.status import index_status, migration_hint, pending_cms_diff, rebuild_hint
from catalog.store import CMS_SOURCE
from embedding_utils import load_embedding_model
from demo_data import (
    get_demo_search_results as search_mock,
    get_demo_stats as get_demo_stats_mock,
    get_demo_status as get_demo_status_mock,
    get_demo_product as get_demo_product_mock,
    get_demo_filters as get_demo_filters_mock,
)
from utilities import open_daz_product
from index_worker import spawn_worker, worker_log_path
from managers.chroma_db_manager import VectorStoreUnavailable
from managers.job_store import FAILED, QUEUED, RUNNING, JobConflict, public_view
from managers.managers import active_index, catalog, daz_pg_analyzer, daz_script_server, generations, job_store
from security import (
    SESSION_COOKIE,
    allowed_origins,
    get_api_token,
    host_origin_guard,
    require_read_access,
    require_token,
    resolve_within_roots,
)

import logging
logger = logging.getLogger(__name__)


APP_MODE = os.getenv("APP_MODE", "production")
_SETTINGS_PATH = SETTINGS_PATH
_DIST_PATH = (
    Path(__file__).parent / "ui_dist"                   # installed wheel / PyInstaller
    if (Path(__file__).parent / "ui_dist").exists()
    else Path(__file__).parent.parent / "ui" / "dist"   # development
)


# ── Settings helpers ──────────────────────────────────────────────────────────

_SETTINGS_DEFAULTS: dict = {
    "cms_host": os.getenv("DB_HOST", "localhost"),
    "cms_port": int(os.getenv("DB_PORT", "17237")),
    "cms_db": os.getenv("DB_NAME", "dzcms"),
    "cms_user": os.getenv("DB_USER", ""),
    "cms_password": os.getenv("DB_PASS", ""),
    "cms_schema": os.getenv("DB_SCHEMA", "dzcontent"),
    "embedding_model": os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL_ID),
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


def _public_settings() -> dict:
    """Settings safe to return to clients — never includes the CMS password."""
    s = _load_settings()
    s["cms_password_set"] = bool(s.pop("cms_password", None))
    return s


# ── Content roots ──────────────────────────────────────────────────────────────

_ROOTS_TTL_SECONDS = 60.0
_roots_cache: tuple[float, list[str]] | None = None


def _content_roots() -> list[str]:
    """Registered content roots (DAZ CMS first, then the running DAZ Studio's library dirs).

    Cached briefly: thumbnail requests arrive in bursts and each would otherwise hit PostgreSQL.
    """
    global _roots_cache
    if APP_MODE == "demo":
        return []
    now = time.monotonic()
    if _roots_cache and now - _roots_cache[0] < _ROOTS_TTL_SECONDS:
        return _roots_cache[1]
    pg_roots = daz_pg_analyzer.get_content_roots() if daz_pg_analyzer else []
    plugin_roots = daz_script_server.get_content_directories() if daz_script_server.is_available() else []
    # Deduplicate while preserving order: PostgreSQL roots first, then any new plugin roots
    roots = list(dict.fromkeys(r for r in pg_roots + plugin_roots if r))
    _roots_cache = (now, roots)
    return roots


def _require_in_roots(path: str) -> Path:
    resolved = resolve_within_roots(path, _content_roots())
    if resolved is None:
        raise HTTPException(status_code=403, detail="Path is outside the registered content roots.")
    return resolved


# ── Catalog guards ─────────────────────────────────────────────────────────────

@contextmanager
def _catalog_reads():
    """Turns an unusable catalog into a 503 with a reason, instead of a bare 500.

    While the catalog migration has failed, product reads are refused outright (startup is
    not blocked; a restart retries the migration). A SQLite error on a read (locked,
    damaged) is reported the same way.
    """
    if catalog.migration_error:
        raise HTTPException(status_code=503, detail={
            "reason": "migration_failed", "repair": migration_hint(catalog.migration_error)})
    try:
        yield
    except sqlite3.Error as e:
        logger.exception("Catalog read failed.")
        raise HTTPException(status_code=503, detail={
            "reason": "catalog_unavailable",
            "repair": f"The local catalog could not be read ({type(e).__name__}: {e}). "
                      f"Retry shortly; if it persists, check the server log."})


_CMS_TTL_SECONDS = 30.0
_cms_cache: tuple | None = None  # (monotonic time, (postgres_count, pending_cms))


def _cms_snapshot() -> tuple:
    """``(postgres_count, pending_cms)`` from the DAZ CMS; ``(-1, None)`` when unreachable.

    ``pending_cms`` is a key-set diff of CMS SKUs against the catalog's present CMS records
    (``{"new", "removed"}``), or None when either side can't be read. Cached briefly: the UI
    polls /status, and each snapshot reads every SKU from PostgreSQL. A stale value only
    delays the "update available" hint by one TTL.
    """
    global _cms_cache
    now = time.monotonic()
    if _cms_cache and now - _cms_cache[0] < _CMS_TTL_SECONDS:
        return _cms_cache[1]
    snapshot = _read_cms_snapshot()
    _cms_cache = (now, snapshot)
    return snapshot


def _read_cms_snapshot() -> tuple:
    if daz_pg_analyzer is None:
        return -1, None
    try:
        count = daz_pg_analyzer.count_skus()
    except Exception:
        logger.warning("DAZ CMS count failed.", exc_info=True)
        return -1, None
    if count is None or count < 0:
        return -1, None
    if catalog.migration_error:
        return count, None
    try:
        skus = daz_pg_analyzer.get_all_skus()
        if count > 0 and not skus:  # get_all_skus() answers [] on a query error
            return count, None
        return count, pending_cms_diff(skus, catalog.present_keys(CMS_SOURCE))
    except Exception:
        logger.warning("Could not diff DAZ CMS SKUs against the catalog.", exc_info=True)
        return count, None


def _index_snapshot() -> dict:
    """Everything /status, /health and /info report about the index, read once."""
    try:
        job_active = job_store.active() is not None
    except Exception:
        logger.warning("Could not read the job store.", exc_info=True)
        job_active = False
    postgres_count, pending = _cms_snapshot()
    configured = configured_model_id()
    idx = index_status(catalog, generations, configured, pending is not None, pending, job_active)

    vector_store = None  # None: no active generation (or no readable catalog)
    if not catalog.migration_error and idx["error"] is None:
        try:
            vector_store = active_index.health()
        except Exception as e:
            vector_store = {"ok": False, "error": f"{type(e).__name__}: {e}",
                            "repair": "Check the server log; `vab index repair` rebuilds the index."}

    if catalog.migration_error:
        repair = migration_hint(catalog.migration_error)
    elif idx["error"]:
        repair = (f"The local catalog could not be read ({idx['error']}). Retry shortly; if it "
                  f"persists, check the server log.")
    elif vector_store is not None and not vector_store["ok"]:
        repair = vector_store.get("repair")
    elif idx["rebuild_required"]:
        repair = rebuild_hint(configured)
    else:
        repair = idx["resume_hint"]
    degraded = bool(catalog.migration_error or idx["error"] or idx["rebuild_required"]
                    or (vector_store is not None and not vector_store["ok"]))
    return {"index": idx, "vector_store": vector_store, "postgres_count": postgres_count,
            "pending": pending, "configured": configured, "repair": repair, "degraded": degraded}


def _vector_count(idx: dict) -> int:
    vectors = idx["vectors"]
    if vectors is None:
        return -1 if (idx["migration_error"] or idx["error"]) else 0
    return vectors["current"] + vectors["stale"]


# ── Format helpers ─────────────────────────────────────────────────────────────

_INTERNAL_FIELDS = ("embedding_text", "doc_hash", "doc_format_version", "distance", "installed")


def _format_product(row: dict) -> dict:
    """Strips internal fields, parses comma-separated fields to lists, normalises shape."""
    result = {k: v for k, v in row.items() if k not in _INTERNAL_FIELDS}
    for field in ("compatible_figures", "subcategories", "tags"):
        raw = result.get(field) or ""
        if not isinstance(raw, list):
            result[field] = [x.strip() for x in raw.split(",") if x.strip()]
    raw_artist = result.get("artist") or ""
    if not isinstance(raw_artist, list):
        result["artist"] = [x.strip() for x in raw_artist.split(",") if x.strip()]
    result["store_url"] = result.pop("url", None) or ""
    result["install_date"] = result.get("enriched_at")
    result["is_installed"] = bool(row["installed"]) if "installed" in row else True
    result["asset_count"] = result.get("asset_count") or 0
    return result


# ── Index jobs ─────────────────────────────────────────────────────────────────
# Jobs live in the persisted job store and run in a separate worker process, so they
# survive API restarts and only one can run across processes (see index_worker.py).

_RESPAWN_SECONDS = 30.0
_last_spawn: float = 0.0


def _spawn_worker() -> None:
    global _last_spawn
    _last_spawn = time.monotonic()
    spawn_worker(job_store)


def _reconcile_jobs() -> None:
    """Recovers jobs whose worker died and makes sure a queued job has a worker.

    Called at startup and on status polls, so a crashed worker is noticed without
    waiting for an API restart. Respawns are rate-limited: a worker that was just
    spawned may simply not have claimed its job yet.
    """
    try:
        recovered = job_store.recover_stale()
        active = job_store.active()
        if active and active["status"] == QUEUED and (
            recovered or time.monotonic() - _last_spawn > _RESPAWN_SECONDS
        ):
            _spawn_worker()
    except Exception:
        logger.exception("Could not reconcile index jobs.")


_TASK_CODES = {QUEUED: 1, RUNNING: 2, FAILED: -1}


def _legacy_task_view(job: dict) -> dict:
    """A job plus the fields the UI and older clients poll (running/progress/error/task_status)."""
    view = public_view(job)
    view.update({
        "task_id": job["id"],
        "task_status": _TASK_CODES.get(job["status"], 0),
        "running": job["status"] in (QUEUED, RUNNING),
        "last_run": view["created_at"],
    })
    if job["status"] != FAILED:
        view["error"] = None
    return view


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    if APP_MODE != "demo" and daz_pg_analyzer is None:
        logger.warning(
            "PostgreSQL credentials not configured — content-roots and asset-file endpoints "
            "will be unavailable. Set DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT in your "
            ".env file, or start the server with --demo, to enable them."
        )
    if APP_MODE == "demo":
        logger.info("Server running in DEMO mode — PostgreSQL not required.")
    get_api_token()  # generate/persist the per-install token at startup so clients can read it
    try:
        load_embedding_model()
    except RuntimeError as exc:
        logger.warning(
            f"[embedding] Model not loaded — search and indexing will be unavailable: {exc}"
        )
    if APP_MODE != "demo":
        if catalog.migration_error:
            logger.error(f"Catalog unavailable — {migration_hint(catalog.migration_error)}")
        else:
            try:
                health = active_index.health()
            except Exception:
                logger.exception("Could not check the active index.")
            else:
                if health is None:
                    logger.warning("No active index generation — search is unavailable. "
                                   + rebuild_hint(configured_model_id()))
                elif not health["ok"]:
                    logger.error(f"Vector store unavailable — search is degraded. {health['repair']}")
        _reconcile_jobs()
    yield


app = FastAPI(
    title=f"Visual Asset Browser API ({APP_MODE.upper()} MODE)",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
# Added last so it runs first: reject foreign Host/Origin before anything else sees the request.
app.middleware("http")(host_origin_guard)


@app.exception_handler(VectorStoreUnavailable)
def vector_store_unavailable(request: Request, exc: VectorStoreUnavailable):
    return JSONResponse(status_code=503, content={"detail": str(exc), "repair": exc.repair})


@app.exception_handler(SearchUnavailable)
def search_unavailable(request: Request, exc: SearchUnavailable):
    return JSONResponse(status_code=503, content={"detail": {"reason": exc.reason, "repair": exc.repair}})

_auth = [Depends(require_token)]
_read = [Depends(require_read_access)]


# ── Auth ───────────────────────────────────────────────────────────────────────

@app.post("/api/v1/auth/session")
def create_session(request: Request, response: Response):
    """Issues the HttpOnly session cookie the browser UI uses (fetches and <img> thumbnails).

    Only browser pages from an allowed origin may call this: the Host/Origin guard has
    already rejected foreign origins, and here the Origin header must be present so a
    cross-site form or <img> without one can't plant the cookie. Non-browser clients use
    `Authorization: Bearer <token>` from the token file instead.
    """
    if not request.headers.get("origin"):
        raise HTTPException(status_code=403, detail="Session requires a browser Origin header.")
    response.set_cookie(
        SESSION_COOKIE,
        get_api_token(),
        httponly=True,
        samesite="strict",
        path="/api/",
    )
    return {"authenticated": True}


# ── Request models ─────────────────────────────────────────────────────────────

class UpdateRequest(BaseModel):
    """`mode`: update (incremental), rebuild (new index generation from SQLite, built beside
    the live one), or repair (quarantine an unopenable store, then rebuild from SQLite).
    `force: true` with no mode re-fetches every product's store data, then rebuilds; it
    never deletes SQLite."""
    force: bool = False
    mode: Optional[Literal["update", "rebuild", "repair"]] = None


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


# ── Status & Update ────────────────────────────────────────────────────────────

@app.get("/api/v1/status", dependencies=_read)
def get_status():
    """Index counts plus connection health. Polled frequently by the UI.

    The legacy top-level keys are kept (deprecated); ``index`` carries the
    reconciliation-backed detail (see catalog.status.index_status).
    """
    if APP_MODE == "demo":
        return get_demo_status_mock()

    snap = _index_snapshot()
    idx, vector_store, pending = snap["index"], snap["vector_store"], snap["pending"]
    postgres_count = snap["postgres_count"]
    sqlite_count = idx["assets"]["total"] if idx["assets"] else -1
    return {
        "status": "degraded" if snap["degraded"] else "ok",
        "postgres_connected": postgres_count >= 0,
        "sqlite_connected": sqlite_count >= 0,
        "chromadb_connected": bool(vector_store and vector_store["ok"]),
        "postgres_count": postgres_count,
        "sqlite_count": sqlite_count,
        "chromadb_count": _vector_count(idx),
        "new_products": pending["new"] if pending else 0,
        "update_available": bool(pending and (pending["new"] or pending["removed"])),
        "repair": snap["repair"],
        "index": idx,
    }


@app.get("/api/v1/health", dependencies=_read)
def get_health():
    """Per-component health with repair guidance. `status` is "ok" or "degraded"; the API
    keeps serving what it can (browse, settings, jobs) while a component is down."""
    if APP_MODE == "demo":
        return {"status": "ok", "mode": "demo", "components": {}}

    snap = _index_snapshot()
    idx, configured = snap["index"], snap["configured"]
    migration_error = catalog.migration_error
    active = idx["active_generation"]

    vector_store = snap["vector_store"]
    if vector_store is None:
        if migration_error or idx["error"]:
            # The catalog can't be read, so whether an index exists is unknown.
            vector_store = {"ok": False, "error": "Index state unknown: the catalog is unavailable.",
                            "repair": snap["repair"]}
        else:
            vector_store = {"ok": False, "error": "No active index generation.",
                            "repair": rebuild_hint(configured)}
    query_model = active["model_id"] if active else configured
    flagged = idx["rebuild_required"] or idx["rebuild_interrupted"] or idx["error"]
    components = {
        "vector_store": vector_store,
        "embedding_model": {"ok": embedding_utils.model_available(query_model),
                            "model_id": query_model,
                            "loaded": embedding_utils.model_loaded(query_model)},
        "catalog": {"ok": migration_error is None, "migration_error": migration_error},
        "index": {"ok": not (idx["rebuild_required"] or idx["error"]),
                  "rebuild_required": idx["rebuild_required"],
                  "rebuild_reason": idx["rebuild_reason"],
                  "rebuild_interrupted": idx["rebuild_interrupted"],
                  "repair": snap["repair"] if flagged else None},
        "sqlite": {"ok": idx["assets"] is not None, "path": catalog.db_path},
        "postgres": {"ok": daz_pg_analyzer is not None},
    }
    if not components["embedding_model"]["ok"]:
        components["embedding_model"]["repair"] = (
            f"Search needs the ONNX model for '{query_model}' (the active index's model), whose "
            f"files are not available locally. Restore it, or rebuild: {rebuild_hint(configured)}"
        )
    if migration_error:
        components["catalog"]["repair"] = migration_hint(migration_error)
    elif idx["error"]:
        components["catalog"].update(ok=False, error=idx["error"], repair=snap["repair"])
    if not components["postgres"]["ok"]:
        components["postgres"]["repair"] = (
            "Configure the DAZ CMS connection in Settings (or .env) and restart; browse and "
            "search still work from the local index."
        )
    if not components["sqlite"]["ok"]:
        components["sqlite"]["repair"] = snap["repair"]
    try:
        _reconcile_jobs()
        latest = job_store.latest()
        components["jobs"] = {
            "ok": not (latest and latest["status"] == FAILED),
            "latest": public_view(latest),
            "worker_log": str(worker_log_path(job_store)),
        }
    except Exception as e:
        components["jobs"] = {"ok": False, "error": str(e)}
    # Postgres is optional for serving; everything else degrades a user-visible feature.
    degraded = any(not c["ok"] for name, c in components.items() if name != "postgres")
    return {"status": "degraded" if degraded else "ok", "mode": APP_MODE, "components": components}


@app.get("/api/v1/update/status", dependencies=_read)
def get_update_status_global():
    """Status of the active index job, or else the most recent one. Polled by the UI."""
    if APP_MODE != "demo":
        _reconcile_jobs()
    latest = job_store.latest()
    if latest is None:
        return {"running": False, "progress": "", "stage": "", "error": None, "last_run": None}
    return _legacy_task_view(latest)


@app.get("/api/v1/update/status/{task_id}", dependencies=_read)
def get_update_status(task_id: str):
    """Status of a specific index job."""
    job = job_store.get(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")
    return _legacy_task_view(job)


@app.get("/api/v1/jobs", dependencies=_read)
def list_jobs(limit: int = Query(20, ge=1, le=200)):
    """Recent index jobs, newest first, including failed and cancelled ones."""
    return {"jobs": [public_view(j) for j in job_store.recent(limit)]}


@app.post("/api/v1/update", status_code=202, dependencies=_auth)
def start_update(body: UpdateRequest = UpdateRequest()):
    """Queues an index job and starts a worker for it. One job runs at a time across the
    API and the CLI; while one is active this returns its id instead of starting another."""
    if APP_MODE == "demo":
        raise HTTPException(status_code=403, detail="Update functionality is disabled in demo mode.")

    if body.mode is None and body.force:
        # Legacy spelling: re-fetch every product's store data, then build a new generation.
        mode, kind, params = "rebuild", "rebuild", {"refetch": True}
    else:
        mode = body.mode or "update"
        kind, params = {
            "update": ("index", {}),
            "rebuild": ("rebuild", {"refetch": False}),  # new generation from SQLite
            "repair": ("repair", {}),
        }[mode]
    try:
        job = job_store.enqueue(kind, params, source="api")
    except JobConflict as e:
        return {"message": "Update already running.", "task_id": e.active["id"] if e.active else None}
    try:
        _spawn_worker()
    except Exception as e:
        # Never leave a queued job that nothing will run: it would block every later update.
        logger.exception("Could not start the index worker.")
        job_store.claim("api", job["id"])
        job_store.finish(job["id"], "api", FAILED, f"Could not start the index worker: {e}", str(e))
        raise HTTPException(status_code=500, detail=f"Could not start the index worker: {e}")
    return {"message": "Update process started.", "task_id": job["id"], "mode": mode}


@app.delete("/api/v1/update", dependencies=_auth)
def cancel_update():
    """Cancels the active index job: at once if still queued, else at the worker's next checkpoint."""
    job = job_store.request_cancel()
    if job is None:
        return {"message": "No update is running."}
    return {"message": "Cancel requested.", "task_id": job["id"], "status": job["status"]}


# ── Browse products ────────────────────────────────────────────────────────────

@app.get("/api/v1/products", dependencies=_read)
def list_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=10000),
    category: Optional[str] = None,
    artist: Optional[str] = None,
    compatible_figure: Optional[str] = None,
    name_query: Optional[str] = None,
    sort_by: str = "name",
    sort_dir: str = "asc",
    is_installed: Optional[bool] = None,
):
    """Paginated product listing with optional filters and sort. `is_installed` filters on
    whether the product is currently in the DAZ CMS (removed products stay listed)."""
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

    with _catalog_reads():
        result = catalog.get_products(
            page=page,
            page_size=page_size,
            category=category,
            artist=artist,
            compatible_figure=compatible_figure,
            name_query=name_query,
            sort_by=sort_by,
            sort_dir=sort_dir,
            installed=is_installed,
        )
    result["products"] = [_format_product(r) for r in result["products"]]
    return result


@app.get("/api/v1/products/{sku}", dependencies=_read)
def get_product(sku: str):
    """Full details for a single product."""
    if APP_MODE == "demo":
        product = get_demo_product_mock(sku)
        if not product:
            raise HTTPException(status_code=404, detail=f"Product '{sku}' not found.")
        return product

    with _catalog_reads():
        row = catalog.get_by_sku(sku)
    if not row:
        raise HTTPException(status_code=404, detail=f"Product '{sku}' not found in local index.")
    return _format_product(row)


@app.get("/api/v1/products/{sku}/assets", dependencies=_read)
def get_product_assets(sku: str):
    """Asset files for a product."""
    return get_assets(sku)


@app.post("/api/v1/products/{sku}/open", status_code=200, dependencies=_auth)
def open_product(sku: str):
    """Navigates the DAZ Studio Content Library to the product.

    Uses the DAZ Script Server plugin when available; falls back to subprocess launch.
    """
    with _catalog_reads():
        row = catalog.get_by_sku(sku)
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

@app.get("/api/v1/filters", dependencies=_read)
def get_filters():
    """All valid filter values (categories, artists, compatible figures)."""
    if APP_MODE == "demo":
        return get_demo_filters_mock()
    with _catalog_reads():
        return catalog.get_filter_values()


# ── Search ─────────────────────────────────────────────────────────────────────

@app.post("/api/v1/search", dependencies=_read)
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
    started = time.monotonic()
    with _catalog_reads():
        rows = active_index.search(
            catalog,
            request.query,
            max_results=request.max_results,
            category=f.category or None,
            artists=[f.artist] if f.artist else None,
            compatible_figures=[f.compatible_figures] if f.compatible_figures else None,
        )
    results = [_format_product(r) for r in rows]
    if f.is_installed is not None:
        results = [r for r in results if r["is_installed"] == f.is_installed]
    if request.min_relevance > 0:
        results = [r for r in results if r.get("relevance_score", 0) >= request.min_relevance]
    took_ms = int((time.monotonic() - started) * 1000)
    return {"results": results, "total": len(results), "query": request.query, "took_ms": took_ms}


# Fixed candidate pool for /query: total_hits stays stable across offset/limit pages.
_QUERY_POOL = 500


@app.post("/api/v1/query", dependencies=_read)
def run_query(request: QueryRequest):
    """MCP-oriented semantic search (prompt-based, full filter set)."""
    logger.info(f"Query: {request}")
    if APP_MODE == "demo":
        return search_mock(**request.model_dump())
    with _catalog_reads():
        rows = active_index.search(
            catalog,
            request.prompt,
            max_results=_QUERY_POOL,
            category=request.categories,
            artists=request.artists,
            compatible_figures=request.compatible_figures,
            tags=request.tags,
        )
    return mcp_response(rows, limit=request.limit, offset=request.offset,
                        score_threshold=request.score_threshold, sort_by=request.sort_by,
                        sort_order=request.sort_order)


# ── Settings ───────────────────────────────────────────────────────────────────

@app.get("/api/v1/settings", dependencies=_auth)
def get_settings():
    """Returns the current application settings (env + settings.json overlay), minus the
    CMS password — `cms_password_set` reports whether one is stored."""
    return _public_settings()


@app.put("/api/v1/settings", dependencies=_auth)
def save_settings(payload: SettingsPayload):
    """Saves settings to settings.json, overlaying the env defaults."""
    existing = {}
    if _SETTINGS_PATH.exists():
        try:
            existing = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    # The password is write-only, so the UI can't round-trip it; blank means "keep".
    if updates.get("cms_password") == "":
        del updates["cms_password"]
    existing.update(updates)
    _save_settings(existing)
    return _public_settings()


@app.post("/api/v1/settings/test-db", dependencies=_auth)
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

@app.get("/api/v1/daz-studio/status", dependencies=_read)
def get_daz_studio_status():
    """Probes the DAZ Script Server plugin to check if it's running."""
    if APP_MODE == "demo":
        return {"plugin_detected": False, "plugin_url": None, "version": None}
    return daz_script_server.status()


@app.get("/api/v1/daz-studio/content-dirs", dependencies=_read)
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


@app.post("/api/v1/scene/load", dependencies=_auth)
def load_asset_into_scene(body: LoadAssetRequest):
    """Loads an asset file into the current DAZ Studio scene via the DAZ Script Server plugin.

    The path must resolve beneath a registered content root.
    """
    _require_in_roots(body.path)
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

@app.get("/api/v1/content-roots", dependencies=_read)
def get_content_roots():
    if APP_MODE == "demo" or daz_pg_analyzer is None:
        return {"content_roots": []}
    roots = daz_pg_analyzer.get_content_roots()
    return {"content_roots": roots}


@app.get("/api/v1/assets/thumbnail", dependencies=_auth)
def get_asset_thumbnail(path: str):
    """Serves the companion thumbnail PNG for a DAZ asset file.

    DAZ Studio places thumbnails alongside content files with the same stem
    and a .png extension (e.g. 'FN Ethan.duf' → 'FN Ethan.png').
    Returns 404 when no thumbnail is found so the UI can show its placeholder.
    Both the asset path and the served PNG must resolve beneath a registered content root.

    Declared before /api/v1/assets/{sku}, which would otherwise match "thumbnail" as a SKU.
    """
    asset = _require_in_roots(path)
    roots = _content_roots()
    candidates = [
        asset.with_suffix(".png"),
        asset.parent / (asset.name + ".png"),
    ]
    for candidate in candidates:
        # Re-check after resolution: the companion PNG could be a link pointing elsewhere.
        if candidate.exists() and resolve_within_roots(str(candidate), roots):
            return FileResponse(str(candidate), media_type="image/png")
    raise HTTPException(status_code=404, detail="No thumbnail found for this asset.")


@app.get("/api/v1/assets/{sku}", dependencies=_read)
def get_assets(sku: str):
    """Asset file paths for a SKU, with resolved absolute paths where available."""
    if APP_MODE == "demo" or daz_pg_analyzer is None:
        return {"sku": sku, "files": []}

    files = daz_pg_analyzer.get_asset_files_by_sku(sku)
    if files is None:
        raise HTTPException(status_code=500, detail="Database error fetching asset files.")

    all_roots = _content_roots()
    for f in files:
        relative = Path(f["path"].lstrip("/")) / f["filename"]
        f["resolved_path"] = None
        for root in all_roots:
            candidate = Path(root) / relative
            if candidate.exists():
                f["resolved_path"] = candidate.as_posix()
                break

    return {"sku": sku, "files": files}


@app.get("/api/v1/browseproduct/{product_id}", dependencies=_auth)
def browse_product(product_id: str):
    if APP_MODE == "demo":
        return {"status": "ok"}
    with _catalog_reads():
        row = catalog.get_by_sku(product_id)
    product_name = (row.get("name") if row else None) or product_id
    success = open_daz_product(args=type("obj", (object,), {"product": product_name}))
    if not success:
        raise HTTPException(status_code=500, detail="Failed to open product in DAZ Studio.")
    return {"success": True, "message": f"Opened '{product_name}' in Content Library."}


@app.get("/api/v1/info", dependencies=_read)
def get_info():
    """Full stats including filter lists (for MCP/dashboard use)."""
    if APP_MODE == "demo":
        logger.info("Demo mode: returning mock database stats.")
        return get_demo_stats_mock()

    with _catalog_reads():
        snap = _index_snapshot()
        if snap["index"]["error"]:
            raise HTTPException(status_code=503, detail={
                "reason": "catalog_unavailable", "repair": snap["repair"]})
        total_docs = max(_vector_count(snap["index"]), 0)
        if total_docs == 0:
            raise HTTPException(status_code=404, detail="Database collection not found or empty.")
        filter_values = catalog.get_filter_values()
        last_update = catalog.get_last_updated()
        sqlite_count = catalog.count()
    postgres_count = snap["postgres_count"]
    new_products = snap["pending"]["new"] if snap["pending"] else 0

    logger.info(
        f"/info: postgres={postgres_count}, sqlite={sqlite_count}, "
        f"chromadb={total_docs}, new_products={new_products}"
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
        "new_products": new_products,
    }


# ── Asset file helpers ─────────────────────────────────────────────────────────

class RevealRequest(BaseModel):
    path: str


@app.post("/api/v1/files/reveal", dependencies=_auth)
def reveal_in_explorer(body: RevealRequest):
    """Opens the system file explorer with the given file selected.

    The path must resolve beneath a registered content root.
    """
    target = _require_in_roots(body.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found.")
    try:
        if sys.platform == "win32":
            # A pre-quoted command string goes straight to CreateProcess (no cmd.exe), so
            # Explorer sees /select,"<path>" intact and nothing in the path is shell-parsed.
            subprocess.Popen(f'explorer /select,"{target}"')
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
