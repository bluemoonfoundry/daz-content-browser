"""Builds, resumes, promotes and cleans up index generations (the Chroma side).

A rebuild writes a new ``building`` generation into its own collection
(``daz_products_g<id>``) beside the active one, so search keeps working on the old
collection until ``finish_rebuild`` promotes the new one; an interrupted run is resumed
from the per-asset vector state in ``asset_vectors``. Existing installs are adopted
without re-embedding: the legacy collection becomes active generation 1 and its vectors
(bare SKU ids) count as current.
"""

import logging
import os
from typing import Callable, Optional

import embedding_utils
from catalog.documents import DOC_FORMAT_VERSION
from catalog.generations import GenerationStore
from catalog.schema import LATEST_VERSION, connect
from catalog.store import CatalogStore
from managers.chroma_db_manager import ChromaDbManager, _is_fatal

logger = logging.getLogger(__name__)

_CHUNK = 500
_PROBE_TEXT = "dimension probe"


class RebuildIncomplete(Exception):
    """A finished build is too broken to replace a working active generation."""


def _batch_size_default() -> int:
    return int(os.getenv("BATCH_SIZE", "512"))


def _metadata(row: dict, gen_id: int) -> dict:
    # Display fields only: filtering (artist, tags, category, figures) goes through ids=
    # resolved from the catalog's exact lookups, not Chroma metadata.
    return {
        "asset_id": row["asset_id"],
        "generation_id": gen_id,
        "doc_hash": row["doc_hash"] or "",
        "sku": row["sku"] or "",
        "url": row["url"] or "",
        "image_url": row["image_url"] or "",
        "name": row["name"] or "",
        "subcategories": row["subcategories"] or "",
        "asset_count": row["asset_count"] or 0,
    }


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


class GenerationBuilder:
    def __init__(self, catalog: CatalogStore, gens: GenerationStore, chroma_path: str,
                 legacy_collection: Optional[str]):
        self.catalog = catalog
        self.gens = gens
        self.chroma_path = chroma_path
        self.legacy_collection = legacy_collection
        self._managers: dict = {}

    def collection(self, gen: dict) -> ChromaDbManager:
        name = gen["collection_name"]
        if name not in self._managers:
            self._managers[name] = ChromaDbManager(self.chroma_path, name)
        return self._managers[name]

    # --- adoption --------------------------------------------------------------------

    def _has_generations(self) -> bool:
        conn = connect(self.gens.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM index_generations").fetchone()[0] > 0
        finally:
            conn.close()

    def _views_by_sku(self, skus: list) -> dict:
        out: dict = {}
        conn = connect(self.catalog.db_path)
        try:
            for i in range(0, len(skus), _CHUNK):
                chunk = skus[i:i + _CHUNK]
                for r in conn.execute(
                        f"SELECT sku, asset_id, doc_hash FROM asset_view WHERE sku IN "
                        f"({','.join('?' * len(chunk))})", chunk):
                    out[r["sku"]] = (r["asset_id"], r["doc_hash"])
        finally:
            conn.close()
        return out

    def adopt_legacy(self, model_id: str) -> Optional[dict]:
        """Adopt the pre-generations collection as active generation 1, without re-embedding."""
        if not self.legacy_collection or self._has_generations():
            return None
        mgr = ChromaDbManager(self.chroma_path, self.legacy_collection)
        try:
            # Check first: opening the manager would create an empty legacy collection.
            if not mgr.exists():
                return None
            dim = mgr.get_dimension()
            ids = sorted(mgr.get_all_ids()) if dim else []
        except BaseException as e:
            if _is_fatal(e):
                raise
            logger.error("Cannot adopt legacy collection %r (%s); a rebuild is required.",
                         self.legacy_collection, _err(e))
            return None
        if not dim:
            logger.info("Legacy collection %r is empty; nothing to adopt.", self.legacy_collection)
            return None
        views = self._views_by_sku(ids)
        # Ids with no catalog row are recorded too (doc_hash NULL, no view row), so the
        # orphan sweep in embed_pending deletes them instead of leaving them searchable.
        items = [(views[sku][0], sku, views[sku][1]) if sku in views else (f"daz:{sku}", sku, None)
                 for sku in ids]
        # One transaction: an interrupted adoption leaves nothing behind and is retried.
        gen = self.gens.adopt(self.legacy_collection, model_id, dim, DOC_FORMAT_VERSION,
                              LATEST_VERSION, items)
        logger.info("Adopted legacy collection %r as generation %d (%d of %d vectors matched).",
                    self.legacy_collection, gen["id"], sum(1 for s in ids if s in views), len(ids))
        return gen

    # --- embedding -------------------------------------------------------------------

    def _upsert(self, mgr: ChromaDbManager, **kwargs) -> None:
        try:
            mgr.collection.upsert(**kwargs)
        except BaseException as first:
            if _is_fatal(first):
                raise
            logger.warning("ChromaDB upsert into %r failed (%s); reconnecting and retrying.",
                           mgr.collection_name, _err(first))
            mgr.reconnect()
            mgr.collection.upsert(**kwargs)

    def _delete_ids(self, mgr: ChromaDbManager, ids: list) -> bool:
        if not ids:
            return True
        try:
            mgr.delete_ids(ids)
            return True
        except BaseException as e:
            if _is_fatal(e):
                raise
            logger.warning("Could not delete %d vector(s) from %r: %s", len(ids),
                           mgr.collection_name, _err(e))
            return False

    def embed_pending(self, gen: dict, *, batch_size: Optional[int] = None,
                      cancel_check: Optional[Callable[[], bool]] = None,
                      on_progress: Optional[Callable] = None,
                      limit: Optional[int] = None) -> dict:
        """Embed every missing, stale or retryable asset of ``gen``, then drop orphan vectors."""
        batch_size = batch_size or _batch_size_default()
        gen_id = gen["id"]
        mgr = self.collection(gen)
        result = {"embedded": 0, "errors": 0, "orphans_removed": 0}
        # One snapshot: a batch that fails is retried on the next run, not in a tight loop.
        todo = self.gens.pending(gen_id, limit=limit)
        total = len(todo)
        for start in range(0, total, batch_size):
            if cancel_check and cancel_check():
                logger.info("Embedding for generation %d cancelled.", gen_id)
                return result
            batch = todo[start:start + batch_size]
            rows = self.catalog.get_rows(batch)
            ids = [r["asset_id"] for r in rows]
            try:
                texts = [r["embedding_text"] or "" for r in rows]
                embeddings = embedding_utils.generate_embeddings(
                    texts, is_query=False, model_id=gen["model_id"])
                self._upsert(mgr, ids=ids, embeddings=embeddings.tolist(), documents=texts,
                             metadatas=[_metadata(r, gen_id) for r in rows])
            except embedding_utils.ModelUnavailable:
                raise
            except BaseException as e:
                if _is_fatal(e):
                    raise
                logger.error("Generation %d: batch of %d failed: %s", gen_id, len(ids), _err(e))
                self.gens.record_error(gen_id, ids, _err(e))
                result["errors"] += len(ids)
            else:
                # Adopted vectors live under bare SKU ids. Delete those before recording the
                # new id: if the delete fails, the asset keeps its old row (old id, old hash)
                # and stays pending, so no unreferenced duplicate is left in the collection.
                new_ids = set(ids)
                replaced = [v for v in self.gens.vector_ids_for(gen_id, new_ids)
                            if v not in new_ids]
                kept_old: set = set()
                if replaced and not self._delete_ids(mgr, replaced):
                    kept_old = set(self.gens.assets_for_vectors(gen_id, replaced).values())
                done = [r for r in rows if r["asset_id"] not in kept_old]
                self.gens.record_current(gen_id, [(r["asset_id"], r["asset_id"], r["doc_hash"])
                                                  for r in done])
                result["embedded"] += len(done)
                result["errors"] += len(kept_old)
            if on_progress:
                on_progress("embed", min(start + batch_size, total), total,
                            f"generation {gen_id}")
        if cancel_check and cancel_check():
            return result
        orphans = self.gens.orphans(gen_id)
        if orphans and self._delete_ids(mgr, orphans):
            assets = self.gens.assets_for_vectors(gen_id, orphans)
            self.gens.drop_vectors(gen_id, list(assets.values()))
            result["orphans_removed"] = len(orphans)
        return result

    # --- lifecycle -------------------------------------------------------------------

    def start_or_resume_rebuild(self, model_id: str, *, refetch: bool) -> dict:
        building = self.gens.building()
        if building is not None:
            if (building["model_id"] == model_id
                    and building["doc_format_version"] == DOC_FORMAT_VERSION):
                # Fresh retry budget, so a build refused for too many errors can recover.
                reset = self.gens.reset_error_attempts(building["id"])
                logger.info("Resuming generation %d (%s); %d failed asset(s) will be retried.",
                            building["id"], model_id, reset)
                return building
            self.discard(building, f"superseded by a rebuild for {model_id} "
                                   f"(doc format {DOC_FORMAT_VERSION})")
        probe = embedding_utils.generate_embeddings(_PROBE_TEXT, is_query=False, model_id=model_id)
        dim = int(probe.shape[-1])
        gen = self.gens.create_building(model_id, dim, DOC_FORMAT_VERSION, LATEST_VERSION,
                                        refetch=refetch)
        logger.info("Started generation %d (%s, dim %d) in %r.", gen["id"], model_id, dim,
                    gen["collection_name"])
        return gen

    def finish_rebuild(self, gen: dict) -> None:
        """Promote ``gen`` once only exhausted errors remain, then drop retired generations."""
        remaining = self.gens.pending(gen["id"])
        if remaining:
            raise RuntimeError(f"Generation {gen['id']} still has {len(remaining)} pending "
                               f"asset(s); not promoting.")
        if self.gens.active() is not None:
            # Replacing a working index: refuse a build that would leave search empty or
            # badly incomplete. It stays building and is resumable.
            totals = self.gens.vector_totals(gen["id"])
            assets = self.catalog.count()
            limit = max(10, assets // 100)
            if (assets and totals["current"] == 0) or totals["error"] > limit:
                raise RebuildIncomplete(
                    f"Generation {gen['id']} not promoted: {totals['current']} current, "
                    f"{totals['error']} failed of {assets} asset(s) (at most {limit} failures "
                    f"allowed); the active index is unchanged. Re-run to retry the failures.")
        self.gens.promote(gen["id"])
        logger.info("Promoted generation %d (%s).", gen["id"], gen["collection_name"])
        self.cleanup_retired()

    def discard(self, gen: dict, reason: str) -> None:
        """Drop a non-active generation's collection and remove its row."""
        current = self.gens.get(gen["id"])
        if current is not None and current["status"] == "active":
            raise ValueError(f"Refusing to discard active generation {gen['id']}")
        logger.warning("Discarding generation %d (%s): %s", gen["id"], gen["collection_name"],
                       reason)
        try:
            self.collection(gen).delete_collection()
        except BaseException as e:
            if _is_fatal(e):
                raise
            # Collection names are never reused (AUTOINCREMENT ids), so a leftover is inert.
            logger.error("Could not drop collection %r: %s", gen["collection_name"], _err(e))
        self._managers.pop(gen["collection_name"], None)
        self.gens.mark_failed(gen["id"], reason)
        self.gens.delete(gen["id"])

    def cleanup_retired(self) -> None:
        for gen in self.gens.retired():
            try:
                self.collection(gen).delete_collection()
            except BaseException as e:
                if _is_fatal(e):
                    raise
                logger.error("Could not drop retired collection %r (will retry): %s",
                             gen["collection_name"], _err(e))
                continue
            self._managers.pop(gen["collection_name"], None)
            self.gens.delete(gen["id"])
            logger.info("Removed retired generation %d (%s).", gen["id"], gen["collection_name"])
