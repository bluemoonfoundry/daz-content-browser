from functools import lru_cache

from api.contracts import (
    ChromaDbManagerProtocol,
    DazPgAnalyzerProtocol,
    DazScriptServerProtocol,
    SQLiteDbProtocol,
    UpdateTaskServiceProtocol,
)
from services.task_service import update_task_service


@lru_cache(maxsize=1)
def _get_managers_module():
    # Lazy import keeps test/runtime startup independent from optional DB deps.
    from managers import managers as managers_module

    return managers_module


def get_sqlite_db() -> SQLiteDbProtocol:
    return _get_managers_module().sqlite_db


def get_chroma_db_manager() -> ChromaDbManagerProtocol:
    return _get_managers_module().chroma_db_manager


def get_daz_pg_analyzer() -> DazPgAnalyzerProtocol | None:
    return _get_managers_module().daz_pg_analyzer


def get_daz_script_server() -> DazScriptServerProtocol:
    return _get_managers_module().daz_script_server


def get_update_task_service() -> UpdateTaskServiceProtocol:
    return update_task_service
