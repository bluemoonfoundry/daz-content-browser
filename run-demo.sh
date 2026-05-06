#!/usr/bin/env bash
if [ -f ".venv/Scripts/python" ]; then
    ".venv/Scripts/python" vab.py server --demo
else
    ".venv/bin/python" vab.py server --demo
fi
