"""Versioned SQLite schema for the source-aware catalog.

The schema version lives in ``PRAGMA user_version``. Each migration runs in its own
``BEGIN IMMEDIATE`` transaction and the version is re-read after the write lock is held,
so concurrent callers apply each migration exactly once. Migration 1 creates the catalog
tables next to the legacy wide ``products`` table; migration 2 imports the legacy rows
verbatim, verifies them, and renames the legacy table to ``products_legacy``.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from catalog.documents import _split_list, doc_hash, payload_hash

logger = logging.getLogger(__name__)


class MigrationFailed(Exception):
    """A migration cannot proceed; its transaction is rolled back."""

    def __init__(self, version: int, reason: str):
        self.version = version
        self.reason = reason
        super().__init__(f"Migration {version} failed: {reason}")


@dataclass
class MigrationContext:
    legacy_table: Optional[str] = None


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    # Switching to WAL on a fresh file can fail with "locked" without honoring the busy
    # timeout when several connections race to do it, so retry briefly.
    for attempt in range(50):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError:
            if attempt == 49:
                raise
            time.sleep(0.05)
    return conn


_MIGRATION_1 = """
CREATE TABLE assets (
    asset_id   TEXT PRIMARY KEY,
    sku        TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX assets_sku ON assets (sku) WHERE sku IS NOT NULL;

CREATE TABLE source_records (
    source          TEXT NOT NULL,
    source_key      TEXT NOT NULL,
    asset_id        TEXT NOT NULL REFERENCES assets (asset_id),
    present         INTEGER NOT NULL DEFAULT 1,
    first_seen      TEXT,
    last_seen       TEXT,
    removed_at      TEXT,
    source_modified TEXT,
    payload         TEXT NOT NULL,
    payload_hash    TEXT,
    fetched_at      TEXT,
    fetch_error     TEXT,
    PRIMARY KEY (source, source_key)
);
CREATE INDEX source_records_asset ON source_records (asset_id);

-- Comma-joined artist/tags/compatible_figures/subcategories are display-only;
-- filters must use the normalized link tables below.
CREATE TABLE asset_view (
    asset_id            TEXT PRIMARY KEY REFERENCES assets (asset_id),
    sku                 TEXT,
    name                TEXT,
    artist              TEXT,
    url                 TEXT,
    image_url           TEXT,
    store               TEXT,
    price               TEXT,
    description         TEXT,
    tags                TEXT,
    formats             TEXT,
    poly_count          TEXT,
    textures_info       TEXT,
    required_products   TEXT,
    compatible_figures  TEXT,
    compatible_software TEXT,
    embedding_text      TEXT,
    last_updated        TEXT,
    category            TEXT,
    subcategories       TEXT,
    styles              TEXT,
    inferred_tags       TEXT,
    enriched_at         TEXT,
    mature              INTEGER,
    asset_count         INTEGER,
    installed           INTEGER NOT NULL,
    doc_hash            TEXT NOT NULL,
    doc_format_version  INTEGER NOT NULL
);

CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE asset_artists (
    asset_id  TEXT NOT NULL REFERENCES assets (asset_id),
    artist_id INTEGER NOT NULL REFERENCES artists (id),
    PRIMARY KEY (asset_id, artist_id)
);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE asset_tags (
    asset_id TEXT NOT NULL REFERENCES assets (asset_id),
    tag_id   INTEGER NOT NULL REFERENCES tags (id),
    PRIMARY KEY (asset_id, tag_id)
);
CREATE TABLE figures (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE asset_figures (
    asset_id  TEXT NOT NULL REFERENCES assets (asset_id),
    figure_id INTEGER NOT NULL REFERENCES figures (id),
    PRIMARY KEY (asset_id, figure_id)
);

CREATE TABLE index_generations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    status             TEXT NOT NULL CHECK (status IN ('building', 'active', 'retired', 'failed')),
    model_id           TEXT NOT NULL,
    model_dim          INTEGER NOT NULL,
    doc_format_version INTEGER NOT NULL,
    schema_version     INTEGER NOT NULL,
    collection_name    TEXT NOT NULL UNIQUE,
    refetch_started_at TEXT,
    created_at         TEXT NOT NULL,
    completed_at       TEXT,
    promoted_at        TEXT,
    error              TEXT
);
CREATE UNIQUE INDEX index_generations_one_active
    ON index_generations ((1)) WHERE status = 'active';
CREATE UNIQUE INDEX index_generations_one_building
    ON index_generations ((1)) WHERE status = 'building';

CREATE TABLE asset_vectors (
    generation_id INTEGER NOT NULL REFERENCES index_generations (id) ON DELETE CASCADE,
    asset_id      TEXT NOT NULL,
    vector_id     TEXT NOT NULL,
    doc_hash      TEXT,
    status        TEXT NOT NULL CHECK (status IN ('current', 'error')),
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    embedded_at   TEXT,
    PRIMARY KEY (generation_id, asset_id)
);
CREATE UNIQUE INDEX asset_vectors_vector ON asset_vectors (generation_id, vector_id);
"""


def _migration_1(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    # executescript() would COMMIT first; split statements to stay in the open transaction.
    statements = [s.strip() for s in _MIGRATION_1.split(";\n") if s.strip()]
    for stmt in statements:
        if sqlite3.complete_statement(stmt + ";"):
            conn.execute(stmt)


# --- lookup tables -------------------------------------------------------------------

# (name table, link table, link column) for artists, tags and figures, in that order.
LOOKUPS = (("artists", "asset_artists", "artist_id"),
           ("tags", "asset_tags", "tag_id"),
           ("figures", "asset_figures", "figure_id"))


def replace_lookups(conn: sqlite3.Connection,
                    entries: Iterable[tuple[str, list, list, list]]) -> None:
    """Rebuild the lookup links of each ``(asset_id, artists, tags, figures)`` entry.

    Names no asset links to any more are pruned, so the name tables hold exactly the
    values in use (``get_filter_values`` reads them directly).
    """
    entries = list(entries)
    if not entries:
        return
    ids = [(e[0],) for e in entries]
    for i, (names, links, col) in enumerate(LOOKUPS):
        conn.executemany(f"DELETE FROM {links} WHERE asset_id = ?", ids)
        pairs = [(e[0], name) for e in entries for name in e[i + 1]]
        conn.executemany(f"INSERT OR IGNORE INTO {names} (name) VALUES (?)",
                         [(name,) for name in {p[1] for p in pairs}])
        conn.executemany(f"INSERT OR IGNORE INTO {links} (asset_id, {col}) "
                         f"SELECT ?, id FROM {names} WHERE name = ?", pairs)
        conn.execute(f"DELETE FROM {names} WHERE id NOT IN (SELECT {col} FROM {links})")


# --- migration 2: lookup indexes + legacy import --------------------------------------

_MIGRATION_2_DDL = (
    "CREATE INDEX asset_artists_artist ON asset_artists (artist_id)",
    "CREATE INDEX asset_tags_tag ON asset_tags (tag_id)",
    "CREATE INDEX asset_figures_figure ON asset_figures (figure_id)",
    "CREATE INDEX asset_view_sku ON asset_view (sku)",
)

LEGACY_RENAMED = "products_legacy"

# Columns of the legacy wide table (sqlite_db_manager.setup_sqlite_db); asset_view has the
# same columns, so a legacy row is copied into the view verbatim.
LEGACY_COLUMNS = ("sku", "url", "image_url", "store", "name", "artist", "price", "description",
                  "tags", "formats", "poly_count", "textures_info", "required_products",
                  "compatible_figures", "compatible_software", "embedding_text", "last_updated",
                  "category", "subcategories", "styles", "inferred_tags", "enriched_at",
                  "mature", "asset_count")
LEGACY_WEB_FIELDS = ("url", "image_url", "store", "price", "description", "poly_count",
                     "textures_info", "required_products", "compatible_software", "mature")
# Legacy embedding text was produced by the original recipe, i.e. doc format 1.
_LEGACY_DOC_FORMAT_VERSION = 1
_VERIFIED_FIELDS = ("name", "embedding_text", "artist")
_SKU_PRESENT = "l.sku IS NOT NULL AND TRIM(l.sku) != ''"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? "
                        "COLLATE NOCASE", (name,)).fetchone() is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({_quote(table)})")}


def _legacy_cms_payload(row: dict) -> dict:
    return {
        "sku": row["sku"],
        "product_name": row["name"],
        "artists": row["artist"],
        "content_types": row["formats"],
        "content_item_count": row["asset_count"],
        "last_modified_date": row["last_updated"],
        "legacy": {"tags": row["tags"], "compatible_figures": row["compatible_figures"],
                   "category": row["category"], "subcategories": row["subcategories"]},
    }


def _copy_legacy_rows(conn: sqlite3.Connection, rows: list, now: str) -> None:
    """Bulk-insert assets, both source records, view rows and lookups for legacy rows."""
    def asset(r):
        return f"daz:{r['sku']}"

    conn.executemany("INSERT INTO assets (asset_id, sku, created_at) VALUES (?, ?, ?)",
                     [(asset(r), r["sku"], now) for r in rows])
    record_sql = ("INSERT INTO source_records (source, source_key, asset_id, present, first_seen,"
                  " last_seen, source_modified, payload, payload_hash, fetched_at)"
                  " VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)")
    # payload_hash stays NULL on the CMS record so the first reconcile refreshes the
    # payload from the live CMS without re-scraping (source_modified is unchanged).
    conn.executemany(record_sql, [
        ("daz_cms", r["sku"], asset(r), r["enriched_at"] or now, now, r["last_updated"],
         json.dumps(_legacy_cms_payload(r), ensure_ascii=False), None, r["enriched_at"])
        for r in rows])
    web = [{k: r[k] for k in LEGACY_WEB_FIELDS} for r in rows]
    conn.executemany(record_sql, [
        ("daz_store_web", r["sku"], asset(r), r["enriched_at"] or now, now, None,
         json.dumps(p, ensure_ascii=False), payload_hash(p), r["enriched_at"])
        for r, p in zip(rows, web)])
    view_cols = ("asset_id",) + LEGACY_COLUMNS + ("installed", "doc_hash", "doc_format_version")
    conn.executemany(
        f"INSERT INTO asset_view ({', '.join(view_cols)}) "
        f"VALUES ({', '.join('?' * len(view_cols))})",
        [(asset(r),) + tuple(r[c] for c in LEGACY_COLUMNS)
         + (1, doc_hash(r), _LEGACY_DOC_FORMAT_VERSION) for r in rows])
    replace_lookups(conn, [(asset(r), _split_list(r["artist"]), _split_list(r["tags"]),
                            _split_list(r["compatible_figures"])) for r in rows])


def _verify_legacy_import(conn: sqlite3.Connection, table: str, expected: int) -> Optional[str]:
    """Return why the imported catalog does not match the legacy table, or None."""
    for what, sql, want in (
            ("assets", "SELECT COUNT(*) FROM assets", expected),
            ("view rows", "SELECT COUNT(*) FROM asset_view", expected),
            ("source records", "SELECT COUNT(*) FROM source_records", 2 * expected)):
        got = conn.execute(sql).fetchone()[0]
        if got != want:
            return f"expected {want} {what}, found {got}"
    cols = _table_columns(conn, table)
    field_checks = " OR ".join(f"v.{c} IS NOT l.{c}" if c in cols else f"v.{c} IS NOT NULL"
                               for c in _VERIFIED_FIELDS)
    bad = conn.execute(f"""
        SELECT l.sku FROM {_quote(table)} AS l
        LEFT JOIN asset_view AS v ON v.asset_id = 'daz:' || l.sku
        LEFT JOIN source_records AS c
            ON c.source = 'daz_cms' AND c.source_key = l.sku AND c.asset_id = 'daz:' || l.sku
        LEFT JOIN source_records AS w
            ON w.source = 'daz_store_web' AND w.source_key = l.sku AND w.asset_id = 'daz:' || l.sku
        WHERE {_SKU_PRESENT}
          AND (v.asset_id IS NULL OR c.source IS NULL OR w.source IS NULL OR {field_checks})
        LIMIT 5""").fetchall()
    if bad:
        return "imported rows differ from legacy rows for SKUs: " + ", ".join(str(r[0]) for r in bad)
    return None


def _migration_2(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    for stmt in _MIGRATION_2_DDL:
        conn.execute(stmt)
    table = ctx.legacy_table
    if not table or not _table_exists(conn, table):
        return
    if _table_exists(conn, LEGACY_RENAMED):
        logger.warning("Not importing legacy table %r: %r already exists", table, LEGACY_RENAMED)
        return
    cols = _table_columns(conn, table)
    if "sku" not in cols:
        raise MigrationFailed(2, f"legacy table {table!r} has no sku column")
    select = ", ".join(f"l.{c}" if c in cols else f"NULL AS {c}" for c in LEGACY_COLUMNS)
    rows = [dict(r) for r in conn.execute(
        f"SELECT {select} FROM {_quote(table)} AS l WHERE {_SKU_PRESENT}")]
    total = conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
    if total != len(rows):
        logger.warning("Skipping %d legacy rows without a SKU (kept in %s)",
                       total - len(rows), LEGACY_RENAMED)
    for r in rows:
        r["sku"] = str(r["sku"])
    _copy_legacy_rows(conn, rows, datetime.now(timezone.utc).isoformat())
    reason = _verify_legacy_import(conn, table, len(rows))
    if reason:
        raise MigrationFailed(2, reason)
    conn.execute(f"ALTER TABLE {_quote(table)} RENAME TO {LEGACY_RENAMED}")
    logger.info("Imported %d legacy products; renamed %r to %r", len(rows), table, LEGACY_RENAMED)


_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection, MigrationContext], None]]] = [
    (1, _migration_1),
    (2, _migration_2),
]
LATEST_VERSION = _MIGRATIONS[-1][0]


def migrate(db_path: str, legacy_table: Optional[str] = None) -> int:
    """Apply pending migrations in order; return the final schema version."""
    ctx = MigrationContext(legacy_table=legacy_table)
    conn = connect(db_path)
    conn.isolation_level = None  # manual transaction control
    try:
        for version, fn in _MIGRATIONS:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= version:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Another process may have applied it while we waited for the lock.
                if conn.execute("PRAGMA user_version").fetchone()[0] >= version:
                    conn.execute("ROLLBACK")
                    continue
                fn(conn, ctx)
                conn.execute(f"PRAGMA user_version = {int(version)}")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
