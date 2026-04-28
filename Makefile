PYTHON  := python
UI_SRC  := ui/src
UI_DIST := ui/dist

.PHONY: install build dev-server dev-ui test clean sync-ui help

help:                ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*##"}; {printf "  %-14s %s\n", $$1, $$2}'

# ── Setup ──────────────────────────────────────────────────────────────────────

install:             ## Install Python deps and UI node modules
	git submodule update --init ui/src
	pip install -e ".[local_llm]"
	cd $(UI_SRC) && npm ci

# ── Build ──────────────────────────────────────────────────────────────────────

build:               ## Build the UI into ui/dist/ (required before running server)
	cd $(UI_SRC) && npm run build

# ── Development ────────────────────────────────────────────────────────────────

dev-server:          ## Run the FastAPI server (serves last built UI at :8000)
	$(PYTHON) src/server.py

dev-ui:              ## Run the Vite dev server with hot reload at :5173 (proxies /api to :8000)
	cd $(UI_SRC) && npm run dev

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

clean:               ## Remove built UI output
	rm -rf $(UI_DIST)
