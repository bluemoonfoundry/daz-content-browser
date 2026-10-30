# Index Generations and Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local product index trustworthy: source-aware assets, reconciliation of
adds/edits/removals, per-asset staleness, versioned migrations, and model-stamped index
generations that are built beside the live one and promoted atomically.

**Architecture:** A new `src/catalog/` package owns the SQLite catalog (assets, per-source
records, a materialized asset view, normalized lookup tables), the generation/vector-state
tables, the reconciler, and the generation builder. Each generation owns one Chroma collection.
The worker's job handlers call the catalog instead of `postgres_db_manager.main`; the API
reads the catalog and searches the active generation with the model that built it.

**Tech Stack:** Python 3.12, SQLite (`sqlite3`), ChromaDB 1.5.x (`collection.query(ids=...)`
is available), ONNX Runtime embeddings, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-10-28-index-generations-design.md`

## Global Constraints

- DAZ CMS PostgreSQL is read-only.
- SQLite stays the canonical metadata store: same file as today (`SQLITE_DB_PATH`, e.g. `enriched_products.db`).
- Existing installs upgrade **without re-scraping and without re-embedding** unless a real change is found.
- Rebuilds are **never started automatically**; status reports `rebuild_required` and waits.
- `--force` must not delete the SQLite file.
- No interrupted run may leave search empty when a good generation existed.
- Catch `BaseException` around Chroma calls via the existing `_is_fatal` pattern.
- Tests need no DAZ CMS and no real ONNX model; run with `.venv\Scripts\python -m pytest tests\ -v`.
- Default model id: `BAAI/bge-large-en-v1.5`. Model resolution order: `settings.json` `embedding_model` → env `EMBEDDING_MODEL` → env `EMBEDDING_MODEL_ID` → default; CLI `--model` overrides for that process.
- Asset ids: `daz:<sku>` for DAZ CMS products. Sources: `daz_cms`, `daz_store_web`.
- Generation collections are named `daz_products_g<id>`; the legacy collection (`CHROMA_COLLECTION`, default `daz_products2`) is adopted as generation 1 under its existing name.
- `DOC_FORMAT_VERSION = 1` at the start.
- Out of scope: Settings runtime switch (6ll), warning dialog (udg), morphs (33x), filesystem/supplemental sources, purge, dropping `products_legacy`.

## Review Focus

1. **A huge first incremental run after upgrade.** Every migrated `daz_cms` record has no payload hash yet. The first run must refresh CMS payloads for all assets **without** re-scraping (re-scrape only when `source_modified` changes) and re-embed only assets whose `doc_hash` actually changed. Pinned by `test_first_run_after_migration_rescrapes_nothing` (Task 7).
2. **The API process opens a collection the worker just deleted** (retired after promotion). The search must reopen on the new active generation, not 500. Pinned by `test_search_follows_promotion_from_another_store_instance` (Task 9).
3. **A product whose SKU has odd characters or is reused under a different name.** Identity is `daz:<sku>`. A rename updates the same asset, it doesn't create a second one. Pinned by `test_renamed_product_updates_same_asset` (Task 7).
4. **The active generation's model files are missing** (models dir deleted, or a model switched via env without exporting). Search must return a clear 503 with a "rebuild required / model missing" detail, not a bare 500. Pinned by `test_search_with_missing_model_returns_503` (Task 9).
5. **Two processes start at once on an un-migrated DB** (API + `vab worker`). The migration must run exactly once and the second process must see the final schema. Pinned by `test_concurrent_migrate_runs_once` (Task 2).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/catalog/__init__.py` | Package marker; re-exports nothing |
| `src/catalog/model_config.py` | `configured_model_id()` resolver |
| `src/catalog/schema.py` | Migration runner + migrations (DDL, legacy import + verification) |
| `src/catalog/documents.py` | Pure functions: view row from source records, embedding text, `doc_hash`, `DOC_FORMAT_VERSION` (moves `determine_categories`, `determine_compatibility`, `generate_embedding_text` here) |
| `src/catalog/store.py` | `CatalogStore`: source-record writes, view refresh, product/filter reads, exact-filter id resolution |
| `src/catalog/generations.py` | `GenerationStore`: generation rows and per-asset vector state |
| `src/catalog/builder.py` | `GenerationBuilder`: Chroma side — adopt legacy, embed pending, rebuild/resume/discard, promote, cleanup |
| `src/catalog/reconcile.py` | `reconcile_daz(...)`: diff CMS vs records, mark removed, scrape, refresh views |
| `src/catalog/active_index.py` | `ActiveIndex`: what the API searches (active generation + its collection + its model), cross-process aware |
| `src/catalog/status.py` | `index_status(...)`: per-state totals + `rebuild_required` |
| `src/embedding_utils.py` | Modify: per-model session cache, `model_id` parameter |
| `src/managers/daz_db_analyzer.py` | Modify: `get_all_products()` |
| `src/managers/managers.py` | Modify: construct `catalog`, `generations`, `active_index`; drop `sqlite_db` at the end |
| `src/managers/postgres_db_manager.py` | Modify: keep only scraping helpers; delete `main`, `generate_and_store_embeddings` |
| `src/index_worker.py` | Modify: handlers `index`, `rebuild`, `repair` on the catalog |
| `src/main.py` | Modify: `vab load` flags, `vab index rebuild` semantics, `stats` |
| `src/server.py` | Modify: products/filters/search/status/health/info on the catalog |
| `src/managers/sqlite_db_manager.py` | Delete (Task 10) |
| `tests/catalog_fakes.py` | Fake CMS rows, fake scraper, deterministic fake embedder |
| `tests/test_catalog_*.py` | New tests per task |

---

### Task 1: Model resolver and per-model embeddings

**Files:**
- Create: `src/catalog/__init__.py`, `src/catalog/model_config.py`, `tests/catalog_fakes.py`
- Modify: `src/embedding_utils.py`
- Test: `tests/test_catalog_models.py`

**Interfaces:**
- Produces:
  - `catalog.model_config.configured_model_id(settings_path: str | None = None) -> str`. Default `settings_path` is the server's `settings.json` path (find the constant in `server.py` and move it into `model_config` as `SETTINGS_PATH`; `server.py` imports it).
  - `embedding_utils.model_dir(model_id: str) -> Path`: `EMBEDDING_MODEL_DIR` if set **and** `model_id == configured_model_id()`, else `models/<slug>`.
  - `embedding_utils.model_available(model_id: str) -> bool`: `model.onnx` exists.
  - `embedding_utils.generate_embeddings(texts, is_query: bool = False, model_id: str | None = None) -> np.ndarray`. `None` = `configured_model_id()`. Sessions cached in `dict[str, (session, tokenizer)]`.
  - `embedding_utils.ModelUnavailable(RuntimeError)` raised when files are missing and export is not possible (frozen build) or for a model that isn't present. **Runtime code never calls `_export_model`** for a non-configured model.
  - `load_embedding_model(model_id: str | None = None)`. `embedding_utils._session` stays meaningful for `/health` as "configured model loaded" (or replace that check with `model_loaded(model_id) -> bool`, and update `server.py` accordingly).
  - `tests/catalog_fakes.py`: `fake_embed(texts, is_query=False, model_id=None) -> np.ndarray`. Deterministic: 8-dim for `"fake/model-a"`, 12-dim for `"fake/model-b"`, derived from `hashlib.sha256(model_id + text)`, L2-normalised; a single str → 1-D. A pytest fixture `fake_embedder(monkeypatch)` in `tests/conftest.py` patches `embedding_utils.generate_embeddings` and `embedding_utils.model_available` (returns True for `fake/*`).

- [ ] **Step 1: Write failing tests**

```python
def test_resolver_order(tmp_path, monkeypatch):
    s = tmp_path / "settings.json"
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL_ID", raising=False)
    assert configured_model_id(str(s)) == "BAAI/bge-large-en-v1.5"
    monkeypatch.setenv("EMBEDDING_MODEL_ID", "x/id")
    assert configured_model_id(str(s)) == "x/id"
    monkeypatch.setenv("EMBEDDING_MODEL", "x/env")
    assert configured_model_id(str(s)) == "x/env"
    s.write_text('{"embedding_model": "x/settings"}')
    assert configured_model_id(str(s)) == "x/settings"

def test_resolver_ignores_unreadable_settings(tmp_path, monkeypatch):
    s = tmp_path / "settings.json"; s.write_text("{not json")
    monkeypatch.setenv("EMBEDDING_MODEL_ID", "x/id")
    assert configured_model_id(str(s)) == "x/id"

def test_model_dir_uses_slug(monkeypatch):
    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    assert embedding_utils.model_dir("BAAI/bge-m3").name == "bge-m3"

def test_unavailable_model_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(embedding_utils, "model_dir", lambda m: tmp_path / "nope")
    with pytest.raises(embedding_utils.ModelUnavailable):
        embedding_utils.get_embedding_model("other/model")

def test_fake_embedder_is_deterministic_per_model(fake_embedder):
    a = embedding_utils.generate_embeddings(["x"], model_id="fake/model-a")
    assert a.shape == (1, 8)
    assert np.allclose(a, embedding_utils.generate_embeddings(["x"], model_id="fake/model-a"))
    assert embedding_utils.generate_embeddings("x", model_id="fake/model-b").shape == (12,)
```

- [ ] **Step 2: Run** `.venv\Scripts\python -m pytest tests\test_catalog_models.py -v`. Expected: FAIL (ImportError on `catalog.model_config`).
- [ ] **Step 3: Implement** the resolver and the per-model cache. `export_model.py` / the first-run export keep working for the configured model only.
- [ ] **Step 4: Run** the new tests plus `tests\test_api.py tests\test_jobs.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(catalog): model resolver and per-model embedding sessions (hc8)`

---

### Task 2: Schema and migration runner

**Files:**
- Create: `src/catalog/schema.py`
- Test: `tests/test_catalog_schema.py`

**Interfaces:**
- Produces:
  - `schema.LATEST_VERSION: int`
  - `schema.connect(db_path: str) -> sqlite3.Connection`: `row_factory=sqlite3.Row`, `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout=10000`.
  - `schema.migrate(db_path: str, legacy_table: str | None = None) -> int`: applies pending migrations in order, each in its own `BEGIN IMMEDIATE` transaction; version stored in `PRAGMA user_version`. Re-checks the version after acquiring the write lock, so concurrent callers apply each migration once. Returns the final version.
  - `schema.MigrationFailed(Exception)` with `.version` and `.reason`.
  - Migration 1 DDL (exact table and column names that later tasks use):
    - `assets(asset_id TEXT PK, sku TEXT, created_at TEXT NOT NULL)`, unique index on `sku` where not null.
    - `source_records(source TEXT, source_key TEXT, asset_id TEXT NOT NULL REFERENCES assets, present INTEGER NOT NULL DEFAULT 1, first_seen TEXT, last_seen TEXT, removed_at TEXT, source_modified TEXT, payload TEXT NOT NULL /* JSON */, payload_hash TEXT, fetched_at TEXT, fetch_error TEXT, PRIMARY KEY(source, source_key))`, index on `asset_id`.
    - `asset_view(asset_id TEXT PK REFERENCES assets, sku TEXT, name TEXT, artist TEXT, url TEXT, image_url TEXT, store TEXT, price TEXT, description TEXT, tags TEXT, formats TEXT, poly_count TEXT, textures_info TEXT, required_products TEXT, compatible_figures TEXT, compatible_software TEXT, embedding_text TEXT, last_updated TEXT, category TEXT, subcategories TEXT, styles TEXT, inferred_tags TEXT, enriched_at TEXT, mature INTEGER, asset_count INTEGER, installed INTEGER NOT NULL, doc_hash TEXT NOT NULL, doc_format_version INTEGER NOT NULL)`. Comma-joined `artist`/`tags`/`compatible_figures`/`subcategories` remain for display compatibility only. **No filter may read them.**
    - `artists(id INTEGER PK, name TEXT UNIQUE NOT NULL)`, `asset_artists(asset_id, artist_id, PK both)`, and the same shape for `tags`/`asset_tags` and `figures`/`asset_figures`.
    - `index_generations(id INTEGER PK AUTOINCREMENT, status TEXT NOT NULL CHECK(status IN ('building','active','retired','failed')), model_id TEXT NOT NULL, model_dim INTEGER NOT NULL, doc_format_version INTEGER NOT NULL, schema_version INTEGER NOT NULL, collection_name TEXT NOT NULL UNIQUE, refetch_started_at TEXT, created_at TEXT NOT NULL, completed_at TEXT, promoted_at TEXT, error TEXT)`, partial unique indexes allowing one `active` and one `building`.
    - `asset_vectors(generation_id INTEGER REFERENCES index_generations ON DELETE CASCADE, asset_id TEXT, vector_id TEXT NOT NULL, doc_hash TEXT, status TEXT NOT NULL CHECK(status IN ('current','error')), error TEXT, attempts INTEGER NOT NULL DEFAULT 0, embedded_at TEXT, PRIMARY KEY(generation_id, asset_id))`, unique `(generation_id, vector_id)`.

- [ ] **Step 1: Write failing tests**

```python
def test_migrate_fresh_db_reaches_latest(tmp_path):
    db = str(tmp_path / "c.db")
    assert migrate(db) == LATEST_VERSION
    names = {r[0] for r in connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"assets", "source_records", "asset_view", "index_generations", "asset_vectors",
            "artists", "asset_artists", "tags", "asset_tags", "figures", "asset_figures"} <= names

def test_migrate_is_idempotent(tmp_path):
    db = str(tmp_path / "c.db")
    migrate(db); assert migrate(db) == LATEST_VERSION

def test_only_one_active_and_one_building_generation(tmp_path): ...
    # inserting a second 'active' row raises sqlite3.IntegrityError; same for 'building'

def test_concurrent_migrate_runs_once(tmp_path):
    # 4 threads call migrate(db) at once; all return LATEST_VERSION; no exception;
    # a counter monkeypatched into migration 1 shows it ran exactly once
```

- [ ] **Step 2: Run** `.venv\Scripts\python -m pytest tests\test_catalog_schema.py -v`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement** `connect`, `migrate`, and migration 1 as a list of `(version, fn(conn, ctx))`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(catalog): versioned SQLite schema for assets, sources, generations (hc8)`

---

### Task 3: Documents: view rows, embedding text, doc hash

**Files:**
- Create: `src/catalog/documents.py`
- Modify: `src/managers/postgres_db_manager.py`. Remove `determine_categories`, `determine_compatibility` and `generate_embedding_text`, and import them from `catalog.documents` where they're still used.
- Test: `tests/test_catalog_documents.py`

**Interfaces:**
- Produces:
  - `DOC_FORMAT_VERSION = 1`
  - `load_figure_names() -> list[str]` (the `.figures.json` lookup moved from `postgres_db_manager.main`, frozen-aware; raises `FileNotFoundError` with the path).
  - `build_view_row(asset_id: str, cms: dict | None, web: dict | None, figure_names: list[str]) -> dict`. `cms` is a `daz_cms` payload with the CMS query's column names (`product_name, artists, sku, last_modified_date (ISO str), product_compatibility, content_item_count, categories, content_types`). `web` is a `daz_store_web` payload (`url, image_url, store, price, description, poly_count, textures_info, required_products, compatible_software, mature`). Field derivation must equal today's `postgres_db_manager.main` insert logic exactly: tags = categories + compatibility string; `compatible_figures` from `determine_compatibility(cms, figure_names)` called with the **CMS payload only**, because today's ETL never passes the web description in, so heuristic 3 doesn't fire. Changing that would alter results and force re-embeds. If `cms` contains a `"legacy"` key (a migrated record not yet refreshed from the CMS), take `tags`, `compatible_figures`, `category` and `subcategories` from it verbatim; category and subcategories from `content_types`; `last_updated` = CMS date; `asset_count` = `content_item_count`. It returns the `asset_view` columns, minus `installed` and `enriched_at`, plus lists `artists_list`, `tags_list`, `figures_list` for the lookup tables. The lists are split on `,` and stripped, empty entries dropped, order kept, deduplicated.
  - `doc_hash(row: dict) -> str`: sha256 hex over `DOC_FORMAT_VERSION`, `embedding_text`, and the vector-side metadata fields (`name, artist, category, subcategories, compatible_figures, tags, asset_count, url, image_url`), serialised as sorted-key JSON.
  - `payload_hash(payload: dict) -> str`: sha256 of sorted-key JSON.

- [ ] **Step 1: Write failing tests**

```python
CMS = {"sku": "12345", "product_name": "Desert Robe for Genesis 8", "artists": "Ann, Bo",
       "product_compatibility": "Genesis 8 Female", "content_item_count": 7,
       "categories": "Wardrobe, Outfits", "content_types": "Follower/Wardrobe/Dress",
       "last_modified_date": "2024-01-02T03:04:05"}
WEB = {"url": "https://www.daz3d.com/desert-robe", "description": "A flowing robe.", "price": "$9.99"}
FIGS = ["Genesis 8", "Genesis 8 Female", "Genesis 8.1 Female"]

def test_view_row_matches_legacy_etl_derivation():
    r = build_view_row("daz:12345", CMS, WEB, FIGS)
    assert r["tags"] == "Wardrobe, Outfits, Genesis 8 Female"
    assert r["compatible_figures"] == "Genesis 8, Genesis 8 Female"
    assert r["category"] == "wardrobe" and r["asset_count"] == 7
    assert r["embedding_text"].startswith("A 3D asset package titled 'Desert Robe for Genesis 8'.")
    assert r["artists_list"] == ["Ann", "Bo"]

def test_figures_ignore_web_description():
    web = dict(WEB, description="Also fits Genesis 8.1 Female")
    assert "Genesis 8.1 Female" not in build_view_row("daz:1", dict(CMS, product_compatibility=None,
                                                       product_name="Robe"), web, FIGS)["compatible_figures"]

def test_legacy_payload_uses_legacy_derived_fields(): ...

def test_view_row_without_web_record():
    assert build_view_row("daz:12345", CMS, None, FIGS)["url"] is None

def test_doc_hash_changes_with_text_and_metadata_but_not_price():
    a = build_view_row("daz:1", CMS, WEB, FIGS)
    assert doc_hash(a) == doc_hash(dict(a, price="$1"))
    assert doc_hash(a) != doc_hash(dict(a, embedding_text=a["embedding_text"] + "x"))
    assert doc_hash(a) != doc_hash(dict(a, name="Other"))

def test_doc_hash_depends_on_format_version(monkeypatch): ...
```

- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement**, moving the three helpers without changing their behaviour.
- [ ] **Step 4: Run** the new tests plus `tests\test_scraping.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(catalog): derive view rows and doc hashes from source records (hc8)`

---

### Task 4: CatalogStore (writes, views, reads, exact filters) and legacy migration

**Files:**
- Create: `src/catalog/store.py`
- Modify: `src/catalog/schema.py` (add migration 2: legacy import)
- Test: `tests/test_catalog_store.py`, `tests/test_catalog_legacy_migration.py`, `tests/fixtures/legacy_products.sql` (old `CREATE TABLE` from `sqlite_db_manager.setup_sqlite_db` plus 3 rows)

**Interfaces:**
- Consumes: Task 2 `connect`/`migrate`; Task 3 `build_view_row`, `doc_hash`, `payload_hash`, `DOC_FORMAT_VERSION`.
- Produces `CatalogStore(db_path: str, legacy_table: str | None = None)`. Its constructor calls `migrate` and stores any `MigrationFailed` on `.migration_error` instead of raising. Methods:
  - `upsert_source(source: str, source_key: str, asset_id: str, payload: dict, source_modified: str | None, *, fetch_error: str | None = None, now: str | None = None) -> None`: creates the asset if needed (`sku` = `source_key` for `daz_*` sources), sets `present=1`, `last_seen=now`, clears `removed_at`, sets `payload_hash`, and sets `fetched_at=now` unless `fetch_error` is set. A failed fetch keeps the previous payload.
  - `mark_absent(source: str, present_keys: set[str], now: str | None = None) -> list[str]`: marks the absent keys `present=0, removed_at=now` (only if not already absent) and returns their asset_ids.
  - `source_index(source: str) -> dict[str, dict]`: maps key → `{asset_id, present, source_modified, payload_hash, fetched_at}`.
  - `refresh_views(asset_ids: Iterable[str], figure_names: list[str]) -> None`: rebuilds `asset_view` and the lookup rows from the current source records. `installed` = a present `daz_cms` record exists. `enriched_at` = the `daz_store_web` `fetched_at`, falling back to the `daz_cms` one.
  - `get_products(page, page_size, category=None, artist=None, compatible_figure=None, name_query=None, sort_by="name", sort_dir="asc", installed: bool | None = None) -> dict`: same return shape as today's `SQLiteWrapper.get_products`. Artist and figure filters use **exact joins** on the lookup tables. Rows include `installed`.
  - `get_by_sku(sku: str) -> dict | None`, `get_rows(asset_ids: list[str]) -> list[dict]`.
  - `get_filter_values() -> dict`: same keys as today, read from the lookup tables and `asset_view.category`.
  - `asset_ids_matching(category=None, artists=None, compatible_figures=None, tags=None) -> set[str] | None`: `None` when there are no filters. Values within one list are OR'd; different lists are AND'd.
  - `count() -> int`, `get_last_updated() -> str | None`, `asset_totals() -> dict` (`{"total", "installed", "not_installed", "fetch_errors"}`).
- Migration 2 (`legacy_table` given and present, `products_legacy` absent):
  - For each legacy row, create `daz:<sku>` plus a `daz_cms` record. Its payload maps `name→product_name`, `artist→artists`, `formats→content_types`, `asset_count→content_item_count` and `last_updated→last_modified_date`, plus `"legacy": {"tags": ..., "compatible_figures": ..., "category": ..., "subcategories": ...}`. `source_modified` = `last_updated` and `payload_hash` = NULL.
  - Also create a `daz_store_web` record from the scraped columns, with `fetched_at` = `enriched_at`.
  - The view row is **copied from the legacy row** (including `embedding_text` verbatim), with `installed=1`, `doc_hash` = `doc_hash(row)` and `doc_format_version=1`. Lookups are split from the legacy comma fields.
  - Verify inside the transaction: asset count == legacy row count, each asset has both source records and a view row, and the view's `name`, `embedding_text` and `artist` equal the legacy values. On mismatch, raise `MigrationFailed(2, reason)`, which rolls back. On success, run `ALTER TABLE <legacy_table> RENAME TO products_legacy`.
  - If the legacy table is absent, the migration is a no-op apart from bumping the version.

- [ ] **Step 1: Write failing tests**

```python
def test_exact_figure_filter(store):  # assets with "Genesis 8" and "Genesis 8.1 Female"
    assert store.asset_ids_matching(compatible_figures=["Genesis 8"]) == {"daz:1"}
    page = store.get_products(1, 50, compatible_figure="Genesis 8")
    assert [p["sku"] for p in page["products"]] == ["1"]

def test_exact_artist_filter(store):  # "Ann" vs "Anna"
    assert store.asset_ids_matching(artists=["Ann"]) == {"daz:1"}

def test_or_within_and_across(store): ...
def test_filter_values_from_lookups(store): ...
def test_absent_marks_not_installed_and_keeps_row(store):
    store.mark_absent("daz_cms", present_keys={"2"})
    store.refresh_views(["daz:1"], FIGS)
    assert store.get_by_sku("1")["installed"] == 0
    assert store.get_products(1, 50, installed=False)["total"] == 1

def test_reappearing_source_clears_removed(store): ...
def test_failed_fetch_keeps_previous_payload(store): ...

# legacy migration
def test_legacy_rows_imported_verbatim(legacy_db):
    s = CatalogStore(legacy_db, legacy_table="products")
    row = s.get_by_sku("A1")
    assert row["embedding_text"] == LEGACY["A1"]["embedding_text"] and row["installed"] == 1
    assert {r[0] for r in connect(legacy_db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")} >= {"products_legacy"}

def test_legacy_migration_is_rerunnable(legacy_db): ...
def test_legacy_migration_failure_rolls_back_and_is_reported(legacy_db, monkeypatch):
    # monkeypatch the verifier to report a mismatch
    s = CatalogStore(legacy_db, legacy_table="products")
    assert s.migration_error is not None
    assert "products" in table_names(legacy_db) and "assets" not in data_tables_with_rows(legacy_db)
```

- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** `CatalogStore` and migration 2. Use `executemany` for the bulk legacy import; a real DB has about 10k+ rows.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(catalog): source-aware catalog store, exact filters, legacy import (hc8)`

---

### Task 5: GenerationStore (generation rows, vector state)

**Files:**
- Create: `src/catalog/generations.py`
- Test: `tests/test_catalog_generations.py`

**Interfaces:**
- Consumes: Task 2 tables.
- Produces `GenerationStore(db_path: str)`. Generation rows are returned as `dict`s with the column names. Methods:
  - `active() -> dict | None`, `building() -> dict | None`, `get(gen_id) -> dict | None`, `retired() -> list[dict]`
  - `create_building(model_id: str, model_dim: int, doc_format_version: int, schema_version: int, *, refetch: bool, now=None) -> dict`: sets `collection_name = f"daz_products_g{id}"` after insert. Sets `refetch_started_at=now` when `refetch`.
  - `create_active_adopted(collection_name: str, model_id: str, model_dim: int, doc_format_version: int, schema_version: int) -> dict`: used once for the legacy collection.
  - `promote(gen_id: int, now=None) -> None`: one transaction: the old active becomes `retired`, the building row becomes `active` with `promoted_at`/`completed_at` set. Raises `ValueError` if `gen_id` isn't `building`.
  - `mark_failed(gen_id: int, error: str) -> None`, `delete(gen_id: int) -> None` (cascades `asset_vectors`).
  - `record_current(gen_id: int, items: list[tuple[str, str, str]]) -> None`: `(asset_id, vector_id, doc_hash)`; upsert with `status='current'`, `error=NULL`, `attempts=0`.
  - `record_error(gen_id: int, asset_ids: list[str], error: str) -> None`: increments `attempts`.
  - `pending(gen_id: int, max_attempts: int = 3, limit: int | None = None) -> list[str]`: asset_ids in `asset_view` that have no vector row, a stale `doc_hash`, or `status='error'` with `attempts < max_attempts`. Ordered by asset_id.
  - `orphans(gen_id: int) -> list[str]`: `vector_id`s whose `asset_id` has no `asset_view` row.
  - `drop_vectors(gen_id: int, asset_ids: list[str]) -> None`
  - `vector_totals(gen_id: int) -> dict`: `{"current", "stale", "missing", "error", "orphan"}`
  - `vector_ids_for(gen_id: int, asset_ids: set[str]) -> list[str]`, `assets_for_vectors(gen_id: int, vector_ids: list[str]) -> dict[str, str]` (vector_id → asset_id)

- [ ] **Step 1: Write failing tests**

```python
def test_promote_swaps_active_atomically(gens): ...      # old → retired, new → active
def test_promote_requires_building(gens): ...
def test_pending_covers_missing_stale_and_retryable_errors(gens, catalog):
    # 3 assets: a has current vector w/ matching hash, b has none, c has stale hash
    assert gens.pending(g) == ["daz:b", "daz:c"]
    gens.record_error(g, ["daz:b"], "boom") ; gens.record_error(g, ["daz:b"], "boom"); gens.record_error(g, ["daz:b"], "boom")
    assert gens.pending(g) == ["daz:c"]                   # exhausted attempts
def test_vector_totals(gens, catalog): ...               # current/stale/missing/error/orphan counts
def test_delete_cascades_vectors(gens): ...
```

- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(catalog): generation and per-asset vector state store (hc8)`

---

### Task 6: GenerationBuilder (Chroma side)

**Files:**
- Create: `src/catalog/builder.py`
- Modify: `src/managers/chroma_db_manager.py`. Add `ChromaDbManager.delete_collection() -> None` (drops this manager's collection, ignoring "does not exist"), `ChromaDbManager.get_dimension() -> int | None` (peeks one embedding) and `ChromaDbManager.delete_ids(ids: list[str]) -> None`.
- Test: `tests/test_catalog_builder.py` (real Chroma in `tmp_path`, `fake_embedder`)

**Interfaces:**
- Consumes: Task 1 `generate_embeddings(..., model_id=)`, `model_available`; Task 4 `CatalogStore.get_rows`; Task 5 `GenerationStore`.
- Produces `GenerationBuilder(catalog: CatalogStore, gens: GenerationStore, chroma_path: str, legacy_collection: str | None)`, with methods:
  - `collection(gen: dict) -> ChromaDbManager`
  - `adopt_legacy(model_id: str) -> dict | None`: only when **no** generation rows exist and the legacy collection exists and opens. It creates the active generation 1 with `model_dim = get_dimension()`. It records `asset_vectors` rows for every collection id that matches an `asset_view.sku` (`vector_id` = the bare SKU, `doc_hash` = the view's current `doc_hash`), so adopted vectors count as current. It returns the generation, or `None` if nothing was adopted. If the collection can't open, it logs and returns `None` (status will then say rebuild required).
  - `embed_pending(gen: dict, *, batch_size: int = BATCH_SIZE env (default 512), cancel_check=None, on_progress=None, limit: int | None = None) -> dict`: loops over `pending(gen_id)`. It embeds the view `embedding_text` with `model_id=gen["model_id"]` and upserts with `ids` = `asset_id` (new vectors). Metadata is `{asset_id, generation_id, doc_hash}` plus the display fields today's code stores, **minus the fields used for filtering**: filtering now goes through `ids=` (Task 9). If an asset already has a vector row under a different vector_id (an adopted bare SKU), the old id is deleted after the upsert. Per-batch Chroma failures use the existing reconnect-and-retry pattern, then `record_error`. It also deletes orphan vectors. It returns `{"embedded", "errors", "orphans_removed"}`, and returns early if `cancel_check()`.
  - `start_or_resume_rebuild(model_id: str, *, refetch: bool) -> dict`: if a `building` generation matches `model_id` and `DOC_FORMAT_VERSION`, it is returned (resume). A non-matching one goes through `discard(building)` first. Otherwise it calls `create_building(model_id, dim, ...)`, where `dim` comes from embedding the string `"dimension probe"`.
  - `finish_rebuild(gen: dict) -> None`: requires `pending(gen_id)` to be empty apart from exhausted errors, then calls `gens.promote` and `cleanup_retired()`.
  - `discard(gen: dict, reason: str) -> None`: drops the collection, calls `mark_failed`, then `gens.delete`. Failed rows are not kept.
  - `cleanup_retired() -> None`: drops each retired collection, then deletes its row. Errors are logged and the row is kept for the next attempt.

- [ ] **Step 1: Write failing tests**

```python
def test_adopt_legacy_counts_existing_vectors_current(env):
    # legacy collection with ids "1","2" (dim 8) + catalog with daz:1, daz:2
    gen = env.builder.adopt_legacy("fake/model-a")
    assert gen["id"] == 1 and gen["model_dim"] == 8 and gen["collection_name"] == "legacy"
    assert env.gens.vector_totals(1)["current"] == 2 and env.gens.pending(1) == []

def test_adopt_is_noop_when_generations_exist(env): ...
def test_embed_pending_replaces_adopted_sku_ids(env): ...   # stale daz:1 re-embedded under id "daz:1"; id "1" gone
def test_rebuild_builds_beside_active_and_promotes(env):
    old = env.active_gen
    gen = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    env.builder.embed_pending(gen)
    assert env.gens.active()["id"] == old["id"]               # still serving old until promote
    env.builder.finish_rebuild(gen)
    assert env.gens.active()["model_id"] == "fake/model-b" and env.gens.active()["model_dim"] == 12
    assert old["collection_name"] not in env.collection_names()

def test_interrupted_rebuild_resumes(env):
    gen = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    env.builder.embed_pending(gen, limit=1)
    again = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    assert again["id"] == gen["id"] and len(env.gens.pending(gen["id"])) == env.n_assets - 1

def test_mismatched_building_is_discarded(env): ...         # building for model-b, rebuild requested for model-a
def test_cancelled_embed_returns_early_and_leaves_active(env): ...
def test_finish_rebuild_refuses_with_pending_work(env): ...
```

- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(catalog): build, resume, promote and clean up index generations (hc8)`

---

### Task 7: Reconciler

**Files:**
- Create: `src/catalog/reconcile.py`
- Modify: `src/managers/daz_db_analyzer.py`. Add `get_all_products() -> list[dict]`: `QUERY_BODY` + `WHERE p.token IS NOT NULL AND p.token != ''` + `QUERY_GROUPING`. Dates come back as ISO strings.
- Modify: `src/managers/postgres_db_manager.py`. Expose `scrape_many(skus: list[str], concurrency: int, on_progress=None) -> dict[str, dict | Exception]` on top of the existing `_scrape_all_async`. An exception value means that SKU's scrape failed.
- Test: `tests/test_catalog_reconcile.py` (fake CMS rows + fake scraper from `tests/catalog_fakes.py`)

**Interfaces:**
- Consumes: Task 4 `CatalogStore`; Task 3 `payload_hash`.
- Produces:
  - `ReconcileResult` dataclass: `new, changed, removed, reappeared, refreshed_payloads, scraped, scrape_errors: int`, `touched: list[str]`
  - `reconcile_daz(catalog: CatalogStore, cms_rows: list[dict], scrape: Callable[[list[str]], dict[str, dict | Exception]], figure_names: list[str], *, refetch_before: str | None = None, refetch_all: bool = False, limit: int | None = None, now: str | None = None, cancel_check=None) -> ReconcileResult`
- Rules (from spec §Reconciliation):
  - **new** key → `upsert_source(daz_cms)` and scrape.
  - **changed** = the `source_modified` differs → upsert and **scrape**.
  - The payload hash differs but `source_modified` is the same, which includes migrated records with `payload_hash` NULL → upsert the `daz_cms` record only, **no scrape**.
  - **absent** → `mark_absent`.
  - **reappeared** → upsert (present again). It's scraped only if `source_modified` changed.
  - `refetch_all` scrapes every present key. `refetch_before=T` scrapes every present key whose `daz_store_web.fetched_at` is null or `< T` (force resume).
  - A scrape exception goes to `upsert_source(daz_store_web, ..., fetch_error=str(e))`, which keeps the old payload.
  - `refresh_views(touched)` runs at the end. `limit` caps how many new/changed keys are processed.
  - `cancel_check` is checked between scrape batches. Records already written stay.

- [ ] **Step 1: Write failing tests**

```python
def test_new_changed_removed_reappeared(env):
    r1 = reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS)
    assert (r1.new, r1.scraped) == (2, 2)
    r2 = reconcile_daz(env.catalog, [dict(CMS_A, last_modified_date="2025-01-01T00:00:00")], env.scrape, FIGS)
    assert (r2.changed, r2.removed) == (1, 1)
    assert env.catalog.get_by_sku("B")["installed"] == 0
    r3 = reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS)
    assert r3.reappeared == 1 and env.catalog.get_by_sku("B")["installed"] == 1

def test_first_run_after_migration_rescrapes_nothing(legacy_env):
    before = {a: legacy_env.catalog.get_rows([a])[0]["doc_hash"] for a in legacy_env.asset_ids}
    r = reconcile_daz(legacy_env.catalog, legacy_env.cms_rows_matching_legacy, legacy_env.scrape, FIGS)
    assert r.scraped == 0 and r.refreshed_payloads == legacy_env.n
    after = {a: legacy_env.catalog.get_rows([a])[0]["doc_hash"] for a in legacy_env.asset_ids}
    assert after == before                                   # nothing would be re-embedded

def test_renamed_product_updates_same_asset(env): ...       # same sku, new name+date → 1 asset, name updated
def test_scrape_failure_recorded_and_run_continues(env): ...
def test_refetch_before_scrapes_only_older_records(env): ...
def test_add_and_remove_in_one_run_are_both_counted(env): ...  # count unchanged, but new=1 removed=1
```

- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(catalog): reconcile DAZ CMS adds, edits, removals per source (hc8)`

---

### Task 8: Job handlers and CLI on the catalog

**Files:**
- Modify: `src/index_worker.py`, `src/main.py`, `src/server.py` (`start_update` mode mapping only), `src/managers/managers.py`, `src/managers/postgres_db_manager.py` (delete `main` and `generate_and_store_embeddings`)
- Test: `tests/test_jobs.py` (replace the `fake_etl` tests), `tests/test_catalog_jobs.py`

**Interfaces:**
- Consumes: Tasks 4–7.
- `managers.py` exports `catalog = CatalogStore(SQLITE_DB_PATH, legacy_table=SQLITE_TABLE_NAME)`, `generations = GenerationStore(SQLITE_DB_PATH)`, and `builder = GenerationBuilder(catalog, generations, CHROMA_PATH, legacy_collection=CHROMA_COLLECTION)`. `chroma_db_manager` stays only until Task 9 removes its last use.
- The handlers all start with `_prepare()`. `_prepare()` raises `RuntimeError(catalog.migration_error)` if migration failed. Otherwise it calls `builder.adopt_legacy(configured_model_id())` and `builder.cleanup_retired()`.
  - `index` (params `all`, `phase` ∈ `etl|embed|all`, `limit`, `model`):
    1. Unless `phase == "embed"`: `reconcile_daz(..., refetch_all=params.all, limit=...)`, which needs the CMS. If `daz_pg_analyzer` is None, raise the existing "DAZ CMS PostgreSQL is not available…" message.
    2. Unless `phase == "etl"`: if there's no active generation, run the rebuild path. Otherwise `embed_pending(active)`.
    3. Return a summary string with the reconcile + embed counts.
  - `rebuild` (params `refetch: bool`, `model: str | None`):
    1. `target = params.model or configured_model_id()`; `gen = builder.start_or_resume_rebuild(target, refetch=refetch)`.
    2. If refetch: `reconcile_daz(..., refetch_before=gen["refetch_started_at"])`.
    3. `embed_pending(gen)`, then `finish_rebuild(gen)`.
    4. If `ctx.cancelled()` is true at any checkpoint, call `builder.discard(gen, "cancelled")` and raise `JobCancelled`. `KeyboardInterrupt` is **not** caught here, so the generation survives.
  - `repair`: today's quarantine step `chroma_db_manager.repair()` only when the store won't open at all, then the same path as `rebuild` with `refetch=False` and target = the active generation's model if one exists, else configured.
  - Remove the "retry downgrades force" logic.
- `run_job`: on `KeyboardInterrupt`, finish the job as `CANCELLED` with progress `"Interrupted — run the same command again to resume."`.
- `start_update` mapping: `mode="update"` → `index {}`. `mode="rebuild"` → `rebuild {refetch: False}`. `force: true` (no mode) → `rebuild {refetch: True}`. `mode="repair"` → `repair {}`. `body.model` is not added (6ll).
- CLI:
  - `vab load` → `index {all, phase, limit}`; `vab load --force` → `rebuild {refetch: True}`.
  - `vab index rebuild [--refetch]` → `rebuild`; `vab index repair` → `repair`.
  - `--model` on `vab load` / `vab index rebuild` sets `params.model`.
  - Help strings must say `--force` re-fetches everything and builds a new generation **without deleting SQLite**.

- [ ] **Step 1: Write failing tests**

```python
def test_index_job_reconciles_and_embeds_into_active(job_env): ...
def test_index_job_without_generation_builds_first_one(job_env): ...
def test_rebuild_job_promotes_new_model(job_env):
    job_env.run("rebuild", {"model": "fake/model-b"})
    assert job_env.gens.active()["model_id"] == "fake/model-b"
def test_cancelled_rebuild_discards_building_keeps_active(job_env, monkeypatch): ...
def test_keyboard_interrupt_keeps_building_generation(job_env, monkeypatch):
    # embed_pending raises KeyboardInterrupt after first batch
    with pytest.raises(KeyboardInterrupt): job_env.run("rebuild", {"model": "fake/model-b"})
    assert job_env.gens.building() is not None
    job_env.run("rebuild", {"model": "fake/model-b"})       # resumes same generation id
def test_force_resume_refetches_only_unrefreshed(job_env): ...
def test_jobs_refuse_when_migration_failed(job_env): ...
def test_api_update_modes_map_to_kinds(prod): ...           # in tests/test_jobs.py, extend existing fixture
```

- [ ] **Step 2: Run** `tests\test_catalog_jobs.py tests\test_jobs.py`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** Delete the now-unused `postgres_db_manager.main`, `generate_and_store_embeddings` and the `__main__` block.
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(catalog): index, rebuild and repair jobs on the generation catalog (hc8)`

---

### Task 9: API: search on the active generation, status, health

**Files:**
- Create: `src/catalog/active_index.py`, `src/catalog/status.py`
- Modify: `src/server.py`, `src/managers/managers.py`, `src/managers/chroma_db_manager.py` (`search` takes `ids: list[str] | None` and no longer builds substring `where` clauses; delete `build_where_clause`), `src/main.py` (`stats`, `query` use the catalog/active index)
- Test: `tests/test_catalog_api.py` (production-mode fixture like `prod` in `tests/test_jobs.py`, with real tmp catalog + Chroma + `fake_embedder`)

**Interfaces:**
- `ActiveIndex(gens: GenerationStore, chroma_path: str)`:
  - `current() -> tuple[dict, ChromaDbManager] | None`. It re-reads `gens.active()` on every call. When the id changed since last time, it opens a new `ChromaDbManager` for that collection_name and drops the old one.
  - `search(catalog: CatalogStore, query: str, *, max_results: int, category=None, artists=None, compatible_figures=None, tags=None) -> list[dict]`:
    - No active generation, or `model_available(gen.model_id)` is False → raise `SearchUnavailable(reason, repair)`.
    - Allowed assets come from `catalog.asset_ids_matching(...)`. If the result is an empty set, return `[]`. Otherwise map them to vector ids with `gens.vector_ids_for`.
    - Embed the query with `model_id=gen["model_id"]` and call Chroma with `ids=` when filtered. Map hits back via `gens.assets_for_vectors`, then `catalog.get_rows`, keeping relevance order and attaching `relevance_score = round(1 - distance, 4)`.
    - On a Chroma "collection does not exist" error, call `current()` once more and retry. That covers promotion by another process.
- `index_status(catalog, gens, configured_model: str, cms_reachable: bool, pending_cms: dict | None) -> dict` returns these keys:
  - `assets`: `{total, installed, not_installed, fetch_errors}`
  - `vectors`: the active generation's `vector_totals`
  - `active_generation`: `{id, model_id, model_dim, doc_format_version, promoted_at}`, or `None`
  - `building_generation`: `{id, model_id, pending}`, or `None`
  - `rebuild_interrupted`: `bool`, true when a building generation exists and no job is running
  - `rebuild_required`: `bool`
  - `rebuild_reason`: one of `"no_index" | "model_changed" | "format_changed" | "model_missing" | None`
  - `configured_model`, `query_model_ignored: true`
  - `migration_error`: `str | None`
- `server.py`:
  - While `catalog.migration_error` is set, `/products`, `/products/{sku}`, `/filters`, `/search`, `/query` and `/info` return 503 with `detail={"reason": "migration_failed", "repair": <error + "restart to retry; see server log">}`. Startup is not blocked.
  - `/api/v1/products` passes an `installed` param through. Add an optional `is_installed: bool | None` query param mapped to `installed`. `_format_product` sets `is_installed = bool(row["installed"])` (demo mode keeps `True`).
  - `/api/v1/products/{sku}`, open and browse use `catalog.get_by_sku`. `/api/v1/filters` uses `catalog.get_filter_values()`.
  - `/api/v1/search` and `/api/v1/query` use `active_index.search`. `SearchUnavailable` → HTTP 503 with `detail={"reason", "repair"}`.
  - `/api/v1/status` keeps the existing keys and adds an `index` key holding `index_status(...)`. The legacy keys are derived as follows:
    - `sqlite_count` = assets total
    - `chromadb_count` = vectors current + stale
    - `postgres_count` = `count_skus()` as today
    - `new_products` = `pending_cms["new"]`, where `pending_cms` comes from a cheap key-set diff (`get_all_skus()` vs `catalog.source_index("daz_cms")` present keys), giving `{"new", "removed"}`. `None` when the CMS is unreachable.
    - `update_available` = `new or removed > 0`
    - `status` = `"degraded"` when `migration_error`, the active collection is unopenable, or `rebuild_required`. Same for `/health`.
    - `repair` = a hint string. For `rebuild_required` it reads ``"Run `vab index rebuild` (or POST /api/v1/update {\"mode\": \"rebuild\"}) to rebuild the index for <configured_model>."``
  - `/api/v1/health` gains components `catalog` (`migration_error`) and `index` (`rebuild_required`, `rebuild_interrupted`, repair). `vector_store` reports the active collection's health.
  - `/api/v1/info`: same keys, with `total_docs` = vectors current + stale and `new_products` from `pending_cms`.

- [ ] **Step 1: Write failing tests**

```python
def test_search_uses_active_generation_model(api): ...      # configured model-b, active model-a → results, embed called with model-a
def test_search_exact_figure_filter(api):
    r = api.post("/api/v1/search", json={"query": "robe", "filters": {"compatible_figures": "Genesis 8"}})
    assert {x["sku"] for x in r.json()["results"]} == {"1"}
def test_search_follows_promotion_from_another_store_instance(api): ...
def test_search_with_missing_model_returns_503(api, monkeypatch):
    monkeypatch.setattr(embedding_utils, "model_available", lambda m: False)
    r = api.post("/api/v1/search", json={"query": "robe"})
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "model_missing"
def test_status_reports_rebuild_required_on_model_change(api, tmp_settings):
    tmp_settings.write_text('{"embedding_model": "fake/model-b"}')
    s = api.get("/api/v1/status").json()
    assert s["index"]["rebuild_required"] and s["index"]["rebuild_reason"] == "model_changed"
    assert s["status"] == "degraded" and "vab index rebuild" in s["repair"]
def test_status_counts_add_and_remove(api): ...             # pending new=1 removed=1 while totals equal
def test_status_reports_interrupted_rebuild(api): ...
def test_products_report_not_installed(api): ...
def test_health_reports_migration_error(api_with_failed_migration): ...
def test_reads_return_503_when_migration_failed(api_with_failed_migration):
    assert api_with_failed_migration.get("/api/v1/products").status_code == 503
```

- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite, including the demo-mode `tests\test_api.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): search active generation, reconciliation-backed status and health (hc8)`

---

### Task 10: Remove the legacy SQLite wrapper, docs, follow-ups

**Files:**
- Delete: `src/managers/sqlite_db_manager.py`
- Modify: `src/managers/managers.py` (remove `sqlite_db`, and `chroma_db_manager` if nothing uses it), `src/main.py`, `CLAUDE.md` (the Build & Test commands, the Architecture data-flow line, the Re-indexing and ChromaDB corruption recovery sections: `vab load --force` no longer wipes SQLite, `vab index rebuild` meaning, generations, `products_legacy`), `README.md` (where it describes `--force`)
- Test: full suite

- [ ] **Step 1: Run** `grep -rn "sqlite_db\b\|SQLiteWrapper\|build_where_clause\|generate_and_store_embeddings" src tests`. Expected: no matches.
- [ ] **Step 2: Delete** the wrapper and fix the imports.
- [ ] **Step 3: Run** `.venv\Scripts\python -m pytest tests\ -v`. Expected: all PASS.
- [ ] **Step 4: Manual smoke test against the real data, after backing up `enriched_products.db` and `chroma_db/`:**
  1. `vab stats`: migration runs. Expected: the asset count equals the old row count, generation 1 is active, and vectors are mostly current.
  2. `vab load`. Expected: `scraped=0`, and only a small number re-embedded.
  3. `vab worker`/server `/api/v1/status`. Expected: `rebuild_required: false`.
  4. A search with a figure filter returns only exact matches.
  5. Record the numbers in the bd issue notes.
- [ ] **Step 5: File follow-ups** with `bd create`: filesystem-only asset source; supplemental metadata source; drop `products_legacy` in a later release; purge long-removed assets (P4). Each is `--deps discovered-from:daz-content-browser-hc8`.
- [ ] **Step 6: Commit** `refactor: retire SQLiteWrapper; document generations and reconciliation (hc8)`
