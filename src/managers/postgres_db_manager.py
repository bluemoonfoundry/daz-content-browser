"""Store-page scraping for DAZ products (``scrape_many`` feeds the catalog reconciler)."""

import asyncio
import logging

import aiohttp
from dotenv import load_dotenv

from utilities import fetch_json_from_url, fetch_html_content, async_fetch_json_from_url, async_fetch_html_content

load_dotenv()

logger = logging.getLogger(__name__)


async def _scrape_product_page_async(
    session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, sku: str
) -> dict:
    """Async version of scrape_product_page — reuses a shared session and semaphore."""
    rv = {}
    slab_url = f'https://www.daz3d.com/dazApi/slab/{sku}'
    async with semaphore:
        base_content = await async_fetch_json_from_url(session, slab_url)
        if base_content is not None:
            mark_url = base_content.get('url', '')
            if mark_url.startswith("/"):
                mark_url = mark_url[1:]
            product_page_url = f'https://www.daz3d.com/{mark_url}'

            raw_image = base_content.get('imageUrl', '')
            idx = raw_image.find('/http')
            image_url = raw_image[idx + 1:] if idx != -1 else raw_image

            prices = base_content.get('prices', {})
            price = prices.get('USD', 'Unknown')

            rv = {
                'url': product_page_url,
                'image_url': image_url,
                'price': f"${price}",
                'mature': base_content.get('mature'),
            }

            _, tags = await async_fetch_html_content(session, product_page_url)
            if tags is not None:
                rv['description'] = tags.get('og:description')
                rv['tags'] = tags.get('keywords')

    return rv


async def _scrape_all_async(products_data: list, concurrency: int, on_progress=None,
                            errors: dict = None) -> list:
    """Scrapes all products concurrently.

    Returns a list of (product_data, web_data) tuples in the same order as products_data.
    A failed scrape yields empty web_data; when ``errors`` is given, the exception is also
    stored there under the product's index.
    """
    total = len(products_data)
    results = [None] * total
    completed = 0

    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency)

    async def scrape_one(i, product):
        nonlocal completed
        sku = product.get('sku')
        try:
            web_data = await _scrape_product_page_async(session, semaphore, sku)
        except Exception as e:
            logger.exception(f"Scrape failed for SKU {sku!r}, using empty web_data.")
            web_data = {}
            if errors is not None:
                errors[i] = e
        results[i] = (product, web_data)
        completed += 1
        if on_progress:
            on_progress("scrape", completed, total, product.get('product_name', sku))

    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[scrape_one(i, p) for i, p in enumerate(products_data)])

    return results


def scrape_many(skus: list, concurrency: int, on_progress=None) -> dict:
    """Scrape the store pages for ``skus``: ``{sku: web payload dict | Exception}``.

    An exception value means that SKU's scrape failed, including the store returning no
    data (``LookupError``), so the caller can record it and keep any earlier payload.
    """
    if not skus:
        return {}
    errors: dict = {}
    scraped = asyncio.run(_scrape_all_async([{"sku": s} for s in skus], concurrency,
                                            on_progress, errors=errors))
    out = {}
    for i, (product, web_data) in enumerate(scraped):
        if i in errors:
            out[product["sku"]] = errors[i]
        elif not web_data:
            out[product["sku"]] = LookupError("no store data")
        else:
            out[product["sku"]] = web_data
    return out


def scrape_product_page(sku):
    """Synchronous scrape for a single SKU (used outside the ETL batch path)."""
    rv = {}
    slab_url = f'https://www.daz3d.com/dazApi/slab/{sku}'
    base_content = fetch_json_from_url(slab_url)
    if base_content is not None:
        mark_url = base_content.get('url', '')
        if mark_url.startswith("/"):
            mark_url = mark_url[1:]
        product_page_url = f'https://www.daz3d.com/{mark_url}'

        raw_image = base_content.get('imageUrl', '')
        idx = raw_image.find('/http')
        image_url = raw_image[idx + 1:] if idx != -1 else raw_image

        prices = base_content.get('prices', {})
        price = prices.get('USD', 'Unknown')

        rv = {
            'url': product_page_url,
            'image_url': image_url,
            'price': f"${price}",
            'mature': base_content.get('mature'),
        }

        html_content, tags = fetch_html_content(product_page_url)
        if tags is not None:
            rv['description'] = tags.get('og:description')
            rv['tags'] = tags.get('keywords')

    return rv
