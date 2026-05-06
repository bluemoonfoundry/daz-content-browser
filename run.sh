#!/usr/bin/env bash
if [ -f ".venv/Scripts/python" ]; then
    ".venv/Scripts/python" vab.py server
else
    ".venv/bin/python" vab.py server
fi
