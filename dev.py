#!/usr/bin/env python
"""Development launcher: runs the API server and Vite dev server together.

Usage:
    python dev.py           # production API + Vite, opens browser at :5173
    python dev.py --demo    # demo API + Vite

The API server is given 15 seconds to start before Vite is launched.
If either process exits unexpectedly the other is stopped automatically.
Ctrl+C stops both cleanly.
"""
import argparse
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent
UI_SRC = ROOT / "ui" / "src"
VENV = ROOT / ".venv"

SERVER_STARTUP_WAIT = 15   # seconds to wait for the API server before starting Vite
BROWSER_OPEN_DELAY  = 8    # additional seconds after Vite starts before opening browser

# npm is a .cmd batch file on Windows
NPM = "npm.cmd" if sys.platform == "win32" else "npm"


def find_venv_python():
    """Return the venv Python executable, falling back to sys.executable."""
    for candidate in [
        VENV / "Scripts" / "python.exe",
        VENV / "Scripts" / "python",
        VENV / "bin" / "python",
    ]:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def main():
    parser = argparse.ArgumentParser(description="Start API server + Vite dev server")
    parser.add_argument("--demo", action="store_true", help="Run API in demo mode")
    parser.add_argument("--host", default="127.0.0.1", help="API server host")
    parser.add_argument("--port", type=int, default=8000, help="API server port")
    parser.add_argument("--ui-port", type=int, default=5173, help="Vite dev server port")
    args = parser.parse_args()

    venv_python = find_venv_python()

    server_cmd = [venv_python, "vab.py", "server", "--host", args.host, "--port", str(args.port)]
    if args.demo:
        server_cmd.append("--demo")

    print("=" * 52)
    print(f"  API server  ->  http://localhost:{args.port}")
    print(f"  UI (Vite)   ->  http://localhost:{args.ui_port}")
    print("=" * 52)
    print(f"\nStarting API server (waiting {SERVER_STARTUP_WAIT}s before launching Vite)...")
    print("Press Ctrl+C to stop both.\n")

    server = subprocess.Popen(server_cmd, cwd=ROOT)

    # Wait for the server to start, but bail early if it exits
    for _ in range(SERVER_STARTUP_WAIT * 2):   # poll every 0.5s
        time.sleep(0.5)
        if server.poll() is not None:
            print(f"\nERROR: API server exited (code {server.returncode}) during startup.")
            print("Check the output above — likely a missing .env, bad DB credentials, or import error.")
            sys.exit(server.returncode)

    print("API server ready. Starting Vite...\n")
    vite = subprocess.Popen([NPM, "run", "dev"], cwd=UI_SRC)

    def _open_browser():
        time.sleep(BROWSER_OPEN_DELAY)
        if server.poll() is None and vite.poll() is None:
            webbrowser.open(f"http://localhost:{args.ui_port}")

    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        while True:
            time.sleep(0.5)
            if server.poll() is not None:
                print(f"\nAPI server stopped (exit code {server.returncode}). Stopping Vite...")
                vite.terminate()
                vite.wait()
                sys.exit(server.returncode)
            if vite.poll() is not None:
                print(f"\nVite stopped (exit code {vite.returncode}). Stopping API server...")
                server.terminate()
                server.wait()
                sys.exit(vite.returncode)

    except KeyboardInterrupt:
        print("\nStopping...")
        server.terminate()
        vite.terminate()
        server.wait()
        vite.wait()


if __name__ == "__main__":
    main()
