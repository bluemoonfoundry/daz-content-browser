"""API smoke tests — run in demo mode, no real database required."""
import pytest
from fastapi.testclient import TestClient


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


def test_search_max_results_caps_results(client):
    """max_results caps the number of results returned; client paginates from there."""
    r = client.post("/api/v1/search", json={"query": "fantasy dress", "max_results": 2})
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) <= 2


def test_search_total_reflects_full_result_count(client):
    """total counts all matching results regardless of max_results."""
    r_all = client.post("/api/v1/search", json={"query": "fantasy", "max_results": 500})
    r_cap = client.post("/api/v1/search", json={"query": "fantasy", "max_results": 1})
    assert r_all.status_code == 200
    assert r_cap.status_code == 200
    # A capped response must not report more total hits than the uncapped one
    assert r_cap.json()["total"] <= r_all.json()["total"]


def test_search_returns_unique_skus(client):
    """Each result in a single response must have a distinct SKU."""
    r = client.post("/api/v1/search", json={"query": "outfit", "max_results": 500})
    assert r.status_code == 200
    skus = [p["sku"] for p in r.json()["results"]]
    assert len(skus) == len(set(skus))


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
