# Morph Ingest & Transpiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Tracking note (project-specific):** This project tracks work with `bd` (beads), not TodoWrite/markdown checklists. Each task below should be filed as a `bd` issue (`bd create --title=... --type=task`) with `bd dep add` chains matching the task ordering, so work can proceed across bounded sessions. Claim (`bd update <id> --claim`) before starting a task, close (`bd close <id>`) when its steps and commit are done. The checkboxes below are the spec of what each issue must accomplish, not a replacement tracker.

**Goal:** Build the Python offline ingest pipeline that converts `.dsf` morph files from a DAZ content library into `.tmb` binary deltas, a `morph_index.db` SQLite metadata/dependency index, and a semantically searchable ChromaDB `morphs` collection.

**Architecture:** A pure-function parsing layer (`dsf_parser.py`, `tmb_format.py`) with no I/O side effects beyond reading/writing single files, wrapped by a stateful SQLite manager (`morph_index_manager.py`) and an orchestrator (`morph_transpiler.py`) that walks the library, calls the parsers, and drives incremental SQLite + ChromaDB writes. A new `vab morphs index` CLI subcommand wires it together, following the existing `vab load` command's structure in `main.py`.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`/`json`/`struct`, existing `chromadb` client and `embedding_utils.generate_embeddings`, `pytest`.

## Global Constraints

- Follow the approved design: `docs/superpowers/specs/2026-08-06-morph-ingest-transpiler-design.md` — every task below implements one section of it; deviations are called out inline.
- `morph_index.db` and the ChromaDB `morphs` collection are separate from the existing product-level SQLite DB and Chroma collection (design §3).
- Only `modifier_library[]` entries with `asset_info.type == "modifier"` and a non-empty `morph.deltas.values` are ingested (design §6).
- `.tmb` files are written to an app-managed `morph_cache/` directory mirroring source `.dsf` relative paths; the DAZ library itself is never written to (design §7).
- `.tmb` binary layout is fixed: 16-byte header (`"TMB1"`, `vertex_count` uint32, `delta_count` uint32, 4 reserved bytes) + `delta_count` × 16-byte records (`vertex_index` uint32, `dx`/`dy`/`dz` float32) (design §5).
- Real per-file failures must not abort a run — log and continue (design §9).
- All tests run via `.venv\Scripts\python -m pytest`, using temp dirs — no real 321K-file run in CI (design §10).
- Test fixtures are copied from the real library at `X:\DAZ Libraries\Project\data\!Daz Original\G3HoodedCloak\Hooded Cloak\Morphs\Daz Original\Base\` — specifically `Billow.dsf` (plain morph, no formula) and `pJCMCloakBend_m90.dsf` (JCM with a bone-rotation-driven formula).

---

### Task 1: `.tmb` binary format module

**Files:**
- Create: `src/tmb_format.py`
- Test: `tests/test_tmb_format.py`

**Interfaces:**
- Produces: `write_tmb(path: str, vertex_count: int, deltas: list[tuple[int, float, float, float]]) -> None`
- Produces: `read_tmb(path: str) -> tuple[int, list[tuple[int, float, float, float]]]` — returns `(vertex_count, deltas)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tmb_format.py
import struct
import pytest
from tmb_format import write_tmb, read_tmb


def test_write_then_read_round_trip(tmp_path):
    path = tmp_path / "test.tmb"
    deltas = [(0, -0.2948112, 0.6714706, -2.386154), (23368, 0.0, 0.0, 0.0)]

    write_tmb(str(path), vertex_count=23369, deltas=deltas)
    vertex_count, read_deltas = read_tmb(str(path))

    assert vertex_count == 23369
    assert len(read_deltas) == 2
    for original, roundtripped in zip(deltas, read_deltas):
        assert roundtripped[0] == original[0]
        assert roundtripped[1] == pytest.approx(original[1], abs=1e-6)
        assert roundtripped[2] == pytest.approx(original[2], abs=1e-6)
        assert roundtripped[3] == pytest.approx(original[3], abs=1e-6)


def test_write_empty_deltas(tmp_path):
    path = tmp_path / "empty.tmb"
    write_tmb(str(path), vertex_count=100, deltas=[])
    vertex_count, deltas = read_tmb(str(path))
    assert vertex_count == 100
    assert deltas == []


def test_read_rejects_bad_magic(tmp_path):
    path = tmp_path / "bad.tmb"
    path.write_bytes(b"NOPE" + b"\x00" * 12)
    with pytest.raises(ValueError, match="magic"):
        read_tmb(str(path))


def test_header_is_16_bytes_and_delta_is_16_bytes(tmp_path):
    path = tmp_path / "sizes.tmb"
    write_tmb(str(path), vertex_count=5, deltas=[(1, 1.0, 2.0, 3.0)])
    data = path.read_bytes()
    assert len(data) == 16 + 16
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests\test_tmb_format.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tmb_format'`

- [ ] **Step 3: Write the implementation**

```python
# src/tmb_format.py
"""Binary read/write for the .tmb (Turbo Morph Binary) format.

Layout:
  HEADER (16 bytes): magic b"TMB1" (4) | vertex_count uint32 (4) |
                      delta_count uint32 (4) | reserved (4, zero)
  DATA: delta_count x { vertex_index uint32, dx float32, dy float32, dz float32 }
"""

import struct

_MAGIC = b"TMB1"
_HEADER = struct.Struct("<4sII4x")
_DELTA = struct.Struct("<Ifff")


def write_tmb(path: str, vertex_count: int, deltas) -> None:
    """Writes a .tmb file. `deltas` is an iterable of (vertex_index, dx, dy, dz)."""
    deltas = list(deltas)
    with open(path, "wb") as f:
        f.write(_HEADER.pack(_MAGIC, vertex_count, len(deltas)))
        for vertex_index, dx, dy, dz in deltas:
            f.write(_DELTA.pack(vertex_index, dx, dy, dz))


def read_tmb(path: str):
    """Reads a .tmb file, returning (vertex_count, deltas) where deltas is a
    list of (vertex_index, dx, dy, dz) tuples."""
    with open(path, "rb") as f:
        header = f.read(_HEADER.size)
        magic, vertex_count, delta_count = _HEADER.unpack(header)
        if magic != _MAGIC:
            raise ValueError(f"Not a TMB file (bad magic bytes: {magic!r})")
        deltas = []
        for _ in range(delta_count):
            deltas.append(_DELTA.unpack(f.read(_DELTA.size)))
    return vertex_count, deltas
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests\test_tmb_format.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/tmb_format.py tests/test_tmb_format.py
git commit -m "feat: add .tmb binary format read/write"
```

---

### Task 2: `morph_index.db` SQLite manager

**Files:**
- Create: `src/managers/morph_index_manager.py`
- Test: `tests/test_morph_index_manager.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: class `MorphIndexManager(db_path: str)` with methods:
  - `setup_db(force_reset: bool = False) -> None`
  - `get_connection() -> sqlite3.Connection`
  - `get_content_hash(guid: str) -> str | None`
  - `insert_morph(record: dict) -> int` — record keys: `guid, label, name, target_figure, group_path, source_dsf_path, tmb_path, vertex_count, delta_count, min_value, max_value, is_clamped, formulas_json, content_hash`. Returns `morph_id`.
  - `get_morph_id_by_guid(guid: str) -> int | None`
  - `get_morphs_by_guids(guids: list[str]) -> list[sqlite3.Row]`
  - `rebuild_dependencies(extract_referenced_guids_fn) -> int` — clears and rebuilds `morph_dependencies` from all rows' `formulas_json`, using the passed extraction function (`str | None -> list[str]`, matching `dsf_parser.extract_referenced_guids`'s signature — injected as a parameter here so this file has no dependency on Task 4's module). Returns edge count inserted.
  - `get_stats() -> dict` — `{"morph_count": int, "dependency_count": int}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_morph_index_manager.py
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "managers"))

import pytest
from managers.morph_index_manager import MorphIndexManager


def make_record(guid="guid-1", **overrides):
    record = {
        "guid": guid,
        "label": "Billow",
        "name": "Billow",
        "target_figure": "GnHdCloak_G3_23369",
        "group_path": "Actor/CloakStyled",
        "source_dsf_path": r"X:\lib\data\Billow.dsf",
        "tmb_path": "data/Billow.tmb",
        "vertex_count": 23369,
        "delta_count": 18503,
        "min_value": 0.0,
        "max_value": 1.0,
        "is_clamped": True,
        "formulas_json": None,
        "content_hash": "hash-1",
    }
    record.update(overrides)
    return record


@pytest.fixture
def manager(tmp_path):
    mgr = MorphIndexManager(str(tmp_path / "morph_index.db"))
    mgr.setup_db()
    return mgr


def test_insert_and_lookup_by_guid(manager):
    morph_id = manager.insert_morph(make_record())
    assert manager.get_morph_id_by_guid("guid-1") == morph_id


def test_get_content_hash_returns_none_for_unknown_guid(manager):
    assert manager.get_content_hash("nope") is None


def test_get_content_hash_returns_stored_value(manager):
    manager.insert_morph(make_record(content_hash="abc123"))
    assert manager.get_content_hash("guid-1") == "abc123"


def test_insert_morph_upsert_preserves_morph_id_on_same_guid(manager):
    first_id = manager.insert_morph(make_record(label="Billow"))
    second_id = manager.insert_morph(make_record(label="Billow v2", content_hash="new-hash"))
    assert first_id == second_id
    assert manager.get_content_hash("guid-1") == "new-hash"


def test_get_morphs_by_guids(manager):
    manager.insert_morph(make_record(guid="a"))
    manager.insert_morph(make_record(guid="b"))
    manager.insert_morph(make_record(guid="c"))
    rows = manager.get_morphs_by_guids(["a", "c"])
    assert sorted(r["guid"] for r in rows) == ["a", "c"]


def test_rebuild_dependencies(manager):
    manager.insert_morph(make_record(guid="parent-guid", formulas_json='[{"op": "noop"}]'))
    manager.insert_morph(make_record(guid="child-guid"))

    def fake_extract(formulas_json):
        if formulas_json == '[{"op": "noop"}]':
            return ["child-guid", "unresolvable-guid"]
        return []

    count = manager.rebuild_dependencies(fake_extract)
    assert count == 1  # only "child-guid" resolves to a real morph
    stats = manager.get_stats()
    assert stats["morph_count"] == 2
    assert stats["dependency_count"] == 1


def test_setup_db_force_reset_wipes_existing_data(tmp_path):
    db_path = str(tmp_path / "morph_index.db")
    mgr = MorphIndexManager(db_path)
    mgr.setup_db()
    mgr.insert_morph(make_record())
    assert mgr.get_stats()["morph_count"] == 1

    mgr2 = MorphIndexManager(db_path)
    mgr2.setup_db(force_reset=True)
    assert mgr2.get_stats()["morph_count"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests\test_morph_index_manager.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'managers.morph_index_manager'`

- [ ] **Step 3: Write the implementation**

```python
# src/managers/morph_index_manager.py
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
        return conn

    def get_content_hash(self, guid: str):
        conn = self.get_connection()
        row = conn.execute("SELECT content_hash FROM morphs WHERE guid = ?", (guid,)).fetchone()
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
        rows = conn.execute("SELECT morph_id, guid, formulas_json FROM morphs").fetchall()
        guid_to_id = {row["guid"]: row["morph_id"] for row in rows}

        inserted = 0
        for row in rows:
            for ref_guid in extract_referenced_guids_fn(row["formulas_json"]):
                referenced_id = guid_to_id.get(ref_guid)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests\test_morph_index_manager.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/managers/morph_index_manager.py tests/test_morph_index_manager.py
git commit -m "feat: add morph_index.db SQLite manager"
```

---

### Task 3: `.dsf` parser + real fixtures

**Files:**
- Create: `src/dsf_parser.py`
- Create: `tests/fixtures/dsf/Billow.dsf` (copy of real file, see below)
- Create: `tests/fixtures/dsf/pJCMCloakBend_m90.dsf` (copy of real file, see below)
- Test: `tests/test_dsf_parser.py`

**Interfaces:**
- Produces: `@dataclass ParsedMorph` with fields `guid: str, label: str, name: str, target_figure: str | None, group_path: str | None, vertex_count: int, deltas: list[tuple[int, float, float, float]], min_value: float, max_value: float, is_clamped: bool, formulas_json: str | None`
- Produces: `parse_dsf_file(path: str) -> ParsedMorph | None`

- [ ] **Step 1: Copy real fixture files**

```bash
mkdir -p tests/fixtures/dsf
cp "/x/DAZ Libraries/Project/data/!Daz Original/G3HoodedCloak/Hooded Cloak/Morphs/Daz Original/Base/Billow.dsf" tests/fixtures/dsf/Billow.dsf
cp "/x/DAZ Libraries/Project/data/!Daz Original/G3HoodedCloak/Hooded Cloak/Morphs/Daz Original/Base/pJCMCloakBend_m90.dsf" tests/fixtures/dsf/pJCMCloakBend_m90.dsf
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_dsf_parser.py
import json
import os

import pytest
from dsf_parser import parse_dsf_file

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "dsf")


def test_parses_plain_morph():
    result = parse_dsf_file(os.path.join(FIXTURES, "Billow.dsf"))
    assert result is not None
    assert result.name == "Billow"
    assert result.label == "Billow"
    assert result.group_path == "Actor/CloakStyled"
    assert result.vertex_count == 23369
    assert len(result.deltas) == 18503
    assert result.deltas[0][0] == 0
    assert result.min_value == 0.0
    assert result.max_value == 1.0
    assert result.is_clamped is True
    assert result.formulas_json is None
    assert result.guid  # asset_info.id, non-empty


def test_parses_jcm_with_bone_driven_formula():
    result = parse_dsf_file(os.path.join(FIXTURES, "pJCMCloakBend_m90.dsf"))
    assert result is not None
    assert result.formulas_json is not None
    formulas = json.loads(result.formulas_json)
    assert formulas[0]["operations"][0]["op"] == "push"
    assert "rotation/x" in formulas[0]["operations"][0]["url"]


def test_returns_none_for_non_modifier_json(tmp_path):
    path = tmp_path / "not_a_modifier.dsf"
    path.write_text(json.dumps({"asset_info": {"type": "geometry", "id": "/x/y.dsf"}}))
    assert parse_dsf_file(str(path)) is None


def test_returns_none_for_modifier_with_no_deltas(tmp_path):
    path = tmp_path / "no_deltas.dsf"
    path.write_text(json.dumps({
        "asset_info": {"type": "modifier", "id": "/x/y.dsf"},
        "modifier_library": [{"id": "y", "name": "y", "channel": {}}],
    }))
    assert parse_dsf_file(str(path)) is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests\test_dsf_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dsf_parser'`

- [ ] **Step 4: Write the implementation**

```python
# src/dsf_parser.py
"""Parses DAZ .dsf modifier files into ParsedMorph records.

Only entries with asset_info.type == "modifier" and a non-empty
morph.deltas.values block are ingestible morphs (see design doc section 6);
everything else returns None.
"""

import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote


@dataclass
class ParsedMorph:
    guid: str
    label: str
    name: str
    target_figure: Optional[str]
    group_path: Optional[str]
    vertex_count: int
    deltas: list
    min_value: float
    max_value: float
    is_clamped: bool
    formulas_json: Optional[str]


def _resolve_target_figure(parent_url: Optional[str]) -> Optional[str]:
    """Best-effort figure name from a modifier's `parent` geometry URL, e.g.
    ".../GnHdCloak_G3_23369.dsf#geometry" -> "GnHdCloak_G3_23369".
    Returns None if parent_url is missing or has no usable path segment.
    """
    if not parent_url:
        return None
    path = parent_url.split("#", 1)[0]
    stem = os.path.splitext(os.path.basename(path))[0]
    return unquote(stem) or None


def parse_dsf_file(path: str) -> Optional[ParsedMorph]:
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)

    asset_info = doc.get("asset_info", {})
    if asset_info.get("type") != "modifier":
        return None
    guid = asset_info.get("id")
    if not guid:
        return None

    for modifier in doc.get("modifier_library", []):
        morph = modifier.get("morph")
        if not morph or not morph.get("deltas", {}).get("values"):
            continue

        channel = modifier.get("channel", {})
        raw_deltas = morph["deltas"]["values"]
        deltas = [(int(v[0]), float(v[1]), float(v[2]), float(v[3])) for v in raw_deltas]

        formulas = modifier.get("formulas")
        formulas_json = json.dumps(formulas) if formulas else None

        return ParsedMorph(
            guid=guid,
            label=channel.get("label") or modifier.get("name") or modifier.get("id"),
            name=modifier.get("name") or modifier.get("id"),
            target_figure=_resolve_target_figure(modifier.get("parent")),
            group_path=modifier.get("group"),
            vertex_count=morph.get("vertex_count", 0),
            deltas=deltas,
            min_value=channel.get("min", 0.0),
            max_value=channel.get("max", 1.0),
            is_clamped=bool(channel.get("clamped", True)),
            formulas_json=formulas_json,
        )

    return None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests\test_dsf_parser.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add src/dsf_parser.py tests/test_dsf_parser.py tests/fixtures/dsf/Billow.dsf tests/fixtures/dsf/pJCMCloakBend_m90.dsf
git commit -m "feat: add .dsf morph parser with real-file fixtures"
```

---

### Task 4: Dependency extraction from formula operation stacks

**Files:**
- Modify: `src/dsf_parser.py` (add function)
- Test: `tests/test_dsf_parser.py` (add test cases)

**Interfaces:**
- Consumes: `formulas_json: str | None` in the same shape `ParsedMorph.formulas_json` produces (a JSON-serialized list of formula objects with `operations` arrays).
- Produces: `extract_referenced_guids(formulas_json: str | None) -> list[str]` — this is the exact function object passed into `MorphIndexManager.rebuild_dependencies()` from Task 2.

- [ ] **Step 1: Write the failing test (append to `tests/test_dsf_parser.py`)**

```python
from dsf_parser import extract_referenced_guids


def test_extract_referenced_guids_from_bone_driven_formula():
    result = parse_dsf_file(os.path.join(FIXTURES, "pJCMCloakBend_m90.dsf"))
    refs = extract_referenced_guids(result.formulas_json)
    # The real fixture references a bone rotation on the cloak geometry, not
    # another morph -- it should still be extracted as a raw path (dependency
    # resolution against known morph guids happens later, in the SQLite layer).
    assert len(refs) == 1
    assert refs[0].endswith("GnHdCloak_G3_23369.dsf")


def test_extract_referenced_guids_handles_multiple_operations_and_formulas():
    formulas_json = json.dumps([
        {
            "output": "Fig:#morphA?value",
            "operations": [
                {"op": "push", "url": "Fig:/data/lib/MorphB.dsf#MorphB?value"},
                {"op": "push", "val": 0.5},
                {"op": "mult"},
            ],
        },
        {
            "output": "Fig:#morphA?value",
            "operations": [
                {"op": "push", "url": "Fig:/data/lib/MorphC.dsf#MorphC?value"},
            ],
        },
    ])
    refs = extract_referenced_guids(formulas_json)
    assert refs == ["/data/lib/MorphB.dsf", "/data/lib/MorphC.dsf"]


def test_extract_referenced_guids_returns_empty_list_for_none():
    assert extract_referenced_guids(None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests\test_dsf_parser.py -v`
Expected: FAIL with `ImportError: cannot import name 'extract_referenced_guids'`

- [ ] **Step 3: Add the implementation (append to `src/dsf_parser.py`)**

```python
def extract_referenced_guids(formulas_json: Optional[str]) -> list:
    """Extracts raw path strings from "push url" operations in a formulas_json
    blob. These are candidate morph guids -- callers (the SQLite dependency
    rebuild) are responsible for checking which ones resolve to an indexed
    morph; non-morph targets (e.g. bone rotations) simply won't match.
    """
    if not formulas_json:
        return []

    formulas = json.loads(formulas_json)
    refs = []
    for formula in formulas:
        for op in formula.get("operations", []):
            url = op.get("url")
            if not url:
                continue
            # url looks like "Label:/data/.../Target.dsf#Node?property"
            after_label = url.split(":", 1)[-1] if ":" in url else url
            path = after_label.split("#", 1)[0]
            refs.append(path)
    return refs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests\test_dsf_parser.py -v`
Expected: PASS (7 tests total)

- [ ] **Step 5: Commit**

```bash
git add src/dsf_parser.py tests/test_dsf_parser.py
git commit -m "feat: extract morph dependency references from formula operation stacks"
```

---

### Task 5: Transpiler orchestrator (`index_library`)

**Files:**
- Create: `src/managers/morph_transpiler.py`
- Test: `tests/test_morph_transpiler.py`

**Interfaces:**
- Consumes: `MorphIndexManager` (Task 2), `parse_dsf_file`/`extract_referenced_guids` (Tasks 3–4), `write_tmb` (Task 1).
- Produces: `index_library(library_root: str, tmb_output_dir: str, morph_index_manager: MorphIndexManager, force: bool = False, on_progress=None) -> dict` returning `{"scanned": int, "ingested": int, "skipped_no_deltas": int, "skipped_unchanged": int, "errors": int, "new_guids": list[str]}`. `new_guids` is consumed by Task 6's embedding step.
- Produces: `compute_content_hash(path: str) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_morph_transpiler.py
import os
import shutil

import pytest
from managers.morph_index_manager import MorphIndexManager
from managers.morph_transpiler import index_library
from tmb_format import read_tmb

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "dsf")


@pytest.fixture
def library(tmp_path):
    lib_root = tmp_path / "library"
    data_dir = lib_root / "data" / "SomeVendor"
    data_dir.mkdir(parents=True)
    shutil.copy(os.path.join(FIXTURES, "Billow.dsf"), data_dir / "Billow.dsf")
    shutil.copy(os.path.join(FIXTURES, "pJCMCloakBend_m90.dsf"), data_dir / "pJCMCloakBend_m90.dsf")
    return str(lib_root)


def test_index_library_ingests_both_fixtures(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    summary = index_library(library, tmb_dir, db)

    assert summary["scanned"] == 2
    assert summary["ingested"] == 2
    assert summary["errors"] == 0
    assert db.get_stats()["morph_count"] == 2
    assert len(summary["new_guids"]) == 2


def test_index_library_writes_tmb_files_matching_source_layout(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    index_library(library, tmb_dir, db)

    expected = os.path.join(tmb_dir, "data", "SomeVendor", "Billow.tmb")
    assert os.path.exists(expected)
    vertex_count, deltas = read_tmb(expected)
    assert vertex_count == 23369
    assert len(deltas) == 18503


def test_index_library_is_incremental_by_default(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    first = index_library(library, tmb_dir, db)
    second = index_library(library, tmb_dir, db)

    assert first["ingested"] == 2
    assert second["ingested"] == 0
    assert second["skipped_unchanged"] == 2


def test_index_library_force_reingests_everything(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    index_library(library, tmb_dir, db)
    second = index_library(library, tmb_dir, db, force=True)

    assert second["ingested"] == 2


def test_index_library_skips_bad_json_without_aborting(library, tmp_path):
    bad_path = os.path.join(library, "data", "SomeVendor", "Corrupt.dsf")
    with open(bad_path, "w") as f:
        f.write("{not valid json")

    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    summary = index_library(library, tmb_dir, db)

    assert summary["errors"] == 1
    assert summary["ingested"] == 2  # the two good files still succeed


def test_index_library_rebuilds_dependencies(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    index_library(library, tmb_dir, db)

    # Neither fixture references another *morph* (the JCM references a bone
    # rotation), so the dependency graph should be empty but not error.
    assert db.get_stats()["dependency_count"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests\test_morph_transpiler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'managers.morph_transpiler'`

- [ ] **Step 3: Write the implementation**

```python
# src/managers/morph_transpiler.py
import hashlib
import logging
import os

from dsf_parser import parse_dsf_file, extract_referenced_guids
from tmb_format import write_tmb

logger = logging.getLogger(__name__)


def compute_content_hash(path: str) -> str:
    stat = os.stat(path)
    raw = f"{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def index_library(library_root: str, tmb_output_dir: str, morph_index_manager, force: bool = False, on_progress=None) -> dict:
    if force:
        morph_index_manager.setup_db(force_reset=True)
    else:
        morph_index_manager.setup_db(force_reset=False)

    summary = {
        "scanned": 0, "ingested": 0, "skipped_no_deltas": 0,
        "skipped_unchanged": 0, "errors": 0, "new_guids": [],
    }

    data_root = os.path.join(library_root, "data")
    for dirpath, _dirnames, filenames in os.walk(data_root):
        for filename in filenames:
            if not filename.lower().endswith(".dsf"):
                continue
            summary["scanned"] += 1
            source_path = os.path.join(dirpath, filename)

            try:
                content_hash = compute_content_hash(source_path)

                parsed = parse_dsf_file(source_path)
                if parsed is None:
                    summary["skipped_no_deltas"] += 1
                    continue

                if not force:
                    existing_hash = morph_index_manager.get_content_hash(parsed.guid)
                    if existing_hash == content_hash:
                        summary["skipped_unchanged"] += 1
                        continue

                rel_path = os.path.relpath(source_path, library_root)
                tmb_rel_path = os.path.splitext(rel_path)[0] + ".tmb"
                tmb_abs_path = os.path.join(tmb_output_dir, tmb_rel_path)
                os.makedirs(os.path.dirname(tmb_abs_path), exist_ok=True)
                write_tmb(tmb_abs_path, parsed.vertex_count, parsed.deltas)

                morph_index_manager.insert_morph({
                    "guid": parsed.guid,
                    "label": parsed.label,
                    "name": parsed.name,
                    "target_figure": parsed.target_figure,
                    "group_path": parsed.group_path,
                    "source_dsf_path": source_path,
                    "tmb_path": tmb_rel_path,
                    "vertex_count": parsed.vertex_count,
                    "delta_count": len(parsed.deltas),
                    "min_value": parsed.min_value,
                    "max_value": parsed.max_value,
                    "is_clamped": parsed.is_clamped,
                    "formulas_json": parsed.formulas_json,
                    "content_hash": content_hash,
                })
                summary["ingested"] += 1
                summary["new_guids"].append(parsed.guid)

                if on_progress and summary["scanned"] % 500 == 0:
                    on_progress("scan", summary["scanned"], None, source_path)

            except Exception:
                logger.warning(f"Failed to ingest {source_path!r}, skipping.", exc_info=True)
                summary["errors"] += 1

    edge_count = morph_index_manager.rebuild_dependencies(extract_referenced_guids)
    logger.info(f"Rebuilt dependency graph: {edge_count} edges.")
    logger.info(f"Index run complete: {summary}")
    return summary
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests\test_morph_transpiler.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/managers/morph_transpiler.py tests/test_morph_transpiler.py
git commit -m "feat: add morph library transpiler orchestrator"
```

---

### Task 6: ChromaDB embedding integration

**Files:**
- Modify: `src/managers/morph_transpiler.py` (add function)
- Test: `tests/test_morph_transpiler.py` (add test cases)

**Interfaces:**
- Consumes: `MorphIndexManager.get_morphs_by_guids` (Task 2), `embedding_utils.generate_embeddings` (existing), `ChromaDbManager` (existing, instantiated with a `"morphs"` collection name per design §3).
- Produces: `embed_and_store_morphs(morph_index_manager, chroma_manager, guids: list[str], on_progress=None) -> int` — returns count embedded. Batches using `BATCH_SIZE` env var, same convention as `postgres_db_manager.generate_and_store_embeddings`.

- [ ] **Step 1: Write the failing test (append to `tests/test_morph_transpiler.py`)**

```python
from unittest.mock import MagicMock, patch
from managers.morph_transpiler import embed_and_store_morphs


def test_embed_and_store_morphs_upserts_into_chroma(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")
    summary = index_library(library, tmb_dir, db)

    fake_chroma = MagicMock()
    fake_embeddings = MagicMock()
    fake_embeddings.tolist.return_value = [[0.1] * 1024, [0.2] * 1024]

    with patch("managers.morph_transpiler.generate_embeddings", return_value=fake_embeddings):
        count = embed_and_store_morphs(db, fake_chroma, summary["new_guids"])

    assert count == 2
    fake_chroma.collection.upsert.assert_called_once()
    call_kwargs = fake_chroma.collection.upsert.call_args.kwargs
    assert sorted(call_kwargs["ids"]) == sorted(summary["new_guids"])
    assert len(call_kwargs["embeddings"]) == 2
    assert len(call_kwargs["documents"]) == 2
    assert len(call_kwargs["metadatas"]) == 2
    assert "label" in call_kwargs["metadatas"][0]


def test_embed_and_store_morphs_returns_zero_for_empty_guids(tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    fake_chroma = MagicMock()
    count = embed_and_store_morphs(db, fake_chroma, [])
    assert count == 0
    fake_chroma.collection.upsert.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests\test_morph_transpiler.py -v`
Expected: FAIL with `ImportError: cannot import name 'embed_and_store_morphs'`

- [ ] **Step 3: Add the implementation (append to `src/managers/morph_transpiler.py`)**

```python
from embedding_utils import generate_embeddings


def _build_embedding_text(row) -> str:
    label = row["label"] or ""
    group_path = row["group_path"] or ""
    return f"{label}. Category: {group_path}." if group_path else f"{label}."


def embed_and_store_morphs(morph_index_manager, chroma_manager, guids: list, on_progress=None) -> int:
    if not guids:
        return 0

    batch_size = int(os.getenv("BATCH_SIZE", "512"))
    total = len(guids)
    embedded = 0

    for i in range(0, total, batch_size):
        batch_guids = guids[i:i + batch_size]
        rows = morph_index_manager.get_morphs_by_guids(batch_guids)
        if not rows:
            continue

        documents = [_build_embedding_text(row) for row in rows]
        metadatas = [
            {
                "guid": row["guid"],
                "label": row["label"] or "",
                "name": row["name"] or "",
                "target_figure": row["target_figure"] or "",
                "group_path": row["group_path"] or "",
            }
            for row in rows
        ]
        ids = [row["guid"] for row in rows]

        embeddings = generate_embeddings(documents, is_query=False).tolist()
        chroma_manager.collection.upsert(
            ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents,
        )
        embedded += len(ids)

        if on_progress:
            on_progress("embed", min(i + batch_size, total), total, f"batch {i // batch_size + 1}")

    return embedded
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests\test_morph_transpiler.py -v`
Expected: PASS (8 tests total)

- [ ] **Step 5: Commit**

```bash
git add src/managers/morph_transpiler.py tests/test_morph_transpiler.py
git commit -m "feat: embed and store morph metadata into ChromaDB"
```

---

### Task 7: `vab morphs index` CLI subcommand

**Files:**
- Modify: `src/main.py`

**Interfaces:**
- Consumes: `index_library`, `embed_and_store_morphs` (Task 5–6), `MorphIndexManager` (Task 2), `ChromaDbManager` (existing).
- Produces: `vab morphs index --library-path <path> [--force]` CLI command.

- [ ] **Step 1: Add the command function (near `load_command` in `src/main.py`)**

```python
def morphs_index_command(args):
    """Indexes .dsf morph files from a DAZ library into morph_index.db, morph_cache/, and ChromaDB."""
    from managers.morph_index_manager import MorphIndexManager
    from managers.morph_transpiler import index_library, embed_and_store_morphs
    from managers.chroma_db_manager import ChromaDbManager

    library_path = args.library_path or os.environ.get("MORPH_LIBRARY_PATH")
    if not library_path:
        print("Error: --library-path is required (or set MORPH_LIBRARY_PATH).", file=sys.stderr)
        sys.exit(1)

    morph_db_path = os.environ.get("MORPH_INDEX_DB_PATH", "morph_index.db")
    tmb_output_dir = os.environ.get("MORPH_CACHE_PATH", "morph_cache")
    chroma_path = os.environ.get("CHROMA_PATH", "chroma_db")

    morph_index_manager = MorphIndexManager(morph_db_path)
    chroma_manager = ChromaDbManager(chroma_path, "morphs")
    if args.force:
        chroma_manager.reset_collection()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
    ) as progress:
        scan_task = progress.add_task("Scanning  ", total=None)
        embed_task = progress.add_task("Embedding ", total=None, visible=False)

        def on_progress(stage, current, total, detail=""):
            if stage == "scan":
                progress.update(scan_task, completed=current, description=f"Scanning  {str(detail)[:35]:<35}")
            elif stage == "embed":
                progress.update(embed_task, visible=True, total=total, completed=current, description="Embedding ")

        summary = index_library(library_path, tmb_output_dir, morph_index_manager, force=args.force, on_progress=on_progress)
        embed_and_store_morphs(morph_index_manager, chroma_manager, summary["new_guids"], on_progress=on_progress)

    print(f"Scanned: {summary['scanned']}, Ingested: {summary['ingested']}, "
          f"Skipped (no deltas): {summary['skipped_no_deltas']}, "
          f"Skipped (unchanged): {summary['skipped_unchanged']}, Errors: {summary['errors']}")
```

- [ ] **Step 2: Wire the subcommand into the argument parser**

`main()` in `src/main.py` uses `subparsers.add_parser(...)` per top-level command, `parsers["cmd"].add_argument(...)` for options, and a `func_map` dict whose values get attached via `sub_parser.set_defaults(func=func_map[cmd])` (see lines 132-144, 188-191, 197-202 for the existing pattern — `load`'s `--force` flag is the closest analog). `morphs index` needs a second-level subparser, which doesn't fit the flat `parsers`/`func_map` dicts, so it's wired directly:

Immediately after the `parsers = {...}` dict closes (after the `"openproduct"` entry, before `parsers["query"].add_argument(...)` starts), add:

```python
    morphs_parser = subparsers.add_parser("morphs", help="Morph library ingest commands.")
    morphs_subparsers = morphs_parser.add_subparsers(dest="morphs_command", required=True)
    morphs_index_parser = morphs_subparsers.add_parser(
        "index", help="Index .dsf morph files into morph_index.db and ChromaDB."
    )
    morphs_index_parser.add_argument(
        "--library-path", type=str, default=None,
        help="Path to the DAZ content library root (containing data/). Falls back to MORPH_LIBRARY_PATH env var.",
    )
    morphs_index_parser.add_argument(
        "--force", action="store_true",
        help="Wipe morph_index.db, morph_cache/, and the morphs Chroma collection, then re-index everything.",
    )
    morphs_index_parser.set_defaults(func=morphs_index_command)
```

This bypasses the `parsers`/`func_map` dicts entirely (they only hold flat, single-level commands) and sets `func` directly on `morphs_index_parser`, exactly as `sub_parser.set_defaults(func=func_map[cmd])` does for the flat commands lower down — `args.func(args)` at the bottom of `main()` picks it up the same way with no further dispatch code needed.

- [ ] **Step 3: Manually verify the CLI wires up correctly**

Run: `.venv\Scripts\python src\main.py morphs index --help`
Expected: Prints help text showing `--library-path` and `--force` options, no traceback.

- [ ] **Step 4: Commit**

```bash
git add src/main.py
git commit -m "feat: add vab morphs index CLI command"
```

---

### Task 8: End-to-end integration test

**Files:**
- Test: `tests/test_morph_index_integration.py`

**Interfaces:**
- Consumes: everything from Tasks 1–6 together (no new production code).

- [ ] **Step 1: Write the integration test**

```python
# tests/test_morph_index_integration.py
import os
import shutil
from unittest.mock import MagicMock, patch

import pytest
from managers.morph_index_manager import MorphIndexManager
from managers.morph_transpiler import index_library, embed_and_store_morphs
from tmb_format import read_tmb

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "dsf")


def test_full_pipeline_end_to_end(tmp_path):
    # Arrange a fake library with both fixtures plus one corrupt file.
    lib_root = tmp_path / "library"
    data_dir = lib_root / "data" / "Vendor"
    data_dir.mkdir(parents=True)
    shutil.copy(os.path.join(FIXTURES, "Billow.dsf"), data_dir / "Billow.dsf")
    shutil.copy(os.path.join(FIXTURES, "pJCMCloakBend_m90.dsf"), data_dir / "pJCMCloakBend_m90.dsf")
    (data_dir / "Corrupt.dsf").write_text("{not valid json")

    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    tmb_dir = str(tmp_path / "morph_cache")
    fake_chroma = MagicMock()
    fake_embeddings = MagicMock()
    fake_embeddings.tolist.return_value = [[0.1] * 1024, [0.2] * 1024]

    # Act
    summary = index_library(str(lib_root), tmb_dir, db)
    with patch("managers.morph_transpiler.generate_embeddings", return_value=fake_embeddings):
        embedded_count = embed_and_store_morphs(db, fake_chroma, summary["new_guids"])

    # Assert: SQLite
    assert summary["ingested"] == 2
    assert summary["errors"] == 1
    stats = db.get_stats()
    assert stats["morph_count"] == 2
    assert stats["dependency_count"] == 0  # neither fixture depends on another indexed morph

    # Assert: .tmb files on disk
    billow_tmb = os.path.join(tmb_dir, "data", "Vendor", "Billow.tmb")
    assert os.path.exists(billow_tmb)
    vertex_count, deltas = read_tmb(billow_tmb)
    assert vertex_count == 23369
    assert len(deltas) == 18503

    # Assert: ChromaDB upsert happened with matching count
    assert embedded_count == 2
    fake_chroma.collection.upsert.assert_called_once()

    # Act again: re-run should be a no-op (incremental)
    second_summary = index_library(str(lib_root), tmb_dir, db)
    assert second_summary["ingested"] == 0
    assert second_summary["skipped_unchanged"] == 2
```

- [ ] **Step 2: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests\test_morph_index_integration.py -v`
Expected: PASS (1 test)

- [ ] **Step 3: Run the full test suite to confirm no regressions**

Run: `.venv\Scripts\python -m pytest tests\ -v`
Expected: PASS — all existing tests plus the new ones from Tasks 1–8.

- [ ] **Step 4: Commit**

```bash
git add tests/test_morph_index_integration.py
git commit -m "test: add end-to-end integration test for morph ingest pipeline"
```

---

## Post-Plan: Real Library Smoke Test (Manual, Not Automated)

Once all 8 tasks are merged, manually run against the real library to sanity-check performance and error rates before considering Subsystem A done:

```bash
.venv\Scripts\python src\main.py morphs index --library-path "X:\DAZ Libraries\Project"
```

Expect a nontrivial error count on 321K real-world files (malformed/legacy `.dsf` variants are normal) — review the logged warnings for any *systematic* parsing failure (e.g. an assumption in `dsf_parser.py` that doesn't hold for a common vendor pattern) rather than expecting zero errors.
