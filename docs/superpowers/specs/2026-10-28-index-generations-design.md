# Canonical Index Generations and Reconciliation — Design

**Issue:** daz-content-browser-hc8 (epic 63j) · **Date:** 2026-10-28 · **Status:** draft for approval

## Intent

The local product index (SQLite enrichment cache + ChromaDB vectors) has no contract with its
source. It can't tell when it is out of date, it never notices edits or removals, it can't tell
which model built its vectors, and a forced rebuild destroys the working index before the new
one exists. This work makes the index **trustworthy**: correct after adds, edits, and removals;
able to report exactly what is stale and why; and never left half-built.

It also must not lock the index to DAZ CMS. Two kinds of future sources need room (not
implementation): assets that exist only on disk with no CMS record, and supplemental metadata
layered over official records to improve search.

## Problems in the current code (verified)

| Problem | Where |
|---|---|
| "New products" health is `postgres_count - chroma_count`; an add + a remove cancel out; stale data undetectable | `src/server.py:329-340`, `:801-826` |
| Artist / figure filters match substrings of comma-joined text (`LIKE`, Chroma `$contains`): "Genesis 8" matches "Genesis 8.1 Female" | `sqlite_db_manager.py:322-330`, `chroma_db_manager.py:70-72` |
| Incremental runs process only SKUs absent from SQLite; edited products are never refreshed | `postgres_db_manager.py:391-396` |
| Products removed from the CMS are never removed or flagged | ETL has no removal path |
| No record of which model/dimension built the vectors | — |
| `--force` deletes the SQLite file *and* resets Chroma before rebuilding; an interrupted rebuild loses both | `postgres_db_manager.py:373-375`, `sqlite_db_manager.py:28-30` |
| Schema changes are ad-hoc `ALTER TABLE ... except: pass` | `sqlite_db_manager.py:42-46` |
| `is_installed` is hard-coded `True` | `server.py:142`, `:485`, `:588` |

## Goals

1. **Reconciliation.** A product added, changed, or removed in DAZ CMS is reconciled correctly
   on the next incremental index run.
2. **Removal = not installed.** A product that leaves the CMS stays searchable and is marked
   *not installed*; if it reappears it flips back to installed. Its scraped data and vector are
   kept (no re-scrape on reinstall). `is_installed` becomes real data.
3. **Staleness is detectable** per product, for both metadata and vectors.
4. **Generations.** Every vector set belongs to a generation that records the model, vector
   dimension, and format versions that produced it. Metadata and vectors expose the same
   generation ID.
5. **Atomic promotion.** A full rebuild builds a new generation beside the active one and
   switches over only when complete. An interrupted rebuild leaves the previous generation fully
   usable, and resumes (not restarts) on retry.
6. **Model changes are explicit.** A mismatch between the configured model and the active
   generation's model is detected and reported as "rebuild required". Search keeps working on
   the active generation (with the model that built it) until a rebuild is promoted.
7. **Exact filtering.** Artists, tags, and compatible figures are stored normalized; filters
   are exact matches.
8. **Versioned migrations.** SQLite schema changes go through numbered migrations. Existing
   installs upgrade in place without re-scraping or re-embedding.
9. **Honest health.** Count arithmetic is replaced by per-state totals from real reconciliation.
10. **Source-aware shape.** Asset identity, per-source records, and per-source removal are
    modelled so that a filesystem-only source or a supplemental-metadata source can be added
    later without another schema redesign.

## Non-goals (this issue)

- Implementing a filesystem-only asset source or any new supplemental metadata source
  (follow-up issues).
- Making the Settings model change take effect at runtime, and its "rebuild required" save
  response — **6ll**.
- The warning dialog with *revert* / *rebuild* actions — **udg**.
- The morphs collection / `vab morphs index` (**33x**).
- A "purge forever" action for removed products.
- Replacing ChromaDB.

## Anti-goals (failure even if it "works")

- Any path where an interrupted run leaves search empty or broken when a good index existed.
- Forcing existing users to re-scrape or fully re-embed just to upgrade.
- Writing to the DAZ CMS (it stays read-only).
- Silently searching with a model different from the one that built the vectors.
- Removal or a CMS edit wiping data that came from a different source.

## Constraints

- DAZ CMS PostgreSQL is read-only.
- SQLite remains the local canonical metadata store (`enriched_products.db`).
- ChromaDB 1.5.x, local persistent store. The current product store is ~2 GB on disk (shared with
  the morphs collection); a side-by-side rebuild temporarily needs room for a second product
  collection.
- All index writes run through the existing job queue / worker (`src/index_worker.py`,
  `job_store.py`). The API server and the worker are separate processes.
- Panics from Chroma are `BaseException`; keep the existing `_is_fatal` handling.
- Tests run in demo mode with no database (`tests/conftest.py`); new tests must not need DAZ CMS
  or the real embedding model.

## Design

### Identity and sources

- **Asset** — the thing a user finds. `asset_id` is a stable string namespaced by origin:
  `daz:<sku>` for CMS products. (A future filesystem source would mint e.g. `fs:<hash>`.) The
  DAZ SKU stays an attribute and a lookup key; it is no longer the identity.
- **Source record** — what one source says about one asset. Keyed by `(source, source_key)`, linked
  to an `asset_id`. Initial sources:
  - `daz_cms` — key = SKU; carries name, artists, content types, categories, item count,
    CMS modified date.
  - `daz_store_web` — key = SKU; carries the scraped store page fields (url, image, price,
    description, poly count, textures, requirements, compatible software, mature).
- Each source record has `present` (still reported by its source), `first_seen`, `last_seen`,
  `removed_at`, `source_modified` (the source's own change marker, e.g. CMS
  `COALESCE(last_update, date_installed)`), `fetched_at`, `fetch_error`, and its fields.
  *(Builder's call: columns per source table vs a JSON payload column — but fields used for
  search, filters, and sorting must be queryable.)*
- **Removal is per source.** When `daz_cms` no longer lists a SKU, only that source record is
  marked `present = 0`. Other sources' records for the asset are untouched.
- **Installed** = the asset has a present `daz_cms` record. (Future filesystem source would also
  count; define it as "any present install-bearing source".)

### Derived asset view

- A materialized **asset view** (one row per asset) merges source records into the fields the
  app reads today (the current wide row). Merge rule: each field has a fixed source precedence;
  for now `daz_cms` wins for identity/catalogue fields, `daz_store_web` supplies page fields,
  derived fields (category/subcategories, compatible figures, tags) are computed as today.
  Precedence lives in one place in code so supplemental sources can slot in later.
- Normalized lookup tables, rebuilt from the view per asset: `artists` / `asset_artists`,
  `tags` / `asset_tags`, `figures` / `asset_figures`. `category` stays a single column.
- `get_products`, `get_filter_values`, and the rows used for embedding read the view + joins.
  Filters use exact joins; `get_filter_values` reads the lookup tables.
- The view row carries `doc_hash`: a hash of the exact embedding text plus the vector-side
  metadata it produces. `doc_format_version` (a code constant) is bumped when the embedding
  text recipe changes.

### Generations

- `index_generations` table: `id`, `status` (`building` | `active` | `retired` | `failed`),
  `model_id`, `model_dim`, `doc_format_version`, `schema_version`, `collection_name`,
  `created_at`, `promoted_at`, `completed_at`, notes/error. At most one `active` and one
  `building` row (partial unique indexes, as in `job_store.py`).
- Each generation owns one Chroma collection (`daz_products_g<id>`). Vector metadata includes
  `asset_id`, `generation_id`, and `doc_hash`.
- `asset_vectors` table keyed by `(generation_id, asset_id)`: `doc_hash` embedded, `status`
  (`current` | `error`), `error`, `embedded_at`. "Missing" = no row; "stale" =
  `asset_vectors.doc_hash != view.doc_hash`.
- **Incremental runs** update the **active** generation in place, per asset. No new generation.
- **Full rebuilds** (model change, `doc_format_version` change, `--force`, repair of a corrupt
  collection) create a `building` generation, fill it from SQLite (re-ETL first only if
  `--force`), then promote in one SQLite transaction: building → active, old active → retired.
  Retired collections are deleted after promotion (best effort; failures are logged and
  retried on the next run).
- **Interrupted rebuild** *(my calls)*: the `building` generation persists. **Any** later
  rebuild — a retry, a fresh job, a CLI run — adopts an existing `building` generation whose
  target model and `doc_format_version` match, and resumes it (embedding only assets without a
  current vector in it). A `building` generation that doesn't match is marked `failed` and its
  collection dropped before the new build starts. Ctrl+C on an inline CLI run, a crash, and
  exhausted retries (`recover_stale`) all count as **interruptions** (generation kept,
  resumable). Only an explicit cancel (`vab jobs cancel`, API cancel) discards it. Status shows
  an interrupted build as "rebuild interrupted — resume available". The active generation is
  untouched throughout.
- **Only one job runs at a time** (existing job-store rule), so incremental runs and rebuilds
  never overlap. An incremental run while an interrupted `building` generation exists updates
  only the active generation; the build catches up on resume via `doc_hash`.
- **`--force` no longer deletes SQLite.** It means "re-fetch every source record, then build a
  new generation". Source records are upserted, so an interrupted force run leaves SQLite
  usable. *(My call)* The `building` row stores `refetch_started_at`; a resumed force run
  re-fetches only source records with `fetched_at` before that marker, then continues the
  build. The current "retry downgrades force to incremental" logic
  (`index_worker.py:83-89`) is replaced by this.
- **Rebuild verbs** *(my call)*: `mode: "rebuild"` / `vab index rebuild` = new generation
  from SQLite, **no re-fetch** (used for model and format changes). `force: true` /
  `vab load --force` = re-fetch everything, then new generation. `repair` = new generation from
  SQLite when the active collection is corrupt (same build path as rebuild). The
  `rebuild_required` repair hint in status points to `rebuild`.
- **Repair** (`vab index repair`, `{"mode":"repair"}`): if the active collection is corrupt or
  unopenable, build a new generation from SQLite and promote. If the whole store won't open,
  the existing quarantine-and-recreate path still applies first.

### Reconciliation (incremental run)

1. Read the full CMS product list with modified dates (one query; DAZ CMS read-only).
2. Diff against `daz_cms` source records:
   - **new** SKU → create asset + source record; scrape `daz_store_web`.
   - **changed** (`source_modified` differs) → refresh the `daz_cms` record; re-scrape
     `daz_store_web`. *(Builder's call whether to re-scrape only when CMS changed or on a TTL;
     CMS-changed is the minimum.)*
   - **missing** from CMS → `present = 0`, `removed_at = now` (asset stays searchable,
     not installed).
   - **reappeared** → `present = 1`, clear `removed_at`, then treat as changed if
     `source_modified` differs.
3. Rebuild the view row, lookup rows, and `doc_hash` for every touched asset.
4. Embed assets in the active generation whose vector is missing, stale, or in `error`
   (bounded retry of errors per run). Delete orphan vectors (vector with no asset).
5. Record the run's counts in the job result.

A per-asset failure records the error on that asset (`fetch_error` or `asset_vectors.error`)
and the run continues; it never aborts the whole run.

### Model handling

- **"Configured model"** *(my call)* is resolved by one function used everywhere (server,
  worker, CLI, migration): `settings.json` `embedding_model` → `EMBEDDING_MODEL` env →
  `EMBEDDING_MODEL_ID` env → default `BAAI/bge-large-en-v1.5`. CLI `--model` overrides it for
  that run. 6ll later folds this into typed config; hc8 only needs the single resolver.
  Because the resolver reads `settings.json`, saving a different model in Settings makes
  `rebuild_required` true in this issue, even though the runtime switch is 6ll's.
- A rebuild job records its **target model** in its job params and on the `building`
  generation row, so a spawned worker never depends on inherited env.
- The `query_model` setting is unused today (UI-only, `server.py:65`). Queries must use the
  active generation's model; status reports `query_model` as ignored, and 6ll removes it.
- The active generation's `model_id`/`model_dim` is the model used for **query** embedding.
  The embedding layer must be able to load a specific model by id (today it's a single global
  session from `EMBEDDING_MODEL_ID`); keep a small cache keyed by model id.
- **Rebuild required** when configured model ≠ active generation's model, or
  `doc_format_version` ≠ active generation's. Exposed in `/api/v1/status` and `/api/v1/health`
  with the two model ids and the repair action (start a rebuild). It does not block search.
- **Rebuilds are never started automatically** (user decision). This includes a
  `doc_format_version` bump shipped in an app update: status shows "rebuild required" and
  waits for the user to start it, exactly as for a model change. Incremental runs while a
  rebuild is required still reconcile sources and embed new/changed assets into the active
  generation using the **active generation's model** (so vectors stay in one space). Their text
  uses the current recipe, since older recipes aren't kept in code; mixing formats within one
  model's vector space is acceptable until the user rebuilds.
- If the active generation's model files are not available locally, search reports
  unavailable with a clear "rebuild required / model missing" message (not a bare 500; see 2jn).
- During a model-change rebuild, both models may be loaded at once (one for queries on the
  active generation, one for building). Accepted.
- Query embeddings use the active generation's model even if the configured model differs.

### Cross-process switching

The API server must notice promotion done by the worker process. On each search (or a short
cache interval), read the active generation id from SQLite; if it changed, open the new
collection and the matching model. Replaces relying on `reconnect()` after a reset.

### Health and status

Replace `new_products = postgres_count - chroma_count` with reconciliation-backed totals:

- assets: total, installed, not installed
- vectors (active generation): current, stale, missing, error, orphan
- source records with fetch errors
- active generation: id, model, dim, promoted_at; building generation (if any) and progress
- `rebuild_required` + reason
- CMS reachable (bool) and, when reachable, CMS-side counts of new/changed/removed *pending*
  (computed cheaply from the diff in step 2, or reported from the last run — builder's call;
  must not be count arithmetic)

Keep existing field names where the UI uses them, deprecating rather than removing in this
issue (6ll owns API contracts); `new_products` should be fed from real pending-new counts.

### Migrations

- A `schema_migrations` table (or `PRAGMA user_version`) and an ordered list of migration
  functions run at startup and by the worker, each in a transaction. Idempotent; safe if two
  processes start at once.
- **Migration 1 (existing installs):** for each row of the current products table, create
  asset `daz:<sku>`, a present `daz_cms` record, and a `daz_store_web` record from the scraped
  columns; build view + lookup rows + `doc_hash`. Adopt the existing Chroma collection
  (`CHROMA_COLLECTION`, default `daz_products2`) as generation 1, `active`, with the currently
  configured model id and the collection's actual vector dimension. Create `asset_vectors` rows
  for every id present in the collection, with the computed `doc_hash` — the existing vectors
  were built from the stored `embedding_text`, so they count as current. Vectors created later
  carry the new metadata fields; adopted ones are treated as current without rewriting
  (builder's call whether to backfill metadata — required only if vector-side filtering needs it).
- *(My call)* Migration 1 verifies in code before committing: every old row has an asset,
  both source records, and a view row, and the view's fields match the old row's. On mismatch
  it rolls back, leaving the old data untouched for a retry on next start. Startup is not
  blocked (same rule as the lzy recovery work): status/health report `degraded` with the
  migration error, product search and listing are unavailable, and index jobs refuse to run. On success the old table is **renamed** `products_legacy`
  (no code reads it). Dropping it is a follow-up issue for a later release, not part of hc8.
- If the collection can't be opened during migration, migrate SQLite anyway and leave no active
  generation → status says rebuild required.

### Filtering in vector search

Filtering must be exact. Approach is the builder's call, verified against Chroma 1.5.x:
array-valued metadata with exact element matching if supported, otherwise per-value boolean
keys, otherwise resolve the allowed `asset_id` set from SQLite joins and pass it to the query.
The `category` filter stays `$eq`.

## Testing

Tests must cover, without DAZ CMS or the real model (fake CMS reader, deterministic fake
embedder, temp SQLite + temp Chroma dir):

- migration from a fixture of today's schema: no data loss, generation 1 adopted, vectors
  counted current, idempotent re-run
- reconciliation: add, change, remove (→ not installed, still searchable), reappear
- staleness detection for metadata change and for `doc_format_version` bump
- model mismatch → `rebuild_required`; search still served by active generation's model
- full rebuild promotes atomically; interruption mid-build leaves active generation serving;
  retry resumes; cancel marks failed and drops the collection
- per-asset scrape / embed failures recorded, run continues
- exact filters: "Genesis 8" does not match "Genesis 8.1 Female"; artist "Ann" does not match
  "Anna"
- health reports per-state totals; add + remove in one run is visible
- server picks up a promotion made by another process
- existing API tests keep passing in demo mode

## Left to the builder

Table and column names beyond those required above; source-record storage layout; the hash
function; retry bounds; cache interval for active-generation checks; the vector-side filter
mechanism (see above); how much of the ETL moves out of `postgres_db_manager.py` (a modest
split into source reader / reconciler / generation manager is encouraged but not required —
6ll does the larger restructuring).

## Follow-ups to file

- Filesystem-only asset source.
- Supplemental metadata source(s).
- Purge of long-removed assets (only if wanted).
