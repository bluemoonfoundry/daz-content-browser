import types

import chromadb
import pytest

from catalog.builder import GenerationBuilder
from catalog.generations import GenerationStore
from catalog.store import CatalogStore
from catalog_fakes import fake_embed
from managers.chroma_db_manager import ChromaDbManager

FIGS = ["Genesis 8", "Genesis 9"]
SKUS = ["1", "2", "3"]


def _cms(sku):
    return {"sku": sku, "product_name": f"Product {sku}", "artists": "Ann",
            "content_types": "Props", "content_item_count": 1,
            "last_modified_date": "2024-01-01T00:00:00"}


def _add_asset(catalog, sku, name=None):
    payload = _cms(sku)
    if name:
        payload["product_name"] = name
    catalog.upsert_source("daz_cms", sku, f"daz:{sku}", payload, "2024-01-01T00:00:00")
    catalog.refresh_views([f"daz:{sku}"], FIGS)


@pytest.fixture
def env(tmp_path, fake_embedder):
    db = str(tmp_path / "catalog.db")
    chroma_path = str(tmp_path / "chroma")
    catalog = CatalogStore(db)
    assert catalog.migration_error is None
    for sku in SKUS:
        _add_asset(catalog, sku)
    gens = GenerationStore(db)

    # A legacy collection as today's indexer writes it: bare SKU ids, model-a vectors.
    legacy = ChromaDbManager(chroma_path, "legacy")
    legacy.collection.upsert(ids=["1", "2"],
                             embeddings=fake_embed(["one", "two"], model_id="fake/model-a").tolist(),
                             documents=["one", "two"])

    builder = GenerationBuilder(catalog, gens, chroma_path, "legacy")

    def collection_names():
        return {c.name for c in chromadb.PersistentClient(path=chroma_path).list_collections()}

    ns = types.SimpleNamespace(catalog=catalog, gens=gens, builder=builder, chroma_path=chroma_path,
                               collection_names=collection_names, n_assets=len(SKUS))
    ns.active_gen = None

    def adopt():
        ns.active_gen = builder.adopt_legacy("fake/model-a")
        return ns.active_gen
    ns.adopt = adopt
    return ns


@pytest.fixture
def adopted(env):
    env.adopt()
    return env


def _ids(env, gen):
    return set(env.builder.collection(gen).get_all_ids())


def test_adopt_legacy_counts_existing_vectors_current(env):
    # legacy collection with ids "1","2" (dim 8) + catalog with daz:1, daz:2 (and daz:3, never embedded)
    gen = env.builder.adopt_legacy("fake/model-a")
    assert gen["id"] == 1 and gen["model_dim"] == 8 and gen["collection_name"] == "legacy"
    assert gen["status"] == "active" and gen["model_id"] == "fake/model-a"
    assert env.gens.vector_totals(1)["current"] == 2
    assert env.gens.pending(1) == ["daz:3"]
    assert env.gens.vector_ids_for(1, {"daz:1", "daz:2"}) == ["1", "2"]


def test_adopt_is_noop_when_generations_exist(env):
    env.gens.create_building("fake/model-b", 12, 1, 1, refetch=False)
    assert env.builder.adopt_legacy("fake/model-a") is None
    assert env.gens.active() is None


def test_adopt_twice_is_noop(adopted):
    assert adopted.builder.adopt_legacy("fake/model-a") is None


def test_adopt_missing_legacy_collection_does_not_create_it(env):
    builder = GenerationBuilder(env.catalog, env.gens, env.chroma_path, "nope")
    assert builder.adopt_legacy("fake/model-a") is None
    assert "nope" not in env.collection_names()
    assert env.gens.active() is None


def test_adopt_empty_legacy_collection_is_noop(env):
    ChromaDbManager(env.chroma_path, "empty").collection  # creates an empty collection
    builder = GenerationBuilder(env.catalog, env.gens, env.chroma_path, "empty")
    assert builder.adopt_legacy("fake/model-a") is None
    assert env.gens.active() is None


def test_adopt_without_legacy_name_is_noop(env):
    builder = GenerationBuilder(env.catalog, env.gens, env.chroma_path, None)
    assert builder.adopt_legacy("fake/model-a") is None


def test_embed_pending_replaces_adopted_sku_ids(adopted):
    env = adopted
    gen = env.active_gen
    _add_asset(env.catalog, "1", name="Renamed")  # daz:1 now stale
    assert env.gens.pending(gen["id"]) == ["daz:1", "daz:3"]
    result = env.builder.embed_pending(gen)
    assert result == {"embedded": 2, "errors": 0, "orphans_removed": 0}
    assert _ids(env, gen) == {"daz:1", "2", "daz:3"}
    assert env.gens.pending(gen["id"]) == []
    assert env.gens.vector_ids_for(gen["id"], {"daz:1", "daz:2", "daz:3"}) == ["2", "daz:1", "daz:3"]
    got = env.builder.collection(gen).collection.get(ids=["daz:1"], include=["metadatas", "documents"])
    meta = got["metadatas"][0]
    assert meta["asset_id"] == "daz:1" and meta["generation_id"] == gen["id"]
    assert meta["doc_hash"] == env.catalog.get_rows(["daz:1"])[0]["doc_hash"]
    assert meta["name"] == "Renamed" and meta["sku"] == "1"
    for filter_field in ("artist", "tags", "category", "compatible_figures"):
        assert filter_field not in meta
    assert got["documents"][0] == env.catalog.get_rows(["daz:1"])[0]["embedding_text"]


def test_embed_pending_removes_orphans(adopted):
    env = adopted
    gen = env.active_gen
    # Removing every source record of daz:2 deletes its view row.
    from catalog.schema import connect
    conn = connect(env.catalog.db_path)
    conn.execute("DELETE FROM source_records WHERE asset_id = 'daz:2'")
    conn.commit()
    conn.close()
    env.catalog.refresh_views(["daz:2"], FIGS)
    result = env.builder.embed_pending(gen)
    assert result["orphans_removed"] == 1
    assert "2" not in _ids(env, gen)
    assert env.gens.vector_totals(gen["id"])["orphan"] == 0


def test_embed_pending_records_upsert_failures(adopted, monkeypatch):
    env = adopted
    gen = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    mgr = env.builder.collection(gen)

    class Broken:
        def upsert(self, **kwargs):
            raise RuntimeError("boom")

    mgr._collection = Broken()
    reconnects = []
    monkeypatch.setattr(mgr, "reconnect", lambda: reconnects.append(1))
    result = env.builder.embed_pending(gen)
    assert result["embedded"] == 0 and result["errors"] == env.n_assets
    assert reconnects == [1]
    assert env.gens.vector_totals(gen["id"])["error"] == env.n_assets
    assert env.gens.active()["id"] == env.active_gen["id"]


def test_rebuild_builds_beside_active_and_promotes(adopted):
    env = adopted
    old = env.active_gen
    gen = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    assert gen["status"] == "building" and gen["model_dim"] == 12
    assert gen["collection_name"] == f"daz_products_g{gen['id']}"
    env.builder.embed_pending(gen)
    assert env.gens.active()["id"] == old["id"]               # still serving old until promote
    assert "legacy" in env.collection_names()
    env.builder.finish_rebuild(gen)
    assert env.gens.active()["model_id"] == "fake/model-b" and env.gens.active()["model_dim"] == 12
    assert old["collection_name"] not in env.collection_names()
    assert env.gens.get(old["id"]) is None and env.gens.retired() == []
    assert _ids(env, env.gens.active()) == {"daz:1", "daz:2", "daz:3"}


def test_first_build_without_active(env):
    gen = env.builder.start_or_resume_rebuild("fake/model-a", refetch=True)
    assert gen["refetch_started_at"] is not None
    env.builder.embed_pending(gen)
    env.builder.finish_rebuild(gen)
    assert env.gens.active()["id"] == gen["id"]


def test_interrupted_rebuild_resumes(adopted):
    env = adopted
    gen = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    env.builder.embed_pending(gen, limit=1)
    again = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    assert again["id"] == gen["id"] and len(env.gens.pending(gen["id"])) == env.n_assets - 1


def test_mismatched_building_is_discarded(adopted):
    env = adopted
    stale = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    env.builder.embed_pending(stale, limit=1)
    assert stale["collection_name"] in env.collection_names()
    gen = env.builder.start_or_resume_rebuild("fake/model-a", refetch=False)
    assert gen["id"] != stale["id"] and gen["model_id"] == "fake/model-a" and gen["model_dim"] == 8
    assert env.gens.get(stale["id"]) is None
    assert stale["collection_name"] not in env.collection_names()
    assert env.gens.active()["id"] == env.active_gen["id"]


def test_cancelled_embed_returns_early_and_leaves_active(adopted):
    env = adopted
    gen = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    result = env.builder.embed_pending(gen, cancel_check=lambda: True)
    assert result["embedded"] == 0
    assert len(env.gens.pending(gen["id"])) == env.n_assets
    assert env.gens.active()["id"] == env.active_gen["id"]
    assert env.gens.building()["id"] == gen["id"]


def test_embed_reports_progress_in_batches(adopted):
    env = adopted
    gen = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    calls = []
    result = env.builder.embed_pending(gen, batch_size=2,
                                       on_progress=lambda *a: calls.append(a))
    assert result["embedded"] == env.n_assets
    assert [c[1:3] for c in calls] == [(2, 3), (3, 3)]


def test_finish_rebuild_refuses_with_pending_work(adopted):
    env = adopted
    gen = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    env.builder.embed_pending(gen, limit=1)
    with pytest.raises(RuntimeError):
        env.builder.finish_rebuild(gen)
    assert env.gens.active()["id"] == env.active_gen["id"]
    assert env.gens.building()["id"] == gen["id"]


def test_finish_rebuild_allows_exhausted_errors(adopted):
    env = adopted
    gen = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    env.builder.embed_pending(gen)
    for _ in range(3):
        env.gens.record_error(gen["id"], ["daz:2"], "bad")
    env.builder.finish_rebuild(gen)
    assert env.gens.active()["id"] == gen["id"]


def test_discard_drops_collection_and_row(adopted):
    env = adopted
    gen = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    env.builder.embed_pending(gen)
    env.builder.discard(gen, "test")
    assert env.gens.get(gen["id"]) is None
    assert gen["collection_name"] not in env.collection_names()
    assert "legacy" in env.collection_names()


def test_cleanup_retired_keeps_row_on_failure(adopted, monkeypatch):
    env = adopted
    gen = env.builder.start_or_resume_rebuild("fake/model-b", refetch=False)
    env.builder.embed_pending(gen)

    def boom(self):
        raise RuntimeError("locked")
    monkeypatch.setattr(ChromaDbManager, "delete_collection", boom)
    env.builder.finish_rebuild(gen)
    assert [g["id"] for g in env.gens.retired()] == [env.active_gen["id"]]
    monkeypatch.undo()
    env.builder.cleanup_retired()
    assert env.gens.retired() == []
    assert "legacy" not in env.collection_names()


def test_chroma_manager_helpers(tmp_path, fake_embedder):
    mgr = ChromaDbManager(str(tmp_path / "c"), "xyz")
    assert mgr.get_dimension() is None
    mgr.collection.upsert(ids=["a", "b"], embeddings=fake_embed(["a", "b"], model_id="fake/model-b").tolist())
    assert mgr.get_dimension() == 12
    mgr.delete_ids(["a", "missing"])
    mgr.delete_ids([])
    assert mgr.get_all_ids() == {"b"}
    mgr.delete_collection()
    mgr.delete_collection()  # already gone: no error
    names = {c.name for c in chromadb.PersistentClient(path=str(tmp_path / "c")).list_collections()}
    assert "xyz" not in names
