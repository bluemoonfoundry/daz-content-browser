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


def test_search_returns_total_pages(client):
    r = client.post("/api/v1/search", json={"query": "fantasy dress", "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert "total_pages" in body
    assert body["total_pages"] >= 1


def test_search_pagination_page_param(client):
    """Page 1 and page 2 with limit=1 should return different SKUs (or page 2 empty)."""
    r1 = client.post("/api/v1/search", json={"query": "fantasy", "limit": 1, "page": 1})
    r2 = client.post("/api/v1/search", json={"query": "fantasy", "limit": 1, "page": 2})
    assert r1.status_code == 200
    assert r2.status_code == 200
    skus1 = [p["sku"] for p in r1.json()["results"]]
    skus2 = [p["sku"] for p in r2.json()["results"]]
    # Pages must not overlap (page 2 may be empty if only 1 result exists)
    assert not set(skus1) & set(skus2)


def test_search_total_pages_matches_total(client):
    """total_pages must be ceil(total / limit), never 0."""
    import math
    r = client.post("/api/v1/search", json={"query": "outfit", "limit": 3})
    assert r.status_code == 200
    body = r.json()
    total = body["total"]
    limit = 3
    expected = max(1, math.ceil(total / limit))
    assert body["total_pages"] == expected


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
