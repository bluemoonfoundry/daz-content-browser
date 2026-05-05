# Visual Asset Browser (VAB)

Visual Asset Browser is a semantic search engine and browser for your DAZ Studio content library. Instead of hunting by product name, you describe what you want — "gritty cyberpunk outfit" or "soft fantasy lighting" — and VAB finds relevant assets from your own collection.

<img width="1482" height="768" alt="screenshot_med" src="https://github.com/user-attachments/assets/eb7fbd1c-8b56-404f-bb5d-ea682a6f9fb4" />

## Features

- **Semantic search** — find assets by meaning, not just keywords
- **Hybrid filtering** — combine a vibe search with hard filters (category, artist, compatible figure)
- **Web UI** — browse and search your library in a browser at `http://localhost:8000`
- **Demo mode** — try the UI without any database setup
- **CLI** — query, index, and inspect your library from the terminal
- **DAZ Studio integration** — open products directly in the Content Library via the DAZ Script Server plugin

---

## Installation

Three distribution formats are available from the [Releases page](https://github.com/bluemoonfoundry/daz-content-browser/releases). Choose the one that fits your setup.

### Option A — Release zip (recommended for most users)

The zip includes the pre-built UI and launcher scripts. You need Python 3.11+ installed, but nothing else.

1. Download `vab-release.zip` from the latest release and unzip it to a permanent location (e.g. `C:\Tools\VAB`).
2. Double-click **`run.bat`** (Windows) or run **`./run.sh`** (Mac/Linux).

The launcher creates a virtual environment, installs all dependencies including CPU-only PyTorch, and starts the server. This only happens on the first run — subsequent launches start immediately.

> **GPU users:** After the first run, you can upgrade to a CUDA-accelerated build. See [Switching to GPU (CUDA)](#switching-to-gpu-cuda) below.

### Option B — pip install (for Python-savvy users)

```bash
pip install "visual-asset-browser[local_llm]"
pip install torch --index-url https://download.pytorch.org/whl/cpu
vab server
```

The `vab` command is added to your PATH by pip. See [CLI reference](#cli-reference) for all commands.

### Option C — Standalone executable (no Python required)

Download `vab-windows.zip` from the latest release. Unzip it and run `vab\vab.exe server` from a terminal. No Python installation needed.

> **Note:** The standalone executable is large (~2–3 GB) because PyTorch is bundled inside it. The zip and pip options above are faster to download and recommended unless you specifically cannot install Python.

---

## Quick start: Demo mode

Demo mode runs the server with mock data — no database or configuration required. It's the fastest way to see the UI.

**From the release zip:**
- Double-click `run-demo.bat` (Windows) or run `./run-demo.sh`

**From source or pip:**
```bash
vab server --demo
# or
python vab.py server --demo
```

Open `http://localhost:8000` in your browser.

---

## Production setup

Production mode connects to your DAZ Studio CMS database (PostgreSQL) to index your real library.

### 1. Configure your environment

Copy `.env.example` to `.env` and edit it:

```bash
cp .env.example .env
```

The key values to set:

| Variable | Description |
|---|---|
| `DAZ_STUDIO_EXE_PATH` | Full path to `DAZStudio.exe` |
| `DB_HOST` / `DB_PORT` | DAZ CMS database host and port (defaults: `127.0.0.1` / `17237`) |
| `DB_NAME` / `DB_USER` / `DB_PASS` | DAZ CMS database credentials |
| `EMBEDDING_DEVICE` | `cpu` (default) or `cuda` for GPU acceleration |

The database is the local PostgreSQL instance that DAZ Studio installs and manages. You do not need to set it up yourself — just make sure DAZ Studio has been run at least once.

### 2. Build the index

Run the `load` command to pull products from the DAZ CMS database, enrich them, and build the local search index:

```bash
python vab.py load
```

This may take a while on first run as it generates embeddings for each product. Subsequent runs are incremental — only new products are processed.

Useful flags:
```bash
python vab.py load --force      # full rebuild, re-processes everything
python vab.py load --limit 100  # process only 100 products (good for testing)
python vab.py load --phase etl  # run only the ETL phase (skip embedding)
```

### 3. Start the server

```bash
python vab.py server
```

Open `http://localhost:8000`. The server also accepts `--host` and `--port`:

```bash
python vab.py server --host 0.0.0.0 --port 9000
```

---

## CLI reference

All commands are run via `python vab.py <command>` (or just `vab <command>` if installed via pip).

```
python vab.py            # show help
python vab.py --help     # same
python vab.py <command> --help  # per-command help
```

### `server` — run the web server

```bash
python vab.py server [--host HOST] [--port PORT] [--demo]
```

Starts the FastAPI server. The built UI is served at `/` if `ui/dist/` is present. Defaults to `127.0.0.1:8000`.

### `load` — build the search index

```bash
python vab.py load [--force] [--all] [--limit N] [--phase {etl,embed,all}]
```

Pulls data from the DAZ CMS PostgreSQL database, enriches it, and stores it in the local SQLite + ChromaDB index.

### `query` — search from the terminal

```bash
python vab.py query "gritty cyberpunk street clothes"
python vab.py query "elegant gown" --categories Clothing --compatible_figures "Genesis 9"
python vab.py query "fantasy scene" --limit 10 --format table
```

Options: `--tags`, `--limit`, `--score`, `--sort-by`, `--sort-order`, `--categories`, `--artists`, `--compatible_figures`, `--format {pretty,json,table}`

### `stats` — index summary

```bash
python vab.py stats
```

Prints document counts and top-N histograms for categories, artists, and tags.

### `openproduct` — open in DAZ Studio

```bash
python vab.py openproduct --product "dForce Night Runner Outfit"
```

Navigates the DAZ Studio Content Library to the named product.

---

## Switching to GPU (CUDA)

By default, CPU-only PyTorch is installed. If you have an NVIDIA GPU, you can get significantly faster indexing by switching to a CUDA build.

Find your CUDA version in the NVIDIA control panel, then run:

```bash
# Replace cu121 with your CUDA version (cu118, cu121, cu124, etc.)
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

Also set `EMBEDDING_DEVICE=cuda` in your `.env` file.

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
make install           # install Python deps + UI node modules
make install-torch-cpu # install CPU PyTorch (or install CUDA version manually)
make build             # build the UI into ui/dist/
make dev-server        # start the server at :8000
make dev-ui            # start the Vite dev server at :5173 with hot reload
```

### All Makefile targets

```
make help
```

| Target | Description |
|---|---|
| `install` | Install Python deps and UI node modules |
| `install-torch-cpu` | Install CPU-only PyTorch |
| `build` | Build the UI into `ui/dist/` |
| `dev-server` | Run the FastAPI server at `:8000` |
| `demo-server` | Run the server in demo mode |
| `dev-ui` | Run the Vite dev server at `:5173` |
| `test` | Run smoke tests |
| `sync-ui` | Pull latest UI submodule commits |
| `release-zip` | Build `dist/vab-release.zip` |
| `release-wheel` | Build pip-installable wheel into `dist/` |
| `release-exe` | Build standalone Windows executable via PyInstaller |
| `gh-release` | Publish `dist/` artifacts to a GitHub release |
| `clean` | Remove `ui/dist/`, `dist/`, `src/ui_dist/` |

### Building release artifacts

Run `make build` first (populates `ui/dist/`), then:

```bash
make release-zip    # → dist/vab-release.zip
make release-wheel  # → dist/visual_asset_browser-*.whl
make release-exe    # → dist/vab/vab.exe  (~2-3 GB)
```

To publish to GitHub Releases:

```bash
make gh-release VERSION=v1.0.0 TITLE="Initial Release" NOTES="First public release"

# Update an existing release
make gh-release VERSION=v1.0.0 UPDATE=1 NOTES="Fixed wheel packaging"
```

`TITLE` defaults to `VERSION` if omitted. All `*.zip` and `*.whl` files in `dist/` are uploaded. If the PyInstaller output directory (`dist/vab/`) is present it is zipped automatically before upload.
