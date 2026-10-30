import logging
import os
import psycopg2
from .daz_db_analyzer import DazDBAnalyzer
from .job_store import JobStore
from .daz_script_server import DazScriptServerClient
from catalog.active_index import ActiveIndex
from catalog.builder import GenerationBuilder
from catalog.generations import GenerationStore
from catalog.store import CatalogStore

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


def _open_catalog(db_path, legacy_table) -> CatalogStore:
    """The catalog store; never raises, so a missing or broken DB can't block startup.

    A failure is kept on ``migration_error`` (CatalogStore already stores migration
    failures there) and every index job refuses to run until it is fixed.
    """
    if not db_path:
        store = CatalogStore.__new__(CatalogStore)
        store.db_path = None
        store.migration_error = "SQLITE_DB_PATH is not set; the catalog database is unavailable."
        return store
    return CatalogStore(db_path, legacy_table=legacy_table, lazy=True)


catalog = _open_catalog(os.environ.get("SQLITE_DB_PATH"), os.environ.get("SQLITE_TABLE_NAME"))
generations = GenerationStore(catalog.db_path, ready=catalog.ensure_migrated)
builder = GenerationBuilder(
    catalog, generations, os.environ.get("CHROMA_PATH", "chroma_db"),
    legacy_collection=os.environ.get("CHROMA_COLLECTION", "daz_products2"),
)

# What the API searches: the active generation's collection, re-read on every call so a
# promotion by the worker process is picked up. Opens lazily, like every Chroma handle.
active_index = ActiveIndex(generations, os.environ.get("CHROMA_PATH", "chroma_db"))

job_store = JobStore(os.path.abspath(os.environ.get("JOBS_DB_PATH", "index_jobs.db")))

try:
    daz_pg_analyzer = DazDBAnalyzer()
except (EnvironmentError, psycopg2.Error) as e:
    # Missing config, or the DAZ CMS isn't running — the server still starts without it
    logger.warning(f"PostgreSQL analyzer not available: {e}")
    daz_pg_analyzer = None

daz_script_server = DazScriptServerClient(
    base_url=os.getenv("DAZ_SCRIPT_SERVER_URL", "http://127.0.0.1:18811")
)