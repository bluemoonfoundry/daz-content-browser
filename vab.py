#!/usr/bin/env python
# vab.py

import sys
import os

_HELP = """\
usage: vab.py <command> [options]

commands:
  server       Run the FastAPI web server
  query        Query the ChromaDB vector store
  load         Load data from DAZ Postgres to SQLite and ChromaDB
  stats        Display stats about the ChromaDB collection
  openproduct  Open a named DAZ product in DAZ Studio

Run 'vab.py <command> --help' for per-command options.
"""

def main_launcher():
    """Entry point for the Visual Asset Browser CLI.

    Adds src/ to sys.path so all modules are importable, then delegates to
    src/main.py. Use 'make install' (or 'pip install -e .') to install deps.
    """
    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] in ("help", "--help", "-h")):
        print(_HELP)
        sys.exit(0)

    project_root = os.path.dirname(os.path.abspath(__file__))
    src_path = os.path.join(project_root, 'src')
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    from main import main
    main()

if __name__ == "__main__":
    main_launcher()