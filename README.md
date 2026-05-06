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

Three distribution formats are available from the [Releases page](https://github.com/bluemoonfoundry/daz-content-browser/releases).

### Option A — Release zip (recommended for most users)

Requires Python 3.11+ installed. No other setup.

1. Download `vab-release.zip` and unzip to a permanent location (e.g. `C:\Tools\VAB`).
2. Open a terminal in that folder and run the installer:
   ```bash
   python install.py
   ```
3. The installer will ask whether you want CPU-only or CUDA PyTorch. Choose CPU if you are unsure — you can switch later by re-running `install.py`. For CUDA version guidance consult [pytorch.org/get-started/locally](https://pytorch.org/get-started/locally/).
4. Once installation finishes, start the server:
   ```
   run.bat          # Windows
   ./run.sh         # Mac / Git Bash
   ```

> **To switch between CPU and GPU later**, just re-run `python install.py` — it will ask again and reinstall the correct torch build.

### Option B — pip install

```bash
pip install "visual-asset-browser[local_llm]"
pip install torch --index-url https://download.pytorch.org/whl/cpu
vab server
```

The `vab` command is added to your PATH by pip. Replace the torch index URL with your CUDA version if needed (see [pytorch.org/get-started/locally](https://pytorch.org/get-started/locally/)).

### Option C — Standalone executable (no Python required)

Download `vab-windows.zip`, unzip it, and run `vab\vab.exe server`. No Python installation needed.

> The standalone executable is large (~2–3 GB) because PyTorch is bundled inside it. Options A and B are faster to download.

---

## Quick start: Demo mode

Demo mode runs the server with built-in mock data. No database or configuration required — useful for trying the UI or developing against the API.

```bash
# From release zip
run-demo.bat          # Windows
./run-demo.sh         # Mac/Linux

# From source or pip
python vab.py server --demo
vab server --demo
```

Server starts at `http://localhost:8000`. Interactive API docs are at `http://localhost:8000/docs`.

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
| `EMBEDDING_DEVICE` | `cpu` (default) or `cuda` for GPU acceleration |

The DAZ CMS database is installed and managed by DAZ Studio — you do not need to set it up. Run DAZ Studio at least once before indexing.

### 2. Build the index

```bash
python vab.py load
```

Pulls products from the DAZ CMS database, generates embeddings, and stores everything in the local SQLite + ChromaDB index. First run is slow (embedding generation); subsequent runs are incremental.

```bash
python vab.py load --force      # full rebuild from scratch
python vab.py load --limit 100  # process only 100 products (testing)
python vab.py load --phase etl  # ETL only, skip embedding
```

### 3. Start the server

```bash
python vab.py server
python vab.py server --host 0.0.0.0 --port 9000
```

---

## API reference

All endpoints are under `/api/v1/`. The server exposes interactive Swagger docs at `/docs` and ReDoc at `/redoc` — these are the authoritative reference for request/response schemas.

Base URL (default): `http://localhost:8000`

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
  "update_available": false
}
```

#### `POST /api/v1/update`

Trigger an incremental re-index. Returns immediately; indexing runs in the background.

```json
// Request body
{ "force": false }

// Response 202
{ "message": "Update process started.", "task_id": "uuid" }
```

Set `force: true` to rebuild the entire index from scratch.

#### `GET /api/v1/update/status`

Status of the most recently triggered update. Poll this while an update is running.

```json
{
  "running": true,
  "progress": "Embedding product 412 of 800…",
  "stage": "embed",
  "error": null,
  "last_run": "2026-05-05T12:00:00+00:00"
}
```

#### `GET /api/v1/update/status/{task_id}`

Status for a specific background task by ID (returned by `POST /api/v1/update`).

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
  "limit": 25,
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

All filter fields are optional. `min_relevance` filters out results below a score threshold (0.0 = no filtering).

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
  "score_threshold": 1.0,
  "sort_by": "relevance",
  "sort_order": "descending"
}
```

All filter arrays are optional. `score_threshold` is the maximum distance score (lower = more similar; 1.0 returns all results).

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

Returns current configuration (environment + `settings.json` overlay). Password field is omitted.

#### `PUT /api/v1/settings`

Saves settings to `settings.json`. Only fields included in the request body are updated; omitted fields are left unchanged.

```json
// Request body (all fields optional)
{
  "cms_host": "localhost",
  "cms_port": 17237,
  "cms_db": "Content",
  "cms_user": "dzcms",
  "cms_password": "secret",
  "cms_schema": "dzcontent",
  "embedding_model": "mixedbread-ai/mxbai-embed-large-v1",
  "query_model": "mixedbread-ai/mxbai-embed-large-v1",
  "daz_script_server_url": "http://localhost:18811",
  "daz_script_server_enabled": true
}
```

#### `POST /api/v1/settings/test-db`

Test a PostgreSQL connection with the given credentials. Accepts the same body as `PUT /api/v1/settings`.

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

Load an asset file into the current DAZ Studio scene.

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

Serve the companion thumbnail image for a DAZ asset file. DAZ Studio places thumbnails alongside content files with the same filename and a `.png` extension.

Returns the PNG directly, or `404` if no thumbnail exists.

#### `POST /api/v1/files/reveal`

Open the system file explorer with the given file selected (Windows Explorer, Finder, or `xdg-open`).

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

---

## Switching to GPU (CUDA)

CPU-only PyTorch is installed by default. To switch to a CUDA build for faster indexing:

```bash
# Replace cu121 with your CUDA version (cu118, cu121, cu124, etc.)
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

Also set `EMBEDDING_DEVICE=cuda` in your `.env` file.

Find your CUDA version in the NVIDIA Control Panel under Help → System Information.

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
make install            # Python deps + UI node modules
make install-torch-cpu  # CPU PyTorch (or install CUDA version manually)
make build              # build UI into ui/dist/
make dev-server         # API server at :8000
make dev-ui             # Vite dev server at :5173 with hot reload (proxies /api to :8000)
```

### Makefile targets

| Target | Description |
|---|---|
| `install` | Install Python deps and UI node modules |
| `install-torch-cpu` | Install CPU-only PyTorch |
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
make release-exe     # → dist/vab/vab.exe  (~2-3 GB)

# Publish to GitHub Releases
make gh-release VERSION=v1.0.0 TITLE="Initial Release" NOTES="First public release"

# Update an existing release
make gh-release VERSION=v1.0.0 UPDATE=1 NOTES="Fixed wheel packaging"
```

`TITLE` defaults to `VERSION` if omitted. All `*.zip` and `*.whl` files in `dist/` are attached. The PyInstaller output directory (`dist/vab/`) is zipped automatically if present.
