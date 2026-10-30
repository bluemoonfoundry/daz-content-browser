"""The FastAPI application: ``create_app(services)`` wires routers, security middleware,
error mapping and the lifespan around a ``Services`` container."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.routers import index, products, system
from catalog.active_index import SearchUnavailable
from catalog.model_config import configured_model_id
from catalog.status import migration_hint, repair_hint
from managers.chroma_db_manager import VectorStoreUnavailable
from security import allowed_origins, get_api_token, host_origin_guard
from services.container import Services, default_services
from services.errors import ServiceError

logger = logging.getLogger(__name__)

API_VERSION = "1.1.0"
_SRC = Path(__file__).parent.parent
DIST_PATH = (
    _SRC / "ui_dist"                       # installed wheel / PyInstaller
    if (_SRC / "ui_dist").exists()
    else _SRC.parent / "ui" / "dist"       # development
)


def _startup(services: Services) -> None:
    get_api_token()  # generate/persist the per-install token at startup so clients can read it
    if services.mode == "demo":
        logger.info("Server running in DEMO mode — PostgreSQL not required.")
        return
    if services.content.cms.get() is None:
        logger.warning(
            "DAZ CMS not available — content-roots and asset-file endpoints will be "
            "unavailable until it is. Configure the connection in Settings (or .env), or "
            "start the server with --demo.")
    if services.load_embedding_model is not None:
        try:
            services.load_embedding_model()
        except RuntimeError as exc:
            logger.warning(f"[embedding] Model not loaded — search and indexing will be "
                           f"unavailable: {exc}")
    catalog, active_index = services.catalog, services.active_index
    if catalog.migration_error:
        logger.error(f"Catalog unavailable — {migration_hint(catalog.migration_error)}")
    else:
        # An upgraded install's pre-generations index is adopted here (no re-embedding),
        # so search works before any index job runs. Never blocks startup.
        try:
            active_index.ensure_adopted(force=True)
        except Exception:
            logger.exception("Could not adopt the existing search index.")
        try:
            health = active_index.health()
        except Exception:
            logger.exception("Could not check the active index.")
        else:
            if health is None and active_index.adoption_error is not None:
                logger.error("Search is unavailable: the existing index could not be adopted. "
                             f"{active_index.adoption_error}")
            elif health is None:
                logger.warning("No active index generation — search is unavailable. "
                               + repair_hint("no_index", configured_model_id()))
            elif not health["ok"]:
                logger.error(f"Vector store unavailable — search is degraded. {health['repair']}")
    services.jobs.reconcile()


def create_app(services: Optional[Services] = None, serve_ui: bool = True) -> FastAPI:
    services = services or default_services()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _startup(app.state.services)
        yield

    app = FastAPI(title=f"Visual Asset Browser API ({services.mode.upper()} MODE)",
                  version=API_VERSION, lifespan=lifespan)
    app.state.services = services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
    # Added last so it runs first: reject foreign Host/Origin before anything else sees the request.
    app.middleware("http")(host_origin_guard)

    @app.exception_handler(ServiceError)
    def service_error(request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(VectorStoreUnavailable)
    def vector_store_unavailable(request: Request, exc: VectorStoreUnavailable):
        return JSONResponse(status_code=503, content={"detail": str(exc), "repair": exc.repair})

    @app.exception_handler(SearchUnavailable)
    def search_unavailable(request: Request, exc: SearchUnavailable):
        return JSONResponse(status_code=503,
                            content={"detail": {"reason": exc.reason, "repair": exc.repair}})

    for router in (index.router, products.router, system.router):
        app.include_router(router)

    if serve_ui and DIST_PATH.exists():
        app.mount("/", StaticFiles(directory=str(DIST_PATH), html=True), name="ui")
    return app
