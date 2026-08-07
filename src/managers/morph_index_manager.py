import os
import sqlite3
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS morphs (
    morph_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guid            TEXT UNIQUE NOT NULL,
    label           TEXT NOT NULL,
    name            TEXT NOT NULL,
    target_figure   TEXT,
    group_path      TEXT,
    source_dsf_path TEXT NOT NULL,
    tmb_path        TEXT NOT NULL,
    vertex_count    INTEGER NOT NULL,
    delta_count     INTEGER NOT NULL,
    min_value       REAL DEFAULT 0.0,
    max_value       REAL DEFAULT 1.0,
    is_clamped      BOOLEAN DEFAULT 1,
    formulas_json   TEXT,
    content_hash    TEXT NOT NULL,
    indexed_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS morph_dependencies (
    link_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dependent_morph_id   INTEGER NOT NULL,
    referenced_morph_id  INTEGER NOT NULL,
    FOREIGN KEY(dependent_morph_id) REFERENCES morphs(morph_id),
    FOREIGN KEY(referenced_morph_id) REFERENCES morphs(morph_id)
);

CREATE INDEX IF NOT EXISTS idx_target_figure ON morphs(target_figure);
CREATE INDEX IF NOT EXISTS idx_dep_dependent ON morph_dependencies(dependent_morph_id);
CREATE INDEX IF NOT EXISTS idx_dep_referenced ON morph_dependencies(referenced_morph_id);
CREATE INDEX IF NOT EXISTS idx_source_dsf_path ON morphs(source_dsf_path);
"""

_UPSERT_MORPH = """
INSERT INTO morphs (
    guid, label, name, target_figure, group_path, source_dsf_path, tmb_path,
    vertex_count, delta_count, min_value, max_value, is_clamped, formulas_json,
    content_hash, indexed_at
) VALUES (
    :guid, :label, :name, :target_figure, :group_path, :source_dsf_path, :tmb_path,
    :vertex_count, :delta_count, :min_value, :max_value, :is_clamped, :formulas_json,
    :content_hash, :indexed_at
)
ON CONFLICT(guid) DO UPDATE SET
    label=excluded.label, name=excluded.name, target_figure=excluded.target_figure,
    group_path=excluded.group_path, source_dsf_path=excluded.source_dsf_path,
    tmb_path=excluded.tmb_path, vertex_count=excluded.vertex_count,
    delta_count=excluded.delta_count, min_value=excluded.min_value,
    max_value=excluded.max_value, is_clamped=excluded.is_clamped,
    formulas_json=excluded.formulas_json, content_hash=excluded.content_hash,
    indexed_at=excluded.indexed_at
"""


class MorphIndexManager:
    """SQLite manager for morph_index.db (metadata, ERC formulas, dependency graph)."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def setup_db(self, force_reset: bool = False) -> None:
        if force_reset and os.path.exists(self.db_path):
            logger.info(f"--force: deleting existing morph index at {self.db_path!r}")
            os.remove(self.db_path)
        conn = self.get_connection()
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        logger.info(f"Morph index ready at {self.db_path!r}")

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def get_content_hash(self, guid: str):
        conn = self.get_connection()
        row = conn.execute("SELECT content_hash FROM morphs WHERE guid = ?", (guid,)).fetchone()
        conn.close()
        return row["content_hash"] if row else None

    def get_content_hash_by_source_path(self, source_dsf_path: str):
        conn = self.get_connection()
        row = conn.execute(
            "SELECT content_hash FROM morphs WHERE source_dsf_path = ?", (source_dsf_path,)
        ).fetchone()
        conn.close()
        return row["content_hash"] if row else None

    def insert_morph(self, record: dict) -> int:
        payload = dict(record)
        payload["indexed_at"] = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        conn.execute(_UPSERT_MORPH, payload)
        conn.commit()
        morph_id = conn.execute(
            "SELECT morph_id FROM morphs WHERE guid = ?", (record["guid"],)
        ).fetchone()["morph_id"]
        conn.close()
        return morph_id

    def get_morph_id_by_guid(self, guid: str):
        conn = self.get_connection()
        row = conn.execute("SELECT morph_id FROM morphs WHERE guid = ?", (guid,)).fetchone()
        conn.close()
        return row["morph_id"] if row else None

    def get_morphs_by_guids(self, guids: list):
        if not guids:
            return []
        conn = self.get_connection()
        placeholders = ",".join(["?"] * len(guids))
        rows = conn.execute(
            f"SELECT * FROM morphs WHERE guid IN ({placeholders})", guids
        ).fetchall()
        conn.close()
        return rows

    def rebuild_dependencies(self, extract_referenced_guids_fn) -> int:
        conn = self.get_connection()
        conn.execute("DELETE FROM morph_dependencies")
        rows = conn.execute("SELECT morph_id, guid, name, formulas_json FROM morphs").fetchall()
        guid_to_id = {row["guid"]: row["morph_id"] for row in rows}
        name_to_id = {row["name"]: row["morph_id"] for row in rows}

        inserted = 0
        for row in rows:
            for ref in extract_referenced_guids_fn(row["formulas_json"]):
                if ref.startswith("name:"):
                    referenced_id = name_to_id.get(ref[len("name:"):])
                else:
                    referenced_id = guid_to_id.get(ref)
                if referenced_id is None:
                    continue
                conn.execute(
                    "INSERT INTO morph_dependencies (dependent_morph_id, referenced_morph_id) VALUES (?, ?)",
                    (row["morph_id"], referenced_id),
                )
                inserted += 1
        conn.commit()
        conn.close()
        return inserted

    def get_stats(self) -> dict:
        conn = self.get_connection()
        morph_count = conn.execute("SELECT COUNT(*) AS c FROM morphs").fetchone()["c"]
        dep_count = conn.execute("SELECT COUNT(*) AS c FROM morph_dependencies").fetchone()["c"]
        conn.close()
        return {"morph_count": morph_count, "dependency_count": dep_count}
