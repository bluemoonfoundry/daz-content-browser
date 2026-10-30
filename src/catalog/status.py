"""Reconciliation-backed index status for /api/v1/status, /api/v1/health and the CLI.

Everything here is read from the catalog and generation tables (no Chroma calls), so it
stays cheap enough for the UI to poll.
"""

import logging
from datetime import date, datetime
from typing import Optional

import embedding_utils
from catalog.documents import DOC_FORMAT_VERSION
from catalog.generations import GenerationStore
from catalog.store import CatalogStore

logger = logging.getLogger(__name__)


def rebuild_hint(model_id: str) -> str:
    return ("Run `vab index rebuild` (or POST /api/v1/update {\"mode\": \"rebuild\"}) "
            f"to rebuild the index for {model_id}.")


def no_index_hint() -> str:
    """For ``no_index``: the incremental run builds the first generation, or adopts an
    existing (pre-generations) index without re-embedding. Never a rebuild."""
    return ("No search index has been built yet. Run `vab load` (or POST /api/v1/update "
            "{\"mode\": \"update\"}) to build the first index; an existing index is adopted "
            "without re-embedding.")


def repair_hint(reason: Optional[str], configured_model: str,
                adoption_error: Optional[dict] = None) -> Optional[str]:
    """The repair guidance for a ``rebuild_reason``."""
    if reason is None:
        return None
    if reason == "no_index":
        return no_index_hint()
    if reason == "adoption_failed" and adoption_error:
        return adoption_error["repair"]
    return rebuild_hint(configured_model)


def migration_hint(error: str) -> str:
    return f"{error}; restart to retry; see server log"


def _plain(value):
    """ISO-format datetimes exactly as the ETL stores ``source_modified`` (ruling R1)."""
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def pending_cms_diff(cms_modified: dict, present_modified: dict) -> dict:
    """Pending DAZ CMS changes: ``{sku: modified date}`` from the CMS against the catalog's
    present ``daz_cms`` records' ``{key: source_modified}``. New = listed but not present,
    changed = present with another modified date, removed = present but no longer listed.
    """
    cms = {str(k): _plain(v) for k, v in cms_modified.items() if k}
    present = {str(k): v for k, v in present_modified.items()}
    return {"new": sum(1 for k in cms if k not in present),
            "changed": sum(1 for k, v in cms.items() if k in present and v != present[k]),
            "removed": sum(1 for k in present if k not in cms)}


def rebuild_reason(active: Optional[dict], configured_model: str,
                   adoption_error: Optional[dict] = None) -> Optional[str]:
    if active is None:
        return "adoption_failed" if adoption_error else "no_index"
    if active["model_id"] != configured_model:
        return "model_changed"
    if active["doc_format_version"] != DOC_FORMAT_VERSION:
        return "format_changed"
    if not embedding_utils.model_available(active["model_id"]):
        return "model_missing"
    return None


def _resume_hint(gen: dict) -> str:
    from index_worker import _resume_hint as worker_hint  # lazy: index_worker imports managers
    return f"Rebuild interrupted — resume available. {worker_hint(gen)}"


def index_status(catalog: CatalogStore, gens: GenerationStore, configured_model: str,
                 cms_reachable: bool, pending_cms: Optional[dict], job_active: bool = False,
                 adoption_error: Optional[dict] = None) -> dict:
    """``adoption_error`` (``{"error", "repair"}``): the legacy index exists but could not be
    adopted; reported as ``rebuild_reason="adoption_failed"`` while nothing is active."""
    out = {
        "assets": None,
        "vectors": None,
        "active_generation": None,
        "building_generation": None,
        "rebuild_interrupted": False,
        "rebuild_required": False,
        "rebuild_reason": None,
        "resume_hint": None,
        "configured_model": configured_model,
        "query_model_ignored": True,
        "migration_error": catalog.migration_error,
        "cms_reachable": cms_reachable,
        "pending_cms": pending_cms,
        "adoption_error": None,
        "error": None,
    }
    if catalog.migration_error:
        return out  # the catalog tables are not usable; nothing more can be read
    try:
        out["assets"] = catalog.asset_totals()
        active = gens.active()
        building = gens.building()
        if active is not None:
            out["vectors"] = gens.vector_totals(active["id"])
            out["active_generation"] = {k: active[k] for k in (
                "id", "model_id", "model_dim", "doc_format_version", "promoted_at")}
        if building is not None:
            out["building_generation"] = {"id": building["id"], "model_id": building["model_id"],
                                          "pending": gens.pending_count(building["id"])}
            out["rebuild_interrupted"] = not job_active
            if not job_active:
                out["resume_hint"] = _resume_hint(building)
        if active is None and adoption_error:
            out["adoption_error"] = adoption_error
        reason = rebuild_reason(active, configured_model, out["adoption_error"])
        out["rebuild_required"] = reason is not None
        out["rebuild_reason"] = reason
    except Exception as e:  # a locked or damaged DB must not break status polling
        logger.exception("Could not read index status.")
        out["error"] = f"{type(e).__name__}: {e}"
    return out
