import asyncio
import logging
import os
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from utilities import fetch_json_from_url, fetch_html_content, async_fetch_json_from_url, async_fetch_html_content
from managers.managers import chroma_db_manager, sqlite_db, daz_pg_analyzer
from embedding_utils import generate_embeddings
import re
from collections import Counter

import aiohttp
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


def determine_categories(content_type_string: str) -> dict:
    """Analyzes a content type string to determine a primary category and subcategories.
    
    Args:
        content_type_string (str): The raw content type string from the database.

    Returns:
        dict: A dictionary with 'category' and 'subcategories' keys.
    """
    IGNORE_WORDS = {'follower', 'default', 'support', 'preset', 'people', 'genesis', 'genesis 9', 'genesis 8', 'genesis 3'}
    PRIORITY_WORDS = {'character', 'clothes', 'accessories', 'environments', 'hair', 'poses', 'animations', 'props', 'tools', 'effects'}
    if not content_type_string: return {'category': None, 'subcategories': []}
    words = re.split(r'[^a-zA-Z0-9]+', content_type_string)
    valid_words = [w.lower().strip() for w in words if w.lower().strip() and w.lower().strip() not in IGNORE_WORDS]
    if not valid_words: return {'category': None, 'subcategories': []}
    primary_category = next((word for word in valid_words if word in PRIORITY_WORDS), None)
    if primary_category is None:
        word_counts = Counter(valid_words)
        if word_counts: primary_category = word_counts.most_common(1)[0][0]
    if primary_category is None: return {'category': None, 'subcategories': []}
    unique_words = set(valid_words)
    unique_words.discard(primary_category)
    return {'category': primary_category, 'subcategories': sorted(list(unique_words))}

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


async def _scrape_all_async(products_data: list, concurrency: int, on_progress=None) -> list:
    """Scrapes all products concurrently.

    Returns a list of (product_data, web_data) tuples in the same order as products_data.
    """
    total = len(products_data)
    results = [None] * total
    completed = 0

    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency)

    async def scrape_one(i, product):
        nonlocal completed
        sku = product.get('sku')
        web_data = await _scrape_product_page_async(session, semaphore, sku)
        results[i] = (product, web_data)
        completed += 1
        if on_progress:
            on_progress("scrape", completed, total, product.get('product_name', sku))

    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[scrape_one(i, p) for i, p in enumerate(products_data)])

    return results


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

def generate_embedding_text(product_data, web_data) -> str:
    """Generates a rich, descriptive paragraph for the embedding model. Focuses on combining factual data with potential use-cases and avoids noisy data.

    Args:
        product_data (dict): The raw product data from the database.
        web_data (dict): The scraped web data including description and tags.

    Returns:
        str: A high-quality descriptive text for embedding generation.
    """
    logger.debug(f"Generating embedding text for: {product_data.get('product_name')}")
    
    # Extract clean data, providing sensible defaults
    name = product_data.get('product_name', 'a 3D asset')
    artist = product_data.get('artists')
    categories = product_data.get('categories')
    web_desc = web_data.get('description', '').strip()

    # --- Build the descriptive text part by part ---
    
    # Start with a clear, factual statement.
    parts = [f"A 3D asset package titled '{name}'."]
    if artist:
        parts.append(f"Created by the artist or studio: {artist}.")

    # Use the categories to add rich, contextual information about the product's use-case.
    if categories:
        # Clean up the category string for better sentence flow
        clean_categories = categories.replace(',', ', ')
        parts.append(f"It is categorized under: {clean_categories}.")
        
        # Add inferred use-case sentences based on keywords in categories. This is very powerful.
        cat_lower = categories.lower()
        if 'props' in cat_lower or 'decor' in cat_lower:
            parts.append("This is a set of props suitable for decorating digital scenes, environments, and dioramas.")
        if 'furniture' in cat_lower:
            parts.append("It includes furniture items for interior design and architectural visualization.")
        if 'character' in cat_lower:
            parts.append("This is a character asset for digital art and animation.")
        if 'hair' in cat_lower:
            parts.append("This is a hairstyle asset for 3D characters.")
        if 'wardrobe' in cat_lower or 'clothes' in cat_lower:
            parts.append("It contains clothing or wardrobe items for 3D figures.")
            
    # Add the high-quality human-written description from the web at the end.
    if web_desc:
        parts.append(f"Product Description: {web_desc}")

    # Join all the parts into a single, cohesive paragraph.
    return " ".join(parts)

def generate_and_store_embeddings(processed_skus, on_progress=None):
    """Fetches processed data from SQLite, generates embeddings, and stores them in ChromaDB in safe-sized batches to avoid database parameter limits.

    Args:
        processed_skus (list): List of SKUs that have been processed and need embeddings.
        on_progress (callable, optional): Progress callback: on_progress(stage, current, total, detail).

    Returns:
        bool: True if successful, False otherwise.
    """
    if not processed_skus:
        logger.info("No new products were processed, skipping embedding generation.")
        return

    total = len(processed_skus)
    logger.info(f"Starting embedding generation for {total} products.")

    BATCH_SIZE = int(os.getenv('BATCH_SIZE', '512'))
    had_errors = False

    # Loop through the list of SKUs in chunks of BATCH_SIZE
    for i in range(0, total, BATCH_SIZE):
        # Get the current batch of SKUs
        sku_batch = processed_skus[i:i + BATCH_SIZE]

        logger.info(f"Embedding batch {i//BATCH_SIZE + 1}/{(total-1)//BATCH_SIZE + 1} ({len(sku_batch)} items)...")

        rows_to_embed = sqlite_db.get_content_by_sku_batch(sku_batch)

        logger.debug(f"Fetched {len(rows_to_embed)} rows from SQLite for embedding.")

        for x, row in enumerate(rows_to_embed):
            if row['sku'] == "":
                raise ValueError(f"Empty SKU found at batch index {x}: {dict(row)}")
        
        if not rows_to_embed:
            continue # Should not happen, but good practice

        # 2. PREPARE DATA FOR BATCH PROCESSING
        ids = [row['sku'] for row in rows_to_embed]
        documents = [row['embedding_text'] for row in rows_to_embed]
        metadatas = [
            {
                "sku":               row["sku"] or "",
                "url":               row["url"] or "",
                "image_url":         row["image_url"] or "",
                "name":              row["name"] or "",
                "artist":            row["artist"] or "",
                "compatible_figures": row["compatible_figures"] or "",
                "tags":              row["tags"] or "",
                "category":          row["category"] or "",
                "subcategories":     row["subcategories"] or "",
            }
            for row in rows_to_embed
        ]

        logger.info(f"Generating embeddings for {len(documents)} documents...")
        embeddings = generate_embeddings(documents, is_query=False).tolist()

        # 4. STORE IN CHROMADB IN A BATCH
        logger.info("Upserting batch into ChromaDB...")
        try:
            chroma_db_manager.collection.upsert(
                ids=ids,
                embeddings=embeddings,
                metadatas=metadatas,
                documents=documents,
            )
            logger.info(f"Upserted {len(ids)} documents into collection '{chroma_db_manager.collection_name}'.")
            if on_progress:
                on_progress("embed", min(i + BATCH_SIZE, total), total, f"batch {i//BATCH_SIZE + 1}")
        except Exception as e:
            logger.error(f"Error publishing batch {i//BATCH_SIZE + 1} to ChromaDB: {e}")
            had_errors = True

    if had_errors:
        logger.warning("Embedding phase completed with errors — some batches were not indexed.")
    else:
        logger.info("Successfully finished processing all batches.")
    return not had_errors



def determine_compatibility(product_data: dict, figure_names: list) -> dict:
    """ Determines compatible figures by checking multiple fields in order of priority:
    1. The formal 'product_compatibility' string.
    2. The product 'name'.
    3. The product 'description' from web scraping (if available).
    
    Args:
        product_data: A dictionary containing the product's raw data.
        figure_names: A list of canonical figure names to search for.

    Returns:
        A dictionary with the clean compatibility string and the original
        compatibility string to be appended to tags.
    """
    compat_str = product_data.get('product_compatibility')
    name = product_data.get('product_name')
    description = product_data.get('description', '') # Description comes from web_data

    # Use a set to automatically handle duplicates
    found_figures = set()

    # --- Heuristic 1: Check the formal compatibility string first ---
    if compat_str:
        compat_lower = compat_str.lower()
        for figure in figure_names:
            if figure.lower() in compat_lower:
                found_figures.add(figure)

    # --- Heuristic 2: If nothing found, check the product name ---
    if not found_figures and name:
        name_lower = name.lower()
        for figure in figure_names:
            # We check for the figure name as a whole word or part of a compound
            # to avoid false positives (e.g., 'Dragon' matching 'Genesis 8 Dragon Form')
            if figure.lower() in name_lower:
                found_figures.add(figure)
    
    # --- Heuristic 3: If still nothing, check the product description ---
    if not found_figures and description:
        desc_lower = description.lower()
        for figure in figure_names:
            if figure.lower() in desc_lower:
                found_figures.add(figure)

    # --- Finalize and Return ---
    new_compatibility = ', '.join(sorted(list(found_figures)))
    
    return {
        'new_compatibility': new_compatibility,
        'tags_to_append': compat_str or '' # Always use the original string for tags
    }

def main(args, on_progress=None):
    """Main ETL and Embedding pipeline with command-line arguments.

    Args:
        args: Parsed argument namespace (force, all, limit, phase).
        on_progress (callable, optional): Progress callback with signature
            on_progress(stage: str, current: int, total: int, detail: str).
            Called after each ETL product and each embedding batch.
    """
    logger.debug(f"ETL args: {args}")

    sqlite_db.setup_sqlite_db(args.force)
    if args.force:
        chroma_db_manager.reset_collection()

    postgres_skus = daz_pg_analyzer.get_all_skus()

    skus_to_process = []
    if args.all or args.force:
        logger.info("--all/--force: targeting all products from PostgreSQL.")
        skus_to_process = postgres_skus
    else:
        logger.info("Default mode: targeting only new SKUs not found in SQLite.")
        sqlite_skus = sqlite_db.get_all_skus_from_sqlite()
        new_skus = list(set(postgres_skus) - set(sqlite_skus))
        skus_to_process = new_skus
        logger.info(f"Found {len(new_skus)} new SKUs to process.")

    if args.limit:
        logger.info(f"--limit: capping at {args.limit} SKUs.")
        skus_to_process = skus_to_process[:args.limit]

    if not skus_to_process and args.phase == 'etl':
        logger.info("No new products to ETL. Exiting.")
        return

    successfully_processed_skus = []

    if skus_to_process:
        logger.info(f"Total SKUs to ETL: {len(skus_to_process)} [phase: {args.phase}]")

    if args.phase == 'etl' or args.phase == 'all':
        logger.info(f"Phase 1: Starting ETL for {len(skus_to_process)} products.")
        products_to_process_data = daz_pg_analyzer.get_products_by_sku_list(skus_to_process)

        figures_path = Path(__file__).parent.parent.parent / '.figures.json'
        try:
            with open(figures_path, 'r') as f:
                figure_names = json.load(f)
            logger.info(f"Loaded {len(figure_names)} figure names from .figures.json.")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Error loading .figures.json: {e}")
            return

        total_etl = len(products_to_process_data)
        failed_skus = []

        # --- Phase 1: Async concurrent web scraping ---
        concurrency = int(os.getenv("ETL_SCRAPE_CONCURRENCY", "10"))
        logger.info(f"Scraping {total_etl} products (concurrency={concurrency})…")

        def _scrape_progress(stage, current, total, detail):
            if on_progress:
                on_progress("etl", current, total, detail)

        scraped = asyncio.run(_scrape_all_async(products_to_process_data, concurrency, _scrape_progress))

        # --- Phase 2: Serial SQLite inserts (fast, no network I/O) ---
        logger.info("Scraping complete. Inserting into SQLite…")
        for product, web_data in scraped:
            sku = product.get('sku')
            try:
                refactored_data = determine_compatibility(product, figure_names)

                final_tags = ', '.join(filter(None, [
                    product.get('categories'),
                    refactored_data['tags_to_append']
                ]))

                embedding_text = generate_embedding_text(product, web_data)
                structured_categories = determine_categories(product.get('content_types'))
                subcategories_str = ','.join(structured_categories['subcategories'])
                pg_date_iso = product['last_modified_date'].isoformat() if product['last_modified_date'] else None

                ok = sqlite_db.insert_item({
                    "sku":                  sku,
                    "url":                  web_data.get('url'),
                    "image_url":            web_data.get('image_url'),
                    "store":                web_data.get('store'),
                    "name":                 product.get('product_name'),
                    "artist":               product.get('artists'),
                    "price":                web_data.get('price'),
                    "description":          web_data.get('description'),
                    "tags":                 final_tags,
                    "formats":              product.get('content_types'),
                    "poly_count":           web_data.get('poly_count'),
                    "textures_info":        web_data.get('textures_info'),
                    "required_products":    web_data.get('required_products'),
                    "compatible_figures":   refactored_data['new_compatibility'],
                    "compatible_software":  web_data.get('compatible_software'),
                    "embedding_text":       embedding_text,
                    "last_updated":         pg_date_iso,
                    "category":             structured_categories['category'],
                    "subcategories":        subcategories_str,
                    "styles":               None,
                    "inferred_tags":        None,
                    "enriched_at":          datetime.now(timezone.utc).isoformat(),
                    "mature":               web_data.get('mature'),
                })
                if ok:
                    successfully_processed_skus.append(sku)
                else:
                    logger.warning(f"SQLite insert failed for SKU {sku!r}, skipping.")
                    failed_skus.append(sku)
            except Exception:
                logger.exception(f"Unexpected error processing SKU {sku!r}, skipping.")
                failed_skus.append(sku)

        if failed_skus:
            logger.warning(f"ETL phase: {len(failed_skus)} product(s) failed: {failed_skus}")
        logger.info(f"ETL phase complete. {len(successfully_processed_skus)} succeeded, {len(failed_skus)} failed.")
    
    if args.phase == 'embed' or args.phase == 'all':

        if args.force or args.all:
            logger.warning("--force and --all have no effect in embed-only mode.")

        # Start with anything freshly ETL'd this run.
        skus_to_embed = list(successfully_processed_skus)

        # In incremental mode, also pick up any SQLite products missing from ChromaDB
        # (e.g. from a previous run where embedding failed).
        if not (args.force or args.all):
            sqlite_all = set(sqlite_db.get_all_skus_from_sqlite())
            chroma_all = chroma_db_manager.get_all_ids()
            logger.info(
                f"Gap check: SQLite={len(sqlite_all)}, ChromaDB={len(chroma_all)}, "
                f"SQLite∩ChromaDB={len(sqlite_all & chroma_all)}, "
                f"SQLite-ChromaDB={len(sqlite_all - chroma_all)}"
            )
            missing_from_chroma = sqlite_all - chroma_all - set(successfully_processed_skus)
            if missing_from_chroma:
                logger.info(
                    f"Found {len(missing_from_chroma)} SQLite product(s) missing from ChromaDB — "
                    "adding to embed queue."
                )
                skus_to_embed.extend(missing_from_chroma)

        if not skus_to_embed:
            logger.info("Nothing to embed.")
            return

        if args.limit:
            logger.info(f"--limit: capping embedding at {args.limit} SKUs.")
            skus_to_embed = skus_to_embed[:args.limit]

        logger.info(f"Phase 2: Starting embedding generation for {len(skus_to_embed)} products.")
        generate_and_store_embeddings(skus_to_embed, on_progress=on_progress)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ETL and Embedding process for Daz Content.")
    parser.add_argument('--force', action='store_true', help="Force a complete rebuild of the SQLite database (implies --all).")
    parser.add_argument('--all', action='store_true', help="Process all products from Postgres, not just new ones.")
    parser.add_argument('--limit', type=int, help="Process only a limited number of products. Ideal for testing.")
    parser.add_argument('--phase', type=str, choices=['etl', 'embed', 'all'], default='all', help="Run only a specific phase: 'etl', 'embed', or both if omitted.")
    args = parser.parse_args()

    main(args)

