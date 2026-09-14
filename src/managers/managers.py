import logging
import os
from fastapi import Request
from .daz_db_analyzer import DazDBAnalyzer
from .sqlite_db_manager import SQLiteWrapper
from .chroma_db_manager import ChromaDbManager
from .daz_script_server import DazScriptServerClient

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


def get_sqlite_db(request: Request):
    """Dependency: SQLite enrichment database."""
    if not hasattr(request.app.state, "sqlite_db"):
        request.app.state.sqlite_db = SQLiteWrapper(
            sqlite_db_path=os.environ.get("SQLITE_DB_PATH"),
            sqlite_db_table=os.environ.get("SQLITE_TABLE_NAME")
        )
    return request.app.state.sqlite_db


def get_chroma_db_manager(request: Request):
    """Dependency: ChromaDB vector index."""
    if not hasattr(request.app.state, "chroma_db_manager"):
        request.app.state.chroma_db_manager = ChromaDbManager(
            os.environ.get("CHROMA_PATH", "chroma_db"),
            os.environ.get("CHROMA_COLLECTION", "daz_products2")
        )
    return request.app.state.chroma_db_manager


def get_daz_pg_analyzer(request: Request):
    """Dependency: DAZ CMS PostgreSQL analyzer."""
    if not hasattr(request.app.state, "daz_pg_analyzer"):
        try:
            request.app.state.daz_pg_analyzer = DazDBAnalyzer()
        except EnvironmentError as e:
            logger.warning(f"PostgreSQL analyzer not available: {e}")
            request.app.state.daz_pg_analyzer = None
    return request.app.state.daz_pg_analyzer


def get_daz_script_server(request: Request):
    """Dependency: DAZ Script Server client."""
    if not hasattr(request.app.state, "daz_script_server"):
        request.app.state.daz_script_server = DazScriptServerClient(
            base_url=os.getenv("DAZ_SCRIPT_SERVER_URL", "http://127.0.0.1:18811")
        )
    return request.app.state.daz_script_server