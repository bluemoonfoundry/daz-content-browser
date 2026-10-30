"""Reconciliation-backed index status for /api/v1/status, /api/v1/health and the CLI.

Everything here is read from the catalog and generation tables (no Chroma calls), so it
stays cheap enough for the UI to poll.
"""

import logging
from typing import Optional

import embedding_utils
from catalog.documents import DOC_FORMAT_VERSION
from catalog.generations import GenerationStore
from catalog.store import CatalogStore

logger = logging.getLogger(__name__)


def rebuild_hint(model_id: str) -> str:
    return ("Run `vab index rebuild` (or POST /api/v1/update {\"mode\": \"rebuild\"}) "
            f"to rebuild the index for {model_id}.")


def migration_hint(error: str) -> str:
    return f"{error}; restart to retry; see server log"


def pending_cms_diff(cms_skus, present_keys: set) -> dict:
    """Pending DAZ CMS changes from a key-set diff: SKUs the CMS has that the catalog's
    present ``daz_cms`` records lack (new), and the reverse (removed)."""
    cms = {str(s) for s in cms_skus if s}
    present = {str(k) for k in present_keys}
    return {"new": len(cms - present), "removed": len(present - cms)}


def rebuild_reason(active: Optional[dict], configured_model: str) -> Optional[str]:
    if active is None:
        return "no_index"
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
                 cms_reachable: bool, pending_cms: Optional[dict], job_active: bool = False) -> dict:
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
        reason = rebuild_reason(active, configured_model)
        out["rebuild_required"] = reason is not None
        out["rebuild_reason"] = reason
    except Exception as e:  # a locked or damaged DB must not break status polling
        logger.exception("Could not read index status.")
        out["error"] = f"{type(e).__name__}: {e}"
    return out
