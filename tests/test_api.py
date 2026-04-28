"""API smoke tests — run in demo mode, no real database required."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture(scope="module")
def client():
    from server import app
    return TestClient(app)


def test_status(client):
    r = client.get("/api/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body
    assert "chromadb_connected" in body


def test_products(client):
    r = client.get("/api/v1/products")
    assert r.status_code == 200
    body = r.json()
    assert "products" in body
    assert "total" in body


def test_filters(client):
    r = client.get("/api/v1/filters")
    assert r.status_code == 200
    body = r.json()
    assert "categories" in body
    assert "artists" in body
    assert "compatible_figures" in body


def test_search(client):
    r = client.post("/api/v1/search", json={"query": "fantasy dress", "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert "results" in body
    assert "total" in body


def test_settings(client):
    r = client.get("/api/v1/settings")
    assert r.status_code == 200
    body = r.json()
    assert "cms_host" in body
    assert "embedding_model" in body


def test_daz_studio_status(client):
    r = client.get("/api/v1/daz-studio/status")
    assert r.status_code == 200
    body = r.json()
    assert "plugin_detected" in body
