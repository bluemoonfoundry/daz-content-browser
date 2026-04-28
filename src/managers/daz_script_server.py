"""Client for the DAZ Script Server plugin (https://github.com/bluemoonfoundry/daz-script-server)."""

import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Scripts registered once per DAZ Studio session on first successful connection.
_STANDARD_SCRIPTS = {
    "browse-product": {
        "description": "Navigate the Content Library pane to a product by name",
        "script": """(function(){
  var args = getArguments()[0];
  var pane = MainWindow.getPaneMgr().findPane("DzContentLibraryPane");
  if (!pane) throw "Content Library pane not found";
  pane.browseToProduct(args.name);
  return { success: true, name: args.name };
})()""",
    },
    "load-asset": {
        "description": "Load an asset file into the current scene by absolute path",
        "script": """(function(){
  var args = getArguments()[0];
  if (!args.path) throw "path argument required";
  App.getContentMgr().openFile(args.path, false);
  return { success: true, path: args.path };
})()""",
    },
    "get-content-dirs": {
        "description": "Return all content library directories configured in DAZ Studio",
        "script": """(function(){
  var mgr = App.getContentMgr();
  var dirs = [];
  var n = mgr.getNumContentDirectories();
  for (var i = 0; i < n; i++) {
    dirs.push(mgr.getContentDirectory(i));
  }
  return { success: true, paths: dirs };
})()""",
    },
}


class DazScriptServerClient:
    """Thin HTTP client for the DAZ Script Server plugin."""

    def __init__(self, base_url: str = "http://127.0.0.1:18811"):
        self.base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._scripts_registered = False
        self._content_dirs_cache: list[str] | None = None

    # ── Token ──────────────────────────────────────────────────────────────────

    def _read_token(self) -> str | None:
        """Auto-reads the token written by the plugin on first run."""
        token_path = Path.home() / ".daz3d" / "dazscriptserver_token.txt"
        if token_path.exists():
            try:
                return token_path.read_text(encoding="utf-8").strip() or None
            except OSError:
                pass
        return None

    def _get_token(self) -> str | None:
        if not self._token:
            self._token = self._read_token()
        return self._token

    def _headers(self) -> dict:
        token = self._get_token()
        return {"X-API-Token": token} if token else {}

    # ── Connectivity ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Returns plugin status suitable for the /api/v1/daz-studio/status endpoint."""
        try:
            r = requests.get(f"{self.base_url}/health", timeout=2)
            if r.ok:
                data = r.json()
                if data.get("running"):
                    self._ensure_scripts_registered()
                return {
                    "plugin_detected": bool(data.get("running")),
                    "plugin_url": self.base_url,
                    "version": data.get("version"),
                    "auth_enabled": data.get("auth_enabled"),
                    "active_requests": data.get("active_requests", 0),
                }
        except Exception:
            pass
        return {
            "plugin_detected": False,
            "plugin_url": self.base_url,
            "version": None,
            "auth_enabled": None,
            "active_requests": 0,
        }

    def is_available(self) -> bool:
        return self.status().get("plugin_detected", False)

    # ── Script registry ────────────────────────────────────────────────────────

    def _ensure_scripts_registered(self) -> None:
        if self._scripts_registered:
            return
        for name, info in _STANDARD_SCRIPTS.items():
            try:
                r = requests.post(
                    f"{self.base_url}/scripts/register",
                    headers=self._headers(),
                    json={"name": name, "description": info["description"], "script": info["script"]},
                    timeout=5,
                )
                if r.ok:
                    logger.info(f"DAZ Script Server: registered '{name}'")
                else:
                    logger.warning(f"DAZ Script Server: could not register '{name}': {r.status_code}")
            except Exception as e:
                logger.warning(f"DAZ Script Server: registration error for '{name}': {e}")
        self._scripts_registered = True

    # ── Execution helpers ──────────────────────────────────────────────────────

    def _execute_registered(self, script_id: str, args: dict) -> dict:
        r = requests.post(
            f"{self.base_url}/scripts/{script_id}/execute",
            headers=self._headers(),
            json={"args": args},
            timeout=10,
        )
        r.raise_for_status()
        result = r.json()
        if not result.get("success"):
            raise RuntimeError(result.get("error") or f"Script '{script_id}' failed")
        return result

    def execute(self, script: str, args: dict | None = None) -> dict:
        """Execute an inline DazScript string. Returns the full response dict."""
        payload: dict = {"script": script}
        if args:
            payload["args"] = args
        r = requests.post(
            f"{self.base_url}/execute",
            headers=self._headers(),
            json=payload,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    # ── High-level actions ─────────────────────────────────────────────────────

    def browse_to_product(self, product_name: str) -> dict:
        """Navigate the DAZ Studio Content Library to the named product."""
        self._ensure_scripts_registered()
        return self._execute_registered("browse-product", {"name": product_name})

    def load_asset(self, asset_path: str) -> dict:
        """Load an asset file into the current DAZ Studio scene."""
        self._ensure_scripts_registered()
        return self._execute_registered("load-asset", {"path": asset_path})

    def get_content_directories(self, force: bool = False) -> list[str]:
        """Returns all content library directories from the running DAZ Studio instance.

        Result is cached for the session lifetime; pass force=True to refresh.
        """
        if not force and self._content_dirs_cache is not None:
            return self._content_dirs_cache
        try:
            self._ensure_scripts_registered()
            result = self._execute_registered("get-content-dirs", {})
            dirs = [d for d in result.get("paths", []) if d]
            self._content_dirs_cache = dirs
            return dirs
        except Exception as e:
            logger.warning(f"DAZ Script Server: could not get content dirs: {e}")
            return []
