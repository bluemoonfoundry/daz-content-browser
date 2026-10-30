""" Manages interactions with ChromaDB for vector storage and retrieval. """

import chromadb
from chromadb.errors import NotFoundError
import logging
import os
import time
from typing import List, Optional

logger = logging.getLogger(__name__)


class VectorStoreUnavailable(RuntimeError):
    """The ChromaDB store could not be opened or queried (missing, locked, or corrupt).

    Carries `repair`, a user-facing hint, so the API can answer 503 with something
    actionable instead of crashing at import time.
    """

    def __init__(self, chroma_db_path: str, collection_name: str, cause: BaseException):
        self.chroma_db_path = chroma_db_path
        self.collection_name = collection_name
        self.cause = f"{type(cause).__name__}: {cause}"
        self.repair = repair_guidance(collection_name)
        super().__init__(
            f"Vector store {collection_name!r} at {chroma_db_path!r} is unavailable ({self.cause})."
        )


def repair_guidance(collection_name: str) -> str:
    if collection_name == "morphs":
        return ("Reset the 'morphs' collection, then re-run `vab morphs index --library-path <library>` "
                "(non --force re-embeds everything missing).")
    return ("Run `vab index repair` (or POST /api/v1/update with {\"mode\": \"repair\"}) to reset the "
            f"{collection_name!r} collection and re-embed it from SQLite. If the store still cannot be "
            "opened, stop the server and run the repair again so the directory can be quarantined.")


def _is_fatal(exc: BaseException) -> bool:
    # chromadb's Rust core surfaces panics as pyo3 PanicException, a BaseException subclass,
    # so "catch Exception" misses exactly the corruption failures we care about.
    return isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit))

def _forget_cached_system(chroma_db_path: str) -> None:
    # chromadb caches one System per persist directory and registers it *before*
    # starting it, so a failed open leaves a half-started System behind that every
    # later open for this path reuses. Drop it so the next attempt (e.g. after a
    # repair) starts clean.
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
        SharedSystemClient._identifier_to_system.pop(str(chroma_db_path), None)
    except Exception:
        logger.debug("Could not evict chromadb's cached System.", exc_info=True)


def store_open_error(chroma_db_path: str) -> Optional[str]:
    """Why the store at ``chroma_db_path`` cannot be opened at all, or None if it opens.

    Opens the client and lists collections without creating or touching any collection.
    """
    try:
        chromadb.PersistentClient(path=chroma_db_path).list_collections()
    except BaseException as e:
        if _is_fatal(e):
            raise
        _forget_cached_system(chroma_db_path)
        return f"{type(e).__name__}: {e}"
    return None


def quarantine_store(chroma_db_path: str, reason: str = "") -> str:
    """Renames an unopenable store directory aside (never deletes it); returns the new path.

    Everything in the store (every collection, e.g. 'morphs') moves with it.
    """
    quarantine = f"{chroma_db_path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    logger.warning(f"Store will not open ({reason}); moving it to {quarantine!r}.")
    _forget_cached_system(chroma_db_path)
    os.rename(chroma_db_path, quarantine)
    return quarantine


class ChromaDbManager:
    """Handles all interactions with ChromaDB."""

    def __init__(self, chroma_db_path:str, collection_name:str, create: bool = True):
        """Create a ChromaDB manager instance.

        The store is opened lazily on first use, so a missing or corrupt store never
        prevents importing (and therefore starting) the API.

        Args:
            chroma_db_path (str): Path to the ChromaDB database directory.
            collection_name (str): Name of the collection to use within ChromaDB.
            create (bool): Create the collection if it does not exist. Readers of an index
                generation pass False, so a collection dropped by another process is
                reported missing instead of being silently re-created empty.
        """

        self.chroma_db_path = chroma_db_path
        self.collection_name = collection_name
        self.create = create
        self._client = None
        self._collection = None
        self.last_error: Optional[str] = None

    def _open(self) -> None:
        try:
            client = chromadb.PersistentClient(path=self.chroma_db_path)
            if self.create:
                collection = client.get_or_create_collection(
                    name=self.collection_name,
                    metadata={"hnsw:space": "cosine"},
                )
            else:
                collection = client.get_collection(name=self.collection_name)
        except BaseException as e:
            if _is_fatal(e):
                raise
            if not isinstance(e, NotFoundError):
                # A missing collection says nothing about the store; keep its System.
                self._forget_cached_system()
            err = VectorStoreUnavailable(self.chroma_db_path, self.collection_name, e)
            self.last_error = err.cause
            logger.error(str(err))
            raise err from e
        self._client, self._collection = client, collection
        self.last_error = None
        logger.info(f"ChromaDB opened — path: {self.chroma_db_path!r}, collection: {self.collection_name!r}")

    def _forget_cached_system(self) -> None:
        _forget_cached_system(self.chroma_db_path)

    @property
    def client(self):
        if self._client is None:
            self._open()
        return self._client

    @property
    def collection(self):
        if self._collection is None:
            self._open()
        return self._collection

    def health(self) -> dict:
        """Opens the store if needed and reports whether it is usable, with repair guidance if not.

        A store can open cleanly yet fail at query time (a corrupt HNSW segment), so an
        error recorded by a failed search keeps the store reported as degraded until a
        later search, reconnect, or repair succeeds.
        """
        try:
            count = self.collection.count()
        except BaseException as e:
            if _is_fatal(e):
                raise
            self.last_error = e.cause if isinstance(e, VectorStoreUnavailable) else f"{type(e).__name__}: {e}"
        else:
            if self.last_error is None:
                return {"ok": True, "path": self.chroma_db_path, "collection": self.collection_name, "count": count}
        return {
            "ok": False,
            "path": self.chroma_db_path,
            "collection": self.collection_name,
            "error": self.last_error,
            "repair": repair_guidance(self.collection_name),
        }

        
    def _clean_metadata(self, item: dict) -> dict:
        """ Cleans metadata dictionary by removing None values and non-serializable types.

        Args:
            item (dict): Input metadata dictionary.

        Returns:
            dict: Cleaned metadata dictionary.
        """
        clean = {}
        for key, value in item.items():
            if value is not None and isinstance(value, (str, int, float, bool)):
                clean[key] = value
        return clean
    
    def reconnect(self) -> None:
        """Recreates the ChromaDB client and refreshes the collection reference.

        Call this when an upsert fails with a stale-connection or missing-collection
        error so the next operation gets a fresh handle.
        """
        self._client = self._collection = None
        self._open()

    def get_all_ids(self) -> set:
        """Returns the set of all document IDs currently stored in the collection."""
        return set(self.collection.get(include=[])["ids"])

    def _bare_client(self):
        """The client without opening (and so without creating) this manager's collection."""
        if self._client is None:
            try:
                self._client = chromadb.PersistentClient(path=self.chroma_db_path)
            except BaseException as e:
                if _is_fatal(e):
                    raise
                self._forget_cached_system()
                err = VectorStoreUnavailable(self.chroma_db_path, self.collection_name, e)
                self.last_error = err.cause
                raise err from e
        return self._client

    def exists(self) -> bool:
        """Whether the collection exists, checked without creating it."""
        names = {getattr(c, "name", c) for c in self._bare_client().list_collections()}
        return self.collection_name in names

    def delete_collection(self) -> None:
        """Drops this manager's collection; a collection that does not exist is not an error."""
        try:
            self._bare_client().delete_collection(name=self.collection_name)
        except VectorStoreUnavailable:
            raise
        except BaseException as e:
            if _is_fatal(e):
                raise
            if not self.exists():
                logger.debug(f"Collection {self.collection_name!r} did not exist; nothing to drop.")
            else:
                raise
        self._collection = None
        logger.info(f"Dropped ChromaDB collection {self.collection_name!r}.")

    def get_dimension(self) -> Optional[int]:
        """Dimension of the stored vectors, from one peeked embedding; None if empty."""
        got = self.collection.get(limit=1, include=["embeddings"])
        embeddings = got.get("embeddings")
        if embeddings is None or len(embeddings) == 0:
            return None
        return len(embeddings[0])

    def delete_ids(self, ids: List[str]) -> None:
        """Deletes vectors by id; ids that are not stored are ignored."""
        ids = list(ids)
        if ids:
            self.collection.delete(ids=ids)

    def reset_collection(self) -> None:
        """Deletes and recreates the default collection, leaving it empty and ready for a fresh index.

        Updates self.collection so existing references stay valid.
        """
        logger.info(f"Resetting ChromaDB collection {self.collection_name!r}...")
        try:
            self.client.delete_collection(name=self.collection_name)
        except Exception:
            logger.warning(f"Collection {self.collection_name!r} did not exist; nothing to delete.")
        self._collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self.last_error = None
        logger.info(f"Collection {self.collection_name!r} reset and ready.")

    def repair(self) -> str:
        """Resets the collection; if the store itself won't open, quarantines the directory.

        Returns a one-line description of what was done. The quarantined directory is
        renamed, not deleted, so nothing is lost if the corruption diagnosis was wrong.
        Quarantining takes every collection in the store with it (e.g. 'morphs'), which
        then need re-indexing too.
        """
        try:
            self.reset_collection()
            return f"Reset collection {self.collection_name!r}."
        except VectorStoreUnavailable as e:
            if not os.path.exists(self.chroma_db_path):
                raise
            self._client = self._collection = None
            quarantine = quarantine_store(self.chroma_db_path, e.cause)
            self._open()
            return (f"Store could not be opened; moved it to {quarantine!r} and created a fresh one. "
                    "Other collections in it (e.g. morphs) must be re-indexed.")

    def search(self, query_embedding, n_results: int, ids: Optional[List[str]] = None) -> list:
        """Nearest vectors to ``query_embedding``: ``[{"id", "distance"}]``, closest first.

        ``ids`` restricts the query to those vector ids (exact filtering is resolved by the
        caller from the catalog); an empty list matches nothing. Ids the collection does not
        hold are dropped instead of failing the query.
        """
        if ids is not None:
            ids = list(dict.fromkeys(ids))
            if not ids:
                return []
            n_results = min(n_results, len(ids))
        if n_results <= 0:
            return []
        embedding = query_embedding.tolist() if hasattr(query_embedding, "tolist") else list(query_embedding)
        kwargs = dict(query_embeddings=[embedding], n_results=n_results, include=["distances"])
        try:
            results = self._query_with_reconnect(ids=ids, **kwargs)
        except VectorStoreUnavailable as e:
            # Chroma fails the whole query when any requested id is unknown.
            if ids is None or "finding id" not in e.cause:
                raise
            try:
                known = set(self.collection.get(ids=ids, include=[])["ids"])
            except BaseException as lookup:
                if _is_fatal(lookup):
                    raise
                raise e from lookup
            ids = [i for i in ids if i in known]
            if not ids:
                self.last_error = None
                return []
            kwargs["n_results"] = min(kwargs["n_results"], len(ids))
            results = self._query_with_reconnect(ids=ids, **kwargs)
        if not results["ids"]:
            return []
        return [{"id": vid, "distance": dist}
                for vid, dist in zip(results["ids"][0], results["distances"][0])]

    def _query_with_reconnect(self, **kwargs):
        """Queries, reconnecting once on failure.

        The index worker is a separate process: when it resets or rebuilds the collection,
        this process's handle points at a collection that no longer exists. A fresh handle
        fixes that; if the retry fails too, the store is genuinely broken.
        """
        try:
            results = self.collection.query(**kwargs)
        except VectorStoreUnavailable:
            raise
        except BaseException as first:
            if _is_fatal(first):
                raise
            logger.warning(f"ChromaDB query failed ({first}); reconnecting and retrying once.")
            try:
                self.reconnect()
                results = self._collection.query(**kwargs)
            except VectorStoreUnavailable:
                raise
            except BaseException as e:
                if _is_fatal(e):
                    raise
                err = VectorStoreUnavailable(self.chroma_db_path, self.collection_name, e)
                self.last_error = err.cause
                raise err from e
        self.last_error = None
        return results

    def get_db_stats(self):
        """Returns the total document count from the ChromaDB collection.

        Returns:
            dict: A dictionary containing total document count.
        """
        return {"total_docs": self.collection.count()}

