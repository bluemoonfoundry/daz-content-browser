import json
import sqlite3
from pathlib import Path

import pytest

from catalog import schema
from catalog.documents import doc_hash
from catalog.schema import LATEST_VERSION, connect
from catalog.store import CatalogStore

FIXTURE_SQL = (Path(__file__).parent / "fixtures" / "legacy_products.sql").read_text(encoding="utf-8")


def _load_legacy() -> dict:
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.executescript(FIXTURE_SQL)
    return {r["sku"]: dict(r) for r in mem.execute("SELECT * FROM products")}


LEGACY = _load_legacy()


@pytest.fixture
def legacy_db(tmp_path):
    db = str(tmp_path / "enriched_products.db")
    conn = sqlite3.connect(db)
    conn.executescript(FIXTURE_SQL)
    conn.close()
    return db


def table_names(db):
    return {r[0] for r in connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")}


def data_tables_with_rows(db):
    conn = connect(db)
    return {t for t in table_names(db)
            if conn.execute(f'SELECT EXISTS (SELECT 1 FROM "{t}")').fetchone()[0]}


def user_version(db):
    return connect(db).execute("PRAGMA user_version").fetchone()[0]


def test_legacy_rows_imported_verbatim(legacy_db):
    s = CatalogStore(legacy_db, legacy_table="products")
    assert s.migration_error is None
    row = s.get_by_sku("A1")
    assert row["embedding_text"] == LEGACY["A1"]["embedding_text"] and row["installed"] == 1
    assert {r[0] for r in connect(legacy_db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")} >= {"products_legacy"}
    assert "products" not in table_names(legacy_db)
    assert user_version(legacy_db) == LATEST_VERSION
    for sku, legacy in LEGACY.items():
        view = s.get_by_sku(sku)
        for col, value in legacy.items():
            assert view[col] == value, (sku, col)
        assert view["asset_id"] == f"daz:{sku}"
        assert view["installed"] == 1 and view["doc_format_version"] == 1
        assert view["doc_hash"] == doc_hash(view)
    assert s.count() == 3


def test_legacy_source_records(legacy_db):
    s = CatalogStore(legacy_db, legacy_table="products")
    conn = connect(legacy_db)
    rec = dict(conn.execute(
        "SELECT * FROM source_records WHERE source='daz_cms' AND source_key='A1'").fetchone())
    a1 = LEGACY["A1"]
    assert json.loads(rec["payload"]) == {
        "sku": "A1", "product_name": a1["name"], "artists": a1["artist"],
        "content_types": a1["formats"], "content_item_count": a1["asset_count"],
        "last_modified_date": a1["last_updated"],
        "legacy": {"tags": a1["tags"], "compatible_figures": a1["compatible_figures"],
                   "category": a1["category"], "subcategories": a1["subcategories"]},
    }
    assert rec["source_modified"] == "2024-03-01T10:20:30"
    assert rec["payload_hash"] is None and rec["present"] == 1 and rec["asset_id"] == "daz:A1"
    web = dict(conn.execute(
        "SELECT * FROM source_records WHERE source='daz_store_web' AND source_key='A1'").fetchone())
    assert web["fetched_at"] == a1["enriched_at"] and web["present"] == 1
    assert json.loads(web["payload"]) == {
        k: a1[k] for k in ("url", "image_url", "store", "price", "description", "poly_count",
                           "textures_info", "required_products", "compatible_software", "mature")}
    # Never-enriched rows still get both records.
    assert set(s.source_index("daz_store_web")) == set(s.source_index("daz_cms")) == {"A1", "A2", "A3"}
    assert s.source_index("daz_store_web")["A3"]["fetched_at"] is None
    assert conn.execute("SELECT sku FROM assets WHERE asset_id='daz:A2'").fetchone()[0] == "A2"


def test_legacy_lookups_are_exact(legacy_db):
    s = CatalogStore(legacy_db, legacy_table="products")
    assert s.asset_ids_matching(artists=["Ann"]) == {"daz:A1"}
    assert s.asset_ids_matching(artists=["Bob Studio"]) == {"daz:A1"}
    assert s.asset_ids_matching(artists=["Anna"]) == {"daz:A2"}
    assert s.asset_ids_matching(compatible_figures=["Genesis 9"]) == {"daz:A2"}
    assert s.asset_ids_matching(tags=["Clothes"]) == {"daz:A1"}
    assert s.get_filter_values() == {
        "categories": ["props", "wardrobe"],
        "artists": ["Ann", "Anna", "Bob Studio"],
        "compatible_figures": ["Genesis 8 Female", "Genesis 8.1 Female", "Genesis 9"],
    }


def test_legacy_migration_is_rerunnable(legacy_db, monkeypatch):
    # A failed attempt leaves everything as it was, so the next start retries cleanly.
    monkeypatch.setattr(schema, "_verify_legacy_import", lambda conn, table, n: "forced mismatch")
    assert CatalogStore(legacy_db, legacy_table="products").migration_error is not None
    monkeypatch.undo()
    s = CatalogStore(legacy_db, legacy_table="products")
    assert s.migration_error is None and s.count() == 3
    # Once migrated, another start is a no-op.
    s2 = CatalogStore(legacy_db, legacy_table="products")
    assert s2.migration_error is None and s2.count() == 3
    conn = connect(legacy_db)
    assert conn.execute("SELECT COUNT(*) FROM products_legacy").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 6


def test_legacy_migration_failure_rolls_back_and_is_reported(legacy_db, monkeypatch):
    # monkeypatch the verifier to report a mismatch
    monkeypatch.setattr(schema, "_verify_legacy_import", lambda conn, table, n: "name mismatch for A1")
    s = CatalogStore(legacy_db, legacy_table="products")
    assert s.migration_error is not None
    assert "name mismatch for A1" in s.migration_error
    assert "products" in table_names(legacy_db) and "assets" not in data_tables_with_rows(legacy_db)
    assert "products_legacy" not in table_names(legacy_db)
    assert data_tables_with_rows(legacy_db) == {"products"}
    assert user_version(legacy_db) == 1
    conn = connect(legacy_db)
    assert {r["sku"]: dict(r) for r in conn.execute("SELECT * FROM products")} == LEGACY


def test_real_verifier_detects_mismatch(legacy_db, monkeypatch):
    # Corrupt the copied view rows before verification: the real verifier must catch it.
    real_copy = schema._copy_legacy_rows

    def bad_copy(conn, rows, now):
        real_copy(conn, rows, now)
        conn.execute("UPDATE asset_view SET embedding_text = embedding_text || ' ' WHERE sku = 'A2'")

    monkeypatch.setattr(schema, "_copy_legacy_rows", bad_copy)
    s = CatalogStore(legacy_db, legacy_table="products")
    assert s.migration_error is not None and "A2" in s.migration_error
    assert "products" in table_names(legacy_db) and "assets" not in data_tables_with_rows(legacy_db)


def test_unexpected_migration_exception_is_reported_not_raised(legacy_db, monkeypatch):
    def boom(row):
        raise ValueError("kaboom")

    monkeypatch.setattr(schema, "doc_hash", boom)
    s = CatalogStore(legacy_db, legacy_table="products")
    assert "kaboom" in s.migration_error
    assert "products" in table_names(legacy_db) and "assets" not in data_tables_with_rows(legacy_db)


def test_no_legacy_table_only_bumps_version(tmp_path):
    db = str(tmp_path / "fresh.db")
    s = CatalogStore(db, legacy_table="products")
    assert s.migration_error is None and s.count() == 0
    assert user_version(db) == LATEST_VERSION
    assert "products_legacy" not in table_names(db)
    s2 = CatalogStore(str(tmp_path / "none.db"))
    assert s2.migration_error is None and user_version(s2.db_path) == LATEST_VERSION


def test_null_sku_rows_are_skipped(legacy_db):
    conn = sqlite3.connect(legacy_db)
    conn.execute("INSERT INTO products (sku, name) VALUES (NULL, 'orphan')")
    conn.execute("INSERT INTO products (sku, name) VALUES ('', 'blank')")
    conn.commit()
    conn.close()
    s = CatalogStore(legacy_db, legacy_table="products")
    assert s.migration_error is None and s.count() == 3
    # Nothing is lost: the renamed table keeps every row.
    assert connect(legacy_db).execute("SELECT COUNT(*) FROM products_legacy").fetchone()[0] == 5


def test_old_legacy_table_without_asset_count(tmp_path):
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE products (sku TEXT PRIMARY KEY, name TEXT, artist TEXT, "
                 "embedding_text TEXT, last_updated TEXT, enriched_at TEXT)")
    conn.execute("INSERT INTO products VALUES ('X', 'Old', 'Ann', 'text', '2020-01-01T00:00:00', NULL)")
    conn.commit()
    conn.close()
    s = CatalogStore(db, legacy_table="products")
    assert s.migration_error is None
    row = s.get_by_sku("X")
    assert (row["name"], row["asset_count"], row["embedding_text"]) == ("Old", None, "text")


def test_bulk_import(tmp_path):
    db = str(tmp_path / "bulk.db")
    conn = sqlite3.connect(db)
    conn.executescript(FIXTURE_SQL.split("INSERT")[0])
    conn.executemany(
        "INSERT INTO products (sku, name, artist, tags, compatible_figures, embedding_text, category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(str(i), f"P{i}", f"Artist {i % 50}", f"T{i % 7}, Shared", f"Genesis {i % 3}",
          f"text {i}", f"c{i % 5}") for i in range(3000)])
    conn.commit()
    conn.close()
    s = CatalogStore(db, legacy_table="products")
    assert s.migration_error is None and s.count() == 3000
    assert len(s.get_filter_values()["artists"]) == 50
    assert len(s.asset_ids_matching(tags=["Shared"])) == 3000
    assert len(s.asset_ids_matching(artists=["Artist 7"], compatible_figures=["Genesis 1"])) == 20
