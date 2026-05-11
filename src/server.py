import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.app_state import APP_MODE, DIST_PATH
from api.dependencies import get_daz_pg_analyzer
from api.routes.integration import router as integration_router
from api.routes.products_search import router as products_router
from api.routes.status_update import router as status_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if APP_MODE != "demo" and get_daz_pg_analyzer() is None:
        raise RuntimeError(
            "Server is in production mode but PostgreSQL credentials are missing. "
            "Set DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT in your .env file, "
            "or start the server with --demo."
        )
    if APP_MODE == "demo":
        logger.info("Server running in DEMO mode — PostgreSQL not required.")
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

app.include_router(status_router)
app.include_router(products_router)
app.include_router(integration_router)

if DIST_PATH.exists():
    app.mount("/", StaticFiles(directory=str(DIST_PATH), html=True), name="ui")
