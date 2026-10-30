"""Small adapters around the outside world the API talks to: the DAZ CMS (PostgreSQL), the
DAZ Script Server plugin, and desktop actions (launching DAZ Studio, revealing files).

The gateways follow the configuration: when a saved setting changes the CMS connection or
the plugin URL, the next ``get()`` reconnects, so those settings apply without a restart.
"""
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from app_config import AppConfig, get_config

logger = logging.getLogger(__name__)

# While the CMS is unreachable, reconnect at most this often (status is polled).
_CMS_RETRY_SECONDS = 60.0


class CmsGateway:
    """The DazDBAnalyzer for the configured DAZ CMS connection, or None while it is
    unconfigured or unreachable (``error`` says why)."""

    def __init__(self, config: Callable[[], AppConfig] = get_config, factory=None):
        self._config = config
        self._factory = factory
        self._lock = threading.Lock()
        self._key = None
        self._analyzer = None
        self._tried: Optional[float] = None
        self.error: Optional[str] = None

    @classmethod
    def fixed(cls, analyzer) -> "CmsGateway":
        """A gateway that always returns ``analyzer`` (tests, one-off tools)."""
        gw = cls(config=lambda: None)
        gw.get = lambda: analyzer
        gw.error = None if analyzer is not None else "The DAZ CMS is not configured."
        return gw

    def get(self):
        cms = self._config().cms
        with self._lock:
            now = time.monotonic()
            if cms == self._key and (self._analyzer is not None or (
                    self._tried is not None and now - self._tried < _CMS_RETRY_SECONDS)):
                return self._analyzer
            changed = cms != self._key
            self._key, self._tried, self._analyzer = cms, now, None
            try:
                self._analyzer = self._connect(cms)
                self.error = None
            except Exception as e:  # EnvironmentError (unconfigured) or psycopg2.Error
                if changed or str(e) != self.error:
                    logger.warning(f"DAZ CMS not available: {e}")
                self.error = str(e)
            return self._analyzer

    def _connect(self, cms):
        if self._factory is not None:
            return self._factory(cms)
        from managers.daz_db_analyzer import DazDBAnalyzer
        return DazDBAnalyzer(cms)


class ScriptServerGateway:
    """The DAZ Script Server client for the configured plugin URL."""

    def __init__(self, config: Callable[[], AppConfig] = get_config, factory=None):
        self._config = config
        self._factory = factory
        self._lock = threading.Lock()
        self._url = None
        self._client = None

    @classmethod
    def fixed(cls, client) -> "ScriptServerGateway":
        gw = cls(config=lambda: None)
        gw.get = lambda: client
        return gw

    def get(self):
        url = self._config().daz_script_server_url
        with self._lock:
            if self._client is None or url != self._url:
                if self._factory is not None:
                    self._client = self._factory(url)
                else:
                    from managers.daz_script_server import DazScriptServerClient
                    self._client = DazScriptServerClient(base_url=url)
                self._url = url
            return self._client


class DesktopActions:
    """Launching DAZ Studio and the system file browser."""

    def open_product(self, product_name: str) -> bool:
        """Opens the product in DAZ Studio's Content Library by launching DAZ Studio."""
        from utilities import run_daz_script
        logger.info(f"Opening product '{product_name}' in DAZ Studio...")
        return run_daz_script("OpenProductInContentLibrarySA.dsa", [product_name])

    def reveal(self, target: Path) -> None:
        """Opens the system file explorer with ``target`` selected. Raises on failure."""
        if sys.platform == "win32":
            # A pre-quoted command string goes straight to CreateProcess (no cmd.exe), so
            # Explorer sees /select,"<path>" intact and nothing in the path is shell-parsed.
            subprocess.Popen(f'explorer /select,"{target}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
