# DAZ Visual Asset Browser (VAB)

VAB is a local REST API server that brings semantic search to your DAZ Studio content library. It indexes your installed products into a vector database and exposes a query interface so you can find assets by meaning rather than by name — "asian city streets", "soft fantasy lighting", "gritty cyberpunk outfit" — across your entire collection.

The server is the product. A web UI is included as a reference client and demonstration of what the API can do, but the API itself is the interface designed for integration, automation, and building your own tools on top of.

<img width="2387" height="1298" alt="Screenshot 2026-05-05 080942" src="https://github.com/user-attachments/assets/4caf9aae-5957-4a2a-9818-182e085d0dc6" />



> [!NOTE]  
> The reference web UI uses the Script Server DAZ plugin to allow users to open the DAZ content manager to a product page in a running DAZ Studio directly from a search result. It's not strictly necessary for searching, but to get full benefit consider installing the plugin. It's free and available here: https://github.com/bluemoonfoundry/daz-script-server/releases/latest

---

## What it does

- **Semantic search** — query your library with natural language; results ranked by meaning, not keywords
- **Hybrid filtering** — combine a semantic query with hard filters on category, artist, and compatible figure
- **Full product index** — browsable paginated catalogue with sorting and filtering
- **DAZ Studio integration** — open products in the Content Library, load assets into scenes, and read content directories from a running DAZ Studio instance via the DAZ Script Server plugin
- **Incremental indexing** — tracks what's already indexed; re-runs only process new products
- **Demo mode** — runs with mock data and no database, useful for UI development and trying the interface
- **Web UI** — a reference browser client served at `/` when `ui/dist/` is present

---

## Installation

The software requires requires Python 3.11+ installed. No other setup. We are working on building out an installer that wraps the Python build explictly into a standalone executable. 

> [!IMPORTANT]
> If you are installing on MacOS, you'll have to remove or comment out the "onnxruntime-directml" dependency in the pyproject.toml file before you can run the installation step

## Option A -- Clone, venv, and run 

The simplest way to get this working in a development setup is to clone the repo, setup a virtual environment, and install the requirements, and run the server. Note that the PyTorch installation in this method needs to be done manually depending on your specific CUDA version. 

```
git clone https://github.com/bluemoonfoundry/daz-content-browser.git

python -m venv install .venv

source ./venv/Scripts/activate

pip install -e ".[local-llm]"

python vab.py server
```

## Option B -- Direct Run

1. Download `vab-release.zip` and unzip to a permanent location (e.g. `C:\Tools\VAB`).
2. Open a terminal in that folder and run the installer:
   ```bash
   python install.py
   ```
3. Once installation finishes, start the server:
   ```bash
   run.bat          # Windows
   ./run.sh         # Mac / Linux
   ```

   The run scripts support optional flags:
   - `--demo` - Run in demo mode (mock data, no database required)
   - `--dev-ui` - Run with Vite dev server for UI development (requires `ui/src/`)

   | Command | API Server | UI | Access URL |
   |---------|-----------|-----|------------|
   | `./run.sh` | Production | Pre-built | `http://localhost:8000` |
   | `./run.sh --demo` | Demo mode | Pre-built | `http://localhost:8000` |
   | `./run.sh --dev-ui` | Production | Vite (hot-reload) | `http://localhost:5173` |
   | `./run.sh --demo --dev-ui` | Demo mode | Vite (hot-reload) | `http://localhost:5173` |

   Interactive API docs are always available at `http://localhost:8000/docs`


---

> [!IMPORTANT]
> **After installation, you need to index your content before the UI will display anything.**
> The server will run, but your library will be empty until you build the index. See [Production setup](#production-setup) below for configuration and indexing instructions. To try the UI immediately with mock data, use [Demo mode](#quick-start-demo-mode) instead.

---

## Quick start: Demo mode

Demo mode runs the server with built-in mock data. No database or configuration required — useful for trying the UI or developing against the API.

```bash
# From release zip
run.bat --demo        # Windows
./run.sh --demo       # Mac/Linux

# From source or pip
python vab.py server --demo
vab server --demo
```

Once started:
- **Web UI**: `http://localhost:8000`
- **API docs**: `http://localhost:8000/docs`

---

## Production setup

### 1. Configure your environment

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `DAZ_STUDIO_EXE_PATH` | Full path to `DAZStudio.exe` |
| `DB_HOST` / `DB_PORT` | DAZ CMS PostgreSQL host and port (defaults: `127.0.0.1` / `17237`) |
| `DB_NAME` / `DB_USER` / `DB_PASS` | DAZ CMS credentials |
| `EMBEDDING_MODEL_DIR` | Local cache path for the ONNX model (default: `models/bge-large-en-v1.5/`) |
| `EMBEDDING_BATCH_SIZE` | Texts per ONNX inference call (default: `32`; lower = less memory) |

The DAZ CMS database is installed and managed by DAZ Studio — you do not need to set it up. Run DAZ Studio at least once before indexing.

### 2. Build the index

```bash
python vab.py load
```

Pulls products from the DAZ CMS database, generates embeddings, and stores everything in the local SQLite catalog and a ChromaDB index generation. First run is slow (embedding generation); subsequent runs are incremental.

```bash
python vab.py load --force      # re-fetch every product and build a new index generation (SQLite is kept)
python vab.py load --limit 100  # process only 100 products (testing)
python vab.py load --phase etl  # ETL only, skip embedding
```

### 3. Start the server

```bash
python vab.py server
python vab.py server --port 9000
```

The server binds to loopback (`127.0.0.1`) by default. Binding elsewhere (e.g. `--host 0.0.0.0`) prints a warning, and requests are still rejected unless their `Host` is listed in `VAB_ALLOWED_HOSTS` — see [Authentication & security](#authentication--security).

---

## API reference

All endpoints are under `/api/v1/`. The server exposes interactive Swagger docs at `/docs` and ReDoc at `/redoc` — these are the authoritative reference for request/response schemas.

Base URL (default): `http://localhost:8000`

### Authentication & security

The server is a single-user desktop backend. It defends against web pages in your browser reaching `http://127.0.0.1` (CSRF, DNS rebinding) and against accidental LAN exposure; other processes running as your user are trusted.

**API token.** Endpoints marked **🔒 token** below require a per-install token. It is generated on first start and stored in `~/.daz3d/vab_api_token.txt` (next to the DAZ Script Server plugin's own token). Send it as a header:

```bash
curl -H "Authorization: Bearer $(cat ~/.daz3d/vab_api_token.txt)" http://127.0.0.1:8000/api/v1/settings
```

The bundled web UI doesn't read the file: it calls `POST /api/v1/auth/session` from its own origin and receives an `HttpOnly`, `SameSite=Strict` session cookie, which also authenticates `<img>` thumbnail requests. That endpoint refuses requests without a browser `Origin` header.

**Read-only endpoints** (status, products, search, filters, info, DAZ Studio status) stay token-free by default so the DAZ Studio pane and MCP clients work without token plumbing. They are still covered by the Host/Origin checks. Set `VAB_REQUIRE_TOKEN_FOR_READS=1` to require the token on them too.

**Host and Origin checks.** Requests whose `Host` isn't loopback get `403` (blocks DNS rebinding). `/api/` requests carrying an `Origin` header must come from an allowed UI origin or the server's own origin, and CORS is limited to the same list.

**Content roots.** `scene/load`, `files/reveal` and `assets/thumbnail` only accept absolute paths that resolve — after `..` and symlinks — beneath a registered content root (DAZ CMS roots plus the running DAZ Studio's library directories). Anything else gets `403`. Demo mode registers no roots, so these endpoints always return `403` there.

| Variable | Description |
|---|---|
| `VAB_API_TOKEN` | Use this fixed token instead of the generated one (tests, scripted clients) |
| `VAB_API_TOKEN_FILE` | Token file location (default: `~/.daz3d/vab_api_token.txt`) |
| `VAB_ALLOWED_HOSTS` | Comma-separated extra `Host` names to accept besides loopback |
| `VAB_ALLOWED_ORIGINS` | Comma-separated UI origins for CORS (default: `http://localhost:5173,http://127.0.0.1:5173`, the Vite dev server) |
| `VAB_REQUIRE_TOKEN_FOR_READS` | `1` to require the token on read-only endpoints too |

#### `POST /api/v1/auth/session`

Issues the UI's session cookie. Only accepted from an allowed browser origin.

```json
{ "authenticated": true }
```

---

### Status & indexing

#### `GET /api/v1/status`

Health check and index counts. Polled frequently by the UI.

```json
{
  "status": "ok",
  "postgres_connected": true,
  "sqlite_connected": true,
  "chromadb_connected": true,
  "postgres_count": 4821,
  "sqlite_count": 4821,
  "chromadb_count": 4821,
  "new_products": 0,
  "update_available": false,
  "repair": null
}
```

The API starts even when the vector store is missing or corrupt. In that case `status` is
`"degraded"`, `chromadb_connected` is `false`, `repair` says how to fix it, and search
endpoints answer `503` with the same `repair` hint. Browsing, settings and jobs keep working.

#### `GET /api/v1/health`

Per-component health (`vector_store`, `embedding_model`, `sqlite`, `postgres`, `jobs`). Each
component reports `ok`, and a `repair` hint when it is not. Overall `status` is `"ok"` or
`"degraded"`.

#### `POST /api/v1/update`

**🔒 token**

Queue an index job. Returns immediately. The job runs in a separate worker process
(`vab worker --drain`), so it survives API restarts.

```json
// Request body
{ "mode": "update" }   // "update" (incremental), "rebuild" (new generation, promoted when complete), "repair"

// Response 202
{ "message": "Update process started.", "task_id": "uuid", "mode": "update" }
```

`rebuild` builds a new index generation from SQLite beside the live one and switches over
when it is complete; the live index keeps serving meanwhile. `repair` moves the vector store
aside to `chroma_db.corrupt-<timestamp>` only if it will not open, then rebuilds from SQLite
without contacting the DAZ CMS. `force: true` is the legacy spelling of a rebuild that first
re-fetches every product's store data; SQLite is never deleted. Rebuilds never start
automatically: after an embedding-model change `/api/v1/status` reports `rebuild_required`. Only one index job runs at a time across the API and the
CLI. While one is active, this returns `"Update already running."` with that job's `task_id`.

#### `DELETE /api/v1/update`

**🔒 token**

Cancel the active index job: immediately if it is still queued, otherwise at the worker's
next checkpoint.

#### `GET /api/v1/update/status`

The active index job, or the most recent one if none is active. Poll this while an update is running.

```json
{
  "task_id": "uuid",
  "kind": "index",
  "status": "running",          // queued | running | succeeded | failed | cancelled
  "running": true,
  "progress": "Embedding: 412/800 (51%) — batch 1",
  "stage": "embed",
  "error": null,
  "attempt": 1,
  "max_attempts": 3,
  "cancel_requested": false,
  "last_run": "2026-05-05T12:00:00+00:00"
}
```

If a worker dies (killed, crashed, machine slept), its lease lapses after
`VAB_JOB_LEASE_SECONDS` (default 60). The job is then re-queued and resumed incrementally,
up to `VAB_JOB_MAX_ATTEMPTS` (default 3), after which it fails with an `error`. The worker
logs to `index_jobs.worker.log`, next to the job database (`JOBS_DB_PATH`, default `index_jobs.db`).

#### `GET /api/v1/update/status/{task_id}`

Status for a specific index job by ID (returned by `POST /api/v1/update`).

#### `GET /api/v1/jobs?limit=20`

Recent index jobs, newest first, including failed and cancelled ones.

---

### Products

#### `GET /api/v1/products`

Paginated product catalogue.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `page` | int | 1 | Page number (1-based) |
| `page_size` | int | 25 | Results per page (max 10000) |
| `category` | string | — | Filter by category |
| `artist` | string | — | Filter by artist |
| `compatible_figure` | string | — | Filter by compatible figure |
| `sort_by` | string | `name` | Field to sort by |
| `sort_dir` | string | `asc` | `asc` or `desc` |

```json
{
  "products": [ /* product objects */ ],
  "total": 4821,
  "page": 1,
  "page_size": 25,
  "total_pages": 193
}
```

#### `GET /api/v1/products/{sku}`

Full details for a single product by SKU.

```json
{
  "sku": "54841",
  "name": "dForce Night Runner Outfit for Genesis 8",
  "artist": ["Daz Originals", "GolaM"],
  "category": "People",
  "subcategories": ["Clothing"],
  "tags": ["sci-fi", "cyberpunk", "outfit"],
  "compatible_figures": ["Genesis 8 Female"],
  "store_url": "https://www.daz3d.com/...",
  "install_date": "2025-01-15T10:00:00",
  "is_installed": true,
  "asset_count": 0
}
```

#### `GET /api/v1/products/{sku}/assets`

Asset file paths for a product, with resolved absolute paths where the files can be found on disk.

#### `POST /api/v1/products/{sku}/open`

**🔒 token**

Navigate the DAZ Studio Content Library to this product. Uses the DAZ Script Server plugin if available, falls back to subprocess launch.

```json
// Response
{ "success": true, "message": "Opened 'Night Runner' via DAZ Script Server.", "via": "plugin" }
```

---

### Search

#### `POST /api/v1/search`

Semantic search with optional filters. Primary search endpoint for the UI.

```json
// Request body
{
  "query": "gritty cyberpunk street clothes",
  "filters": {
    "category": "People",
    "artist": "Daz Originals",
    "compatible_figures": "Genesis 9",
    "is_installed": true
  },
  "max_results": 500,
  "min_relevance": 0.0
}

// Response
{
  "results": [ /* product objects with relevance_score */ ],
  "total": 12,
  "query": "gritty cyberpunk street clothes",
  "took_ms": 84
}
```

All filter fields are optional and exact-match; each takes one value or a list (OR within a list, AND across filters), and `tags` is accepted too. `is_installed` filters on whether the product is currently in the DAZ CMS. `max_results` caps how many results the server returns (1–1000, default 500); pagination is handled client-side. `min_relevance` (0–1, relevance = 1 − cosine distance) drops results below it (0.0 = no filtering). `query` is 1–1000 characters.

#### `POST /api/v1/query`

Full-featured semantic search with multi-value filters. Designed for programmatic and MCP use.

```json
// Request body
{
  "prompt": "elegant fantasy gown",
  "limit": 10,
  "offset": 0,
  "tags": ["fantasy", "gown"],
  "artists": ["Daz Originals"],
  "categories": ["Clothing"],
  "compatible_figures": ["Genesis 9"],
  "is_installed": true,
  "score_threshold": 1.0,
  "min_relevance": 0.25,
  "sort_by": "relevance",
  "sort_order": "descending"
}
```

`/query` runs the same search as `/search` (same exact filters, including `is_installed`, and the same relevance) over a fixed pool of the 500 nearest products, paged with `offset` (0–500) and `limit` (1–100) so `total_hits` stays stable across pages. Filter arrays hold up to 50 values. `score_threshold` (0–2, default 1.0) is the legacy cut as a maximum cosine distance, i.e. `min_relevance = 1 - score_threshold`; when both are given the stricter applies. `sort_by` is `relevance` or a metadata field (`name`, `artist`, `category`, `sku`, `asset_count`, ...).

---

### Filters

#### `GET /api/v1/filters`

All distinct filter values present in the index. Use this to populate filter UI dropdowns.

```json
{
  "categories": ["Clothing", "Environments", "People", ...],
  "artists": ["Daz Originals", "Renderosity", ...],
  "compatible_figures": ["Genesis 9", "Genesis 8 Female", ...]
}
```

---

### Settings

#### `GET /api/v1/settings`

**🔒 token**

Returns the effective settings (defaults → `.env` → environment → `settings.json`; see `src/app_config.py`). The CMS password is never returned; `cms_password_set` reports whether one is configured. `applies` says when a saved change takes effect: `immediate` (CMS connection, plugin URL), `next_index_job` (`embedding_model`: `/status` then reports the rebuild it needs) or `restart`. `query_model` is read-only: the active index's model, which every query embeds with.

#### `PUT /api/v1/settings`

**🔒 token**

Saves settings to `settings.json`. Only fields included in the request body are updated; omitted fields are left unchanged. `cms_password` is write-only: an empty string keeps the stored password. The response is the `GET` shape plus `changed`, `applied`, `applies_on_next_index_job`, `restart_required` and `ignored`. `query_model`, `cms_schema` and `daz_script_server_enabled` are deprecated: accepted for older clients, ignored, and listed in `ignored`.

```json
// Request body (all fields optional)
{
  "cms_host": "localhost",
  "cms_port": 17237,
  "cms_db": "Content",
  "cms_user": "dzcms",
  "cms_password": "secret",
  "embedding_model": "BAAI/bge-large-en-v1.5",
  "daz_script_server_url": "http://localhost:18811"
}
```

#### `POST /api/v1/settings/test-db`

**🔒 token**

Test a DAZ CMS connection: the saved settings overlaid with the given ones (a blank password keeps the saved one). Accepts the same body as `PUT /api/v1/settings`.

```json
// Response
{ "success": true, "message": "Connected in 12ms", "latency_ms": 12 }
```

---

### DAZ Studio integration

These endpoints require a running DAZ Studio instance with the **DAZ Script Server** plugin installed and enabled.

#### `GET /api/v1/daz-studio/status`

Probe whether the DAZ Script Server plugin is running.

```json
{ "plugin_detected": true, "plugin_url": "http://localhost:18811", "version": "1.0" }
```

#### `GET /api/v1/daz-studio/content-dirs`

Returns the content library directories from the running DAZ Studio instance.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `refresh` | bool | false | Force re-query from DAZ Studio |

```json
{ "dirs": ["C:/Users/.../Documents/DAZ 3D/Studio/My Library", ...], "count": 3 }
```

#### `POST /api/v1/scene/load`

**🔒 token**

Load an asset file into the current DAZ Studio scene. `path` must resolve beneath a registered content root, otherwise `403`.

```json
// Request body
{ "path": "C:/Users/.../My Library/People/Genesis 9/Starter Essentials/Figures/Genesis 9.duf" }

// Response
{ "success": true, "message": "Loaded 'Genesis 9.duf' into scene.", "detail": { ... } }
```

---

### Assets & files

#### `GET /api/v1/assets/{sku}`

Asset file records for a product with resolved absolute paths. Searches both PostgreSQL content roots and DAZ Script Server content directories to find files on disk.

```json
{
  "sku": "54841",
  "files": [
    {
      "path": "/People/Genesis 8 Female/Clothing",
      "filename": "Night Runner Jacket.duf",
      "resolved_path": "C:/Users/.../My Library/People/Genesis 8 Female/Clothing/Night Runner Jacket.duf"
    }
  ]
}
```

`resolved_path` is `null` if the file cannot be found on disk.

#### `GET /api/v1/assets/thumbnail?path={path}`

**🔒 token**

Serve the companion thumbnail image for a DAZ asset file. DAZ Studio places thumbnails alongside content files with the same filename and a `.png` extension.

Returns the PNG directly, `404` if no thumbnail exists, or `403` if the asset (or its thumbnail) is outside the registered content roots.

#### `POST /api/v1/files/reveal`

**🔒 token**

Open the system file explorer with the given file selected (Windows Explorer, Finder, or `xdg-open`). `path` must resolve beneath a registered content root, otherwise `403`.

```json
// Request body
{ "path": "C:/Users/.../Night Runner Jacket.duf" }
```

---

### Info

#### `GET /api/v1/info`

Full database statistics including category, artist, and tag histograms. Expensive — intended for dashboards and MCP clients, not frequent polling.

#### `GET /api/v1/content-roots`

Content root directories from the DAZ CMS PostgreSQL database.

```json
{ "content_roots": ["C:/Users/.../Documents/DAZ 3D/Studio/My Library"] }
```

---

## CLI reference

All commands run via `python vab.py <command>` (or `vab <command>` if installed via pip).

```bash
python vab.py                    # show help
python vab.py <command> --help   # per-command help
```

| Command | Description |
|---|---|
| `server` | Start the API server |
| `load` | Build / update the search index from DAZ CMS |
| `morphs index` | Index morph .dsf files from a DAZ content library |
| `query` | Semantic search from the terminal |
| `stats` | Print index summary and histograms |
| `openproduct` | Open a product in DAZ Studio Content Library |

### `server`
```bash
python vab.py server [--host HOST] [--port PORT] [--demo]
```

### `load`
```bash
python vab.py load [--force] [--all] [--limit N] [--phase {etl,embed,all}]
```

### `index rebuild` / `index repair`

```bash
vab index rebuild [--refetch] [--model MODEL_ID]   # new generation from SQLite, promoted when complete
                                                   # (--refetch = same as load --force; SQLite is kept)
vab index repair    # quarantine the vector store if it won't open, then rebuild from SQLite (no DAZ CMS needed)
```

`load`, `index rebuild` and `index repair` run through the same job queue as the API. If
another index job is already active, they exit with an error naming it.

### `jobs` / `worker`

```bash
vab jobs list                 # recent index jobs
vab jobs show [JOB_ID]        # one job as JSON (default: the active or latest)
vab jobs cancel [JOB_ID]      # cancel the active job
vab jobs selftest [--background] [--fail]   # no-op job to check the worker
vab worker [--drain]          # run queued jobs; the API spawns `worker --drain` itself
```

### `query`
```bash
python vab.py query "elegant gown" [--categories Clothing] [--compatible_figures "Genesis 9"]
                                   [--artists "Daz Originals"] [--tags tag1 tag2]
                                   [--limit N] [--score F] [--sort-by relevance|name]
                                   [--sort-order ascending|descending]
                                   [--format pretty|json|table]
```

### `stats`
```bash
python vab.py stats
```

### `openproduct`
```bash
python vab.py openproduct --product "dForce Night Runner Outfit"
```

### `morphs index`
```bash
python vab.py morphs index --library-path "C:/Users/YourName/Documents/DAZ 3D/Studio/My Library" [--force]
```

Indexes .dsf morph files from a DAZ content library into the morph SQLite database and ChromaDB collection.
Use `--force` to rebuild the entire index from scratch (wipes existing data).

---

## Developer guide

### Prerequisites

- Python 3.11+
- Node.js 18+ (for the UI)
- Git with submodule support

### Setup

```bash
git clone https://github.com/bluemoonfoundry/daz-content-browser.git
cd daz-content-browser
make install            # Python deps + UI node modules (includes CPU-only torch)
make build              # build UI into ui/dist/
```

> [!NOTE]
> The `ui/src` submodule (`bmf-daz-content-browser-ui`) is a **private** repository, so `make install` can't clone it for outside contributors — it will print a warning and skip the UI node module install rather than fail. Everything else (Python deps, running the server against the pre-built UI) works normally. UI source isn't currently open for external contribution; the pre-built UI in `ui/dist/` is committed to this repo and kept up to date, so it's the only way to run the UI without submodule access.

For development, you can run:

| Command | Mode | UI | Access URL |
|---------|------|-----|------------|
| `./run.sh` | Production | Pre-built from `ui/dist/` | `http://localhost:8000` |
| `./run.sh --demo` | Demo | Pre-built from `ui/dist/` | `http://localhost:8000` |
| `./run.sh --dev-ui` | Production | Vite with hot-reload | `http://localhost:5173` |
| `./run.sh --demo --dev-ui` | Demo | Vite with hot-reload | `http://localhost:5173` |

> Note: The `--dev-ui` flag (and `make build` / `make dev-ui`) requires the `ui/src/` submodule to be present, which in turn requires access to the private frontend repo. Without it, use the pre-built UI at `:8000`.

### Makefile targets

| Target | Description |
|---|---|
| `install` | Install Python deps (including CPU-only PyTorch) and UI node modules |
| `build` | Build the UI into `ui/dist/` |
| `dev-server` | Run the API server at `:8000` |
| `demo-server` | Run the server in demo mode |
| `dev-ui` | Run the Vite dev server at `:5173` |
| `test` | Run smoke tests |
| `sync-ui` | Pull latest UI submodule commits |
| `release-zip` | Build `dist/vab-release.zip` |
| `release-wheel` | Build pip-installable wheel into `dist/` |
| `release-exe` | Build standalone Windows executable via PyInstaller |
| `gh-release` | Publish `dist/` artifacts to a GitHub release |
| `clean` | Remove `ui/dist/`, `dist/`, `src/ui_dist/` |

### Building and publishing releases

```bash
make build           # required first — populates ui/dist/

make release-zip     # → dist/vab-release.zip
make release-wheel   # → dist/visual_asset_browser-*.whl
make release-exe     # → dist/vab/vab.exe

# Publish to GitHub Releases
make gh-release VERSION=v1.0.0 TITLE="Initial Release" NOTES="First public release"

# Update an existing release
make gh-release VERSION=v1.0.0 UPDATE=1 NOTES="Fixed wheel packaging"
```

`TITLE` defaults to `VERSION` if omitted. All `*.zip` and `*.whl` files in `dist/` are attached. The PyInstaller output directory (`dist/vab/`) is zipped automatically if present.
