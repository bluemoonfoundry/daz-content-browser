"""Index generations and per-asset vector state.

A generation is one vector collection built with one embedding model and document
format. At most one is ``active`` (served) and one ``building`` at a time. The
``asset_vectors`` table records which assets are embedded in which generation and the
``doc_hash`` they were embedded from, so staleness is a comparison with
``asset_view.doc_hash``. The schema is created by ``CatalogStore``/``migrate``; this
class assumes it exists.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Optional

from catalog.schema import connect

_CHUNK = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunks(items: list, size: int = _CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _marks(items: list) -> str:
    return ",".join("?" * len(items))


class GenerationStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    @contextmanager
    def _conn(self):
        conn = connect(self.db_path)
        conn.isolation_level = None  # autocommit; atomic writes use explicit BEGIN IMMEDIATE
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self):
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # --- generation rows -----------------------------------------------------------

    def _one(self, where: str, params: tuple = ()) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(f"SELECT * FROM index_generations WHERE {where}", params).fetchone()
        return dict(row) if row else None

    def active(self) -> Optional[dict]:
        return self._one("status = 'active'")

    def building(self) -> Optional[dict]:
        return self._one("status = 'building'")

    def get(self, gen_id: int) -> Optional[dict]:
        return self._one("id = ?", (gen_id,))

    def retired(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM index_generations WHERE status = 'retired' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def create_building(self, model_id: str, model_dim: int, doc_format_version: int,
                        schema_version: int, *, refetch: bool, now: Optional[str] = None) -> dict:
        now = now or _now()
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO index_generations (status, model_id, model_dim, doc_format_version, "
                "schema_version, collection_name, refetch_started_at, created_at) "
                "VALUES ('building', ?, ?, ?, ?, ?, ?, ?)",
                (model_id, model_dim, doc_format_version, schema_version,
                 f"pending-{uuid.uuid4().hex}", now if refetch else None, now))
            gen_id = cur.lastrowid
            conn.execute("UPDATE index_generations SET collection_name = ? WHERE id = ?",
                         (f"daz_products_g{gen_id}", gen_id))
        return self.get(gen_id)

    def create_active_adopted(self, collection_name: str, model_id: str, model_dim: int,
                              doc_format_version: int, schema_version: int) -> dict:
        now = _now()
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO index_generations (status, model_id, model_dim, doc_format_version, "
                "schema_version, collection_name, created_at, completed_at, promoted_at) "
                "VALUES ('active', ?, ?, ?, ?, ?, ?, ?, ?)",
                (model_id, model_dim, doc_format_version, schema_version, collection_name,
                 now, now, now))
            gen_id = cur.lastrowid
        return self.get(gen_id)

    def promote(self, gen_id: int, now: Optional[str] = None) -> None:
        """Atomically retire the current active generation (if any) and activate ``gen_id``."""
        now = now or _now()
        with self._write() as conn:
            row = conn.execute("SELECT status FROM index_generations WHERE id = ?",
                               (gen_id,)).fetchone()
            if row is None or row["status"] != "building":
                raise ValueError(f"Generation {gen_id} is not building")
            conn.execute("UPDATE index_generations SET status = 'retired' WHERE status = 'active'")
            conn.execute("UPDATE index_generations SET status = 'active', promoted_at = ?, "
                         "completed_at = ? WHERE id = ?", (now, now, gen_id))

    def mark_failed(self, gen_id: int, error: str) -> None:
        with self._write() as conn:
            conn.execute("UPDATE index_generations SET status = 'failed', error = ? WHERE id = ?",
                         (error, gen_id))

    def delete(self, gen_id: int) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM index_generations WHERE id = ?", (gen_id,))

    # --- vector state --------------------------------------------------------------

    def record_current(self, gen_id: int, items: Iterable[tuple]) -> None:
        # Replaces vector_id: callers that change an asset's vector id must delete the old
        # Chroma id themselves.
        now = _now()
        rows = [(gen_id, a, v, h, now) for a, v, h in items]
        if not rows:
            return
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO asset_vectors (generation_id, asset_id, vector_id, doc_hash, status, "
                "error, attempts, embedded_at) VALUES (?, ?, ?, ?, 'current', NULL, 0, ?) "
                "ON CONFLICT (generation_id, asset_id) DO UPDATE SET vector_id = excluded.vector_id, "
                "doc_hash = excluded.doc_hash, status = 'current', error = NULL, attempts = 0, "
                "embedded_at = excluded.embedded_at", rows)

    def record_error(self, gen_id: int, asset_ids: list, error: str) -> None:
        if not asset_ids:
            return
        # An asset with no vector row yet gets a placeholder vector_id (NOT NULL column).
        # For error rows ``doc_hash`` is the content hash last attempted; a failure on new
        # content restarts the retry budget (attempts = 1).
        rows = [(gen_id, a, f"error:{a}", a, error) for a in asset_ids]
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO asset_vectors (generation_id, asset_id, vector_id, doc_hash, status, "
                "error, attempts) VALUES (?, ?, ?, (SELECT doc_hash FROM asset_view WHERE asset_id = ?), "
                "'error', ?, 1) "
                "ON CONFLICT (generation_id, asset_id) DO UPDATE SET "
                "attempts = CASE WHEN status = 'error' AND doc_hash IS excluded.doc_hash "
                "THEN attempts + 1 ELSE 1 END, "
                "status = 'error', error = excluded.error, doc_hash = excluded.doc_hash", rows)

    def drop_vectors(self, gen_id: int, asset_ids: list) -> None:
        with self._write() as conn:
            for chunk in _chunks(list(asset_ids)):
                conn.execute(
                    f"DELETE FROM asset_vectors WHERE generation_id = ? AND asset_id IN "
                    f"({_marks(chunk)})", (gen_id, *chunk))

    def pending(self, gen_id: int, max_attempts: int = 3, limit: Optional[int] = None) -> list:
        sql = ("SELECT v.asset_id FROM asset_view v "
               "LEFT JOIN asset_vectors av ON av.generation_id = ? AND av.asset_id = v.asset_id "
               "WHERE av.asset_id IS NULL "
               "OR (av.status = 'current' AND av.doc_hash IS NOT v.doc_hash) "
               "OR (av.status = 'error' AND (av.attempts < ? OR av.doc_hash IS NOT v.doc_hash)) "
               "ORDER BY v.asset_id")
        params: list = [gen_id, max_attempts]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._conn() as conn:
            return [r[0] for r in conn.execute(sql, params).fetchall()]

    def orphans(self, gen_id: int) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT av.vector_id FROM asset_vectors av "
                "LEFT JOIN asset_view v ON v.asset_id = av.asset_id "
                "WHERE av.generation_id = ? AND v.asset_id IS NULL AND av.vector_id NOT LIKE 'error:%' "
                "ORDER BY av.vector_id",
                (gen_id,)).fetchall()
        return [r[0] for r in rows]

    def vector_totals(self, gen_id: int) -> dict:
        join = ("FROM asset_vectors av JOIN asset_view v ON v.asset_id = av.asset_id "
                "WHERE av.generation_id = ? ")
        with self._conn() as conn:
            def one(sql: str) -> int:
                return conn.execute(sql, (gen_id,)).fetchone()[0]
            return {
                "current": one("SELECT COUNT(*) " + join +
                               "AND av.status = 'current' AND av.doc_hash IS v.doc_hash"),
                "stale": one("SELECT COUNT(*) " + join +
                             "AND av.status = 'current' AND av.doc_hash IS NOT v.doc_hash"),
                "missing": one(
                    "SELECT COUNT(*) FROM asset_view v WHERE NOT EXISTS (SELECT 1 FROM "
                    "asset_vectors av WHERE av.generation_id = ? AND av.asset_id = v.asset_id)"),
                "error": one("SELECT COUNT(*) " + join + "AND av.status = 'error'"),
                "orphan": one(
                    "SELECT COUNT(*) FROM asset_vectors av WHERE av.generation_id = ? AND "
                    "NOT EXISTS (SELECT 1 FROM asset_view v WHERE v.asset_id = av.asset_id)"),
            }

    def vector_ids_for(self, gen_id: int, asset_ids: set) -> list:
        out: list = []
        with self._conn() as conn:
            for chunk in _chunks(sorted(asset_ids)):
                out += [r[0] for r in conn.execute(
                    f"SELECT vector_id FROM asset_vectors WHERE generation_id = ? AND asset_id IN "
                    f"({_marks(chunk)}) AND vector_id NOT LIKE 'error:%' ORDER BY vector_id",
                    (gen_id, *chunk))]
        return out

    def assets_for_vectors(self, gen_id: int, vector_ids: list) -> dict:
        out: dict = {}
        with self._conn() as conn:
            for chunk in _chunks(list(vector_ids)):
                out.update({r[0]: r[1] for r in conn.execute(
                    f"SELECT vector_id, asset_id FROM asset_vectors WHERE generation_id = ? AND "
                    f"vector_id IN ({_marks(chunk)})", (gen_id, *chunk))})
        return out
