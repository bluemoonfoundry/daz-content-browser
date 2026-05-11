import logging
import os
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from utilities import fetch_json_from_url, fetch_html_content
from managers.managers import chroma_db_manager, sqlite_db, daz_pg_analyzer
from embedding_utils import generate_embeddings
from managers.etl_transforms import (
    determine_categories as _determine_categories,
    determine_compatibility as _determine_compatibility,
    generate_embedding_text as _generate_embedding_text,
)

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


def determine_categories(content_type_string: str) -> dict:
    """Backward-compatible wrapper around pure ETL transform helper."""
    return _determine_categories(content_type_string)

def scrape_product_page(sku):
    """Given a SKU, fetch additional product details by scraping the product page.

    Args:
        sku (str): The SKU of the product to scrape.
    """
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

        # Now go to the product page. The main thing we need from that page is a description
        time.sleep(0.2)  # avoid rate-limiting daz3d.com
        html_content, tags = fetch_html_content(product_page_url)
        if tags is not None:
            rv['description'] = tags.get('og:description')
            rv['tags'] = tags.get('keywords')

    return rv

def generate_embedding_text(product_data, web_data) -> str:
    """Backward-compatible wrapper around pure ETL transform helper."""
    return _generate_embedding_text(product_data, web_data)

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
    """Backward-compatible wrapper around pure ETL transform helper."""
    return _determine_compatibility(product_data, figure_names)

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
        for etl_idx, product in enumerate(products_to_process_data):
            sku = product.get('sku')
            try:
                logger.info(f"Processing '{product.get('product_name')}' (SKU: {sku})")

                refactored_data = determine_compatibility(product, figure_names)

                final_tags = ', '.join(filter(None, [
                    product.get('categories'),
                    refactored_data['tags_to_append']
                ]))

                web_data = scrape_product_page(sku)
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
                    if on_progress:
                        on_progress("etl", len(successfully_processed_skus), total_etl, product.get('product_name', sku))
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
