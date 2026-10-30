"""The API on the generation catalog: search on the active generation, exact filters,
cross-process promotion, reconciliation-backed status and health (production mode)."""
import types

import pytest
from fastapi.testclient import TestClient

import embedding_utils
from catalog.builder import GenerationBuilder
from catalog.generations import GenerationStore
from catalog.store import CMS_SOURCE, WEB_SOURCE, CatalogStore
from catalog_fakes import fake_embed, web_payload
from managers.job_store import JobStore

AUTH = {"Authorization": "Bearer test-token"}
BASE = "http://127.0.0.1"
FIGS = ["Genesis 8", "Genesis 8.1 Female", "Genesis 9"]
# sku -> (name, compatible figures, artist)
PRODUCTS = {
    "1": ("Silk Robe for G8", "Genesis 8", "Ann"),
    "2": ("Silk Robe for G8.1", "Genesis 8.1 Female", "Anna"),
    "3": ("Wool Robe for G9", "Genesis 9", "Ann"),
}


def _add(catalog, sku, name, figures, artist):
    # A legacy-shaped CMS payload keeps the derived fields verbatim, so figure names are
    # exactly what the test says (no substring-based compatibility detection).
    payload = {"sku": sku, "product_name": name, "artists": artist, "content_types": "Wardrobe",
               "content_item_count": 2, "last_modified_date": "2024-01-01T00:00:00",
               "legacy": {"tags": "robe, clothing", "compatible_figures": figures,
                          "category": "Wardrobe", "subcategories": "Outfits",
                          "embedding_text": f"{name}. A robe."}}
    catalog.upsert_source(CMS_SOURCE, sku, f"daz:{sku}", payload, "2024-01-01T00:00:00")
    catalog.upsert_source(WEB_SOURCE, sku, f"daz:{sku}", web_payload(sku), None)
    catalog.refresh_views([f"daz:{sku}"], FIGS)


def _build(builder, model_id):
    gen = builder.start_or_resume_rebuild(model_id, refetch=False)
    builder.embed_pending(gen)
    builder.finish_rebuild(gen)
    return gen


class FakeAnalyzer:
    def __init__(self, skus):
        self.skus = list(skus)
        self.sku_reads = 0

    def count_skus(self):
        return len(set(self.skus))

    def get_sku_modified(self):
        self.sku_reads += 1
        return {s: "2024-01-01T00:00:00" for s in self.skus}

    def get_content_roots(self):
        return []


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    import catalog.model_config as model_config
    path = tmp_path / "settings.json"
    path.write_text('{"embedding_model": "fake/model-a"}', encoding="utf-8")
    monkeypatch.delenv("EMBEDDING_MODEL_OVERRIDE", raising=False)
    monkeypatch.setattr(model_config, "SETTINGS_PATH", path)
    return path


def _patch_server(monkeypatch, tmp_path, catalog, gens, chroma_path, analyzer):
    import server
    from catalog.active_index import ActiveIndex

    monkeypatch.setattr(server, "APP_MODE", "production")
    monkeypatch.setattr(server, "catalog", catalog)
    monkeypatch.setattr(server, "generations", gens)
    monkeypatch.setattr(server, "active_index", ActiveIndex(gens, chroma_path))
    monkeypatch.setattr(server, "job_store", JobStore(str(tmp_path / "api_jobs.db")))
    monkeypatch.setattr(server, "spawn_worker", lambda store: None)
    monkeypatch.setattr(server, "load_embedding_model", lambda: None)
    monkeypatch.setattr(server, "daz_pg_analyzer", analyzer)
    monkeypatch.setattr(server, "_cms_cache", None)
    return server


@pytest.fixture
def env(tmp_path, monkeypatch, fake_embedder, tmp_settings):
    db = str(tmp_path / "catalog.db")
    chroma_path = str(tmp_path / "chroma")
    catalog = CatalogStore(db)
    assert catalog.migration_error is None
    for sku, (name, figures, artist) in PRODUCTS.items():
        _add(catalog, sku, name, figures, artist)
    # "3" left the DAZ CMS: still searchable, not installed.
    catalog.mark_absent(CMS_SOURCE, {"1", "2"})
    catalog.refresh_views(["daz:3"], FIGS)
    gens = GenerationStore(db)
    builder = GenerationBuilder(catalog, gens, chroma_path, None)
    active = _build(builder, "fake/model-a")

    calls = []

    def spy(texts, is_query=False, model_id=None):
        calls.append((is_query, model_id))
        return fake_embed(texts, is_query=is_query, model_id=model_id)

    monkeypatch.setattr(embedding_utils, "generate_embeddings", spy)
    analyzer = FakeAnalyzer(["1", "2"])
    server = _patch_server(monkeypatch, tmp_path, catalog, gens, chroma_path, analyzer)

    def other_process():
        """Fresh store instances on the same files, as another process would have."""
        c = CatalogStore(db)
        g = GenerationStore(db)
        return types.SimpleNamespace(catalog=c, gens=g,
                                     builder=GenerationBuilder(c, g, chroma_path, None))

    return types.SimpleNamespace(server=server, catalog=catalog, gens=gens, active=active,
                                 calls=calls, analyzer=analyzer, settings=tmp_settings,
                                 other_process=other_process, chroma_path=chroma_path)


@pytest.fixture
def api(env):
    return TestClient(env.server.app, base_url=BASE, headers=AUTH)


@pytest.fixture
def api_with_failed_migration(tmp_path, monkeypatch, fake_embedder, tmp_settings):
    catalog = CatalogStore.__new__(CatalogStore)
    catalog.db_path = None
    catalog.migration_error = "Migration 1 verification failed: sku X lost its name"
    gens = GenerationStore(None)
    server = _patch_server(monkeypatch, tmp_path, catalog, gens, str(tmp_path / "chroma"), None)
    return TestClient(server.app, base_url=BASE, headers=AUTH)


def _query_models(env):
    return [m for is_query, m in env.calls if is_query]


# ── Search ────────────────────────────────────────────────────────────────────

def test_search_uses_active_generation_model(api, env):
    env.settings.write_text('{"embedding_model": "fake/model-b"}', encoding="utf-8")
    r = api.post("/api/v1/search", json={"query": "robe"})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert {x["sku"] for x in results} == {"1", "2", "3"}
    assert _query_models(env) == ["fake/model-a"]
    scores = [x["relevance_score"] for x in results]
    assert scores == sorted(scores, reverse=True)


def test_search_exact_figure_filter(api):
    r = api.post("/api/v1/search", json={"query": "robe", "filters": {"compatible_figures": "Genesis 8"}})
    assert {x["sku"] for x in r.json()["results"]} == {"1"}


def test_search_exact_artist_filter(api):
    r = api.post("/api/v1/search", json={"query": "robe", "filters": {"artist": "Ann"}})
    assert {x["sku"] for x in r.json()["results"]} == {"1", "3"}


def test_search_filter_matching_nothing_returns_empty(api, env):
    r = api.post("/api/v1/search", json={"query": "robe", "filters": {"artist": "Nobody"}})
    assert r.status_code == 200 and r.json()["results"] == []
    assert _query_models(env) == []  # no embedding needed


def test_search_results_report_installed_and_hide_internals(api):
    results = {x["sku"]: x for x in api.post("/api/v1/search", json={"query": "robe"}).json()["results"]}
    assert results["1"]["is_installed"] is True and results["3"]["is_installed"] is False
    for field in ("doc_hash", "doc_format_version", "embedding_text", "distance", "installed"):
        assert field not in results["1"]
    assert results["1"]["compatible_figures"] == ["Genesis 8"]
    assert results["1"]["store_url"] == "https://www.daz3d.com/1"
    r = api.post("/api/v1/search", json={"query": "robe", "filters": {"is_installed": False}})
    assert {x["sku"] for x in r.json()["results"]} == {"3"}


def test_search_follows_promotion_from_another_store_instance(api, env):
    assert api.post("/api/v1/search", json={"query": "robe"}).status_code == 200  # warm handle
    other = env.other_process()
    new = _build(other.builder, "fake/model-b")  # promotes and drops the old collection
    assert env.gens.active()["id"] == new["id"] != env.active["id"]

    r = api.post("/api/v1/search", json={"query": "robe"})
    assert r.status_code == 200, r.text
    assert {x["sku"] for x in r.json()["results"]} == {"1", "2", "3"}
    assert _query_models(env)[-1] == "fake/model-b"


def test_search_retries_when_collection_retired_mid_query(api, env, monkeypatch, caplog):
    stale = env.gens.active()
    api.post("/api/v1/search", json={"query": "robe"})  # warm handle on the old collection
    _build(env.other_process().builder, "fake/model-b")
    real_active = env.gens.active
    answers = [stale]
    # The first read still sees the old generation (promotion lands between the read and the
    # query), so the query hits a collection that no longer exists and must retry.
    monkeypatch.setattr(env.gens, "active", lambda: answers.pop(0) if answers else real_active())
    caplog.set_level("INFO", logger="catalog.active_index")
    r = api.post("/api/v1/search", json={"query": "robe"})
    assert r.status_code == 200, r.text
    assert {x["sku"] for x in r.json()["results"]} == {"1", "2", "3"}
    assert _query_models(env)[-2:] == ["fake/model-a", "fake/model-b"]
    assert "changed from" in caplog.text


def test_search_with_missing_model_returns_503(api, monkeypatch):
    monkeypatch.setattr(embedding_utils, "model_available", lambda m: False)
    r = api.post("/api/v1/search", json={"query": "robe"})
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "model_missing"
    assert "fake/model-a" in r.json()["detail"]["repair"]
    assert api.post("/api/v1/query", json={"prompt": "robe"}).status_code == 503


def test_search_without_active_generation_returns_503(api, env):
    env.gens.mark_failed(env.active["id"], "gone")
    r = api.post("/api/v1/search", json={"query": "robe"})
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "no_index"
    assert "`vab load`" in r.json()["detail"]["repair"]
    assert "vab index rebuild" not in r.json()["detail"]["repair"]


def test_query_keeps_mcp_response_shape(api):
    r = api.post("/api/v1/query", json={"prompt": "robe", "compatible_figures": ["Genesis 8"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"total_hits", "limit", "offset", "results"}
    assert body["total_hits"] == 1
    [hit] = body["results"]
    assert set(hit) == {"id", "distance", "relevance_score", "metadata"}
    assert hit["id"] == "1" and hit["relevance_score"] == round(1 - hit["distance"], 4)
    meta = hit["metadata"]
    assert meta["compatible_figures"] == "Genesis 8" and meta["artist"] == "Ann"
    assert meta["category"] == "Wardrobe" and meta["subcategories"] == "Outfits"
    assert meta["sku"] == "1" and meta["url"] == "https://www.daz3d.com/1"
    assert meta["asset_count"] == 2
    for key in ("sku", "url", "image_url", "name", "artist", "compatible_figures", "tags",
                "category", "subcategories"):
        assert isinstance(meta[key], str)


def test_query_sorts_paginates_and_thresholds(api):
    body = api.post("/api/v1/query", json={"prompt": "robe", "sort_by": "name",
                                            "sort_order": "ascending", "limit": 2}).json()
    assert body["total_hits"] == 3
    assert [h["metadata"]["name"] for h in body["results"]] == ["Silk Robe for G8", "Silk Robe for G8.1"]
    page2 = api.post("/api/v1/query", json={"prompt": "robe", "sort_by": "name",
                                             "sort_order": "ascending", "limit": 2, "offset": 2}).json()
    assert [h["id"] for h in page2["results"]] == ["3"]
    none = api.post("/api/v1/query", json={"prompt": "robe", "score_threshold": -5}).json()
    assert none["total_hits"] == 0 and none["results"] == []


# ── Browse ────────────────────────────────────────────────────────────────────

def test_products_report_not_installed(api):
    products = {p["sku"]: p for p in api.get("/api/v1/products").json()["products"]}
    assert products["3"]["is_installed"] is False
    assert products["1"]["is_installed"] is True
    assert "doc_hash" not in products["1"] and "doc_format_version" not in products["1"]
    only = api.get("/api/v1/products", params={"is_installed": "false"}).json()
    assert [p["sku"] for p in only["products"]] == ["3"] and only["total"] == 1


def test_product_detail_and_filters_come_from_catalog(api):
    p = api.get("/api/v1/products/2").json()
    assert p["name"] == "Silk Robe for G8.1" and p["compatible_figures"] == ["Genesis 8.1 Female"]
    assert api.get("/api/v1/products/nope").status_code == 404
    filters = api.get("/api/v1/filters").json()
    assert filters["compatible_figures"] == ["Genesis 8", "Genesis 8.1 Female", "Genesis 9"]
    assert filters["artists"] == ["Ann", "Anna"]


def test_info_counts_vectors_and_pending(api):
    info = api.get("/api/v1/info").json()
    assert info["total_docs"] == 3
    assert info["total_products_sqlite"] == 3
    assert info["new_products"] == 0
    assert info["histograms"]["artists"] == ["Ann", "Anna"]


# ── Status & health ───────────────────────────────────────────────────────────

def test_status_healthy(api, env):
    s = api.get("/api/v1/status").json()
    assert s["status"] == "ok" and s["repair"] is None
    assert s["sqlite_count"] == 3 and s["chromadb_count"] == 3 and s["chromadb_connected"]
    idx = s["index"]
    assert idx["assets"] == {"total": 3, "installed": 2, "not_installed": 1, "fetch_errors": 0}
    assert idx["vectors"]["current"] == 3 and idx["vectors"]["missing"] == 0
    assert idx["active_generation"]["id"] == env.active["id"]
    assert idx["active_generation"]["model_id"] == "fake/model-a"
    assert idx["building_generation"] is None
    assert idx["rebuild_required"] is False and idx["rebuild_reason"] is None
    assert idx["configured_model"] == "fake/model-a" and idx["query_model_ignored"] is True
    assert idx["migration_error"] is None


def test_status_reports_rebuild_required_on_model_change(api, tmp_settings):
    tmp_settings.write_text('{"embedding_model": "fake/model-b"}')
    s = api.get("/api/v1/status").json()
    assert s["index"]["rebuild_required"] and s["index"]["rebuild_reason"] == "model_changed"
    assert s["status"] == "degraded" and "vab index rebuild" in s["repair"]
    assert s["repair"] == ("Run `vab index rebuild` (or POST /api/v1/update {\"mode\": \"rebuild\"}) "
                           "to rebuild the index for fake/model-b.")
    # Search keeps working on the active generation meanwhile.
    assert api.post("/api/v1/search", json={"query": "robe"}).status_code == 200


def test_status_reports_format_change(api, env, monkeypatch):
    import catalog.status as status_module
    monkeypatch.setattr(status_module, "DOC_FORMAT_VERSION", 999)
    s = api.get("/api/v1/status").json()
    assert s["index"]["rebuild_reason"] == "format_changed" and s["status"] == "degraded"


def test_status_counts_add_and_remove(api, env):
    # CMS count equals installed count (2), so count arithmetic would see nothing; the key
    # diff sees one new product ("9") and one removed ("2").
    env.analyzer.skus = ["1", "9"]
    s = api.get("/api/v1/status").json()
    assert s["postgres_count"] == 2 and s["postgres_connected"]
    assert s["new_products"] == 1 and s["update_available"] is True
    assert s["index"]["pending_cms"] == {"new": 1, "changed": 0, "removed": 1}
    assert s["index"]["cms_reachable"] is True


def test_status_without_cms(api, env, monkeypatch):
    monkeypatch.setattr(env.server, "daz_pg_analyzer", None)
    s = api.get("/api/v1/status").json()
    assert s["postgres_connected"] is False and s["new_products"] == 0
    assert s["index"]["cms_reachable"] is False and s["index"]["pending_cms"] is None
    assert s["status"] == "ok"


def test_status_survives_cms_errors(api, env, monkeypatch):
    def boom():
        raise RuntimeError("connection reset")
    monkeypatch.setattr(env.analyzer, "get_sku_modified", boom)
    s = api.get("/api/v1/status").json()
    assert s["index"]["pending_cms"] is None and s["index"]["cms_reachable"] is False
    assert s["new_products"] == 0 and s["update_available"] is False


def test_status_reports_interrupted_rebuild(api, env):
    other = env.other_process()
    gen = other.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    other.builder.embed_pending(gen, limit=1)
    s = api.get("/api/v1/status").json()["index"]
    assert s["building_generation"] == {"id": gen["id"], "model_id": "fake/model-b", "pending": 2}
    assert s["rebuild_interrupted"] is True
    assert s["active_generation"]["id"] == env.active["id"]

    env.server.job_store.enqueue("rebuild", {"refetch": False})  # a job is on it now
    assert api.get("/api/v1/status").json()["index"]["rebuild_interrupted"] is False


def test_health_reports_index_components(api, env, tmp_settings):
    h = api.get("/api/v1/health").json()
    assert h["status"] == "ok", h
    assert h["components"]["catalog"] == {"ok": True, "migration_error": None}
    assert h["components"]["vector_store"]["collection"] == env.active["collection_name"]
    assert h["components"]["index"]["rebuild_required"] is False

    tmp_settings.write_text('{"embedding_model": "fake/model-b"}')
    h = api.get("/api/v1/health").json()
    assert h["status"] == "degraded"
    index = h["components"]["index"]
    assert index["ok"] is False and index["rebuild_required"] is True
    assert "vab index rebuild" in index["repair"]


def test_health_reports_missing_active_collection(api, env):
    import chromadb
    chromadb.PersistentClient(path=env.chroma_path).delete_collection(env.active["collection_name"])
    h = api.get("/api/v1/health").json()
    assert h["status"] == "degraded"
    assert h["components"]["vector_store"]["ok"] is False
    assert api.get("/api/v1/status").json()["status"] == "degraded"
    # The missing collection must not be silently re-created empty.
    names = {c.name for c in chromadb.PersistentClient(path=env.chroma_path).list_collections()}
    assert env.active["collection_name"] not in names


def test_health_reports_migration_error(api_with_failed_migration):
    h = api_with_failed_migration.get("/api/v1/health").json()
    assert h["status"] == "degraded"
    catalog = h["components"]["catalog"]
    assert catalog["ok"] is False and "sku X lost its name" in catalog["migration_error"]
    s = api_with_failed_migration.get("/api/v1/status").json()
    assert s["status"] == "degraded"
    assert "restart to retry; see server log" in s["repair"]
    assert s["index"]["migration_error"] == catalog["migration_error"]


def test_reads_return_503_when_migration_failed(api_with_failed_migration):
    assert api_with_failed_migration.get("/api/v1/products").status_code == 503
    for method, path, body in [("get", "/api/v1/products/1", None), ("get", "/api/v1/filters", None),
                               ("post", "/api/v1/search", {"query": "robe"}),
                               ("post", "/api/v1/query", {"prompt": "robe"}),
                               ("get", "/api/v1/info", None)]:
        r = getattr(api_with_failed_migration, method)(path, **({"json": body} if body else {}))
        assert r.status_code == 503, path
        detail = r.json()["detail"]
        assert detail["reason"] == "migration_failed"
        assert detail["repair"].endswith("restart to retry; see server log")


def test_catalog_read_errors_are_503(api, env, monkeypatch):
    import sqlite3

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(env.catalog, "get_products", broken)
    r = api.get("/api/v1/products")
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "catalog_unavailable"


# ── CLI ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def cli(env, monkeypatch):
    import main
    from managers import managers as m
    monkeypatch.setattr(m, "catalog", env.catalog)
    monkeypatch.setattr(m, "generations", env.gens)
    monkeypatch.setattr(m, "active_index", env.server.active_index)
    monkeypatch.setattr(main, "job_store", env.server.job_store)
    return main


def test_cli_query_uses_active_index(cli, env, capsys):
    args = types.SimpleNamespace(prompt="robe", tags=None, limit=5, score=1.0, sort_by="relevance",
                                 sort_order="descending", categories=None, artists=None,
                                 compatible_figures=["Genesis 8"], format="json")
    cli.query_command(args)
    out = capsys.readouterr().out
    assert '"total_hits": 1' in out and "Silk Robe for G8" in out
    assert _query_models(env) == ["fake/model-a"]


def test_cli_stats_reports_generation(cli, env, capsys):
    cli.stats_command(types.SimpleNamespace())
    out = capsys.readouterr().out
    assert f"Active generation:       {env.active['id']} (fake/model-a, dim 8)" in out
    assert "Total Documents Indexed: 3" in out
    assert "Genesis 8.1 Female" in out


# ── Fix round 1 ───────────────────────────────────────────────────────────────

def test_unloadable_model_returns_503(api, monkeypatch):
    # Files present (model_available is True) but loading them fails.
    def broken(texts, is_query=False, model_id=None):
        raise RuntimeError("tokenizer.json not found")
    monkeypatch.setattr(embedding_utils, "generate_embeddings", broken)
    for path, body in (("/api/v1/search", {"query": "robe"}), ("/api/v1/query", {"prompt": "robe"})):
        r = api.post(path, json=body)
        assert r.status_code == 503, path
        detail = r.json()["detail"]
        assert detail["reason"] == "model_load_failed"
        assert "fake/model-a" in detail["repair"] and "tokenizer.json not found" in detail["repair"]


def test_status_caches_cms_snapshot(api, env, monkeypatch):
    env.analyzer.skus = ["1", "9"]
    assert api.get("/api/v1/status").json()["new_products"] == 1
    env.analyzer.skus = ["1", "9", "10"]
    assert api.get("/api/v1/status").json()["new_products"] == 1  # within the TTL
    api.get("/api/v1/health")
    assert env.analyzer.sku_reads == 1
    monkeypatch.setattr(env.server, "_CMS_TTL_SECONDS", 0)
    assert api.get("/api/v1/status").json()["new_products"] == 2
    assert env.analyzer.sku_reads == 2


def test_pending_count_matches_pending(env):
    other = env.other_process()
    gen = other.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    assert env.gens.pending_count(gen["id"]) == len(env.gens.pending(gen["id"])) == 3
    other.builder.embed_pending(gen, limit=1)
    assert env.gens.pending_count(gen["id"]) == len(env.gens.pending(gen["id"])) == 2
    other.gens.record_error(gen["id"], ["daz:3"], "boom")
    for attempts in (1, 3):
        assert env.gens.pending_count(gen["id"], attempts) == len(env.gens.pending(gen["id"], attempts))
    assert env.gens.pending_count(env.active["id"]) == len(env.gens.pending(env.active["id"])) == 0


def test_present_keys(env):
    assert env.catalog.present_keys(CMS_SOURCE) == {"1", "2"}
    assert env.catalog.present_keys(WEB_SOURCE) == {"1", "2", "3"}


def _break_status_reads(env, monkeypatch):
    import sqlite3

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(env.catalog, "asset_totals", locked)


def test_health_reports_catalog_read_error(api, env, monkeypatch):
    _break_status_reads(env, monkeypatch)
    h = api.get("/api/v1/health").json()
    assert h["status"] == "degraded"
    catalog = h["components"]["catalog"]
    assert catalog["ok"] is False and "database is locked" in catalog["error"]
    for component in (catalog, h["components"]["vector_store"]):
        assert "rebuild" not in component["repair"] and "server log" in component["repair"]
    assert "No active index generation" not in h["components"]["vector_store"]["error"]


def test_info_returns_503_on_catalog_read_error(api, env, monkeypatch):
    _break_status_reads(env, monkeypatch)
    r = api.get("/api/v1/info")
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "catalog_unavailable"


def test_vector_missing_from_collection_is_marked_missing(api, env, caplog):
    import chromadb
    chromadb.PersistentClient(path=env.chroma_path).get_collection(
        env.active["collection_name"]).delete(ids=["daz:1"])  # behind the catalog's back
    r = api.post("/api/v1/search", json={"query": "robe", "filters": {"artist": "Ann"}})
    assert r.status_code == 200, r.text
    assert {x["sku"] for x in r.json()["results"]} == {"3"}
    assert "reconnecting" not in caplog.text
    assert api.get("/api/v1/health").json()["components"]["vector_store"]["ok"] is True
    assert env.gens.pending(env.active["id"]) == ["daz:1"]
    # Healed: the next filtered search no longer asks Chroma for the unknown id.
    caplog.clear()
    r = api.post("/api/v1/search", json={"query": "robe", "filters": {"artist": "Ann"}})
    assert {x["sku"] for x in r.json()["results"]} == {"3"}
    assert "not in" not in caplog.text
