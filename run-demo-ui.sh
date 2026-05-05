#!/usr/bin/env bash
set -e

echo "================================================"
echo "  Visual Asset Browser - Demo Dev Mode"
echo "  API server  ->  http://localhost:8000  (demo)"
echo "  UI (Vite)   ->  http://localhost:5173"
echo "================================================"
echo
echo "Tip: run 'make install' first if you haven't set up the environment."
echo

# Start API server in demo mode in the background
python vab.py server --demo &
SERVER_PID=$!

# Kill the server when this script exits
trap "echo ''; echo 'Stopping API server...'; kill $SERVER_PID 2>/dev/null; exit" INT TERM EXIT

# Open browser once Vite is ready
(sleep 5 && (open http://localhost:5173 2>/dev/null || xdg-open http://localhost:5173 2>/dev/null || true)) &

# Run Vite dev server in the foreground — Ctrl+C stops both
cd ui/src && npm run dev
