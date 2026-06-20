#!/usr/bin/env python
"""Interactive installer for Visual Asset Browser.

Creates the virtual environment and installs all dependencies.
Embedding inference runs via ONNX Runtime, using DirectML (DX12) GPU
acceleration when available and falling back to CPU automatically.

Usage: python install.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
VENV = ROOT / ".venv"


def run(cmd):
    print(f"  > {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\nERROR: command failed (exit code {result.returncode})")
        sys.exit(result.returncode)


def find_in_venv(name):
    """Return the path to an executable inside the venv."""
    for candidate in [
        VENV / "Scripts" / f"{name}.exe",
        VENV / "Scripts" / name,
        VENV / "bin" / name,
    ]:
        if candidate.exists():
            return candidate
    return None


def main():
    print("=" * 52)
    print("  Visual Asset Browser — Installer")
    print("=" * 52)
    print()

    # ── Step 1: create (or reuse) the venv ────────────────
    if VENV.exists():
        print(f"A virtual environment already exists at {VENV.name}/")
        ans = input("Recreate it from scratch? [y/N]: ").strip().lower()
        if ans == "y":
            print("Removing existing venv...")
            shutil.rmtree(VENV)
        else:
            print("Keeping existing venv.\n")

    if not VENV.exists():
        print("Creating virtual environment...")
        run([sys.executable, "-m", "venv", str(VENV)])
        print()

    pip = find_in_venv("pip")
    if not pip:
        print("ERROR: pip not found in the new venv.")
        sys.exit(1)

    # ── Step 2: install CPU-only torch (transitive dep of optimum) ────
    print("Installing PyTorch (CPU build)...")
    run([str(pip), "install", "torch",
         "--index-url", "https://download.pytorch.org/whl/cpu"])

    # ── Step 3: install the rest of the dependencies ──────
    print("\nInstalling application dependencies...")
    run([str(pip), "install", "-e", ".[local_llm]"])

    # chromadb and optimum both pull in plain `onnxruntime` transitively, which
    # shares its import namespace with `onnxruntime-directml` — whichever package
    # is installed last silently wins. Force-reinstall the DirectML build last so
    # GPU acceleration is actually active instead of a silent CPU fallback.
    print("\nEnsuring onnxruntime-directml takes priority over plain onnxruntime...")
    run([str(pip), "install", "--no-deps", "--force-reinstall", "onnxruntime-directml"])

    # ── Step 4: create .env if missing ────────────────────
    env_file = ROOT / ".env"
    if not env_file.exists():
        print("\nCreating .env from .env.example...")
        shutil.copy(ROOT / ".env.example", env_file)
        print("  Edit .env to configure your DAZ CMS database connection before indexing.")

    # ── Done ──────────────────────────────────────────────
    print()
    print("=" * 52)
    print("  Installation complete!")
    print()
    print("  Embedding: ONNX Runtime with DirectML GPU acceleration (CPU fallback if unavailable)")
    print("  Model will be downloaded on first server start.")
    print()
    print("  Usage:")
    print("    run.bat                    (Windows)")
    print("    ./run.sh                   (Mac / Linux)")
    print()
    print("  Options:")
    print("    --demo       Demo mode (mock data, no database)")
    print("    --dev-ui     Run with Vite dev server (for UI development)")
    print()
    print("  Examples:")
    print("    ./run.sh                   Production with pre-built UI")
    print("    ./run.sh --demo            Demo mode")
    print("    ./run.sh --dev-ui          Production with hot-reload")
    print("    ./run.sh --demo --dev-ui   Demo with hot-reload")
    print("=" * 52)


if __name__ == "__main__":
    main()
