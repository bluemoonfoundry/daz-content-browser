"""One-off script used to build cpp/tests/fixtures/morph_index.db from the real
root-level morph_index.db. Not part of the build; kept for reproducibility.

Run from repo root:
    .venv\\Scripts\\python cpp\\tests\\fixtures\\build_fixture_db.py
"""
import os
import sqlite3

# NOTE: this worktree doesn't have a real morph_index.db of its own -- the
# task briefing points at the main checkout's root-level db as the source.
SRC_DB = "Y:/working/BlueMoonFoundry/daz-content-browser/morph_index.db"
DST_DB = os.path.join(os.path.dirname(__file__), "morph_index.db")

# Chosen fixture rows:
#   3027 - "BaseFeminine_body_cbs_thigh_x115n_l", has non-null formulas_json,
#          and has two dependency edges (-> 3074, -> 3026).
#   3074 - "body_cbs_thigh_x115n_l", formulas_json NULL, referenced by 3027.
#   3026 - "BaseFeminine_body_bs_Body", formulas_json NULL, referenced by 3027.
MORPH_IDS = (3027, 3074, 3026)

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


def main():
    if os.path.exists(DST_DB):
        os.remove(DST_DB)

    dst = sqlite3.connect(DST_DB)
    dst.executescript(_SCHEMA)

    src = sqlite3.connect(SRC_DB)
    src.row_factory = sqlite3.Row

    placeholders = ",".join("?" * len(MORPH_IDS))
    morph_rows = src.execute(
        f"SELECT * FROM morphs WHERE morph_id IN ({placeholders})", MORPH_IDS
    ).fetchall()
    assert len(morph_rows) == len(MORPH_IDS), "expected all fixture morph_ids to exist in source db"

    cols = [d[0] for d in morph_rows[0].keys()] if False else morph_rows[0].keys()
    col_names = list(morph_rows[0].keys())
    insert_sql = (
        f"INSERT INTO morphs ({', '.join(col_names)}) VALUES ({', '.join('?' for _ in col_names)})"
    )
    for row in morph_rows:
        dst.execute(insert_sql, tuple(row[c] for c in col_names))

    dep_rows = src.execute(
        f"SELECT dependent_morph_id, referenced_morph_id FROM morph_dependencies "
        f"WHERE dependent_morph_id IN ({placeholders})",
        MORPH_IDS,
    ).fetchall()
    for row in dep_rows:
        dst.execute(
            "INSERT INTO morph_dependencies (dependent_morph_id, referenced_morph_id) VALUES (?, ?)",
            (row["dependent_morph_id"], row["referenced_morph_id"]),
        )

    dst.commit()
    src.close()
    dst.close()

    print(f"Wrote fixture db to {DST_DB}")
    print(f"  morphs: {len(morph_rows)}, dependency edges: {len(dep_rows)}")


if __name__ == "__main__":
    main()
