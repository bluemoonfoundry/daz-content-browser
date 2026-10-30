"""DAZ Studio and content-library actions: content roots, asset files, thumbnails, loading
into the scene, opening a product, revealing a file. Every filesystem path a client sends
must resolve beneath a registered content root."""
import logging
import time
from pathlib import Path
from typing import Optional

from security import resolve_within_roots
from services.errors import ActionFailed, ComponentUnavailable, Forbidden, NotFound

logger = logging.getLogger(__name__)

ROOTS_TTL_SECONDS = 60.0
_PLUGIN_NOT_RUNNING = "DAZ Script Server plugin is not running."


class ContentService:
    def __init__(self, cms, script_server, desktop, products, demo: bool = False):
        """``cms``/``script_server`` are gateways (``.get()``); ``products`` resolves names."""
        self.cms = cms
        self.script_server = script_server
        self.desktop = desktop
        self.products = products
        self.demo = demo
        self._roots_cache: Optional[tuple] = None

    # ── Roots ──────────────────────────────────────────────────────────────────

    def content_roots(self) -> list:
        """Registered content roots (DAZ CMS first, then the running DAZ Studio's library dirs).

        Cached briefly: thumbnail requests arrive in bursts and each would otherwise hit PostgreSQL.
        """
        if self.demo:
            return []
        now = time.monotonic()
        if self._roots_cache and now - self._roots_cache[0] < ROOTS_TTL_SECONDS:
            return self._roots_cache[1]
        analyzer = self.cms.get()
        client = self.script_server.get()
        pg_roots = analyzer.get_content_roots() if analyzer else []
        plugin_roots = client.get_content_directories() if client.is_available() else []
        # Deduplicate while preserving order: PostgreSQL roots first, then any new plugin roots
        roots = list(dict.fromkeys(r for r in (pg_roots or []) + plugin_roots if r))
        self._roots_cache = (now, roots)
        return roots

    def cms_content_roots(self) -> list:
        analyzer = None if self.demo else self.cms.get()
        return (analyzer.get_content_roots() or []) if analyzer else []

    def require_in_roots(self, path: str) -> Path:
        resolved = resolve_within_roots(path, self.content_roots())
        if resolved is None:
            raise Forbidden("Path is outside the registered content roots.")
        return resolved

    # ── Assets ─────────────────────────────────────────────────────────────────

    def asset_files(self, sku: str) -> list:
        """Asset files for a SKU, with resolved absolute paths where available."""
        analyzer = None if self.demo else self.cms.get()
        if analyzer is None:
            return []
        files = analyzer.get_asset_files_by_sku(sku)
        if files is None:
            raise ActionFailed("Database error fetching asset files.")
        roots = self.content_roots()
        for f in files:
            relative = Path(f["path"].lstrip("/")) / f["filename"]
            f["resolved_path"] = None
            for root in roots:
                candidate = Path(root) / relative
                if candidate.exists():
                    f["resolved_path"] = candidate.as_posix()
                    break
        return files

    def thumbnail(self, path: str) -> Path:
        """The companion PNG DAZ Studio places beside a content file ('FN Ethan.duf' ->
        'FN Ethan.png'). Both the asset and the PNG must resolve beneath a content root."""
        asset = self.require_in_roots(path)
        roots = self.content_roots()
        for candidate in (asset.with_suffix(".png"), asset.parent / (asset.name + ".png")):
            # Re-check after resolution: the companion PNG could be a link pointing elsewhere.
            if candidate.exists() and resolve_within_roots(str(candidate), roots):
                return candidate
        raise NotFound("No thumbnail found for this asset.")

    # ── DAZ Studio ─────────────────────────────────────────────────────────────

    def plugin_status(self) -> dict:
        if self.demo:
            return {"plugin_detected": False, "plugin_url": None, "version": None}
        return self.script_server.get().status()

    def content_dirs(self, refresh: bool) -> dict:
        if self.demo:
            return {"dirs": [], "count": 0, "source": "demo"}
        client = self.script_server.get()
        if not client.is_available():
            raise ComponentUnavailable(_PLUGIN_NOT_RUNNING)
        dirs = client.get_content_directories(force=refresh)
        return {"dirs": dirs, "count": len(dirs), "source": "plugin"}

    def load_into_scene(self, path: str) -> dict:
        self.require_in_roots(path)
        if self.demo:
            return {"success": True, "message": "Demo mode — no action taken.", "detail": None}
        client = self.script_server.get()
        if not client.is_available():
            raise ComponentUnavailable(_PLUGIN_NOT_RUNNING)
        try:
            result = client.load_asset(path)
        except Exception as e:
            raise ActionFailed(str(e))
        return {"success": True, "message": f"Loaded '{path}' into scene.", "detail": result}

    def open_product(self, sku: str, require_known: bool = True) -> dict:
        """Navigates the DAZ Studio Content Library to the product: through the DAZ Script
        Server plugin when it is running, else by launching DAZ Studio with a script."""
        name = self.products.name_for(sku)
        if name is None:
            if require_known:
                raise NotFound(f"Product '{sku}' not found.")
            name = sku
        if self.demo:
            return {"success": True, "message": "Demo mode — no action taken.", "via": "demo",
                    "detail": None}
        client = self.script_server.get()
        if client.is_available():
            try:
                result = client.browse_to_product(name)
                return {"success": True, "message": f"Opened '{name}' via DAZ Script Server.",
                        "via": "plugin", "detail": result}
            except Exception as e:
                logger.warning(f"DAZ Script Server browse failed, falling back to subprocess: {e}")
        if not self.desktop.open_product(name):
            raise ActionFailed("Failed to open product in DAZ Studio.")
        return {"success": True, "message": f"Opened '{name}' via subprocess.", "via": "subprocess",
                "detail": None}

    def reveal(self, path: str) -> dict:
        target = self.require_in_roots(path)
        if not target.exists():
            raise NotFound("Path not found.")
        try:
            self.desktop.reveal(target)
        except Exception as e:
            raise ActionFailed(str(e))
        return {"success": True}
