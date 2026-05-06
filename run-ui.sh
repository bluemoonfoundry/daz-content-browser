#!/usr/bin/env bash
if [ -f ".venv/Scripts/python" ]; then
    ".venv/Scripts/python" dev.py "$@"
else
    ".venv/bin/python" dev.py "$@"
fi
