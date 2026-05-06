#!/usr/bin/env python
"""Interactive installer for Visual Asset Browser.

Creates the virtual environment and installs all dependencies
including the correct PyTorch build (CPU or CUDA).

Usage: python install.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
VENV = ROOT / ".venv"

CUDA_VERSIONS = [
    ("CUDA 11.8", "cu118"),
    ("CUDA 12.1", "cu121"),
    ("CUDA 12.4", "cu124"),
    ("CUDA 12.6", "cu126"),
]


def run(cmd):
    print(f"  > {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\nERROR: command failed (exit code {result.returncode})")
        sys.exit(result.returncode)


def ask(prompt, options):
    """Present a numbered menu and return the chosen value."""
    while True:
        print(prompt)
        for i, (label, _) in enumerate(options, 1):
            print(f"  {i}. {label}")
        choice = input("Enter number: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx][1]
        except ValueError:
            pass
        print("  Invalid choice, please try again.\n")


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

    # ── Step 2: choose PyTorch build ──────────────────────
    options = (
        [("CPU only  — works on all machines, slower embedding", "cpu")]
        + [(f"CUDA {ver[0].split()[1]}  — NVIDIA GPU, faster embedding  ({ver[1]})", ver[1])
           for ver in CUDA_VERSIONS]
        + [("Other CUDA version  — consult https://pytorch.org/get-started/locally/ for your version", "custom")]
    )
    device = ask(
        "Select PyTorch build:\n  (check NVIDIA Control Panel → Help → System Information for your CUDA version)",
        options,
    )

    if device == "custom":
        print()
        print("  Visit https://pytorch.org/get-started/locally/ to find the correct")
        print("  version string for your system (e.g. 118, 121, 124, 126, 130 ...).")
        print()
        while True:
            value = input("  Enter your CUDA version number: ").strip()
            if value.isdigit():
                device = f"cu{value}"
                break
            print("  Please enter a numeric value only (e.g. 121, 130).")

    # ── Step 3: install torch ─────────────────────────────
    print("\nInstalling PyTorch...")
    if device == "cpu":
        run([str(pip), "install", "torch",
             "--index-url", "https://download.pytorch.org/whl/cpu"])
    else:
        run([str(pip), "install", "torch",
             "--index-url", f"https://download.pytorch.org/whl/{device}"])

    # ── Step 4: install the rest of the dependencies ──────
    print("\nInstalling application dependencies...")
    run([str(pip), "install", "-e", ".[local_llm]"])

    # ── Step 5: create .env if missing ────────────────────
    env_file = ROOT / ".env"
    if not env_file.exists():
        print("\nCreating .env from .env.example...")
        shutil.copy(ROOT / ".env.example", env_file)
        if device != "cpu":
            text = env_file.read_text(encoding="utf-8")
            text = text.replace("EMBEDDING_DEVICE=cpu", "EMBEDDING_DEVICE=cuda")
            env_file.write_text(text, encoding="utf-8")
            print("  Set EMBEDDING_DEVICE=cuda in .env")
        print(f"  Edit .env to configure your DAZ CMS database connection before indexing.")

    # ── Done ──────────────────────────────────────────────
    torch_desc = "CPU" if device == "cpu" else f"CUDA ({device})"
    print()
    print("=" * 52)
    print("  Installation complete!")
    print()
    print(f"  PyTorch build : {torch_desc}")
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
    print()
    print("  To switch GPU/CPU later, re-run install.py.")
    print("=" * 52)


if __name__ == "__main__":
    main()
