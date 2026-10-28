# Design: Morph Ingest & Transpiler (JIT Binary Morph Loader — Subsystem A)

**Status:** Approved for planning
**Parent spec:** JIT Binary Morph Loader & Semantic Asset Manager (Option 3 Architecture), v1.0 — provided by user, not checked into repo
**Scope:** This document covers only Subsystem A (Offline Ingest Engine / Transpiler). The parent spec's Subsystems B–D (C++ Daz Studio SDK injector plugin, scene-lifecycle hooks, Qt UI panel) are out of scope here and will get their own specs once this subsystem's output data exists to build against.

## 1. Problem & Goal

The parent spec's JIT morph loader needs, as a prerequisite, an offline pipeline that:
1. Walks a DAZ content library's `data/` tree for `.dsf` morph files
2. Converts their vertex deltas into a compact binary format (`.tmb`) for fast later loading
3. Extracts morph metadata and ERC (formula) dependency information into SQLite
4. Generates semantic embeddings (label/group/vendor text) into ChromaDB for search

This is a standalone, testable Python subsystem that fits the existing codebase's stack (FastAPI/SQLite/ChromaDB/ONNX embeddings) and produces the data that a future C++ injector plugin will consume. It does not touch Daz Studio, the SDK, or any UI.

## 2. Key Finding: Real `.dsf` Formulas Diverge from the Parent Spec

The parent spec's proposed `erc_relationships` table (`parent_morph_id`, `child_morph_id`, `stage`, `scalar_multiplier`) assumes ERC dependencies are simple single-multiply parent→child links. Inspection of real files in the user's DAZ library (`X:\DAZ Libraries\Project\data`) shows formulas are actually RPN-style operation stacks that can reference arbitrary properties, not just other morphs:

```json
"formulas": {
  "output": "GnHdCloak_G3_23369:#pJCMCloakBend_m90?value",
  "operations": [
    { "op": "push", "url": "Cloak:/data/%21Daz%20Original/G3HoodedCloak/Hooded%20Cloak/GnHdCloak_G3_23369.dsf#Cloak?rotation/x" },
    { "op": "push", "val": -0.01111111 },
    { "op": "mult" }
  ]
}
```

This example is a JCM (joint-controlled morph) driven by a bone rotation, not by another morph — the flattened parent/child/scalar model cannot represent it. The schema below replaces that table with a raw operation-stack capture plus a best-effort morph-to-morph dependency edge table.

## 3. Architecture & Data Flow

```
DAZ Library data/**/*.dsf (read-only source)
        │
        ▼
  vab morphs index  (new CLI subcommand)
        │
        ├─► walk + filter (type=modifier, has morph.deltas)
        │
        ├─► write morph_cache/<mirrored-path>.tmb   (binary deltas)
        │
        ├─► insert into morph_index.db (SQLite)      (metadata, raw formulas, dependency edges)
        │
        └─► batch embed (label + group_path + vendor) via embedding_utils.py
                   │
                   ▼
            ChromaDB "morphs" collection (separate from the existing product collection)
```

New files, following existing project layout conventions:

| File | Role |
|---|---|
| `src/managers/morph_index_manager.py` | SQLite wrapper for `morph_index.db`, parallel to `sqlite_db_manager.py` |
| `src/managers/morph_transpiler.py` | `.dsf` → `.tmb` conversion + morph_index population, parallel to `postgres_db_manager.py`'s ETL role (source is the filesystem here, not Postgres) |
| `src/tmb_format.py` | Binary read/write helpers for `.tmb` |
| CLI wiring in `main.py` | New `vab morphs index` subcommand alongside existing `vab` subcommands |

**DB isolation:** `morph_index.db` and the Chroma `morphs` collection are separate from the existing product-level `SQLiteDBManager` database and product Chroma collection. Morphs are a structurally different entity (per-vertex binary payload, dependency graph) and keeping them decoupled avoids coupling product-catalog concerns to morph concerns.

**Embedding reuse:** the morph indexer reuses `src/embedding_utils.py`'s already-loaded ONNX model (BAAI/bge-large-en-v1.5) rather than standing up a separate embedding stack, and follows the existing `.env` → `settings.json` config layering.

## 4. SQLite Schema (`morph_index.db`)

```sql
CREATE TABLE morphs (
    morph_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guid            TEXT UNIQUE NOT NULL,   -- e.g. "/data/%21Daz%20Original/.../Billow.dsf"
    label           TEXT NOT NULL,          -- channel.label (falls back to name if blank)
    name            TEXT NOT NULL,          -- morph id/name
    target_figure   TEXT,                   -- resolved from `parent` geometry ref where possible; NULL if unresolved
    group_path      TEXT,                   -- modifier_library[].group
    source_dsf_path TEXT NOT NULL,          -- absolute path to source .dsf, for re-ingest/debugging
    tmb_path        TEXT NOT NULL,          -- relative path under morph_cache/
    vertex_count    INTEGER NOT NULL,       -- base mesh vertex count
    delta_count     INTEGER NOT NULL,       -- number of sparse delta rows actually present
    min_value       REAL DEFAULT 0.0,
    max_value       REAL DEFAULT 1.0,
    is_clamped      BOOLEAN DEFAULT 1,
    formulas_json   TEXT,                   -- raw operations stack, verbatim, NULL if none
    content_hash    TEXT NOT NULL,          -- hash of source .dsf mtime+size, for incremental skip
    indexed_at      TEXT NOT NULL
);

-- Best-effort graph edges extracted from formulas_json where a "push url"
-- operand resolves to another *indexed* morph's guid. Non-morph operands
-- (bone rotations, etc.) are NOT represented here — they only exist in
-- formulas_json on the dependent morph.
CREATE TABLE morph_dependencies (
    link_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dependent_morph_id   INTEGER NOT NULL,   -- the morph whose formula references another morph
    referenced_morph_id  INTEGER NOT NULL,   -- the morph being referenced
    FOREIGN KEY(dependent_morph_id) REFERENCES morphs(morph_id),
    FOREIGN KEY(referenced_morph_id) REFERENCES morphs(morph_id)
);

CREATE INDEX idx_target_figure ON morphs(target_figure);
CREATE INDEX idx_dep_dependent ON morph_dependencies(dependent_morph_id);
CREATE INDEX idx_dep_referenced ON morph_dependencies(referenced_morph_id);
```

Deviations from the parent spec's original schema:
- Dropped `erc_relationships(stage, scalar_multiplier)` — replaced by `formulas_json` (raw, verbatim capture) + `morph_dependencies` (best-effort morph-to-morph edges only). See Section 2.
- `target_figure` is best-effort, not guaranteed — the `parent` field is a geometry URL, not always a clean figure name. Resolution failures leave it `NULL` rather than aborting ingest.
- Added `content_hash` / `indexed_at` for incremental re-runs, matching the existing product indexer's incremental pattern.

## 5. `.tmb` Binary Format

Matches the real `morph.deltas.values` shape (`[vertex_index, dx, dy, dz]` per row). One deviation from the parent spec: `vertex_count` is **signed** (int32), not uint32 — real `.dsf` files legitimately use `-1` as a documented DSON sentinel meaning "unspecified/same as base mesh" (found during the full real-library run; see `docs/superpowers/plans/2026-08-06-morph-ingest-transpiler.md` follow-up `daz-content-browser-uc9`). Packing it as unsigned raised `struct.error` and silently dropped every affected file.

```
HEADER (16 bytes):
  magic bytes        "TMB1"        (4 bytes)
  vertex_count        int32         (4 bytes)  -- base mesh vertex count; -1 = unspecified (DSON sentinel)
  delta_count         uint32        (4 bytes)  -- sparse delta rows present
  reserved                          (4 bytes)

DATA (delta_count × 16 bytes):
  vertex_index  uint32
  dx            float32
  dy            float32
  dz            float32
```

Deltas are sparse relative to the base mesh (e.g. the `Billow.dsf` example has 18,503 deltas against a 23,369-vertex mesh) — `delta_count` and `vertex_count` are deliberately separate fields so a future C++ reader can bounds-check indices without re-deriving mesh size.

## 6. Ingest Filtering Rule

Only `modifier_library[]` entries where `type == "modifier"` **and** a non-empty `morph.deltas` block is present are ingested. Modifier types with no deltas of their own (pure geometry files, pose-control-only modifiers, zero-delta "master dial" controllers that exist only to drive ERC formulas on other morphs) are skipped for this subsystem. This may leave some formula references pointing at unindexed morphs — those stay visible in `formulas_json` but won't get a `morph_dependencies` row, since that table only links morphs both present in `morphs`.

## 7. `.tmb` Output Location

Generated `.tmb` files are written to an app-managed `morph_cache/` directory (sibling to the existing `chroma_db/` directory), mirroring the relative path of each source `.dsf`. The DAZ content library itself is treated as read-only source data — nothing is written back into it.

## 8. CLI

```
vab morphs index --library-path <path> [--force]
```

- `--library-path` (or a new `MORPH_LIBRARY_PATH` setting, following existing `.env` → `settings.json` layering) points at the DAZ library root containing `data/`.
- Default behavior is incremental: a `.dsf` file is skipped if its `content_hash` (derived from mtime + size) already matches a row in `morphs`.
- `--force` wipes `morph_index.db`, `morph_cache/`, and the Chroma `morphs` collection, then re-ingests everything from scratch — mirroring `vab load --force`'s semantics for the existing product pipeline.
- Progress is logged every N files; a final run summary reports files scanned, morphs ingested, files skipped (no deltas), and error count — following the existing product indexer's logging style.

## 9. Error Handling

Ingest is per-file try/except: a malformed `.dsf` or JSON parse error logs a warning with the file path and increments an error counter, then ingest continues to the next file. With ~321K files in a real library, partial per-file failures are expected, not exceptional — no single bad file should abort a run. This applies the parent spec's "fail gracefully, don't crash" directive at file granularity, appropriate for the Python ingest side (the C++ injector subsystem will need its own, separate error-handling design later).

## 10. Testing

- **`.tmb` round-trip:** fixture-based unit tests for the binary writer/reader — write known deltas, read back, assert equality. No real library needed.
- **`.dsf` → row mapping:** unit tests using 2–3 real files copied into `tests/fixtures/dsf/` from the user's library — at minimum one plain morph with no formula (`Billow.dsf`) and one JCM with a bone-rotation-driven formula (`pJCMCloakBend_m90.dsf`), to exercise both the plain path and the raw-formula-capture path.
- **Integration test:** point the indexer at the fixtures directory, run it, and assert `morph_index.db` row counts, `morph_cache/*.tmb` file presence, and Chroma `morphs` collection count all match expectations.
- All tests run via the existing `.venv\Scripts\python -m pytest` in demo-mode-style isolation (temp dirs for `morph_index.db` / `morph_cache/`), not against the full 321K-file real library in CI.

## 11. Explicitly Out of Scope (Deferred to Later Specs)

- C++ Daz Studio SDK injector plugin (parent spec Subsystem B)
- Scene lifecycle hooks / custom scene manifest persistence (Subsystem C)
- Qt dockable search UI panel (Subsystem D)
- Any interpretation/evaluation of the RPN formula operation stacks — this subsystem only captures them verbatim; evaluating them against a live scene graph is the injector's job.
- `simdjson`/`ijson` streaming JSON parsing — stdlib `json.load` per file is used instead; this can be revisited if profiling on the full library shows it's the bottleneck (unlikely versus I/O and embedding cost).
