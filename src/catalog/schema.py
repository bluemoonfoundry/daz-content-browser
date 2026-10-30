"""Versioned SQLite schema for the source-aware catalog.

The schema version lives in ``PRAGMA user_version``. Each migration runs in its own
``BEGIN IMMEDIATE`` transaction and the version is re-read after the write lock is held,
so concurrent callers apply each migration exactly once. Migrations coexist with the
legacy wide ``products`` table in the same file and never touch it.
"""

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Optional


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


_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection, MigrationContext], None]]] = [
    (1, _migration_1),
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
