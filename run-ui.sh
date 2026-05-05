#!/usr/bin/env bash
set -e

echo "================================================"
echo "  Visual Asset Browser"
echo "================================================"
echo

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.11+ from https://python.org"
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Setting up virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dependencies (first run may take several minutes)..."
pip install torch --index-url https://download.pytorch.org/whl/cpu --quiet || { echo "ERROR: Dependency installation failed."; exit 1; }
pip install ".[local_llm]" --quiet || { echo "ERROR: Dependency installation failed."; exit 1; }

echo
echo "To use a GPU instead, run after setup:"
echo "  source .venv/bin/activate"
echo "  pip install torch --index-url https://download.pytorch.org/whl/cu121"
echo
echo "Starting server at http://localhost:8000"
echo "Press Ctrl+C to stop."
echo

# Open browser after the server has had a moment to start
(sleep 3 && (open http://localhost:8000 2>/dev/null || xdg-open http://localhost:8000 2>/dev/null || true)) &

python vab.py server
