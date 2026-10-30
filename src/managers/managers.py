"""Process-wide adapters, built from the configuration (app_config.get_config()).

Shared by the API (through services.container.default_services), the index worker and the
CLI. Nothing here touches a database at import: the catalog migrates lazily, Chroma opens
lazily, and the DAZ CMS is connected on first use (CmsGateway).
"""
import os

from app_config import get_config
from catalog.active_index import ActiveIndex
from catalog.builder import GenerationBuilder
from catalog.generations import GenerationStore
from catalog.store import CatalogStore
from services.adapters import CmsGateway, DesktopActions, ScriptServerGateway
from .job_store import JobStore


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


_config = get_config()

catalog = _open_catalog(_config.sqlite_db_path, _config.sqlite_table_name)
generations = GenerationStore(catalog.db_path, ready=catalog.ensure_migrated)
builder = GenerationBuilder(catalog, generations, _config.chroma_path,
                            legacy_collection=_config.chroma_collection)


def _adopt_legacy():
    from catalog.model_config import configured_model_id
    return builder.adopt_legacy(configured_model_id())


# What the API searches: the active generation's collection, re-read on every call so a
# promotion by the worker process is picked up. Opens lazily, like every Chroma handle.
# While no generation exists, the pre-generations collection is adopted (server startup,
# or lazily on first use), so an upgraded install serves search before any index job.
active_index = ActiveIndex(generations, _config.chroma_path, adopt=_adopt_legacy)

job_store = JobStore(os.path.abspath(_config.jobs_db_path))

# The DAZ CMS (PostgreSQL); follows the saved connection settings, None while unavailable.
cms = CmsGateway()
script_server = ScriptServerGateway()
desktop = DesktopActions()
