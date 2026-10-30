import sqlite3

import pytest

from catalog.generations import GenerationStore
from catalog.schema import connect
from catalog.store import CatalogStore


def _q(db, sql, *params):
    conn = connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _asset(db, asset_id, h):
    conn = connect(db)
    conn.execute("INSERT OR IGNORE INTO assets (asset_id, sku, created_at) VALUES (?, NULL, 'x')",
                 (asset_id,))
    conn.execute("INSERT OR REPLACE INTO asset_view (asset_id, installed, doc_hash, doc_format_version)"
                 " VALUES (?, 0, ?, 1)", (asset_id, h))
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "catalog.db")
    CatalogStore(path)
    return path


@pytest.fixture
def gens(db):
    return GenerationStore(db)


@pytest.fixture
def catalog(db):
    for a, h in (("daz:a", "ha"), ("daz:b", "hb"), ("daz:c", "hc")):
        _asset(db, a, h)
    return db


def _build(gens, refetch=False):
    return gens.create_building("m", 1024, 1, 1, refetch=refetch)


def test_create_building_names_collection(gens):
    g = _build(gens, refetch=True)
    assert g["status"] == "building"
    assert g["collection_name"] == f"daz_products_g{g['id']}"
    assert g["refetch_started_at"] is not None
    assert gens.building()["id"] == g["id"]
    assert gens.get(g["id"]) == g


def test_no_refetch_leaves_timestamp_null(gens):
    assert _build(gens)["refetch_started_at"] is None


def test_second_building_rejected(gens):
    _build(gens)
    with pytest.raises(sqlite3.IntegrityError):
        _build(gens)


def test_promote_first_build_without_active(gens):
    g = _build(gens)
    gens.promote(g["id"], now="t1")
    a = gens.active()
    assert a["id"] == g["id"] and a["promoted_at"] == "t1" and a["completed_at"] == "t1"
    assert gens.building() is None


def test_promote_swaps_active_atomically(gens):
    old = gens.create_active_adopted("daz_products2", "m", 1024, 1, 1)
    assert gens.active()["collection_name"] == "daz_products2"
    new = _build(gens)
    gens.promote(new["id"])
    assert gens.active()["id"] == new["id"]
    assert [r["id"] for r in gens.retired()] == [old["id"]]


def test_promote_requires_building(gens):
    old = gens.create_active_adopted("c", "m", 1024, 1, 1)
    with pytest.raises(ValueError):
        gens.promote(old["id"])
    with pytest.raises(ValueError):
        gens.promote(999)
    assert gens.active()["id"] == old["id"]


def test_mark_failed(gens):
    g = _build(gens)
    gens.mark_failed(g["id"], "boom")
    r = gens.get(g["id"])
    assert r["status"] == "failed" and r["error"] == "boom"
    assert gens.building() is None


def test_pending_covers_missing_stale_and_retryable_errors(gens, catalog):
    g = _build(gens)["id"]
    gens.record_current(g, [("daz:a", "v-a", "ha"), ("daz:c", "v-c", "OLD")])
    assert gens.pending(g) == ["daz:b", "daz:c"]
    assert gens.pending(g, limit=1) == ["daz:b"]
    for _ in range(3):
        gens.record_error(g, ["daz:b"], "boom")
    assert gens.pending(g) == ["daz:c"]
    assert gens.pending(g, max_attempts=4) == ["daz:b", "daz:c"]
    gens.record_current(g, [("daz:b", "v-b", "hb"), ("daz:c", "v-c", "hc")])
    assert gens.pending(g) == []


def test_record_error_on_existing_vector_increments(gens, catalog):
    g = _build(gens)["id"]
    gens.record_current(g, [("daz:a", "v-a", "ha")])
    gens.record_error(g, ["daz:a"], "x")
    gens.record_error(g, ["daz:a"], "y")
    row = dict(_q(catalog, "SELECT * FROM asset_vectors")[0])
    assert row["status"] == "error" and row["attempts"] == 2 and row["error"] == "y"
    assert row["vector_id"] == "v-a"
    gens.record_current(g, [("daz:a", "v-a", "ha")])
    row = dict(_q(catalog, "SELECT * FROM asset_vectors")[0])
    assert row["status"] == "current" and row["attempts"] == 0 and row["error"] is None


def test_vector_totals_and_orphans(gens, catalog):
    g = _build(gens)["id"]
    gens.record_current(g, [("daz:a", "v-a", "ha"), ("daz:c", "v-c", "OLD"),
                            ("daz:gone", "v-gone", "hg")])
    assert gens.vector_totals(g) == {"current": 1, "stale": 1, "missing": 1, "error": 0, "orphan": 1}
    assert gens.orphans(g) == ["v-gone"]
    gens.record_error(g, ["daz:b"], "e")
    assert gens.vector_totals(g) == {"current": 1, "stale": 1, "missing": 0, "error": 1, "orphan": 1}
    gens.drop_vectors(g, ["daz:gone"])
    assert gens.orphans(g) == []
    assert gens.vector_totals(g)["orphan"] == 0


def test_vector_lookups(gens, catalog):
    g = _build(gens)["id"]
    gens.record_current(g, [("daz:a", "v-a", "ha"), ("daz:b", "v-b", "hb")])
    assert gens.vector_ids_for(g, {"daz:a", "daz:b", "daz:zz"}) == ["v-a", "v-b"]
    assert gens.assets_for_vectors(g, ["v-b", "nope"]) == {"v-b": "daz:b"}


def test_delete_cascades_vectors(gens, catalog):
    g = _build(gens)["id"]
    gens.record_current(g, [("daz:a", "v-a", "ha")])
    gens.delete(g)
    assert gens.get(g) is None
    assert _q(catalog, "SELECT COUNT(*) FROM asset_vectors")[0][0] == 0


def test_exhausted_error_becomes_pending_when_content_changes(gens, catalog):
    g = _build(gens)["id"]
    gens.record_current(g, [("daz:a", "v-a", "ha"), ("daz:c", "v-c", "hc")])
    for _ in range(3):
        gens.record_error(g, ["daz:b"], "boom")
    assert gens.pending(g) == []
    _asset(catalog, "daz:b", "hb2")
    assert gens.pending(g) == ["daz:b"]
    assert gens.vector_totals(g)["error"] == 1


def test_failure_after_content_change_restarts_retry_budget(gens, catalog):
    g = _build(gens)["id"]
    for _ in range(3):
        gens.record_error(g, ["daz:b"], "boom")
    _asset(catalog, "daz:b", "hb2")
    gens.record_error(g, ["daz:b"], "boom2")
    row = _q(catalog, "SELECT * FROM asset_vectors WHERE asset_id = 'daz:b'")[0]
    assert row["attempts"] == 1 and row["doc_hash"] == "hb2"
    assert "daz:b" in gens.pending(g)
    gens.record_error(g, ["daz:b"], "boom3")
    gens.record_error(g, ["daz:b"], "boom4")
    assert "daz:b" not in gens.pending(g)


def test_error_keeps_real_vector_id(gens, catalog):
    g = _build(gens)["id"]
    gens.record_current(g, [("daz:a", "v-a", "ha")])
    _asset(catalog, "daz:a", "ha2")
    gens.record_error(g, ["daz:a"], "x")
    row = _q(catalog, "SELECT * FROM asset_vectors")[0]
    assert row["vector_id"] == "v-a" and row["attempts"] == 1 and row["doc_hash"] == "ha2"
    assert gens.vector_ids_for(g, {"daz:a"}) == ["v-a"]


def test_placeholder_ids_never_returned(gens, catalog):
    g = _build(gens)["id"]
    gens.record_error(g, ["daz:b", "daz:gone"], "x")
    assert gens.vector_ids_for(g, {"daz:b", "daz:gone"}) == []
    assert gens.orphans(g) == []


def test_adopt_inserts_generation_and_vectors(gens, catalog):
    g = gens.adopt("legacy", "m", 8, 1, 1, [("daz:a", "a", "ha"), ("daz:x", "x", None)])
    assert g["status"] == "active" and g["collection_name"] == "legacy"
    assert gens.vector_totals(g["id"])["current"] == 1
    assert gens.orphans(g["id"]) == ["x"]


def test_adopt_is_atomic(gens, catalog):
    # Duplicate vector ids violate asset_vectors_vector: nothing must be left behind.
    with pytest.raises(sqlite3.IntegrityError):
        gens.adopt("legacy", "m", 8, 1, 1, [("daz:a", "a", "ha"), ("daz:b", "a", "hb")])
    assert _q(catalog, "SELECT COUNT(*) FROM index_generations")[0][0] == 0
    assert _q(catalog, "SELECT COUNT(*) FROM asset_vectors")[0][0] == 0
    g = gens.adopt("legacy", "m", 8, 1, 1, [("daz:a", "a", "ha"), ("daz:b", "b", "hb")])
    assert gens.active()["id"] == g["id"] and gens.vector_totals(g["id"])["current"] == 2


def test_reset_error_attempts(gens, catalog):
    g = _build(gens)["id"]
    gens.record_current(g, [("daz:a", "v-a", "ha"), ("daz:c", "v-c", "hc")])
    for _ in range(3):
        gens.record_error(g, ["daz:b"], "boom")
    assert gens.pending(g) == []
    assert gens.reset_error_attempts(g) == 1
    assert gens.pending(g) == ["daz:b"]


def test_migration_3_rehashes_and_keeps_current_vectors_current(tmp_path):
    from catalog.documents import doc_hash
    from catalog.schema import migrate
    path = str(tmp_path / "m3.db")
    CatalogStore(path)
    g = GenerationStore(path)
    conn = connect(path)
    for aid, text in (("daz:1", "text one"), ("daz:2", "text two")):
        conn.execute("INSERT INTO assets (asset_id, sku, created_at) VALUES (?, NULL, 'x')", (aid,))
        conn.execute("INSERT INTO asset_view (asset_id, installed, doc_hash, doc_format_version,"
                     " embedding_text) VALUES (?, 1, ?, 1, ?)", (aid, "old-" + aid, text))
    conn.execute("PRAGMA user_version = 2")  # as if at v2 with old-formula hashes
    conn.commit()
    conn.close()
    gen = g.adopt("c", "m", 4, 1, 2, [("daz:1", "daz:1", "old-daz:1"), ("daz:2", "daz:2", "old-daz:2")])
    # daz:2's vector was stale even under the old formula
    conn = connect(path)
    conn.execute("UPDATE asset_vectors SET doc_hash = 'older' WHERE asset_id = 'daz:2'")
    conn.commit()
    conn.close()
    before = g.vector_totals(gen["id"])
    assert migrate(path) == 3
    after = g.vector_totals(gen["id"])
    assert after["current"] == before["current"]
    assert g.pending(gen["id"]) == ["daz:2"]
    views = {r["asset_id"]: r["doc_hash"] for r in _q(path, "SELECT asset_id, doc_hash FROM asset_view")}
    assert views == {a: doc_hash({"embedding_text": t})
                     for a, t in (("daz:1", "text one"), ("daz:2", "text two"))}
