"""Route tests using dependency overrides with lightweight fakes.

These tests avoid real DB/plugin managers and validate route behavior using
FastAPI dependency injection.
"""

import importlib
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class FakeCollection:
    def count(self):
        return 5


class FakeChroma:
    def __init__(self):
        self.collection = FakeCollection()

    def search(self, **kwargs):
        return {
            "results": [
                {
                    "id": "sku-1",
                    "relevance_score": 0.91,
                    "metadata": {
                        "name": "Alpha Outfit",
                        "artist": "Artist A",
                        "category": "People",
                        "compatible_figures": "Genesis 8 Female",
                        "tags": "outfit,scifi",
                        "url": "https://example.com/sku-1",
                    },
                }
            ],
            "total_hits": 1,
            "took_ms": 3,
        }

    def get_db_stats(self):
        return {
            "total_docs": 5,
            "last_update": "2026-01-01T00:00:00+00:00",
            "histograms": {"tags": {"scifi": 2}},
        }


class FakeSQLite:
    def count(self):
        return 5

    def get_products(self, **kwargs):
        return {
            "products": [
                {
                    "sku": "sku-1",
                    "name": "Alpha Outfit",
                    "artist": "Artist A",
                    "category": "People",
                    "compatible_figures": "Genesis 8 Female",
                    "tags": "outfit,scifi",
                    "subcategories": "Clothing",
                    "url": "https://example.com/sku-1",
                }
            ],
            "total": 1,
            "page": 1,
            "page_size": 25,
            "total_pages": 1,
        }

    def get_sku_row(self, sku):
        if sku != "sku-1":
            return None
        return {
            "sku": "sku-1",
            "name": "Alpha Outfit",
            "artist": "Artist A",
            "category": "People",
            "compatible_figures": "Genesis 8 Female",
            "tags": "outfit,scifi",
            "subcategories": "Clothing",
            "url": "https://example.com/sku-1",
        }

    def get_filter_values(self):
        return {
            "categories": ["People"],
            "artists": ["Artist A"],
            "compatible_figures": ["Genesis 8 Female"],
        }


class FakePgAnalyzer:
    def count_skus(self):
        return 7

    def get_content_roots(self):
        return ["/content"]

    def get_asset_files_by_sku(self, sku):
        return [{"path": "/People", "filename": "thing.duf", "content_type": "Preset"}]


class FakeScriptServer:
    def status(self):
        return {"plugin_detected": True, "plugin_url": "http://fake", "version": "1.0"}

    def is_available(self):
        return True

    def browse_to_product(self, product_name):
        return {"success": True, "name": product_name}

    def load_asset(self, asset_path):
        return {"success": True, "path": asset_path}

    def get_content_directories(self, force=False):
        return ["/content"]


class FakeTaskService:
    def __init__(self):
        self.current = {"running": False, "progress": "", "stage": "", "error": None, "last_run": None}
        self.tasks = {}
        self.running = False

    def get_current(self):
        return self.current

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def is_running(self):
        return self.running

    def prune_old(self):
        return None

    def create_task(self, task_id):
        self.running = True
        self.current["running"] = True
        task = {"task_id": task_id, "task_status": 1, "stage": "start", "progress": "queued", "created_at": "2026-01-01T00:00:00+00:00"}
        self.tasks[task_id] = task
        return task

    def finish_from_task_entry(self, task_entry):
        self.running = False
        self.current["running"] = False


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("APP_MODE", "production")

    import api.app_state as app_state
    import api.dependencies as deps
    import api.routes.integration as routes_integration
    import api.routes.products_search as routes_products
    import api.routes.status_update as routes_status
    import server as server_mod

    importlib.reload(app_state)
    importlib.reload(deps)
    importlib.reload(server_mod)

    fake_chroma = FakeChroma()
    fake_sqlite = FakeSQLite()
    fake_pg = FakePgAnalyzer()
    fake_script = FakeScriptServer()
    fake_tasks = FakeTaskService()

    fake_api_tasks = types.ModuleType("api_tasks")

    def fake_run_update_flow(task_entry, force=False):
        task_entry["task_status"] = 0
        task_entry["stage"] = "finished"
        task_entry["progress"] = "done"

    fake_api_tasks.run_update_flow = fake_run_update_flow
    monkeypatch.setitem(sys.modules, "api_tasks", fake_api_tasks)

    # Avoid real manager import during app lifespan startup validation.
    server_mod.get_daz_pg_analyzer = lambda: fake_pg

    server_mod.app.dependency_overrides[deps.get_chroma_db_manager] = lambda: fake_chroma
    server_mod.app.dependency_overrides[deps.get_sqlite_db] = lambda: fake_sqlite
    server_mod.app.dependency_overrides[deps.get_daz_pg_analyzer] = lambda: fake_pg
    server_mod.app.dependency_overrides[deps.get_daz_script_server] = lambda: fake_script
    server_mod.app.dependency_overrides[deps.get_update_task_service] = lambda: fake_tasks
    server_mod.app.dependency_overrides[routes_status.get_chroma_db_manager] = lambda: fake_chroma
    server_mod.app.dependency_overrides[routes_status.get_sqlite_db] = lambda: fake_sqlite
    server_mod.app.dependency_overrides[routes_status.get_daz_pg_analyzer] = lambda: fake_pg
    server_mod.app.dependency_overrides[routes_status.get_update_task_service] = lambda: fake_tasks
    server_mod.app.dependency_overrides[routes_products.get_chroma_db_manager] = lambda: fake_chroma
    server_mod.app.dependency_overrides[routes_products.get_sqlite_db] = lambda: fake_sqlite
    server_mod.app.dependency_overrides[routes_products.get_daz_script_server] = lambda: fake_script
    server_mod.app.dependency_overrides[routes_integration.get_chroma_db_manager] = lambda: fake_chroma
    server_mod.app.dependency_overrides[routes_integration.get_sqlite_db] = lambda: fake_sqlite
    server_mod.app.dependency_overrides[routes_integration.get_daz_pg_analyzer] = lambda: fake_pg
    server_mod.app.dependency_overrides[routes_integration.get_daz_script_server] = lambda: fake_script

    with TestClient(server_mod.app) as test_client:
        yield test_client

    server_mod.app.dependency_overrides.clear()


def test_status_with_fakes(client):
    r = client.get("/api/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["postgres_count"] == 7
    assert body["chromadb_count"] == 5
    assert body["update_available"] is True


def test_products_and_filters_with_fakes(client):
    p = client.get("/api/v1/products")
    assert p.status_code == 200
    assert p.json()["products"][0]["sku"] == "sku-1"

    f = client.get("/api/v1/filters")
    assert f.status_code == 200
    assert "People" in f.json()["categories"]


def test_search_and_query_with_fakes(client):
    s = client.post("/api/v1/search", json={"query": "sci-fi outfit", "limit": 5})
    assert s.status_code == 200
    assert s.json()["total"] == 1

    q = client.post("/api/v1/query", json={"prompt": "sci-fi outfit"})
    assert q.status_code == 200
    assert q.json()["total_hits"] == 1


def test_integration_routes_with_fakes(client):
    ds = client.get("/api/v1/daz-studio/status")
    assert ds.status_code == 200
    assert ds.json()["plugin_detected"] is True

    roots = client.get("/api/v1/content-roots")
    assert roots.status_code == 200
    assert roots.json()["content_roots"] == ["/content"]

    info = client.get("/api/v1/info")
    assert info.status_code == 200
    assert info.json()["total_products_postgres"] == 7


def test_update_routes_with_fakes(client):
    start = client.post("/api/v1/update", json={"force": False})
    assert start.status_code == 202
    task_id = start.json()["task_id"]
    assert task_id

    status_global = client.get("/api/v1/update/status")
    assert status_global.status_code == 200
    assert status_global.json()["running"] is False

    status_task = client.get(f"/api/v1/update/status/{task_id}")
    assert status_task.status_code == 200
    assert status_task.json()["task_status"] == 0
    assert status_task.json()["stage"] == "finished"


def test_update_task_not_found(client):
    missing = client.get("/api/v1/update/status/does-not-exist")
    assert missing.status_code == 404


def test_update_already_running(client):
    import server as server_mod

    fake_tasks = FakeTaskService()
    fake_tasks.running = True
    fake_tasks.current["running"] = True

    import api.dependencies as deps
    import api.routes.status_update as routes_status

    server_mod.app.dependency_overrides[deps.get_update_task_service] = lambda: fake_tasks
    server_mod.app.dependency_overrides[routes_status.get_update_task_service] = lambda: fake_tasks

    resp = client.post("/api/v1/update", json={"force": False})
    assert resp.status_code == 202
    assert resp.json()["task_id"] is None
