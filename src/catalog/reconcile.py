"""Reconcile a full DAZ CMS listing into the catalog (incremental index run).

Diffs the CMS rows against the ``daz_cms`` source records and writes only what changed:

* **new** SKU -> ``daz_cms`` record + store scrape (``daz_store_web``).
* **changed** (``source_modified`` differs) -> refreshed ``daz_cms`` record + re-scrape.
* same ``source_modified`` but a different payload (incl. migrated records whose hash is
  NULL) -> ``daz_cms`` record only, no scrape.
* **absent** from the listing -> ``mark_absent`` (kept searchable, not installed).
* **reappeared** -> present again; re-scraped only when ``source_modified`` changed.

A new/changed SKU's CMS record is written together with its scrape result, batch by batch,
so a cancelled run leaves the unprocessed SKUs unwritten and the next run picks them up.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Optional

from catalog.documents import payload_hash
from catalog.store import CMS_SOURCE, WEB_SOURCE, CatalogStore

logger = logging.getLogger(__name__)

# SKUs handed to the scraper per call; ``cancel_check`` runs between batches.
SCRAPE_BATCH = 100


class ReconcileAborted(Exception):
    """The CMS listing can't be trusted; nothing was written."""


@dataclass
class ReconcileResult:
    new: int = 0
    changed: int = 0
    removed: int = 0
    reappeared: int = 0
    refreshed_payloads: int = 0
    scraped: int = 0
    scrape_errors: int = 0
    touched: list = field(default_factory=list)


def _asset_id(sku: str) -> str:
    return f"daz:{sku}"


def _plain(value):
    """ISO-format datetimes the same way the legacy ETL did; leave everything else."""
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def _dedupe(cms_rows: list) -> dict:
    rows, dups = {}, 0
    for row in cms_rows:
        sku = row.get("sku")
        if sku is None or str(sku).strip() == "":
            continue
        sku = str(sku)
        if sku in rows:
            dups += 1
            continue
        rows[sku] = {k: _plain(v) for k, v in row.items()}
    if dups:
        logger.warning("CMS listing has %d duplicate SKU row(s); kept the first row per SKU", dups)
    return rows


def reconcile_daz(catalog: CatalogStore, cms_rows: list,
                  scrape: Callable[[list], dict], figure_names: list, *,
                  refetch_before: Optional[str] = None, refetch_all: bool = False,
                  limit: Optional[int] = None, now: Optional[str] = None,
                  cancel_check=None) -> ReconcileResult:
    """Bring the catalog's DAZ source records in line with ``cms_rows``.

    ``scrape(skus)`` returns ``{sku: web payload dict | Exception}``. ``limit`` caps how
    many new/changed SKUs are processed (the rest wait for a later run). ``refetch_all``
    re-scrapes every listed SKU; ``refetch_before=T`` re-scrapes listed SKUs whose store
    data was never fetched or was fetched before ``T``; ``T`` must use the same UTC
    ``isoformat()`` as ``fetched_at`` (the store's ``_now()``), because they are compared
    as strings.

    A reappeared SKU is marked present (installed) immediately, whatever ``limit`` or
    ``cancel_check`` do; if its ``source_modified`` also changed it is then processed as
    changed, now or on a later run. ``reappeared`` and ``refreshed_payloads`` (or
    ``changed``) can therefore count the same SKU.
    """
    now = now or datetime.now(timezone.utc).isoformat()
    rows = _dedupe(cms_rows)
    cms_index = catalog.source_index(CMS_SOURCE)
    if not rows and any(r["present"] for r in cms_index.values()):
        raise ReconcileAborted("DAZ CMS returned no products while the catalog has installed "
                               "products; refusing to mark the whole library uninstalled")

    result = ReconcileResult()
    touched: list = []
    cms_writes: set = set()       # new/changed keys: CMS record written with their scrape
    cms_only: list = []           # keys whose CMS record is written without a scrape
    reappeared: list = []         # absent keys listed again: marked present up front
    for sku, row in rows.items():
        known = cms_index.get(sku)
        if known is None:
            cms_writes.add(sku)
            continue
        if not known["present"]:
            reappeared.append(sku)
        if row.get("last_modified_date") != known["source_modified"]:
            cms_writes.add(sku)
        else:
            refreshed = payload_hash(row) != known["payload_hash"]
            result.refreshed_payloads += refreshed
            if refreshed:
                cms_only.append(sku)

    # ``limit`` caps new/changed keys; listing order decides which go first.
    ordered = [sku for sku in rows if sku in cms_writes]
    if limit is not None:
        ordered = ordered[:max(limit, 0)]
    deferred = cms_writes.difference(ordered)  # left for a later run: write nothing now
    cms_writes = set(ordered)
    to_scrape = list(ordered)
    if refetch_all or refetch_before is not None:
        web_index = catalog.source_index(WEB_SOURCE)
        for sku in rows:
            if sku in cms_writes or sku in deferred:
                continue
            fetched = (web_index.get(sku) or {}).get("fetched_at")
            if refetch_all or fetched is None or fetched < refetch_before:
                to_scrape.append(sku)

    removed = catalog.mark_absent(CMS_SOURCE, set(rows), now=now)
    result.removed = len(removed)
    touched += removed

    back = catalog.mark_present(CMS_SOURCE, reappeared, now=now)
    result.reappeared = len(back)
    touched += back

    for sku in cms_only:
        catalog.upsert_source(CMS_SOURCE, sku, _asset_id(sku), rows[sku],
                              rows[sku].get("last_modified_date"), now=now)
        touched.append(_asset_id(sku))

    for i in range(0, len(to_scrape), SCRAPE_BATCH):
        if cancel_check and cancel_check():
            logger.info("Reconcile cancelled after %d of %d scrapes", i, len(to_scrape))
            break
        batch = to_scrape[i:i + SCRAPE_BATCH]
        try:
            pages = scrape(batch) or {}
        except Exception as e:  # a broken scraper fails this batch, not the run
            logger.exception("Scrape batch failed")
            pages = {sku: e for sku in batch}
        for sku in batch:
            asset_id = _asset_id(sku)
            if sku in cms_writes:
                known = cms_index.get(sku)
                if known is None:
                    result.new += 1
                else:  # in cms_writes, so source_modified differs
                    result.changed += 1
                catalog.upsert_source(CMS_SOURCE, sku, asset_id, rows[sku],
                                      rows[sku].get("last_modified_date"), now=now)
            page = pages.get(sku, LookupError("no scrape result"))
            if not isinstance(page, BaseException) and not page:
                page = LookupError("no store data")  # never overwrite good data with {}
            if isinstance(page, BaseException):
                result.scrape_errors += 1
                catalog.upsert_source(WEB_SOURCE, sku, asset_id, {}, None,
                                      fetch_error=str(page) or type(page).__name__, now=now)
            else:
                result.scraped += 1
                catalog.upsert_source(WEB_SOURCE, sku, asset_id, page, None, now=now)
            touched.append(asset_id)

    result.touched = list(dict.fromkeys(touched))
    catalog.refresh_views(result.touched, figure_names)
    logger.info("Reconciled DAZ CMS: %d new, %d changed, %d removed, %d reappeared, "
                "%d refreshed, %d scraped, %d scrape errors", result.new, result.changed,
                result.removed, result.reappeared, result.refreshed_payloads,
                result.scraped, result.scrape_errors)
    return result
