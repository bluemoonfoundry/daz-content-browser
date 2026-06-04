"""Unit tests for async ETL scraping (daz-content-browser-bqz).

All tests use mocked network I/O — no real HTTP calls are made.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _products(skus):
    return [{"sku": s, "product_name": f"Product {s}"} for s in skus]


def _make_fake_scrape(fail_skus=()):
    """Returns an async mock that succeeds for all SKUs except those in fail_skus."""
    async def fake_scrape(session, semaphore, sku):
        if sku in fail_skus:
            raise RuntimeError(f"simulated network error for {sku}")
        return {
            "url": f"https://www.daz3d.com/product-{sku}",
            "image_url": f"https://gcdn.daz3d.com/{sku}.jpg",
            "description": f"Description for {sku}",
            "price": "$9.99",
            "mature": False,
        }
    return fake_scrape


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScrapeAllAsync:

    def test_returns_one_result_per_product(self):
        from managers.postgres_db_manager import _scrape_all_async

        products = _products(["111", "222", "333"])
        with patch("managers.postgres_db_manager._scrape_product_page_async", side_effect=_make_fake_scrape()):
            results = asyncio.run(_scrape_all_async(products, concurrency=3))

        assert len(results) == 3

    def test_preserves_input_order(self):
        """Results must map back to the original product list by index, regardless
        of which coroutine finishes first."""
        from managers.postgres_db_manager import _scrape_all_async

        skus = ["aaa", "bbb", "ccc", "ddd", "eee"]
        products = _products(skus)

        with patch("managers.postgres_db_manager._scrape_product_page_async", side_effect=_make_fake_scrape()):
            results = asyncio.run(_scrape_all_async(products, concurrency=5))

        for i, (product, web_data) in enumerate(results):
            assert product["sku"] == skus[i], f"position {i}: expected {skus[i]}, got {product['sku']}"

    def test_web_data_fields_populated(self):
        from managers.postgres_db_manager import _scrape_all_async

        products = _products(["99999"])
        with patch("managers.postgres_db_manager._scrape_product_page_async", side_effect=_make_fake_scrape()):
            results = asyncio.run(_scrape_all_async(products, concurrency=1))

        _, web_data = results[0]
        assert web_data["url"] == "https://www.daz3d.com/product-99999"
        assert web_data["image_url"] == "https://gcdn.daz3d.com/99999.jpg"
        assert web_data["description"] == "Description for 99999"

    def test_individual_failure_does_not_abort_batch(self):
        """A single scrape error must not raise — the failed product gets an empty
        web_data dict so the caller's SQLite insert path can still run."""
        from managers.postgres_db_manager import _scrape_all_async

        products = _products(["good1", "bad", "good2"])

        async def failing_scrape(session, semaphore, sku):
            if sku == "bad":
                raise RuntimeError("network error")
            return {"url": f"https://example.com/{sku}"}

        with patch("managers.postgres_db_manager._scrape_product_page_async", side_effect=failing_scrape):
            results = asyncio.run(_scrape_all_async(products, concurrency=3))

        assert len(results) == 3
        skus_out = [p["sku"] for p, _ in results]
        assert skus_out == ["good1", "bad", "good2"]

        good1_data = results[0][1]
        bad_data = results[1][1]
        good2_data = results[2][1]
        assert good1_data.get("url") == "https://example.com/good1"
        assert isinstance(bad_data, dict)   # empty, not None
        assert good2_data.get("url") == "https://example.com/good2"

    def test_progress_callback_called_for_each_product(self):
        from managers.postgres_db_manager import _scrape_all_async

        products = _products(["p1", "p2", "p3"])
        calls = []

        def on_progress(stage, current, total, detail):
            calls.append((stage, current, total))

        with patch("managers.postgres_db_manager._scrape_product_page_async", side_effect=_make_fake_scrape()):
            asyncio.run(_scrape_all_async(products, concurrency=3, on_progress=on_progress))

        assert len(calls) == 3
        totals = {t for _, _, t in calls}
        assert totals == {3}
        currents = sorted(c for _, c, _ in calls)
        assert currents == [1, 2, 3]

    def test_empty_input_returns_empty_list(self):
        from managers.postgres_db_manager import _scrape_all_async

        results = asyncio.run(_scrape_all_async([], concurrency=5))
        assert results == []

    def test_concurrency_one_still_works(self):
        """Degenerate case: semaphore of 1 should behave like serial execution."""
        from managers.postgres_db_manager import _scrape_all_async

        products = _products(["x1", "x2"])
        with patch("managers.postgres_db_manager._scrape_product_page_async", side_effect=_make_fake_scrape()):
            results = asyncio.run(_scrape_all_async(products, concurrency=1))

        assert [p["sku"] for p, _ in results] == ["x1", "x2"]
