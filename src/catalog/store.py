"""Source-aware catalog store: source records in, derived asset view + exact filters out.

SQLite (the same file as the legacy enrichment cache) is the canonical store. Writers
record what each source says about an asset (``upsert_source`` / ``mark_absent``);
``refresh_views`` merges those records into one ``asset_view`` row per asset plus the
normalized artist/tag/figure lookup rows. Readers only touch the view and lookups.
"""

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Optional

from catalog.documents import DOC_FORMAT_VERSION, build_view_row, doc_hash, payload_hash
from catalog.schema import LOOKUPS, connect, migrate, replace_lookups

logger = logging.getLogger(__name__)

CMS_SOURCE = "daz_cms"
WEB_SOURCE = "daz_store_web"

_VIEW_COLUMNS = ("asset_id", "sku", "name", "artist", "url", "image_url", "store", "price",
                 "description", "tags", "formats", "poly_count", "textures_info",
                 "required_products", "compatible_figures", "compatible_software",
                 "embedding_text", "last_updated", "category", "subcategories", "styles",
                 "inferred_tags", "enriched_at", "mature", "asset_count", "installed",
                 "doc_hash", "doc_format_version")
_SORT_COLUMNS = {"name": "name", "install_date": "enriched_at",
                 "last_updated": "last_updated", "artist": "artist"}
# Keep IN (...) lists well under SQLite's bound-parameter limit.
_CHUNK = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunks(items: list, size: int = _CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [v for v in value if v]


class CatalogStore:
    def __init__(self, db_path: str, legacy_table: Optional[str] = None, lazy: bool = False):
        """``lazy`` defers the migration to first use, so constructing the store (e.g. at
        import time) does no SQLite I/O."""
        self.db_path = db_path
        self._legacy_table = legacy_table
        self._migration_error: Optional[str] = None
        self._migrated = False
        if not lazy:
            self.ensure_migrated()

    def ensure_migrated(self) -> None:
        if self._migrated:
            return
        self._migrated = True
        try:
            migrate(self.db_path, legacy_table=self._legacy_table)
        except Exception as e:  # never block startup; callers report it via status/health
            self._migration_error = str(e)
            logger.error("Catalog migration failed (restart to retry): %s", e)

    @property
    def migration_error(self) -> Optional[str]:
        self.ensure_migrated()
        return self._migration_error

    @migration_error.setter
    def migration_error(self, value: Optional[str]) -> None:
        self._migrated = True
        self._migration_error = value

    @contextmanager
    def _conn(self):
        self.ensure_migrated()
        conn = connect(self.db_path)
        try:
            with conn:  # commit on success, roll back on error
                yield conn
        finally:
            conn.close()

    # --- writes ----------------------------------------------------------------------

    def upsert_source(self, source: str, source_key: str, asset_id: str, payload: dict,
                      source_modified: Optional[str], *, fetch_error: Optional[str] = None,
                      now: Optional[str] = None) -> None:
        """Record what ``source`` says about ``asset_id``; a failed fetch keeps the old payload."""
        now = now or _now()
        sku = source_key if source.startswith("daz_") else None
        body = json.dumps(payload, ensure_ascii=False)
        phash = payload_hash(payload)
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO assets (asset_id, sku, created_at) VALUES (?, ?, ?)",
                         (asset_id, sku, now))
            if fetch_error is None:
                conn.execute(
                    "INSERT INTO source_records (source, source_key, asset_id, present, first_seen,"
                    " last_seen, removed_at, source_modified, payload, payload_hash, fetched_at,"
                    " fetch_error) VALUES (?, ?, ?, 1, ?, ?, NULL, ?, ?, ?, ?, NULL)"
                    " ON CONFLICT (source, source_key) DO UPDATE SET present = 1,"
                    " last_seen = excluded.last_seen, removed_at = NULL,"
                    " source_modified = excluded.source_modified, payload = excluded.payload,"
                    " payload_hash = excluded.payload_hash, fetched_at = excluded.fetched_at,"
                    " fetch_error = NULL",
                    (source, source_key, asset_id, now, now, source_modified, body, phash, now))
            else:
                # A new record stores the (probably empty) payload with no hash and no
                # fetched_at; an existing one keeps payload, hash, source_modified, fetched_at.
                conn.execute(
                    "INSERT INTO source_records (source, source_key, asset_id, present, first_seen,"
                    " last_seen, removed_at, source_modified, payload, payload_hash, fetched_at,"
                    " fetch_error) VALUES (?, ?, ?, 1, ?, ?, NULL, ?, ?, NULL, NULL, ?)"
                    " ON CONFLICT (source, source_key) DO UPDATE SET present = 1,"
                    " last_seen = excluded.last_seen, removed_at = NULL,"
                    " fetch_error = excluded.fetch_error",
                    (source, source_key, asset_id, now, now, source_modified, body, fetch_error))

    def mark_absent(self, source: str, present_keys: set, now: Optional[str] = None) -> list:
        """Mark records of ``source`` whose key is not in ``present_keys`` as removed."""
        now = now or _now()
        present_keys = {str(k) for k in present_keys}
        with self._conn() as conn:
            gone = [(r["source_key"], r["asset_id"]) for r in conn.execute(
                "SELECT source_key, asset_id FROM source_records WHERE source = ? AND present = 1",
                (source,)) if r["source_key"] not in present_keys]
            conn.executemany(
                "UPDATE source_records SET present = 0, removed_at = ?"
                " WHERE source = ? AND source_key = ?",
                [(now, source, key) for key, _ in gone])
        return [asset_id for _, asset_id in gone]

    def mark_present(self, source: str, keys: Iterable[str], now: Optional[str] = None) -> list:
        """Mark currently-absent records of ``source`` present again; return their asset ids.

        Only ``present``, ``removed_at`` and ``last_seen`` change; payload, hashes,
        ``source_modified`` and ``fetched_at`` are kept for the caller to refresh.
        """
        now = now or _now()
        keys = list(dict.fromkeys(str(k) for k in keys))
        back = []
        with self._conn() as conn:
            for chunk in _chunks(keys):
                back += [(r["source_key"], r["asset_id"]) for r in conn.execute(
                    f"SELECT source_key, asset_id FROM source_records WHERE source = ?"
                    f" AND present = 0 AND source_key IN ({','.join('?' * len(chunk))})",
                    [source] + chunk)]
            conn.executemany(
                "UPDATE source_records SET present = 1, removed_at = NULL, last_seen = ?"
                " WHERE source = ? AND source_key = ?", [(now, source, k) for k, _ in back])
        return [asset_id for _, asset_id in back]

    def refresh_views(self, asset_ids: Iterable[str], figure_names: list) -> None:
        """Rebuild ``asset_view`` and lookup rows for ``asset_ids`` from their source records."""
        ids = list(dict.fromkeys(asset_ids))
        if not ids:
            return
        with self._conn() as conn:
            for chunk in _chunks(ids):
                marks = ",".join("?" * len(chunk))
                records: dict = {a: {} for a in chunk}
                for r in conn.execute(
                        f"SELECT * FROM source_records WHERE asset_id IN ({marks})", chunk):
                    records[r["asset_id"]][r["source"]] = r
                skus = dict(conn.execute(
                    f"SELECT asset_id, sku FROM assets WHERE asset_id IN ({marks})", chunk).fetchall())
                rows, lookups, orphans = [], [], []
                for asset_id in chunk:
                    recs = records[asset_id]
                    if not recs:
                        orphans.append(asset_id)
                        continue
                    cms, web = recs.get(CMS_SOURCE), recs.get(WEB_SOURCE)
                    row = build_view_row(asset_id, json.loads(cms["payload"]) if cms else None,
                                         json.loads(web["payload"]) if web else None,
                                         figure_names)
                    lookups.append((asset_id, row.pop("artists_list"), row.pop("tags_list"),
                                    row.pop("figures_list")))
                    row["sku"] = skus.get(asset_id) or row["sku"]
                    row["installed"] = 1 if cms is not None and cms["present"] else 0
                    row["enriched_at"] = ((web["fetched_at"] if web else None)
                                          or (cms["fetched_at"] if cms else None))
                    row["doc_format_version"] = DOC_FORMAT_VERSION
                    row["doc_hash"] = doc_hash(row)
                    rows.append(tuple(row[c] for c in _VIEW_COLUMNS))
                conn.executemany(
                    f"INSERT OR REPLACE INTO asset_view ({', '.join(_VIEW_COLUMNS)})"
                    f" VALUES ({', '.join('?' * len(_VIEW_COLUMNS))})", rows)
                if orphans:
                    conn.executemany("DELETE FROM asset_view WHERE asset_id = ?",
                                     [(a,) for a in orphans])
                replace_lookups(conn, lookups + [(a, [], [], []) for a in orphans])

    # --- source reads ----------------------------------------------------------------

    def source_index(self, source: str) -> dict:
        with self._conn() as conn:
            return {r["source_key"]: {"asset_id": r["asset_id"], "present": r["present"],
                                      "source_modified": r["source_modified"],
                                      "payload_hash": r["payload_hash"],
                                      "fetched_at": r["fetched_at"]}
                    for r in conn.execute(
                        "SELECT source_key, asset_id, present, source_modified, payload_hash,"
                        " fetched_at FROM source_records WHERE source = ?", (source,))}

    def present_keys(self, source: str) -> set:
        """Keys of the records ``source`` currently lists (``present = 1``)."""
        with self._conn() as conn:
            return {r[0] for r in conn.execute(
                "SELECT source_key FROM source_records WHERE source = ? AND present = 1", (source,))}

    def present_modified(self, source: str) -> dict:
        """``{key: source_modified}`` of the records ``source`` currently lists."""
        with self._conn() as conn:
            return {r[0]: r[1] for r in conn.execute(
                "SELECT source_key, source_modified FROM source_records"
                " WHERE source = ? AND present = 1", (source,))}

    # --- filters ---------------------------------------------------------------------

    @staticmethod
    def _filter_clauses(category=None, artists=None, compatible_figures=None, tags=None):
        """SQL conditions on ``asset_view v``: OR within a list, AND across lists, exact match."""
        conditions, params = [], []
        cats = _as_list(category)
        if cats:
            conditions.append(f"v.category IN ({','.join('?' * len(cats))})")
            params += cats
        (artist_t, artist_l, artist_c), (tag_t, tag_l, tag_c), (fig_t, fig_l, fig_c) = LOOKUPS
        for values, names, links, col in ((artists, artist_t, artist_l, artist_c),
                                          (tags, tag_t, tag_l, tag_c),
                                          (compatible_figures, fig_t, fig_l, fig_c)):
            values = _as_list(values)
            if values:
                conditions.append(
                    f"v.asset_id IN (SELECT l.asset_id FROM {links} l JOIN {names} n"
                    f" ON n.id = l.{col} WHERE n.name IN ({','.join('?' * len(values))}))")
                params += values
        return conditions, params

    def asset_ids_matching(self, category=None, artists=None, compatible_figures=None,
                           tags=None, installed: Optional[bool] = None) -> Optional[set]:
        """Asset ids passing the exact filters, or None when no filter is given."""
        conditions, params = self._filter_clauses(category, artists, compatible_figures, tags)
        if installed is not None:
            conditions.append("v.installed = ?")
            params.append(1 if installed else 0)
        if not conditions:
            return None
        with self._conn() as conn:
            return {r[0] for r in conn.execute(
                f"SELECT v.asset_id FROM asset_view v WHERE {' AND '.join(conditions)}", params)}

    # --- view reads ------------------------------------------------------------------

    def get_products(self, page: int = 1, page_size: int = 25, category: Optional[str] = None,
                     artist: Optional[str] = None, compatible_figure: Optional[str] = None,
                     name_query: Optional[str] = None, sort_by: str = "name",
                     sort_dir: str = "asc", installed: Optional[bool] = None) -> dict:
        """Paginated, filtered, sorted view rows (same shape as the legacy SQLiteWrapper)."""
        sort_col = _SORT_COLUMNS.get(sort_by, "name")
        order = "ASC" if sort_dir.lower() == "asc" else "DESC"
        conditions, params = self._filter_clauses(
            category=category or None,
            artists=[artist] if artist else None,
            compatible_figures=[compatible_figure.strip()] if compatible_figure else None)
        if name_query:
            conditions.append("v.name LIKE ?")
            params.append(f"%{name_query}%")
        if installed is not None:
            conditions.append("v.installed = ?")
            params.append(1 if installed else 0)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM asset_view v {where}", params).fetchone()[0]
            rows = [dict(r) for r in conn.execute(
                f"SELECT v.* FROM asset_view v {where} ORDER BY v.{sort_col} {order}, v.asset_id"
                " LIMIT ? OFFSET ?", params + [page_size, (page - 1) * page_size])]
        return {"products": rows, "total": total, "page": page, "page_size": page_size,
                "total_pages": max(1, (total + page_size - 1) // page_size)}

    def get_by_sku(self, sku: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM asset_view WHERE sku = ?", (sku,)).fetchone()
        return dict(row) if row else None

    def get_rows(self, asset_ids: list) -> list:
        """View rows for ``asset_ids`` in the given order; unknown ids are skipped."""
        ids = list(asset_ids)
        found = {}
        with self._conn() as conn:
            for chunk in _chunks(list(dict.fromkeys(ids))):
                for r in conn.execute(
                        f"SELECT * FROM asset_view WHERE asset_id IN ({','.join('?' * len(chunk))})",
                        chunk):
                    found[r["asset_id"]] = dict(r)
        return [found[a] for a in ids if a in found]

    def get_filter_values(self) -> dict:
        with self._conn() as conn:
            def names(sql):
                return sorted(r[0] for r in conn.execute(sql))
            return {
                "categories": names("SELECT DISTINCT category FROM asset_view"
                                    " WHERE category IS NOT NULL AND category != ''"),
                "artists": names("SELECT name FROM artists"),
                "compatible_figures": names("SELECT name FROM figures"),
            }

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM asset_view").fetchone()[0]

    def get_last_updated(self) -> str:
        """Most recent ``last_updated`` across assets, or "N/A" (legacy contract) if none."""
        with self._conn() as conn:
            value = conn.execute("SELECT MAX(last_updated) FROM asset_view"
                                 " WHERE last_updated IS NOT NULL AND last_updated != ''").fetchone()[0]
        return value or "N/A"

    def asset_totals(self) -> dict:
        with self._conn() as conn:
            total, inst = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(installed), 0) FROM asset_view").fetchone()
            errors = conn.execute("SELECT COUNT(DISTINCT asset_id) FROM source_records"
                                  " WHERE fetch_error IS NOT NULL").fetchone()[0]
        return {"total": total, "installed": inst, "not_installed": total - inst,
                "fetch_errors": errors}
