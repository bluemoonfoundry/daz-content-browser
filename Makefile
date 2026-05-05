PYTHON  := python
UI_SRC  := ui/src
UI_DIST := ui/dist
DIST    := dist

# gh-release flags (pass on command line)
VERSION ?=
TITLE   ?= $(VERSION)
NOTES   ?=
UPDATE  ?=

.PHONY: install install-torch-cpu build dev-server demo-server dev-ui \
        open-server open-demo-server \
        release-zip release-wheel release-exe gh-release test clean sync-ui help

help:                ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*##"}; {printf "  %-16s %s\n", $$1, $$2}'

# ── Setup ──────────────────────────────────────────────────────────────────────

install:             ## Install Python deps and UI node modules
	git submodule update --init ui/src
	pip install -e ".[local_llm]"
	cd $(UI_SRC) && npm ci

install-torch-cpu:   ## Install CPU-only PyTorch (no GPU required)
	pip install torch --index-url https://download.pytorch.org/whl/cpu

# ── Build ──────────────────────────────────────────────────────────────────────

build:               ## Build the UI into ui/dist/ (required before running server)
	cd $(UI_SRC) && npm run build

# ── Development ────────────────────────────────────────────────────────────────

dev-server:          ## Run the FastAPI server (serves last built UI at :8000)
	$(PYTHON) vab.py server

demo-server:         ## Run the FastAPI server in demo mode (no DB required)
	$(PYTHON) vab.py server --demo

open-server:         ## Run API server + Vite dev UI, open browser at :5173
	$(PYTHON) dev.py

open-demo-server:    ## Run demo API server + Vite dev UI, open browser at :5173
	$(PYTHON) dev.py --demo

dev-ui:              ## Run the Vite dev server with hot reload at :5173 (proxies /api to :8000)
	cd $(UI_SRC) && npm run dev

# ── Release ────────────────────────────────────────────────────────────────────

release-zip:         ## Build distributable zip with pre-built UI (run 'make build' first)
	$(PYTHON) build_zip.py

release-wheel:       ## Build pip-installable wheel (run 'make build' first)
	$(PYTHON) -c "import shutil; shutil.copytree('ui/dist', 'src/ui_dist', dirs_exist_ok=True)"
	$(PYTHON) -m build --wheel --outdir $(DIST)
	$(PYTHON) -c "import shutil; shutil.rmtree('src/ui_dist', ignore_errors=True)"

release-exe:         ## Build standalone Windows executable via PyInstaller (run 'make build' first)
	pyinstaller vab.spec --distpath $(DIST)

gh-release:          ## Publish dist/ to a GitHub release (VERSION=v1.0.0 [TITLE="..."] [NOTES="..."] [UPDATE=1])
	@test -n "$(VERSION)" || { echo "ERROR: VERSION is required.  make gh-release VERSION=v1.0.0"; exit 1; }
	@if [ -d "$(DIST)/vab" ]; then \
	    echo "Zipping PyInstaller output..."; \
	    $(PYTHON) -c "import shutil; shutil.make_archive('$(DIST)/vab-windows-$(VERSION)', 'zip', '$(DIST)', 'vab')"; \
	fi
	@ARTIFACTS=""; \
	for f in $(DIST)/*.zip $(DIST)/*.whl; do [ -f "$$f" ] && ARTIFACTS="$$ARTIFACTS $$f"; done; \
	[ -n "$$ARTIFACTS" ] || { echo "ERROR: no artifacts found in $(DIST)/. Run release targets first."; exit 1; }; \
	if [ -n "$(UPDATE)" ]; then \
	    echo "Updating release $(VERSION)..."; \
	    gh release edit "$(VERSION)" --title "$(TITLE)" --notes "$(NOTES)"; \
	    gh release upload "$(VERSION)" $$ARTIFACTS --clobber; \
	else \
	    echo "Creating release $(VERSION)..."; \
	    gh release create "$(VERSION)" $$ARTIFACTS --title "$(TITLE)" --notes "$(NOTES)"; \
	fi

# ── Submodule ──────────────────────────────────────────────────────────────────

sync-ui:             ## Pull latest UI commits and stage the new submodule pin
	git submodule update --remote --merge ui/src
	git add ui/src
	@echo ""
	@echo "UI submodule updated. Commit to pin the new version:"
	@echo "  git commit -m 'chore: bump UI to latest'"

# ── Test ───────────────────────────────────────────────────────────────────────

test:                ## Run smoke tests (run 'make build' first)
	$(PYTHON) -m pytest tests/ -v

# ── Clean ──────────────────────────────────────────────────────────────────────

clean:               ## Remove built UI output and dist artifacts
	rm -rf $(UI_DIST) $(DIST) src/ui_dist
