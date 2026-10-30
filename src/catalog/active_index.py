"""The active index generation as the API sees it: which collection to query, and with
which embedding model.

The active generation is re-read from SQLite on every call (one indexed read), so a
promotion made by another process (the index worker, a CLI rebuild) is picked up by the
next search. Queries always embed with the active generation's model, never the
configured one: vectors from different models are not comparable.
"""

import logging
import threading
from typing import Optional

import embedding_utils
from catalog.generations import GenerationStore
from catalog.model_config import configured_model_id
from catalog.status import rebuild_hint
from catalog.store import CatalogStore
from managers.chroma_db_manager import ChromaDbManager, VectorStoreUnavailable, _is_fatal

logger = logging.getLogger(__name__)

# Fields (and their order) of the vector metadata the MCP /query endpoint has always returned.
_MCP_STRING_FIELDS = ("sku", "url", "image_url", "name", "artist", "compatible_figures", "tags",
                      "category", "subcategories")


class SearchUnavailable(RuntimeError):
    """Search cannot be served right now; ``reason`` is a stable code, ``repair`` a hint."""

    def __init__(self, reason: str, repair: str):
        super().__init__(f"Search unavailable ({reason}): {repair}")
        self.reason = reason
        self.repair = repair


class ActiveIndex:
    def __init__(self, gens: GenerationStore, chroma_path: str):
        self.gens = gens
        self.chroma_path = chroma_path
        self._lock = threading.Lock()
        self._gen_id: Optional[int] = None
        self._mgr: Optional[ChromaDbManager] = None

    def current(self) -> Optional[tuple]:
        """``(generation, ChromaDbManager)`` for the active generation, or None if there is none."""
        gen = self.gens.active()
        with self._lock:
            if gen is None:
                self._gen_id = self._mgr = None
                return None
            if (self._mgr is None or self._gen_id != gen["id"]
                    or self._mgr.collection_name != gen["collection_name"]):
                # create=False: a collection dropped by another process must surface as
                # missing, not be re-created empty and silently return no results.
                self._mgr = ChromaDbManager(self.chroma_path, gen["collection_name"], create=False)
                self._gen_id = gen["id"]
            return gen, self._mgr

    def health(self) -> Optional[dict]:
        """The active collection's health (see ChromaDbManager.health), or None without one."""
        cur = self.current()
        return None if cur is None else cur[1].health()

    def _serving(self) -> tuple:
        cur = self.current()
        if cur is None:
            raise SearchUnavailable(
                "no_index", f"No search index has been built yet. {rebuild_hint(configured_model_id())}")
        gen = cur[0]
        if not embedding_utils.model_available(gen["model_id"]):
            raise SearchUnavailable("model_missing", _model_missing_hint(gen["model_id"]))
        return cur

    def search(self, catalog: CatalogStore, query: str, *, max_results: int, category=None,
               artists=None, compatible_figures=None, tags=None) -> list:
        """Catalog rows for the nearest assets, closest first, each with ``distance`` and
        ``relevance_score``. Filters are exact (resolved from the catalog, OR within a list,
        AND across lists). Raises SearchUnavailable when no usable index exists."""
        gen, mgr = self._serving()
        allowed = catalog.asset_ids_matching(category=category, artists=artists,
                                             compatible_figures=compatible_figures, tags=tags)
        if allowed is not None and not allowed:
            return []
        try:
            hits = self._query(gen, mgr, query, max_results, allowed)
        except VectorStoreUnavailable:
            # Another process may have promoted a new generation and dropped this one's
            # collection between our read of the active row and the query: refresh once.
            refreshed = self._serving()
            if refreshed[0]["id"] == gen["id"]:
                raise
            logger.info("Active generation changed from %d to %d during a search; retrying.",
                        gen["id"], refreshed[0]["id"])
            gen, mgr = refreshed
            hits = self._query(gen, mgr, query, max_results, allowed)

        assets = self.gens.assets_for_vectors(gen["id"], [h["id"] for h in hits])
        distances: dict = {}
        for h in hits:
            asset_id = assets.get(h["id"])
            if asset_id is not None and asset_id not in distances:
                distances[asset_id] = h["distance"]
        rows = catalog.get_rows(list(distances))
        for row in rows:
            row["distance"] = distances[row["asset_id"]]
            row["relevance_score"] = round(1.0 - row["distance"], 4)
        return rows

    def _query(self, gen: dict, mgr: ChromaDbManager, query: str, max_results: int,
               allowed: Optional[set]) -> list:
        ids = None
        if allowed is not None:
            ids = self.gens.vector_ids_for(gen["id"], allowed)
            if not ids:
                return []
        try:
            embedding = embedding_utils.generate_embeddings(query, is_query=True,
                                                            model_id=gen["model_id"])
        except embedding_utils.ModelUnavailable:
            raise SearchUnavailable("model_missing", _model_missing_hint(gen["model_id"]))
        except BaseException as e:
            if _is_fatal(e):
                raise
            # Files present but unusable (missing tokenizer.json, corrupt ONNX, ...).
            logger.exception("Could not load embedding model %r.", gen["model_id"])
            raise SearchUnavailable("model_load_failed", _model_load_failed_hint(gen["model_id"], e))
        hits, missing = mgr.search(embedding, max_results, ids=ids)
        if missing:
            self._forget_missing_vectors(gen, missing)
        return hits

    def _forget_missing_vectors(self, gen: dict, vector_ids: list) -> None:
        """Drop ``asset_vectors`` rows whose vector is not in the collection, so those assets
        count as missing and the next index run re-embeds them."""
        try:
            assets = self.gens.assets_for_vectors(gen["id"], vector_ids)
            self.gens.drop_vectors(gen["id"], list(assets.values()))
        except Exception:
            logger.exception("Could not drop stale vector rows of generation %d.", gen["id"])
            return
        logger.warning("Generation %d: %d vector(s) recorded but not in %r; marked missing so "
                       "the next index run re-embeds them.", gen["id"], len(assets),
                       gen["collection_name"])


def _model_missing_hint(model_id: str) -> str:
    return (f"The active index was built with '{model_id}', whose model files are not "
            f"available locally. Restore that model, or rebuild the index: "
            f"{rebuild_hint(configured_model_id())}")


def _model_load_failed_hint(model_id: str, error: BaseException) -> str:
    directory = embedding_utils.model_dir(model_id)
    return (f"The model files for '{model_id}' in '{directory}' could not be loaded "
            f"({type(error).__name__}: {error}). Re-export or restore that model (e.g. delete "
            f"the directory and restart the server), or rebuild the index: "
            f"{rebuild_hint(configured_model_id())}")


def mcp_metadata(row: dict) -> dict:
    """A catalog row as the vector metadata MCP clients of /api/v1/query have always seen."""
    meta = {k: str(row.get(k) or "") for k in _MCP_STRING_FIELDS}
    meta["asset_count"] = int(row.get("asset_count") or 0)
    return meta


def mcp_response(rows: list, *, limit: int, offset: int, score_threshold: float,
                 sort_by: str, sort_order: str) -> dict:
    """The /api/v1/query response for search rows: threshold (max cosine distance), optional
    metadata sort, then offset/limit over the fixed candidate pool."""
    hits = [{"id": row.get("sku") or row["asset_id"], "distance": row["distance"],
             "relevance_score": row["relevance_score"], "metadata": mcp_metadata(row)}
            for row in rows if row["distance"] <= score_threshold]
    if sort_by != "relevance":
        def key(hit):
            value = hit["metadata"].get(sort_by)
            return (0, value, "") if isinstance(value, (int, float)) else (1, 0, str(value or ""))
        hits.sort(key=key, reverse=sort_order == "descending")
    return {"total_hits": len(hits), "limit": limit, "offset": offset,
            "results": hits[offset:offset + limit]}
