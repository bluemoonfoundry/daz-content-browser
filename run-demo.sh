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
pip install . --quiet

echo
echo "Starting demo server at http://localhost:8000"
echo "No database required in demo mode."
echo "Press Ctrl+C to stop."
echo

python vab.py server --demo
