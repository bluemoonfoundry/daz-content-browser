#!/usr/bin/env bash
set -e

echo "================================================"
echo "  Visual Asset Browser -- Demo Mode"
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

echo "Installing dependencies..."
pip install . --quiet || { echo "ERROR: Dependency installation failed."; exit 1; }

echo
echo "Starting demo server at http://localhost:8000"
echo "No database required in demo mode."
echo "Press Ctrl+C to stop."
echo

# Open browser after the server has had a moment to start
(sleep 3 && (open http://localhost:8000 2>/dev/null || xdg-open http://localhost:8000 2>/dev/null || true)) &

python vab.py server --demo
