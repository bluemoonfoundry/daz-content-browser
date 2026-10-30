"""Pure derivation of ``asset_view`` rows, embedding text and document hashes.

Everything here reproduces the legacy ETL (``postgres_db_manager.main``) exactly: existing
installs keep their vectors only when the derived embedding text is byte-identical.
"""

import hashlib
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

DOC_FORMAT_VERSION = 1


def load_figure_names() -> list:
    """Load the canonical figure names from ``.figures.json`` (frozen-aware)."""
    if getattr(sys, "frozen", False):
        path = Path(sys._MEIPASS) / ".figures.json"
    else:
        path = Path(__file__).parent.parent.parent / ".figures.json"
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"figure names file not found: {path}") from e


def determine_categories(content_type_string: str) -> dict:
    """Analyzes a content type string to determine a primary category and subcategories.

    Returns a dict with 'category' and 'subcategories' keys.
    """
    IGNORE_WORDS = {'follower', 'default', 'support', 'preset', 'people', 'genesis', 'genesis 9', 'genesis 8', 'genesis 3'}
    PRIORITY_WORDS = {'character', 'clothes', 'wardrobe', 'accessories', 'environments', 'hair', 'poses', 'animations', 'props', 'tools', 'effects'}
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


def determine_compatibility(product_data: dict, figure_names: list) -> dict:
    """ Determines compatible figures by checking multiple fields in order of priority:
    1. The formal 'product_compatibility' string.
    2. The product 'name'.
    3. The product 'description' (only if the caller put one in ``product_data``).

    Returns:
        A dictionary with the clean compatibility string ('new_compatibility') and the
        original compatibility string to be appended to tags ('tags_to_append').
    """
    compat_str = product_data.get('product_compatibility')
    name = product_data.get('product_name')
    description = product_data.get('description', '')

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
            if figure.lower() in name_lower:
                found_figures.add(figure)

    # --- Heuristic 3: If still nothing, check the product description ---
    if not found_figures and description:
        desc_lower = description.lower()
        for figure in figure_names:
            if figure.lower() in desc_lower:
                found_figures.add(figure)

    new_compatibility = ', '.join(sorted(list(found_figures)))

    return {
        'new_compatibility': new_compatibility,
        'tags_to_append': compat_str or ''  # Always use the original string for tags
    }


def _split_list(value) -> list:
    """Split a comma-separated string: strip, drop empties, keep order, dedupe."""
    if not value:
        return []
    seen, out = set(), []
    for part in str(value).split(','):
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return out


def build_view_row(asset_id: str, cms, web, figure_names: list) -> dict:
    """Derive one ``asset_view`` row (minus ``installed``/``enriched_at``/``doc_format_version``).

    ``doc_hash`` is NOT included; the caller computes it with :func:`doc_hash`. The extra
    keys ``artists_list``, ``tags_list`` and ``figures_list`` feed the lookup tables.
    """
    cms = cms or {}
    web = web or {}
    # Legacy ETL never passed the web description into compatibility detection.
    compat = determine_compatibility(cms, figure_names)
    tags = ', '.join(filter(None, [cms.get('categories'), compat['tags_to_append']]))
    figures = compat['new_compatibility']

    cats = determine_categories(cms.get('content_types'))
    category = cats['category']
    subcategories = ','.join(cats['subcategories'])

    # generate_embedding_text crashes on a None description; '' gives identical text.
    text_web = dict(web, description=web.get('description') or '')
    embedding_text = generate_embedding_text(cms, text_web)

    # A migrated (not yet refreshed) CMS payload carries the legacy-derived values. Its
    # embedding text is kept verbatim: the legacy payload lacks ``categories``, so it
    # cannot be re-derived, and the existing vectors were built from exactly this text.
    legacy = cms.get('legacy')
    if legacy:
        tags = legacy.get('tags')
        figures = legacy.get('compatible_figures')
        category = legacy.get('category')
        subcategories = legacy.get('subcategories')
        if 'embedding_text' in legacy:
            embedding_text = legacy['embedding_text']

    artist = cms.get('artists')

    return {
        "asset_id": asset_id,
        "sku": cms.get('sku'),
        "name": cms.get('product_name'),
        "artist": artist,
        "url": web.get('url'),
        "image_url": web.get('image_url'),
        "store": web.get('store'),
        "price": web.get('price'),
        "description": web.get('description'),
        "tags": tags,
        "formats": cms.get('content_types'),
        "poly_count": web.get('poly_count'),
        "textures_info": web.get('textures_info'),
        "required_products": web.get('required_products'),
        "compatible_figures": figures,
        "compatible_software": web.get('compatible_software'),
        "embedding_text": embedding_text,
        "last_updated": cms.get('last_modified_date'),
        "category": category,
        "subcategories": subcategories,
        "styles": None,
        "inferred_tags": None,
        "mature": web.get('mature'),
        "asset_count": cms.get('content_item_count'),
        "artists_list": _split_list(artist),
        "tags_list": _split_list(tags),
        "figures_list": _split_list(figures),
    }


def doc_hash(row: dict) -> str:
    """sha256 over the format version and the embedding text only.

    A vector is stale exactly when the text it was embedded from changed; display and
    filter fields are read from the catalog, never from the vector store.
    """
    material = {"v": DOC_FORMAT_VERSION, "embedding_text": row.get("embedding_text")}
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def payload_hash(payload: dict) -> str:
    """sha256 of the sorted-key JSON of a source payload."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
