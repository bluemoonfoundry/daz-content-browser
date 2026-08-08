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

# Full re-index from scratch (wipes ChromaDB, re-embeds from SQLite)
vab load --force

# Embed only — re-embeds products already in SQLite but missing from ChromaDB
vab load --phase embed

# Index morph library from DAZ content
vab morphs index --library-path "C:/path/to/DAZ/library"

# Re-index morphs from scratch
vab morphs index --library-path "C:/path/to/DAZ/library" --force
```

## Architecture Overview

**Data flow:** PostgreSQL (DAZ CMS) → SQLite (enrichment cache) → ChromaDB (vector index)

- `src/server.py` — FastAPI app; all HTTP endpoints; lifespan preloads the embedding model
- `src/embedding_utils.py` — ONNX Runtime inference (BAAI/bge-large-en-v1.5, 1024-dim);
  exports model to `models/bge-large-en-v1.5/` on first run, loads from cache thereafter
- `src/managers/chroma_db_manager.py` — ChromaDB wrapper; cosine-similarity search;
  `reconnect()` refreshes the client when the connection goes stale mid-batch
- `src/managers/postgres_db_manager.py` — ETL + embedding pipeline; async web scraping;
  writes to SQLite then calls `generate_embeddings()` in batches of `BATCH_SIZE` (default 1024),
  which internally sub-batches at `EMBEDDING_BATCH_SIZE` (default 32) for ONNX inference
- `src/managers/sqlite_db_manager.py` — enrichment cache; survives ChromaDB resets
- `src/managers/managers.py` — module-level singletons imported everywhere

**Embedding stack:** `optimum[onnxruntime]` + `onnxruntime` (CPU); no CUDA required.
CPU-only torch is a transitive dep of optimum — install with
`pip install torch --index-url https://download.pytorch.org/whl/cpu`.

## Conventions & Patterns

**Re-indexing:** ChromaDB must be rebuilt when the embedding model changes.
Use `vab load --force` (or `POST /api/v1/update` with `{"force": true}`).
SQLite is preserved; only embeddings are regenerated.

**ChromaDB corruption recovery:** If the server crashes mid-write (force-kill, closed terminal),
ChromaDB's Rust index can panic on next startup with:
`range start index N out of range for slice of length M`
Fix: delete the `chroma_db/` directory and re-index. SQLite survives.
```bash
rd /s /q chroma_db          # Windows
rm -rf chroma_db            # Mac / Linux
vab load --phase embed      # re-embed from SQLite (no web scraping needed)
```

**Search:** The server returns up to `max_results` items in a single response.
Pagination is client-side — callers slice the results array themselves.

**Settings:** Runtime config is layered: `.env` defaults → `settings.json` overrides →
`GET /api/v1/settings` / `PUT /api/v1/settings` to read/write at runtime.

**Demo mode:** `APP_MODE=demo` (or `--demo` flag) serves mock data; no database required.
Used by all tests via the `client` fixture in `tests/conftest.py`.
