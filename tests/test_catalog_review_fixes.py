"""Final whole-branch review fixes (hc8): server-side legacy adoption (C1), job-path model
export (I1), changed CMS products in status (I2), adoption failures (M1/M2), first-build
--limit warning (M4), cumulative scrape progress (M6) and the installed filter in SQL (M7)."""
import sqlite3
import types
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import embedding_utils
import index_worker
from catalog.active_index import ActiveIndex
from catalog.builder import AdoptionFailed, GenerationBuilder
from catalog.generations import GenerationStore
from catalog.model_config import configured_model_id
from catalog.status import no_index_hint, pending_cms_diff
from catalog.store import CMS_SOURCE, CatalogStore
from catalog_fakes import FakeScraper, cms_row, fake_embed
from managers.chroma_db_manager import ChromaDbManager
from managers.job_store import FAILED, SUCCEEDED, JobStore
from services.adapters import CmsGateway
from test_catalog_api import AUTH, BASE, FIGS, PRODUCTS, FakeAnalyzer, _add, _patch_server
from test_catalog_api import tmp_settings  # noqa: F401  (fixture)


# ── helpers ────────────────────────────────────────────────────────────────────

def _legacy(chroma_path, name="legacy", skus=("1", "2", "3"), model_id="fake/model-a"):
    mgr = ChromaDbManager(chroma_path, name)
    mgr.collection.upsert(ids=list(skus), embeddings=fake_embed(
        [f"Product {s}" for s in skus], model_id=model_id).tolist(), documents=list(skus))
    return mgr


@pytest.fixture
def legacy_env(tmp_path, monkeypatch, fake_embedder, tmp_settings):
    """An upgraded install: catalog migrated, the legacy collection on disk, no generations."""
    db = str(tmp_path / "catalog.db")
    chroma_path = str(tmp_path / "chroma")
    catalog = CatalogStore(db)
    for sku, (name, figures, artist) in PRODUCTS.items():
        _add(catalog, sku, name, figures, artist)
    gens = GenerationStore(db)
    _legacy(chroma_path)
    builder = GenerationBuilder(catalog, gens, chroma_path, "legacy")
    active = ActiveIndex(gens, chroma_path,
                         adopt=lambda: builder.adopt_legacy(configured_model_id()))
    server = _patch_server(monkeypatch, tmp_path, catalog, gens, chroma_path,
                           FakeAnalyzer(["1", "2", "3"]))
    monkeypatch.setattr(server, "active_index", active)
    return types.SimpleNamespace(server=server, catalog=catalog, gens=gens, builder=builder,
                                 active_index=active, chroma_path=chroma_path, db=db)


# ── C1: the server adopts the legacy collection ───────────────────────────────

def test_upgraded_install_serves_search_without_a_job(legacy_env):
    api = TestClient(legacy_env.server.app, base_url=BASE, headers=AUTH)
    assert legacy_env.gens.active() is None
    r = api.post("/api/v1/search", json={"query": "robe"})
    assert r.status_code == 200, r.text
    assert {x["sku"] for x in r.json()["results"]} == {"1", "2", "3"}
    s = api.get("/api/v1/status").json()
    assert s["index"]["rebuild_required"] is False, s
    assert s["index"]["active_generation"]["id"] == 1
    assert s["index"]["active_generation"]["model_id"] == "fake/model-a"
    assert legacy_env.gens.active()["collection_name"] == "legacy"


def test_lifespan_adopts_legacy_collection(legacy_env):
    with TestClient(legacy_env.server.app, base_url=BASE, headers=AUTH):
        assert legacy_env.gens.active()["collection_name"] == "legacy"


def test_lifespan_survives_adoption_errors(legacy_env, monkeypatch):
    def boom():
        raise RuntimeError("chroma exploded")
    monkeypatch.setattr(legacy_env.active_index, "_adopt", boom)
    with TestClient(legacy_env.server.app, base_url=BASE, headers=AUTH) as api:
        assert api.get("/api/v1/status").status_code == 200


def test_active_index_current_adopts_lazily(legacy_env):
    gen, mgr = legacy_env.active_index.current()
    assert gen["id"] == 1 and mgr.collection_name == "legacy"


def test_concurrent_adoption_counts_as_success(legacy_env, monkeypatch):
    other = GenerationBuilder(CatalogStore(legacy_env.db), GenerationStore(legacy_env.db),
                              legacy_env.chroma_path, "legacy")
    # Our check saw no generations, then the other process adopted before our insert.
    monkeypatch.setattr(legacy_env.builder, "_has_generations", lambda: False)
    adopted = other.adopt_legacy("fake/model-a")
    gen = legacy_env.builder.adopt_legacy("fake/model-a")
    assert gen is not None and gen["id"] == adopted["id"]
    assert legacy_env.active_index.current()[0]["id"] == adopted["id"]


def test_no_index_hints_point_to_incremental_load(tmp_path, monkeypatch, fake_embedder,
                                                  tmp_settings):
    db = str(tmp_path / "catalog.db")
    catalog = CatalogStore(db)
    gens = GenerationStore(db)
    chroma_path = str(tmp_path / "chroma")
    builder = GenerationBuilder(catalog, gens, chroma_path, "legacy")  # nothing to adopt
    server = _patch_server(monkeypatch, tmp_path, catalog, gens, chroma_path, None)
    monkeypatch.setattr(server, "active_index", ActiveIndex(
        gens, chroma_path, adopt=lambda: builder.adopt_legacy(configured_model_id())))
    api = TestClient(server.app, base_url=BASE, headers=AUTH)

    hint = no_index_hint()
    assert "`vab load`" in hint and "vab index rebuild" not in hint
    assert '"mode": "update"' in hint
    r = api.post("/api/v1/search", json={"query": "robe"})
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "no_index"
    assert "`vab load`" in r.json()["detail"]["repair"]
    assert "vab index rebuild" not in r.json()["detail"]["repair"]
    s = api.get("/api/v1/status").json()
    assert s["index"]["rebuild_reason"] == "no_index" and s["repair"] == hint
    h = api.get("/api/v1/health").json()
    assert h["components"]["vector_store"]["repair"] == hint
    assert h["components"]["index"]["repair"] == hint


def test_cli_stats_no_index_hint(tmp_path, monkeypatch, fake_embedder, tmp_settings, capsys):
    import main
    from managers import managers as m
    db = str(tmp_path / "catalog.db")
    monkeypatch.setattr(m, "catalog", CatalogStore(db))
    monkeypatch.setattr(m, "generations", GenerationStore(db))
    monkeypatch.setattr(main, "job_store", JobStore(str(tmp_path / "jobs.db")))
    main.stats_command(types.SimpleNamespace())
    out = capsys.readouterr().out
    assert "`vab load`" in out and "vab index rebuild" not in out


# ── M1 / M2: adoption failures ────────────────────────────────────────────────

def test_unreadable_legacy_collection_raises_adoption_failed(legacy_env, monkeypatch):
    def broken(self):
        raise RuntimeError("Error loading hnsw index")
    monkeypatch.setattr(ChromaDbManager, "get_dimension", broken)
    with pytest.raises(AdoptionFailed) as exc:
        legacy_env.builder.adopt_legacy("fake/model-a")
    assert "vab index repair" in exc.value.repair
    assert legacy_env.gens.active() is None


def test_adoption_failure_surfaces_in_status_and_search(legacy_env, monkeypatch):
    def broken(self):
        raise RuntimeError("Error loading hnsw index")
    monkeypatch.setattr(ChromaDbManager, "get_dimension", broken)
    api = TestClient(legacy_env.server.app, base_url=BASE, headers=AUTH)
    r = api.post("/api/v1/search", json={"query": "robe"})
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "adoption_failed"
    s = api.get("/api/v1/status").json()
    assert s["status"] == "degraded"
    assert s["index"]["rebuild_reason"] == "adoption_failed"
    assert "vab index repair" in s["repair"]
    h = api.get("/api/v1/health").json()
    assert h["components"]["index"]["rebuild_reason"] == "adoption_failed"
    assert "vab index repair" in h["components"]["vector_store"]["repair"]


def test_adoption_rejects_dimension_mismatch(legacy_env):
    # Vectors are model-a (dim 8); the stamped model-b embeds at dim 12.
    with pytest.raises(AdoptionFailed) as exc:
        legacy_env.builder.adopt_legacy("fake/model-b")
    msg = str(exc.value) + exc.value.repair
    assert "fake/model-b" in msg and "12" in msg and "8" in msg
    assert "EMBEDDING_MODEL_ID" in exc.value.repair and "embedding_model" in exc.value.repair
    assert legacy_env.gens.active() is None


def test_adoption_with_unavailable_model_adopts_unverified(legacy_env, caplog):
    gen = legacy_env.builder.adopt_legacy("other/not-downloaded")
    assert gen is not None and gen["model_id"] == "other/not-downloaded" and gen["model_dim"] == 8
    assert "cannot verify" in caplog.text.lower()


# ── job paths (I1, M1, M4) ────────────────────────────────────────────────────

@pytest.fixture
def job_env(tmp_path, monkeypatch, fake_embedder):
    from managers import managers as m

    monkeypatch.setenv("EMBEDDING_MODEL_OVERRIDE", "fake/model-a")
    monkeypatch.setattr(index_worker, "_CANCEL_CHECK_SECONDS", 0)
    db = str(tmp_path / "catalog.db")
    chroma_path = str(tmp_path / "chroma")
    catalog = CatalogStore(db)
    gens = GenerationStore(db)
    builder = GenerationBuilder(catalog, gens, chroma_path, "legacy")
    ns = types.SimpleNamespace(catalog=catalog, gens=gens, builder=builder, chroma_path=chroma_path,
                               cms=[cms_row(s) for s in ("A1", "B2", "C3")],
                               store=JobStore(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(m, "catalog", catalog)
    monkeypatch.setattr(m, "generations", gens)
    monkeypatch.setattr(m, "builder", builder)
    monkeypatch.setattr(m, "cms", CmsGateway.fixed(
        types.SimpleNamespace(get_all_products=lambda: list(ns.cms))))
    monkeypatch.setattr(index_worker, "_scraper", lambda ctx: FakeScraper())
    monkeypatch.setattr(index_worker, "_figure_names", lambda: FIGS)
    ns.run = lambda kind, params=None: index_worker.run_inline(ns.store, kind, params or {})
    return ns


@pytest.fixture
def exportable(tmp_path, monkeypatch):
    """Real load_embedding_model over a tmp models root; export writes the files and counts."""
    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    monkeypatch.setattr(embedding_utils, "_MODELS_ROOT", tmp_path / "models")
    monkeypatch.setattr(embedding_utils, "_sessions", {})
    monkeypatch.setattr(embedding_utils, "model_available",
                        lambda m: (embedding_utils.model_dir(m) / "model.onnx").exists())
    exports = []

    def export(model_id=None):
        exports.append(model_id)
        d = embedding_utils.model_dir(model_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "model.onnx").write_bytes(b"onnx")
    monkeypatch.setattr(embedding_utils, "_export_model", export)
    monkeypatch.setattr(embedding_utils, "_load_from_cache", lambda m: ("session", "tokenizer"))
    monkeypatch.setattr("sys.frozen", False, raising=False)
    return exports


def test_first_build_exports_missing_model(job_env, exportable):
    done = job_env.run("index", {"model": "fake/model-x"})
    assert done["status"] == SUCCEEDED, done
    assert exportable == ["fake/model-x"]
    assert job_env.gens.active()["model_id"] == "fake/model-x"


def test_rebuild_exports_missing_model(job_env, exportable):
    done = job_env.run("rebuild", {"model": "fake/model-x"})
    assert done["status"] == SUCCEEDED, done
    assert exportable == ["fake/model-x"]


def test_frozen_job_does_not_export(job_env, exportable, monkeypatch):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    done = job_env.run("rebuild", {"model": "fake/model-x"})
    assert done["status"] == FAILED and "ModelUnavailable" in done["error"]
    assert exportable == []


def test_index_job_fails_on_unadoptable_legacy_collection(job_env, monkeypatch):
    _legacy(job_env.chroma_path, skus=("A1", "B2"))
    monkeypatch.setenv("EMBEDDING_MODEL_OVERRIDE", "fake/model-b")  # vectors are model-a's
    done = job_env.run("index")
    assert done["status"] == FAILED and "AdoptionFailed" in done["error"]
    assert job_env.gens.active() is None and job_env.gens.building() is None


def test_repair_builds_past_unreadable_legacy_collection(job_env, monkeypatch):
    _legacy(job_env.chroma_path, skus=("A1", "B2"))
    job_env.builder.legacy_collection = None  # fill the catalog without adopting
    assert job_env.run("index", {"phase": "etl"})["status"] == SUCCEEDED
    job_env.builder.legacy_collection = "legacy"
    real = ChromaDbManager.get_dimension

    def broken(self):
        if self.collection_name == "legacy":
            raise RuntimeError("Error loading hnsw index")
        return real(self)
    monkeypatch.setattr(ChromaDbManager, "get_dimension", broken)
    done = job_env.run("repair")
    assert done["status"] == SUCCEEDED, done
    assert job_env.gens.active()["collection_name"] != "legacy"


def test_first_build_warns_that_limit_does_not_apply(job_env, caplog):
    caplog.set_level("WARNING", logger="index_worker")
    done = job_env.run("index", {"limit": 1})
    assert done["status"] == SUCCEEDED, done
    assert "--limit" in caplog.text and "first" in caplog.text
    assert job_env.gens.vector_totals(job_env.gens.active()["id"])["current"] == 1  # limit on CMS


def test_load_limit_help_mentions_first_build(capsys, monkeypatch):
    import main
    monkeypatch.setattr("sys.argv", ["vab", "load", "--help"])
    with pytest.raises(SystemExit):
        main.main()
    out = " ".join(capsys.readouterr().out.split()).lower()
    assert "does not apply to the first build" in out


# ── I2: changed CMS products ──────────────────────────────────────────────────

def test_pending_cms_diff_counts_changed():
    cms = {"1": "2024-01-01T00:00:00", "2": datetime(2025, 5, 5), "9": None}
    present = {"1": "2024-01-01T00:00:00", "2": "2024-01-01T00:00:00", "3": "x"}
    assert pending_cms_diff(cms, present) == {"new": 1, "changed": 1, "removed": 1}


def test_pending_cms_diff_isoformat_equality():
    present = {"1": "2024-01-01T00:00:00"}
    assert pending_cms_diff({"1": datetime(2024, 1, 1)}, present) == {
        "new": 0, "changed": 0, "removed": 0}


class DatedAnalyzer(FakeAnalyzer):
    def __init__(self, dates):
        super().__init__(list(dates))
        self.dates = dict(dates)

    def get_sku_modified(self):
        self.sku_reads += 1
        return dict(self.dates)


@pytest.fixture
def dated_api(tmp_path, monkeypatch, fake_embedder, tmp_settings):
    db = str(tmp_path / "catalog.db")
    chroma_path = str(tmp_path / "chroma")
    catalog = CatalogStore(db)
    for sku, (name, figures, artist) in PRODUCTS.items():
        _add(catalog, sku, name, figures, artist)
    gens = GenerationStore(db)
    builder = GenerationBuilder(catalog, gens, chroma_path, None)
    gen = builder.start_or_resume_rebuild("fake/model-a", refetch=False)
    builder.embed_pending(gen)
    builder.finish_rebuild(gen)
    # The stored source_modified is "2024-01-01T00:00:00"; the CMS stub hands back datetimes.
    analyzer = DatedAnalyzer({s: datetime(2024, 1, 1) for s in PRODUCTS})
    server = _patch_server(monkeypatch, tmp_path, catalog, gens, chroma_path, analyzer)
    return types.SimpleNamespace(api=TestClient(server.app, base_url=BASE, headers=AUTH),
                                 analyzer=analyzer, server=server)


def test_status_unchanged_dates_mean_no_update(dated_api):
    s = dated_api.api.get("/api/v1/status").json()
    assert s["index"]["pending_cms"] == {"new": 0, "changed": 0, "removed": 0}
    assert s["update_available"] is False and s["new_products"] == 0


def test_status_reports_changed_product(dated_api):
    dated_api.analyzer.dates["2"] = datetime(2025, 3, 3, 12, 0)
    s = dated_api.api.get("/api/v1/status").json()
    assert s["index"]["pending_cms"] == {"new": 0, "changed": 1, "removed": 0}
    assert s["update_available"] is True and s["new_products"] == 0


def test_analyzer_sku_modified_converts_and_dedupes(monkeypatch):
    from managers.daz_db_analyzer import DazDBAnalyzer
    a = DazDBAnalyzer.__new__(DazDBAnalyzer)
    rows = [{"token": "1", "modified": datetime(2024, 1, 1)},
            {"token": "1", "modified": datetime(2030, 1, 1)},
            {"token": "2", "modified": None}]
    monkeypatch.setattr(a, "_execute_query", lambda sql, params=None: rows, raising=False)
    assert a.get_sku_modified() == {"1": "2024-01-01T00:00:00", "2": None}
    monkeypatch.setattr(a, "_execute_query", lambda sql, params=None: None, raising=False)
    assert a.get_sku_modified() is None


def test_store_present_modified(tmp_path):
    catalog = CatalogStore(str(tmp_path / "c.db"))
    _add(catalog, "1", "A", "Genesis 9", "Ann")
    _add(catalog, "2", "B", "Genesis 9", "Ann")
    catalog.mark_absent(CMS_SOURCE, {"1"})
    assert catalog.present_modified(CMS_SOURCE) == {"1": "2024-01-01T00:00:00"}


# ── M6: cumulative scrape progress ────────────────────────────────────────────

def test_scraper_reports_cumulative_progress(monkeypatch):
    import managers.postgres_db_manager as pdm

    def fake_scrape_many(skus, concurrency, on_progress=None):
        for i, s in enumerate(skus):
            on_progress("etl", i + 1, len(skus), s)
        return {s: {"url": s} for s in skus}
    monkeypatch.setattr(pdm, "scrape_many", fake_scrape_many)
    seen = []
    ctx = types.SimpleNamespace(on_progress=lambda stage, cur, tot, detail="": seen.append((cur, tot)))
    scrape = index_worker._scraper(ctx)
    scrape.plan(250)
    for start in range(0, 250, 100):
        scrape([str(i) for i in range(start, min(start + 100, 250))])
    currents = [c for c, _ in seen]
    assert currents == sorted(currents) and seen[-1] == (250, 250)
    assert {t for _, t in seen} == {250}


def test_reconcile_announces_scrape_total(tmp_path):
    from catalog.reconcile import reconcile_daz
    catalog = CatalogStore(str(tmp_path / "c.db"))
    totals = []
    reconcile_daz(catalog, [cms_row("A"), cms_row("B")], FakeScraper(), FIGS,
                  on_scrape_plan=totals.append)
    assert totals == [2]


# ── M7: installed filter in SQL ───────────────────────────────────────────────

def test_search_installed_filter_fills_max_results(dated_api):
    # One not-installed product of three: a post-filter over the top-1 hit usually drops it.
    from catalog.store import CMS_SOURCE as CMS
    dated_api.server.catalog.mark_absent(CMS, {"1", "2"})
    dated_api.server.catalog.refresh_views(["daz:3"], FIGS)
    r = dated_api.api.post("/api/v1/search", json={
        "query": "Silk Robe for G8", "max_results": 1, "filters": {"is_installed": False}})
    assert [x["sku"] for x in r.json()["results"]] == ["3"]
    r = dated_api.api.post("/api/v1/search", json={
        "query": "Wool Robe for G9", "max_results": 2, "filters": {"is_installed": True}})
    assert {x["sku"] for x in r.json()["results"]} == {"1", "2"}


def test_asset_ids_matching_installed(tmp_path):
    catalog = CatalogStore(str(tmp_path / "c.db"))
    for sku in ("1", "2"):
        _add(catalog, sku, f"P{sku}", "Genesis 9", "Ann")
    catalog.mark_absent(CMS_SOURCE, {"1"})
    catalog.refresh_views(["daz:2"], FIGS)
    assert catalog.asset_ids_matching(installed=False) == {"daz:2"}
    assert catalog.asset_ids_matching(installed=True, artists=["Ann"]) == {"daz:1"}
    assert catalog.asset_ids_matching() is None
