#!/usr/bin/env python
"""Development launcher: runs the API server and Vite dev server together.

Usage:
    python dev.py           # production API + Vite, opens browser at :5173
    python dev.py --demo    # demo API + Vite

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

# On Windows npm is a .cmd batch file; it needs shell=True or the .cmd extension
NPM = "npm.cmd" if sys.platform == "win32" else "npm"


def main():
    parser = argparse.ArgumentParser(description="Start API server + Vite dev server")
    parser.add_argument("--demo", action="store_true", help="Run API in demo mode")
    parser.add_argument("--host", default="127.0.0.1", help="API server host")
    parser.add_argument("--port", type=int, default=8000, help="API server port")
    parser.add_argument("--ui-port", type=int, default=5173, help="Vite port (informational)")
    args = parser.parse_args()

    server_cmd = [sys.executable, "vab.py", "server", "--host", args.host, "--port", str(args.port)]
    if args.demo:
        server_cmd.append("--demo")

    print("=" * 50)
    print(f"  API server  ->  http://localhost:{args.port}")
    print(f"  UI (Vite)   ->  http://localhost:{args.ui_port}")
    print("=" * 50)
    print("Press Ctrl+C to stop both.\n")

    server = subprocess.Popen(server_cmd, cwd=ROOT)
    vite = subprocess.Popen([NPM, "run", "dev"], cwd=UI_SRC)

    # Open browser after a short delay so Vite has time to compile
    def _open_browser():
        time.sleep(5)
        if server.poll() is None and vite.poll() is None:
            webbrowser.open(f"http://localhost:{args.ui_port}")

    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        while True:
            time.sleep(0.5)
            server_rc = server.poll()
            vite_rc = vite.poll()

            if server_rc is not None:
                print(f"\nAPI server stopped (exit code {server_rc}). Stopping Vite...")
                vite.terminate()
                vite.wait()
                sys.exit(server_rc)

            if vite_rc is not None:
                print(f"\nVite stopped (exit code {vite_rc}). Stopping API server...")
                server.terminate()
                server.wait()
                sys.exit(vite_rc)

    except KeyboardInterrupt:
        print("\nStopping...")
        server.terminate()
        vite.terminate()
        server.wait()
        vite.wait()


if __name__ == "__main__":
    main()
