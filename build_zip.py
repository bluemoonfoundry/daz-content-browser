#!/usr/bin/env python
"""Build the distributable zip artifact (Option A release).

Usage:    python build_zip.py
Makefile: make release-zip

Prereq: run 'make build' first to populate ui/dist/.
Output: dist/vab-release.zip
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
STAGING = DIST / "vab-release"

INCLUDE_FILES = [
    "vab.py",
    "pyproject.toml",
    "run.bat",
    "run.sh",
    "run-ui.bat",
    "run-ui.sh",
    "run-demo.bat",
    "run-demo.sh",
    "run-demo-ui.bat",
    "run-demo-ui.sh",
]


def main():
    ui_dist = ROOT / "ui" / "dist"
    if not ui_dist.exists():
        print("ERROR: ui/dist/ not found. Run 'make build' first.")
        sys.exit(1)

    DIST.mkdir(exist_ok=True)
    shutil.rmtree(STAGING, ignore_errors=True)
    STAGING.mkdir()

    shutil.copytree(ROOT / "src", STAGING / "src")
    shutil.copytree(ui_dist, STAGING / "ui" / "dist")

    for name in INCLUDE_FILES:
        src = ROOT / name
        if src.exists():
            shutil.copy(src, STAGING / name)
        else:
            print(f"WARNING: {name} not found, skipping.")

    out = DIST / "vab-release"
    shutil.make_archive(str(out), "zip", str(DIST), "vab-release")
    shutil.rmtree(STAGING)
    size_mb = (out.with_suffix(".zip")).stat().st_size / 1_048_576
    print(f"Created {out}.zip ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
