import json

import pytest

from catalog.documents import build_view_row, doc_hash, payload_hash
from catalog.schema import connect
from catalog.store import CatalogStore

FIGS = ["Genesis 8", "Genesis 8.1 Female", "Genesis 9"]


def _cms(sku, name, artist, *, figures=None, tags=None, category=None, date="2024-01-01T00:00:00"):
    payload = {"sku": sku, "product_name": name, "artists": artist, "content_types": "Props",
               "content_item_count": 1, "last_modified_date": date}
    if figures is not None:
        # The legacy override pins tags/figures/category exactly, independent of the
        # substring-based figure heuristic, so filter exactness can be tested precisely.
        payload["legacy"] = {"tags": tags, "compatible_figures": figures,
                             "category": category, "subcategories": ""}
    return payload


def _web(sku, description="desc"):
    return {"url": f"https://www.daz3d.com/{sku}", "image_url": None, "store": "DAZ 3D",
            "price": "$1", "description": description, "poly_count": None,
            "textures_info": None, "required_products": None,
            "compatible_software": None, "mature": 0}


@pytest.fixture
def store(tmp_path):
    s = CatalogStore(str(tmp_path / "catalog.db"))
    assert s.migration_error is None
    s.upsert_source("daz_cms", "1", "daz:1",
                    _cms("1", "Alpha", "Ann", figures="Genesis 8", tags="Props, Decor",
                         category="props"),
                    "2024-01-01T00:00:00", now="2024-01-02T00:00:00")
    s.upsert_source("daz_cms", "2", "daz:2",
                    _cms("2", "Beta", "Anna", figures="Genesis 8.1 Female", tags="Props Extra",
                         category="wardrobe", date="2024-02-01T00:00:00"),
                    "2024-02-01T00:00:00", now="2024-02-02T00:00:00")
    s.upsert_source("daz_store_web", "1", "daz:1", _web("1"), None, now="2024-01-03T00:00:00")
    s.upsert_source("daz_store_web", "2", "daz:2", _web("2"), None, now="2024-02-03T00:00:00")
    s.refresh_views(["daz:1", "daz:2"], FIGS)
    return s


def _record(store, source, key):
    return dict(connect(store.db_path).execute(
        "SELECT * FROM source_records WHERE source = ? AND source_key = ?", (source, key)).fetchone())


def test_exact_figure_filter(store):  # assets with "Genesis 8" and "Genesis 8.1 Female"
    assert store.asset_ids_matching(compatible_figures=["Genesis 8"]) == {"daz:1"}
    page = store.get_products(1, 50, compatible_figure="Genesis 8")
    assert [p["sku"] for p in page["products"]] == ["1"]
    assert store.asset_ids_matching(compatible_figures=["Genesis"]) == set()


def test_exact_artist_filter(store):  # "Ann" vs "Anna"
    assert store.asset_ids_matching(artists=["Ann"]) == {"daz:1"}
    assert [p["sku"] for p in store.get_products(1, 50, artist="Ann")["products"]] == ["1"]
    assert [p["sku"] for p in store.get_products(1, 50, artist="Anna")["products"]] == ["2"]
    assert store.get_products(1, 50, artist="An")["total"] == 0


def test_or_within_and_across(store):
    assert store.asset_ids_matching() is None
    assert store.asset_ids_matching(artists=[], tags=None) is None
    assert store.asset_ids_matching(artists=["Ann", "Anna"]) == {"daz:1", "daz:2"}
    assert store.asset_ids_matching(artists=["Ann", "Anna"],
                                    compatible_figures=["Genesis 8.1 Female"]) == {"daz:2"}
    assert store.asset_ids_matching(category="props", tags=["Decor", "Props Extra"]) == {"daz:1"}
    assert store.asset_ids_matching(artists=["Ann"], tags=["Props Extra"]) == set()
    assert store.asset_ids_matching(tags=["Props"]) == {"daz:1"}  # not a prefix of "Props Extra"


def test_filter_values_from_lookups(store):
    assert store.get_filter_values() == {
        "categories": ["props", "wardrobe"],
        "artists": ["Ann", "Anna"],
        "compatible_figures": ["Genesis 8", "Genesis 8.1 Female"],
    }
    # A rebuilt view drops lookup values no asset uses any more.
    store.upsert_source("daz_cms", "2", "daz:2",
                        _cms("2", "Beta", "Zed", figures="Genesis 9", tags="X", category="hair"),
                        "2024-02-01T00:00:00")
    store.refresh_views(["daz:2"], FIGS)
    assert store.get_filter_values() == {
        "categories": ["hair", "props"],
        "artists": ["Ann", "Zed"],
        "compatible_figures": ["Genesis 8", "Genesis 9"],
    }
    assert store.asset_ids_matching(artists=["Anna"]) == set()


def test_absent_marks_not_installed_and_keeps_row(store):
    assert store.mark_absent("daz_cms", present_keys={"2"}, now="2024-05-01T00:00:00") == ["daz:1"]
    store.refresh_views(["daz:1"], FIGS)
    assert store.get_by_sku("1")["installed"] == 0
    assert store.get_products(1, 50, installed=False)["total"] == 1
    assert store.get_products(1, 50, installed=True)["total"] == 1
    assert store.get_products(1, 50)["total"] == 2
    # Removal is per source: the web record is untouched.
    assert store.source_index("daz_store_web")["1"]["present"] == 1
    rec = _record(store, "daz_cms", "1")
    assert (rec["present"], rec["removed_at"]) == (0, "2024-05-01T00:00:00")
    # Already absent: not re-marked, removed_at kept.
    assert store.mark_absent("daz_cms", present_keys={"2"}, now="2024-06-01T00:00:00") == []
    assert _record(store, "daz_cms", "1")["removed_at"] == "2024-05-01T00:00:00"
    assert store.asset_totals() == {"total": 2, "installed": 1, "not_installed": 1, "fetch_errors": 0}


def test_reappearing_source_clears_removed(store):
    store.mark_absent("daz_cms", present_keys={"2"}, now="2024-05-01T00:00:00")
    store.refresh_views(["daz:1"], FIGS)
    first_seen = _record(store, "daz_cms", "1")["first_seen"]
    store.upsert_source("daz_cms", "1", "daz:1",
                        _cms("1", "Alpha", "Ann", figures="Genesis 8", tags="Props, Decor",
                             category="props"),
                        "2024-01-01T00:00:00", now="2024-07-01T00:00:00")
    rec = _record(store, "daz_cms", "1")
    assert (rec["present"], rec["removed_at"], rec["last_seen"]) == (1, None, "2024-07-01T00:00:00")
    assert rec["first_seen"] == first_seen == "2024-01-02T00:00:00"
    store.refresh_views(["daz:1"], FIGS)
    assert store.get_by_sku("1")["installed"] == 1
    assert connect(store.db_path).execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 2


def test_failed_fetch_keeps_previous_payload(store):
    good = _web("1", description="original description")
    store.upsert_source("daz_store_web", "1", "daz:1", good, None, now="2024-03-01T00:00:00")
    store.upsert_source("daz_store_web", "1", "daz:1", {}, None, fetch_error="HTTP 503",
                        now="2024-04-01T00:00:00")
    idx = store.source_index("daz_store_web")["1"]
    assert idx["fetched_at"] == "2024-03-01T00:00:00"
    assert idx["payload_hash"] == payload_hash(good)
    rec = _record(store, "daz_store_web", "1")
    assert json.loads(rec["payload"]) == good
    assert (rec["fetch_error"], rec["last_seen"]) == ("HTTP 503", "2024-04-01T00:00:00")
    store.refresh_views(["daz:1"], FIGS)
    row = store.get_by_sku("1")
    assert row["description"] == "original description"
    assert row["enriched_at"] == "2024-03-01T00:00:00"
    assert store.asset_totals()["fetch_errors"] == 1
    # The next successful fetch clears the error.
    store.upsert_source("daz_store_web", "1", "daz:1", good, None, now="2024-05-01T00:00:00")
    assert _record(store, "daz_store_web", "1")["fetch_error"] is None
    assert store.asset_totals()["fetch_errors"] == 0


def test_failed_first_fetch_creates_record_without_fetched_at(tmp_path):
    s = CatalogStore(str(tmp_path / "c.db"))
    s.upsert_source("daz_cms", "9", "daz:9", _cms("9", "Nine", "Ann"), "2024-01-01T00:00:00",
                    now="2024-01-01T00:00:00")
    s.upsert_source("daz_store_web", "9", "daz:9", {}, None, fetch_error="timeout",
                    now="2024-01-02T00:00:00")
    assert s.source_index("daz_store_web")["9"]["fetched_at"] is None
    s.refresh_views(["daz:9"], FIGS)
    # enriched_at falls back to the daz_cms fetch time.
    assert s.get_by_sku("9")["enriched_at"] == "2024-01-01T00:00:00"


def test_refresh_views_derives_row_from_sources(tmp_path):
    s = CatalogStore(str(tmp_path / "c.db"))
    cms = {"sku": "7", "product_name": "G9 Hair", "artists": "Ann, Bob",
           "categories": "Hair,Genesis 9", "content_types": "Follower/Hair",
           "product_compatibility": "Genesis 9", "content_item_count": 4,
           "last_modified_date": "2024-01-01T00:00:00"}
    web = _web("7", description="Long hair.")
    s.upsert_source("daz_cms", "7", "daz:7", cms, "2024-01-01T00:00:00", now="2024-01-05T00:00:00")
    s.upsert_source("daz_store_web", "7", "daz:7", web, None, now="2024-01-06T00:00:00")
    s.refresh_views(["daz:7"], FIGS)
    expected = build_view_row("daz:7", cms, web, FIGS)
    row = s.get_rows(["daz:7"])[0]
    for k in ("name", "artist", "embedding_text", "tags", "compatible_figures", "category",
              "subcategories", "url", "description", "asset_count", "last_updated"):
        assert row[k] == expected[k], k
    assert row["doc_hash"] == doc_hash(row)
    assert row["installed"] == 1 and row["doc_format_version"] == 1
    assert row["enriched_at"] == "2024-01-06T00:00:00"
    assert s.asset_ids_matching(artists=["Bob"]) == {"daz:7"}
    assert s.asset_ids_matching(compatible_figures=["Genesis 9"]) == {"daz:7"}


def test_source_index_shape(store):
    idx = store.source_index("daz_cms")
    assert set(idx) == {"1", "2"}
    assert idx["1"] == {"asset_id": "daz:1", "present": 1, "source_modified": "2024-01-01T00:00:00",
                        "payload_hash": payload_hash(_cms("1", "Alpha", "Ann", figures="Genesis 8",
                                                          tags="Props, Decor", category="props")),
                        "fetched_at": "2024-01-02T00:00:00"}
    assert store.source_index("nothing") == {}


def test_non_daz_source_asset_has_no_sku(tmp_path):
    s = CatalogStore(str(tmp_path / "c.db"))
    s.upsert_source("fs", "abc", "fs:abc", {"path": "x"}, None)
    assert connect(s.db_path).execute(
        "SELECT sku FROM assets WHERE asset_id = 'fs:abc'").fetchone()[0] is None


def test_get_products_paging_sorting_and_name_query(store):
    page = store.get_products(1, 1, sort_by="name", sort_dir="desc")
    assert set(page) == {"products", "total", "page", "page_size", "total_pages"}
    assert (page["total"], page["page"], page["page_size"], page["total_pages"]) == (2, 1, 1, 2)
    assert page["products"][0]["name"] == "Beta"
    assert "installed" in page["products"][0]
    assert store.get_products(2, 1, sort_by="name")["products"][0]["name"] == "Beta"
    assert [p["sku"] for p in store.get_products(1, 50, sort_by="install_date",
                                                 sort_dir="desc")["products"]] == ["2", "1"]
    assert [p["sku"] for p in store.get_products(1, 50, sort_by="bogus; DROP")["products"]] == ["1", "2"]
    assert [p["sku"] for p in store.get_products(1, 50, name_query="lph")["products"]] == ["1"]
    assert store.get_products(1, 50, category="wardrobe")["total"] == 1
    empty = store.get_products(1, 50, artist="Nobody")
    assert (empty["products"], empty["total"], empty["total_pages"]) == ([], 0, 1)


def test_reads(store):
    assert store.get_by_sku("1")["name"] == "Alpha"
    assert store.get_by_sku("nope") is None
    assert [r["asset_id"] for r in store.get_rows(["daz:2", "missing", "daz:1"])] == ["daz:2", "daz:1"]
    assert store.get_rows([]) == []
    assert store.count() == 2
    assert store.get_last_updated() == "2024-02-01T00:00:00"
    assert store.asset_totals() == {"total": 2, "installed": 2, "not_installed": 0, "fetch_errors": 0}


def test_empty_store_reads(tmp_path):
    s = CatalogStore(str(tmp_path / "c.db"))
    assert s.count() == 0
    assert s.get_last_updated() is None
    assert s.get_filter_values() == {"categories": [], "artists": [], "compatible_figures": []}
    assert s.asset_totals() == {"total": 0, "installed": 0, "not_installed": 0, "fetch_errors": 0}


def test_refresh_views_for_asset_without_sources_removes_view(store):
    conn = connect(store.db_path)
    conn.execute("DELETE FROM source_records WHERE asset_id = 'daz:2'")
    conn.commit()
    store.refresh_views(["daz:2"], FIGS)
    assert store.get_by_sku("2") is None
    assert store.asset_ids_matching(artists=["Anna"]) == set()
