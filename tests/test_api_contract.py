"""The API contract: OpenAPI examples match the models, request bounds are enforced,
and demo responses use the documented filters and shapes."""
import inspect

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

import api.schemas as schemas
from api_harness import demo_app

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(tmp_path):
    return TestClient(demo_app(tmp_path), base_url="http://127.0.0.1", headers=AUTH)


def _models_with_examples():
    for name, model in inspect.getmembers(schemas, inspect.isclass):
        if issubclass(model, BaseModel) and model.__module__ == schemas.__name__:
            for example in (model.model_config.get("json_schema_extra") or {}).get("examples", []):
                yield pytest.param(model, example, id=name)


@pytest.mark.parametrize("model,example", list(_models_with_examples()))
def test_openapi_examples_validate_against_their_models(model, example):
    model.model_validate(example)


def test_every_json_endpoint_declares_a_response_model(client):
    spec = client.get("/openapi.json").json()
    missing = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            content = op["responses"].get("200", op["responses"].get("202", {})).get("content", {})
            schema = content.get("application/json", {}).get("schema")
            if "image/png" not in content and (not schema or schema == {}):
                missing.append(f"{method.upper()} {path}")
    assert missing == []


@pytest.mark.parametrize("path", ["/api/v1/status", "/api/v1/health", "/api/v1/info",
                                  "/api/v1/filters", "/api/v1/products",
                                  "/api/v1/products/85227", "/api/v1/settings",
                                  "/api/v1/update/status", "/api/v1/jobs",
                                  "/api/v1/daz-studio/status", "/api/v1/content-roots",
                                  "/api/v1/assets/85227"])
def test_demo_get_endpoints_respond(client, path):
    assert client.get(path).status_code == 200, client.get(path).text


@pytest.mark.parametrize("method,path,body,params", [
    ("POST", "/api/v1/search", {"query": ""}, None),
    ("POST", "/api/v1/search", {"query": "x", "max_results": 0}, None),
    ("POST", "/api/v1/search", {"query": "x", "max_results": 100_000}, None),
    ("POST", "/api/v1/search", {"query": "x", "min_relevance": 1.5}, None),
    ("POST", "/api/v1/search", {"query": "x" * 5000}, None),
    ("POST", "/api/v1/query", {"prompt": "x", "limit": 0}, None),
    ("POST", "/api/v1/query", {"prompt": "x", "limit": 1000}, None),
    ("POST", "/api/v1/query", {"prompt": "x", "offset": -1}, None),
    ("POST", "/api/v1/query", {"prompt": "x", "offset": 100_000}, None),
    ("POST", "/api/v1/query", {"prompt": "x", "score_threshold": 3}, None),
    ("POST", "/api/v1/query", {"prompt": "x", "sort_by": "embedding_text"}, None),
    ("POST", "/api/v1/query", {"prompt": "x", "tags": ["t"] * 51}, None),
    ("GET", "/api/v1/products", None, {"page": 0}),
    ("GET", "/api/v1/products", None, {"page_size": 20_000}),
    ("GET", "/api/v1/products", None, {"sort_by": "price"}),
    ("GET", "/api/v1/products", None, {"sort_dir": "sideways"}),
    ("GET", "/api/v1/jobs", None, {"limit": 1000}),
    ("PUT", "/api/v1/settings", {"cms_port": 70000}, None),
])
def test_out_of_bounds_requests_are_rejected(client, method, path, body, params):
    assert client.request(method, path, json=body, params=params).status_code == 422


def test_products_filters_including_is_installed(client):
    def skus(**params):
        return {p["sku"] for p in client.get("/api/v1/products", params=params).json()["products"]}
    assert skus(is_installed=False) == {"88441"}
    assert len(skus(is_installed=True)) == 3
    assert skus(category="props") == {"90567"}
    assert skus(artist="Val-Z") == {"91123"}
    assert skus(compatible_figure="Genesis 9") == {"85227", "91123"}
    assert skus(name_query="wizard") == {"90567"}
    names = [p["name"] for p in client.get("/api/v1/products",
                                           params={"sort_dir": "desc"}).json()["products"]]
    assert names == sorted(names, reverse=True)


def test_search_and_query_apply_the_same_filters(client):
    search = client.post("/api/v1/search", json={
        "query": "outfit", "filters": {"is_installed": True,
                                       "compatible_figures": ["Genesis 9"],
                                       "category": "clothes"}}).json()
    query = client.post("/api/v1/query", json={
        "prompt": "outfit", "is_installed": True, "compatible_figures": ["Genesis 9"],
        "categories": ["clothes"]}).json()
    assert [r["sku"] for r in search["results"]] == [h["id"] for h in query["results"]] == ["91123"]
    none = client.post("/api/v1/search", json={"query": "outfit",
                                                "filters": {"is_installed": False,
                                                            "category": "clothes"}}).json()
    assert none["results"] == [] and none["total"] == 0


def test_query_pages_a_stable_pool(client):
    first = client.post("/api/v1/query", json={"prompt": "genesis", "limit": 2}).json()
    second = client.post("/api/v1/query", json={"prompt": "genesis", "limit": 2,
                                                 "offset": 2}).json()
    assert first["total_hits"] == second["total_hits"] == 4
    ids = [h["id"] for h in first["results"] + second["results"]]
    assert len(set(ids)) == 4


def test_demo_update_is_forbidden(client):
    r = client.post("/api/v1/update", json={})
    assert r.status_code == 403 and "demo" in r.json()["detail"]


def test_settings_put_reports_application(client, tmp_path, monkeypatch):
    monkeypatch.setenv("VAB_SETTINGS_PATH", str(tmp_path / "settings.json"))
    body = client.put("/api/v1/settings", json={"cms_host": "db.local",
                                                "query_model": "x"}).json()
    assert body["changed"] == ["cms_host"] and body["applied"] == ["cms_host"]
    assert body["ignored"] == ["query_model"] and body["cms_host"] == "db.local"
    assert body["applies"]["embedding_model"] == "next_index_job"
