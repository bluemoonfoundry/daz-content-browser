#!/usr/bin/env bash
[ -f .venv/bin/activate ] && source .venv/bin/activate
python dev.py "$@"
