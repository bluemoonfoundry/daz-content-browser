"""Reconciling DAZ CMS listings into the catalog: adds, edits, removals per source."""

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalog.documents import build_view_row
from catalog.reconcile import ReconcileAborted, ReconcileResult, reconcile_daz
from catalog.schema import LEGACY_COLUMNS, connect
from catalog.store import CatalogStore
from catalog_fakes import FIGS, FakeScraper, cms_row, web_payload

CMS_A = cms_row("A", "Alpha Dress", categories="Wardrobe, Clothes", compat="Genesis 8 Female")
CMS_B = cms_row("B", "Beta Props")


@pytest.fixture
def env(tmp_path):
    return SimpleNamespace(catalog=CatalogStore(str(tmp_path / "catalog.db")), scrape=FakeScraper())


def _web_record(catalog, sku):
    row = connect(catalog.db_path).execute(
        "SELECT * FROM source_records WHERE source='daz_store_web' AND source_key=?",
        (sku,)).fetchone()
    return dict(row) if row else None


# --- legacy fixture ------------------------------------------------------------------

# The legacy wide table's DDL (column types matter: asset_count is INTEGER).
LEGACY_DDL = (Path(__file__).parent / "fixtures" / "legacy_products.sql").read_text(
    encoding="utf-8").split("INSERT")[0]

_LEGACY_SOURCES = [
    (cms_row("L1", "Summer Dress", date="2024-03-01T10:20:30", artists="Ann, Bob Studio",
             categories="Wardrobe,Clothes", compat="Genesis 8 Female",
             content_types="Wardrobe/Clothes", count=7),
     web_payload("L1", "A light summer dress.")),
    (cms_row("L2", "Café Props", date="2023-11-05T00:00:00", artists="Anna",
             categories="Props,Decor", compat="Genesis 8.1 Female, Genesis 9",
             content_types="Props", count=2),
     dict(web_payload("L2"), description=None)),
    (cms_row("L3", "Bare Tool", date=None, artists=None, categories=None, compat=None,
             content_types=None, count=0),
     {}),
]


@pytest.fixture
def legacy_env(tmp_path):
    """A legacy wide table written the way the old ETL wrote it, migrated into a catalog."""
    db = str(tmp_path / "enriched_products.db")
    conn = sqlite3.connect(db)
    conn.executescript(LEGACY_DDL)
    for cms, web in _LEGACY_SOURCES:
        row = build_view_row(f"daz:{cms['sku']}", cms, web, FIGS)
        row["enriched_at"] = "2024-03-02T08:00:00+00:00" if web else None
        conn.execute(f"INSERT INTO products VALUES ({', '.join('?' * len(LEGACY_COLUMNS))})",
                     [row.get(c) for c in LEGACY_COLUMNS])
    conn.commit()
    conn.close()
    catalog = CatalogStore(db, legacy_table="products")
    assert catalog.migration_error is None
    return SimpleNamespace(catalog=catalog, scrape=FakeScraper(),
                           cms_rows_matching_legacy=[dict(c) for c, _ in _LEGACY_SOURCES],
                           asset_ids=[f"daz:{c['sku']}" for c, _ in _LEGACY_SOURCES],
                           n=len(_LEGACY_SOURCES))


# --- brief tests ---------------------------------------------------------------------

def test_new_changed_removed_reappeared(env):
    r1 = reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS)
    assert isinstance(r1, ReconcileResult)
    assert (r1.new, r1.scraped) == (2, 2)
    r2 = reconcile_daz(env.catalog, [dict(CMS_A, last_modified_date="2025-01-01T00:00:00")], env.scrape, FIGS)
    assert (r2.changed, r2.removed) == (1, 1)
    assert env.catalog.get_by_sku("B")["installed"] == 0
    r3 = reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS)
    assert r3.reappeared == 1 and env.catalog.get_by_sku("B")["installed"] == 1
    # B came back unchanged: not re-scraped, scraped data kept.
    assert env.scrape.calls[-1] == ["A"]
    assert env.catalog.get_by_sku("B")["url"] == web_payload("B")["url"]


def test_first_run_after_migration_rescrapes_nothing(legacy_env):
    before = {a: legacy_env.catalog.get_rows([a])[0]["doc_hash"] for a in legacy_env.asset_ids}
    r = reconcile_daz(legacy_env.catalog, legacy_env.cms_rows_matching_legacy, legacy_env.scrape, FIGS)
    assert r.scraped == 0 and r.refreshed_payloads == legacy_env.n
    after = {a: legacy_env.catalog.get_rows([a])[0]["doc_hash"] for a in legacy_env.asset_ids}
    assert after == before                                   # nothing would be re-embedded
    assert legacy_env.scrape.calls == []
    # The fresh CMS payload replaced the legacy one; a second run is a no-op.
    payload = json.loads(connect(legacy_env.catalog.db_path).execute(
        "SELECT payload FROM source_records WHERE source='daz_cms' AND source_key='L1'").fetchone()[0])
    assert "legacy" not in payload and payload["categories"] == "Wardrobe,Clothes"
    r2 = reconcile_daz(legacy_env.catalog, legacy_env.cms_rows_matching_legacy, legacy_env.scrape, FIGS)
    assert (r2.new, r2.changed, r2.refreshed_payloads, r2.scraped, r2.touched) == (0, 0, 0, 0, [])


def test_renamed_product_updates_same_asset(env):
    reconcile_daz(env.catalog, [CMS_A], env.scrape, FIGS)
    renamed = dict(CMS_A, product_name="Alpha Gown", last_modified_date="2025-02-02T00:00:00")
    r = reconcile_daz(env.catalog, [renamed], env.scrape, FIGS)
    assert (r.new, r.changed, r.scraped, r.touched) == (0, 1, 1, ["daz:A"])
    assert env.catalog.count() == 1
    row = env.catalog.get_by_sku("A")
    assert (row["asset_id"], row["name"], row["last_updated"]) == ("daz:A", "Alpha Gown",
                                                                     "2025-02-02T00:00:00")
    assert "Alpha Gown" in row["embedding_text"]


def test_scrape_failure_recorded_and_run_continues(env):
    env.scrape.fail = {"B"}
    r = reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS)
    assert (r.new, r.scraped, r.scrape_errors) == (2, 1, 1)
    assert env.catalog.get_by_sku("A")["url"] == web_payload("A")["url"]
    b = env.catalog.get_by_sku("B")
    assert b["installed"] == 1 and b["url"] is None and b["name"] == "Beta Props"
    rec = _web_record(env.catalog, "B")
    assert "simulated failure" in rec["fetch_error"] and rec["fetched_at"] is None
    assert env.catalog.asset_totals()["fetch_errors"] == 1
    # Failed fetches are not retried automatically on the next unchanged run ...
    r2 = reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS)
    assert r2.scraped == 0 and r2.scrape_errors == 0
    # ... and a later failure keeps the earlier good payload.
    env.scrape.fail = {"A"}
    r3 = reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS, refetch_all=True)
    assert (r3.scraped, r3.scrape_errors) == (1, 1)
    assert env.catalog.get_by_sku("A")["url"] == web_payload("A")["url"]
    assert env.catalog.get_by_sku("B")["url"] == web_payload("B")["url"]
    assert _web_record(env.catalog, "B")["fetch_error"] is None


def test_refetch_before_scrapes_only_older_records(env):
    reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS, now="2026-01-01T00:00:00+00:00")
    cms_c = cms_row("C")
    reconcile_daz(env.catalog, [CMS_A, CMS_B, cms_c], env.scrape, FIGS,
                  now="2026-03-01T00:00:00+00:00")
    env.scrape.calls.clear()
    r = reconcile_daz(env.catalog, [CMS_A, CMS_B, cms_c], env.scrape, FIGS,
                      refetch_before="2026-02-01T00:00:00+00:00", now="2026-04-01T00:00:00+00:00")
    assert sorted(env.scrape.scraped) == ["A", "B"]
    assert (r.scraped, r.new, r.changed) == (2, 0, 0)
    assert sorted(r.touched) == ["daz:A", "daz:B"]
    assert _web_record(env.catalog, "A")["fetched_at"] == "2026-04-01T00:00:00+00:00"
    assert _web_record(env.catalog, "C")["fetched_at"] == "2026-03-01T00:00:00+00:00"


def test_refetch_before_includes_never_fetched_records(env):
    env.scrape.fail = {"B"}
    reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS, now="2026-01-01T00:00:00+00:00")
    env.scrape.fail = set()
    env.scrape.calls.clear()
    r = reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS,
                      refetch_before="2025-01-01T00:00:00+00:00")
    assert env.scrape.scraped == ["B"] and r.scraped == 1
    assert env.catalog.get_by_sku("B")["url"] == web_payload("B")["url"]


def test_refetch_all_skips_absent_records(env):
    reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS)
    env.scrape.calls.clear()
    r = reconcile_daz(env.catalog, [CMS_A], env.scrape, FIGS, refetch_all=True)
    assert env.scrape.scraped == ["A"] and (r.scraped, r.removed) == (1, 1)


def test_add_and_remove_in_one_run_are_both_counted(env):
    reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS)
    r = reconcile_daz(env.catalog, [CMS_A, cms_row("C")], env.scrape, FIGS)
    assert (r.new, r.removed, r.changed, r.reappeared) == (1, 1, 0, 0)
    assert sorted(r.touched) == ["daz:B", "daz:C"]
    totals = env.catalog.asset_totals()
    assert (totals["total"], totals["installed"], totals["not_installed"]) == (3, 2, 1)


# --- controller rulings ----------------------------------------------------------------

def test_empty_listing_with_present_records_aborts_without_writing(env):
    reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS)
    with pytest.raises(ReconcileAborted):
        reconcile_daz(env.catalog, [], env.scrape, FIGS)
    assert env.catalog.asset_totals()["installed"] == 2


def test_empty_listing_on_empty_catalog_is_a_noop(env):
    r = reconcile_daz(env.catalog, [], env.scrape, FIGS)
    assert (r.new, r.removed, r.scraped, r.touched) == (0, 0, 0, [])


def test_duplicate_skus_keep_first_and_warn_once(env, caplog):
    dup = dict(CMS_A, product_name="Second Row", product_id=99)
    with caplog.at_level("WARNING", logger="catalog.reconcile"):
        r = reconcile_daz(env.catalog, [CMS_A, dup, dict(dup)], env.scrape, FIGS)
    assert r.new == 1 and env.scrape.scraped == ["A"]
    assert env.catalog.get_by_sku("A")["name"] == "Alpha Dress"
    warnings = [m for m in caplog.messages if "duplicate" in m.lower()]
    assert len(warnings) == 1 and "2" in warnings[0]


def test_payload_change_without_date_change_refreshes_without_scrape(env):
    reconcile_daz(env.catalog, [CMS_A], env.scrape, FIGS)
    env.scrape.calls.clear()
    r = reconcile_daz(env.catalog, [dict(CMS_A, content_item_count=9)], env.scrape, FIGS)
    assert (r.refreshed_payloads, r.changed, r.scraped, r.touched) == (1, 0, 0, ["daz:A"])
    assert env.scrape.calls == [] and env.catalog.get_by_sku("A")["asset_count"] == 9


def test_reappeared_with_new_date_is_rescraped(env):
    reconcile_daz(env.catalog, [CMS_A, CMS_B], env.scrape, FIGS)
    reconcile_daz(env.catalog, [CMS_A], env.scrape, FIGS)
    env.scrape.calls.clear()
    r = reconcile_daz(env.catalog, [CMS_A, dict(CMS_B, last_modified_date="2027-01-01T00:00:00")],
                      env.scrape, FIGS)
    assert (r.reappeared, r.changed) == (1, 1) and env.scrape.scraped == ["B"]
    assert env.catalog.get_by_sku("B")["installed"] == 1


def test_limit_caps_new_and_changed_keys(env):
    rows = [cms_row(s) for s in "ABCDE"]
    r = reconcile_daz(env.catalog, rows, env.scrape, FIGS, limit=2)
    assert (r.new, r.scraped) == (2, 2) and env.catalog.count() == 2
    # A refetch never scrapes keys the limit deferred (they have no CMS record yet).
    env.scrape.calls.clear()
    r1b = reconcile_daz(env.catalog, rows, env.scrape, FIGS, limit=1, refetch_all=True)
    assert sorted(env.scrape.scraped) == ["A", "B", "C"] and r1b.new == 1
    assert _web_record(env.catalog, "D") is None
    r2 = reconcile_daz(env.catalog, rows, env.scrape, FIGS)
    assert (r2.new, r2.removed) == (2, 0) and env.catalog.count() == 5


def test_cancel_between_batches_keeps_written_records_and_resumes(env, monkeypatch):
    import catalog.reconcile as rec
    monkeypatch.setattr(rec, "SCRAPE_BATCH", 2)
    rows = [cms_row(s) for s in "ABCDE"]
    checks = []

    def cancel_after_first_batch():
        checks.append(1)
        return len(checks) > 1

    r = reconcile_daz(env.catalog, rows, env.scrape, FIGS, cancel_check=cancel_after_first_batch)
    assert r.scraped == 2 and env.catalog.count() == 2
    # The unprocessed keys were never written, so the next run picks them up as new.
    r2 = reconcile_daz(env.catalog, rows, env.scrape, FIGS)
    assert (r2.new, r2.scraped) == (3, 3) and env.catalog.count() == 5


def test_datetime_values_in_rows_are_stored_as_iso(env):
    row = dict(CMS_A, last_modified_date=datetime(2024, 5, 6, 7, 8, 9))
    reconcile_daz(env.catalog, [row], env.scrape, FIGS)
    assert env.catalog.source_index("daz_cms")["A"]["source_modified"] == "2024-05-06T07:08:09"
    r = reconcile_daz(env.catalog, [dict(CMS_A, last_modified_date="2024-05-06T07:08:09")],
                      env.scrape, FIGS)
    assert (r.changed, r.refreshed_payloads, r.scraped) == (0, 0, 0)


# --- CMS query + scraper adapters ------------------------------------------------------

def test_get_all_products_converts_dates_to_iso():
    from managers.daz_db_analyzer import DazDBAnalyzer
    analyzer = DazDBAnalyzer.__new__(DazDBAnalyzer)
    seen = {}

    def fake_query(sql, params=None):
        seen["sql"] = sql
        return [dict(CMS_A, last_modified_date=datetime(2024, 1, 2, 3, 4, 5)),
                dict(CMS_B, last_modified_date=None)]

    analyzer._execute_query = fake_query
    rows = analyzer.get_all_products()
    assert rows[0]["last_modified_date"] == "2024-01-02T03:04:05"
    assert rows[1]["last_modified_date"] is None
    assert rows[0]["product_name"] == "Alpha Dress"
    assert "p.token IS NOT NULL AND p.token != ''" in seen["sql"]
    assert seen["sql"].index("WHERE p.token") < seen["sql"].index("GROUP BY")


def test_get_all_products_raises_on_query_error():
    from managers.daz_db_analyzer import DazDBAnalyzer
    analyzer = DazDBAnalyzer.__new__(DazDBAnalyzer)
    analyzer._execute_query = lambda sql, params=None: None
    with pytest.raises(RuntimeError):
        analyzer.get_all_products()


def test_scrape_many_maps_empty_and_failed_scrapes_to_exceptions(monkeypatch):
    import managers.postgres_db_manager as pdm

    async def fake_page(session, semaphore, sku):
        if sku == "boom":
            raise ConnectionError("network down")
        return {} if sku == "gone" else {"url": f"https://www.daz3d.com/{sku}"}

    monkeypatch.setattr(pdm, "_scrape_product_page_async", fake_page)
    progress = []
    out = pdm.scrape_many(["ok", "gone", "boom"], concurrency=2,
                          on_progress=lambda *a: progress.append(a))
    assert out["ok"] == {"url": "https://www.daz3d.com/ok"}
    assert isinstance(out["gone"], LookupError)
    assert isinstance(out["boom"], Exception) and "network down" in str(out["boom"])
    assert len(progress) == 3
    assert pdm.scrape_many([], concurrency=2) == {}
