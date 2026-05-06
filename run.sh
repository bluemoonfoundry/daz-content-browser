#!/usr/bin/env bash
# Unified launcher for VAB
# Usage:
#   ./run.sh                    # production mode with pre-built UI
#   ./run.sh --demo             # demo mode with pre-built UI
#   ./run.sh --dev-ui           # production mode with Vite dev server (hot-reload)
#   ./run.sh --demo --dev-ui    # demo mode with Vite dev server

DEMO=false
DEV_UI=false

# Parse arguments
for arg in "$@"; do
    case $arg in
        --demo)
            DEMO=true
            shift
            ;;
        --dev-ui)
            DEV_UI=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--demo] [--dev-ui]"
            echo ""
            echo "Options:"
            echo "  --demo     Run in demo mode (mock data, no database)"
            echo "  --dev-ui   Run with Vite dev server for UI development (requires ui/src/)"
            echo ""
            echo "Examples:"
            echo "  $0                    # production mode with pre-built UI"
            echo "  $0 --demo             # demo mode with pre-built UI"
            echo "  $0 --dev-ui           # production mode with Vite dev server"
            echo "  $0 --demo --dev-ui    # demo mode with Vite dev server"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Find Python executable
if [ -f ".venv/Scripts/python" ]; then
    PYTHON=".venv/Scripts/python"
else
    PYTHON=".venv/bin/python"
fi

# Build command
if [ "$DEV_UI" = true ]; then
    # Use dev.py for Vite dev server
    CMD=("$PYTHON" "dev.py")
    if [ "$DEMO" = true ]; then
        CMD+=("--demo")
    fi
else
    # Use vab.py server for pre-built UI
    CMD=("$PYTHON" "vab.py" "server")
    if [ "$DEMO" = true ]; then
        CMD+=("--demo")
    fi
fi

# Run
"${CMD[@]}"
