# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->


## Build & Test

```bash
# Run tests (demo mode, no database required)
.venv\Scripts\python -m pytest tests\ -v

# Start dev server (production mode)
make dev-server

# Start dev server (demo mode, no database required)
make demo-server

# Incremental: reconcile the DAZ CMS into SQLite, embed new/changed products
vab load

# Re-fetch every product's store data and build a new index generation (SQLite is NOT deleted)
vab load --force

# Embed only — embeds products already in SQLite that the active generation is missing
vab load --phase embed

# New generation from SQLite beside the live one, promoted when complete
# (--refetch = same as load --force; --model switches the embedding model)
vab index rebuild [--refetch] [--model MODEL_ID]

# Index morph library from DAZ content
vab morphs index --library-path "C:/path/to/DAZ/library"

# Repair a corrupt product vector store: quarantines the Chroma store only if it
# can't be opened, then rebuilds from SQLite
vab index repair

# Inspect / cancel index jobs (shared by the API and CLI)
vab jobs list
vab jobs cancel

# Re-index morphs from scratch
vab morphs index --library-path "C:/path/to/DAZ/library" --force
```

## Architecture Overview

**Data flow:** PostgreSQL (DAZ CMS) → SQLite catalog (per-source records, asset view, lookup tables,
generations) → one ChromaDB collection per index generation

- `src/server.py` — FastAPI app; all HTTP endpoints; lifespan preloads the embedding model
- `src/embedding_utils.py` — ONNX Runtime inference (BAAI/bge-large-en-v1.5, 1024-dim);
  exports model to `models/bge-large-en-v1.5/` on first run, loads from cache thereafter
- `src/managers/chroma_db_manager.py` — ChromaDB wrapper; cosine-similarity search;
  `reconnect()` refreshes the client when the connection goes stale mid-batch
- `src/managers/postgres_db_manager.py` — ETL + embedding pipeline; async web scraping;
  writes to SQLite then calls `generate_embeddings()` in batches of `BATCH_SIZE` (default 1024),
  which internally sub-batches at `EMBEDDING_BATCH_SIZE` (default 32) for ONNX inference
- `src/catalog/` — the SQLite catalog and index generations: `store.py` (per-source records,
  `asset_view`, exact-match artist/figure lookup tables), `schema.py` (migrations),
  `documents.py` (embedding text/metadata), `reconcile.py` (CMS → catalog), `generations.py`
  (`index_generations` / `asset_vectors` tables), `builder.py` (builds and promotes
  generations), `active_index.py` (search against the active generation), `status.py`
  (reconciliation-backed status/health, rebuild hints), `model_config.py`. Survives
  ChromaDB resets
- `src/managers/managers.py` — module-level singletons (`catalog`, `generations`, `builder`,
  `active_index`, `job_store`, ...) imported everywhere; none of them
  touch ChromaDB at import time (`ChromaDbManager` opens lazily), so a broken store never
  blocks startup
- `src/managers/job_store.py` — SQLite job table (`JOBS_DB_PATH`, default `index_jobs.db`);
  a partial unique index allows one queued/running job across all processes; workers hold
  a heartbeat lease, and lapsed leases are re-queued (resuming incrementally) or failed
- `src/index_worker.py` — job handlers (`index`, `rebuild`, `repair`, `selftest`) and the worker loop.
  The API never indexes in-process: `POST /api/v1/update` queues a job and spawns a detached
  `vab worker --drain`; `vab load` / `vab index` run their job inline through the same queue

**Embedding stack:** `optimum[onnxruntime]` + `onnxruntime` (CPU); no CUDA required.
CPU-only torch is a transitive dep of optimum — install with
`pip install torch --index-url https://download.pytorch.org/whl/cpu`.

## Conventions & Patterns

**Re-indexing:** Each embedding model gets its own index generation (a Chroma collection).
Changing the configured model does not touch the live index: `/api/v1/status` and
`vab stats` report `rebuild_required` with the reason and the exact command, and rebuilds
never start automatically. Run `vab index rebuild --model MODEL_ID` (or `POST /api/v1/update`
with `{"mode": "rebuild"}`); the new generation is built beside the live one and promoted
when complete, and an interrupted rebuild resumes when run again. `vab load --force` (or
`{"force": true}`) additionally re-fetches every product's store data first. SQLite is never
deleted.

**Index generations:** On first start the legacy `products` table is migrated into the
catalog and renamed `products_legacy` (kept for one release); the existing Chroma collection
is adopted as generation 1. `index_generations` tracks generations and which is active;
`asset_vectors` records which asset is embedded in which generation and from which document
hash, so only new or changed assets are re-embedded. A rebuild with more than 1% (minimum 10)
failed assets will not replace an existing index: the active generation stays live and the
job reports a hint to resume with `vab index rebuild --model ...`.

**ChromaDB corruption recovery:** If the server crashes mid-write (force-kill, closed terminal),
ChromaDB's Rust index can panic with:
`range start index N out of range for slice of length M`
The API still starts; `/api/v1/status` and `/api/v1/health` report `degraded` with a repair
hint. Fix: `vab index repair` (or `POST /api/v1/update` with `{"mode": "repair"}`). It moves
the Chroma store aside to `chroma_db.corrupt-<ts>` only if the store cannot be opened, then
rebuilds the index from SQLite (no CMS contact). Do not delete `chroma_db/` by hand: it holds
every generation, including the live one. Panics surface as pyo3 `PanicException`, a
`BaseException`, so catch `BaseException` around Chroma calls (see `_is_fatal` in
`chroma_db_manager.py`).

**ChromaDB corruption recovery (morphs collection):** The same class of corruption
(`Error loading hnsw index` / `Error creating hnsw segment reader`) can hit just the
`morphs` collection if a `vab morphs index` run is killed mid-embed. Corruption is scoped
to the collection being actively written — the product-generation collections in the
same `chroma_db/` store are unaffected, so there's no need to wipe the whole store. Recover
with a targeted collection reset instead:
```python
from src.managers.chroma_db_manager import ChromaDbManager
ChromaDbManager(chroma_db_path, "morphs").reset_collection()
```
```bash
vab morphs index --library-path "C:/path/to/DAZ/library"   # non --force: self-healing
                                                              # embed logic re-embeds every
                                                              # morph_index.db row missing
                                                              # from the now-empty collection
```

**Search:** The server returns up to `max_results` items in a single response.
Pagination is client-side — callers slice the results array themselves.

**Settings:** Runtime config is layered: `.env` defaults → `settings.json` overrides →
`GET /api/v1/settings` / `PUT /api/v1/settings` to read/write at runtime.

**Demo mode:** `APP_MODE=demo` (or `--demo` flag) serves mock data; no database required.
Used by all tests via the `client` fixture in `tests/conftest.py`.
