"""What /status, /health and /info report: the catalog, the active index generation, the
vector store, the DAZ CMS diff, the embedding model and index jobs."""
import logging
import time
from typing import Callable

import embedding_utils
from catalog.model_config import configured_model_id
from catalog.status import index_status, migration_hint, pending_cms_diff, rebuild_hint, repair_hint
from catalog.store import CMS_SOURCE
from services.catalog_reads import catalog_reads
from services.errors import NotFound, Unavailable

logger = logging.getLogger(__name__)

CMS_TTL_SECONDS = 30.0


class StatusService:
    def __init__(self, catalog, generations, active_index, job_store, cms, jobs,
                 worker_log: Callable[[object], object], mode: str = "production"):
        self.catalog = catalog
        self.generations = generations
        self.active_index = active_index
        self.job_store = job_store
        self.cms = cms                # CmsGateway
        self.jobs = jobs              # JobService
        self._worker_log = worker_log
        self.mode = mode
        self.cms_ttl_seconds = CMS_TTL_SECONDS
        self._cms_cache = None        # (monotonic time, (postgres_count, pending_cms))

    # ── CMS ────────────────────────────────────────────────────────────────────

    def cms_snapshot(self) -> tuple:
        """``(postgres_count, pending_cms)`` from the DAZ CMS; ``(-1, None)`` when unreachable.

        ``pending_cms`` diffs every CMS SKU and modified date against the catalog's present
        CMS records (``{"new", "changed", "removed"}``), or is None when either side can't
        be read. Cached briefly: the UI polls /status, and each snapshot reads every SKU from
        PostgreSQL. A stale value only delays the "update available" hint by one TTL.
        """
        now = time.monotonic()
        if self._cms_cache and now - self._cms_cache[0] < self.cms_ttl_seconds:
            return self._cms_cache[1]
        snapshot = self._read_cms_snapshot()
        self._cms_cache = (now, snapshot)
        return snapshot

    def _read_cms_snapshot(self) -> tuple:
        analyzer = self.cms.get()
        if analyzer is None:
            return -1, None
        try:
            count = analyzer.count_skus()
        except Exception:
            logger.warning("DAZ CMS count failed.", exc_info=True)
            return -1, None
        if count is None or count < 0:
            return -1, None
        if self.catalog.migration_error:
            return count, None
        try:
            modified = analyzer.get_sku_modified()
            if modified is None or (count > 0 and not modified):  # None: the query failed
                return count, None
            return count, pending_cms_diff(modified, self.catalog.present_modified(CMS_SOURCE))
        except Exception:
            logger.warning("Could not diff DAZ CMS SKUs against the catalog.", exc_info=True)
            return count, None

    # ── Snapshot ───────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Everything /status, /health and /info report about the index, read once."""
        try:
            job_active = self.job_store.active() is not None
        except Exception:
            logger.warning("Could not read the job store.", exc_info=True)
            job_active = False
        postgres_count, pending = self.cms_snapshot()
        configured = configured_model_id()
        catalog = self.catalog
        adoption = None
        if not catalog.migration_error:
            self.active_index.ensure_adopted()  # rate-limited; a no-op once any generation exists
            adoption = self.active_index.adoption_status()
        idx = index_status(catalog, self.generations, configured, pending is not None, pending,
                           job_active, adoption_error=adoption)

        vector_store = None  # None: no active generation (or no readable catalog)
        if not catalog.migration_error and idx["error"] is None:
            try:
                vector_store = self.active_index.health()
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
            repair = repair_hint(idx["rebuild_reason"], configured, idx["adoption_error"])
        else:
            repair = idx["resume_hint"]
        degraded = bool(catalog.migration_error or idx["error"] or idx["rebuild_required"]
                        or (vector_store is not None and not vector_store["ok"]))
        return {"index": idx, "vector_store": vector_store, "postgres_count": postgres_count,
                "pending": pending, "configured": configured, "repair": repair, "degraded": degraded}

    @staticmethod
    def vector_count(idx: dict) -> int:
        vectors = idx["vectors"]
        if vectors is None:
            return -1 if (idx["migration_error"] or idx["error"]) else 0
        return vectors["current"] + vectors["stale"]

    # ── Endpoints ──────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Index counts plus connection health. The legacy top-level keys are kept
        (deprecated); ``index`` carries the reconciliation-backed detail."""
        snap = self.snapshot()
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
            "chromadb_count": self.vector_count(idx),
            "new_products": pending["new"] if pending else 0,
            "update_available": bool(pending and (pending["new"] or pending["changed"]
                                                   or pending["removed"])),
            "repair": snap["repair"],
            "index": idx,
        }

    def health(self) -> dict:
        """Per-component health with repair guidance. ``status`` is "ok" or "degraded"; the
        API keeps serving what it can (browse, settings, jobs) while a component is down."""
        snap = self.snapshot()
        idx, configured = snap["index"], snap["configured"]
        migration_error = self.catalog.migration_error
        active = idx["active_generation"]

        vector_store = snap["vector_store"]
        if vector_store is None:
            if migration_error or idx["error"]:
                # The catalog can't be read, so whether an index exists is unknown.
                vector_store = {"ok": False, "error": "Index state unknown: the catalog is unavailable.",
                                "repair": snap["repair"]}
            elif idx["adoption_error"]:
                vector_store = {"ok": False, "error": idx["adoption_error"]["error"],
                                "repair": idx["adoption_error"]["repair"]}
            else:
                vector_store = {"ok": False, "error": "No active index generation.",
                                "repair": repair_hint("no_index", configured)}
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
            "sqlite": {"ok": idx["assets"] is not None, "path": self.catalog.db_path},
            "postgres": {"ok": self.cms.get() is not None},
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
            components["postgres"]["error"] = self.cms.error
            components["postgres"]["repair"] = (
                "Configure the DAZ CMS connection in Settings (applied immediately) or .env; "
                "browse and search still work from the local index."
            )
        if not components["sqlite"]["ok"]:
            components["sqlite"]["repair"] = snap["repair"]
        try:
            components["jobs"] = self.jobs.health(str(self._worker_log(self.job_store)))
        except Exception as e:
            components["jobs"] = {"ok": False, "error": str(e)}
        # Postgres is optional for serving; everything else degrades a user-visible feature.
        degraded = any(not c["ok"] for name, c in components.items() if name != "postgres")
        return {"status": "degraded" if degraded else "ok", "mode": self.mode,
                "components": components}

    def info(self) -> dict:
        """Totals and filter histograms (MCP/dashboard)."""
        with catalog_reads(self.catalog):
            snap = self.snapshot()
            if snap["index"]["error"]:
                raise Unavailable("catalog_unavailable", snap["repair"])
            total_docs = max(self.vector_count(snap["index"]), 0)
            if total_docs == 0:
                raise NotFound("Database collection not found or empty.")
            filter_values = self.catalog.get_filter_values()
            last_update = self.catalog.get_last_updated()
            sqlite_count = self.catalog.count()
        postgres_count = snap["postgres_count"]
        new_products = snap["pending"]["new"] if snap["pending"] else 0
        logger.info(f"/info: postgres={postgres_count}, sqlite={sqlite_count}, "
                    f"chromadb={total_docs}, new_products={new_products}")
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
