"""Builds API apps over test adapters (no module globals, no real databases)."""
from api.app import create_app
from index_worker import worker_log_path
from managers.job_store import JobStore
from services.adapters import CmsGateway, ScriptServerGateway
from services.container import build_demo_services, build_services


class FakeDesktop:
    def __init__(self):
        self.launched = []
        self.opened = []

    def open_product(self, name):
        self.opened.append(name)
        return True

    def reveal(self, target):
        self.launched.append(target)


class FakeScriptServer:
    def __init__(self, available=False):
        self.available = available
        self.loaded = []

    def is_available(self):
        return self.available

    def status(self):
        return {"plugin_detected": self.available, "plugin_url": "http://127.0.0.1:18811",
                "version": None, "auth_enabled": None, "active_requests": 0}

    def load_asset(self, path):
        self.loaded.append(path)
        return {"ok": True}

    def get_content_directories(self, force=False):
        return []

    def browse_to_product(self, name):
        raise AssertionError("not expected in this test")


class ServerHarness:
    """A production-mode app over the given adapters. Attributes may be replaced (e.g. by
    monkeypatch) until ``app`` is first read; ``daz_pg_analyzer``, ``daz_script_server``,
    ``spawn_worker`` and ``load_embedding_model`` are looked up on every use."""

    def __init__(self, catalog, generations, active_index, job_store, analyzer=None,
                 script_server=None, desktop=None):
        self.catalog = catalog
        self.generations = generations
        self.active_index = active_index
        self.job_store = job_store
        self.daz_pg_analyzer = analyzer
        self.daz_script_server = script_server or FakeScriptServer()
        self.desktop = desktop or FakeDesktop()
        self.spawn_worker = lambda store: None
        self.load_embedding_model = lambda: None
        self._app = None
        self._services = None

    @property
    def app(self):
        if self._app is None:
            cms = CmsGateway.fixed(None)
            cms.get = lambda: self.daz_pg_analyzer
            script_server = ScriptServerGateway.fixed(None)
            script_server.get = lambda: self.daz_script_server
            self._services = build_services(
                catalog=self.catalog, generations=self.generations,
                active_index=self.active_index, job_store=self.job_store, cms=cms,
                script_server=script_server, desktop=self.desktop,
                spawn_worker=lambda store: self.spawn_worker(store),
                worker_log=worker_log_path,
                load_embedding_model=lambda: self.load_embedding_model())
            self._app = create_app(self._services, serve_ui=False)
        return self._app

    @property
    def services(self):
        self.app
        return self._services


def demo_app(tmp_path):
    services = build_demo_services(JobStore(str(tmp_path / "demo_jobs.db")))
    return create_app(services, serve_ui=False)
