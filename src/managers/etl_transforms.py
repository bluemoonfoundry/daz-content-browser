"""Pure ETL transformation helpers.

These functions are intentionally side-effect free so they can be unit-tested
without touching external services or databases.
"""

from collections import Counter
import logging
import re

logger = logging.getLogger(__name__)

_IGNORE_WORDS = {
    "follower",
    "default",
    "support",
    "preset",
    "people",
    "genesis",
    "genesis 9",
    "genesis 8",
    "genesis 3",
}

_PRIORITY_WORDS = {
    "character",
    "clothes",
    "accessories",
    "environments",
    "hair",
    "poses",
    "animations",
    "props",
    "tools",
    "effects",
}


def determine_categories(content_type_string: str) -> dict:
    """Infer a primary category plus subcategories from raw content type text."""
    if not content_type_string:
        return {"category": None, "subcategories": []}

    words = re.split(r"[^a-zA-Z0-9]+", content_type_string)
    valid_words = [
        w.lower().strip()
        for w in words
        if w.lower().strip() and w.lower().strip() not in _IGNORE_WORDS
    ]
    if not valid_words:
        return {"category": None, "subcategories": []}

    primary_category = next((word for word in valid_words if word in _PRIORITY_WORDS), None)
    if primary_category is None:
        word_counts = Counter(valid_words)
        if word_counts:
            primary_category = word_counts.most_common(1)[0][0]

    if primary_category is None:
        return {"category": None, "subcategories": []}

    unique_words = set(valid_words)
    unique_words.discard(primary_category)
    return {"category": primary_category, "subcategories": sorted(unique_words)}


def determine_compatibility(product_data: dict, figure_names: list[str]) -> dict:
    """Infer compatible figures and preserve original compatibility tags."""
    compat_str = product_data.get("product_compatibility")
    name = product_data.get("product_name")
    description = product_data.get("description", "")

    found_figures: set[str] = set()

    if compat_str:
        compat_lower = compat_str.lower()
        for figure in figure_names:
            if figure.lower() in compat_lower:
                found_figures.add(figure)

    if not found_figures and name:
        name_lower = name.lower()
        for figure in figure_names:
            if figure.lower() in name_lower:
                found_figures.add(figure)

    if not found_figures and description:
        desc_lower = description.lower()
        for figure in figure_names:
            if figure.lower() in desc_lower:
                found_figures.add(figure)

    new_compatibility = ", ".join(sorted(found_figures))
    return {
        "new_compatibility": new_compatibility,
        "tags_to_append": compat_str or "",
    }


def generate_embedding_text(product_data: dict, web_data: dict) -> str:
    """Build a descriptive embedding text from product and scraped metadata."""
    logger.debug("Generating embedding text for: %s", product_data.get("product_name"))

    name = product_data.get("product_name", "a 3D asset")
    artist = product_data.get("artists")
    categories = product_data.get("categories")
    web_desc = web_data.get("description", "").strip()

    parts = [f"A 3D asset package titled '{name}'."]
    if artist:
        parts.append(f"Created by the artist or studio: {artist}.")

    if categories:
        clean_categories = categories.replace(",", ", ")
        parts.append(f"It is categorized under: {clean_categories}.")

        cat_lower = categories.lower()
        if "props" in cat_lower or "decor" in cat_lower:
            parts.append(
                "This is a set of props suitable for decorating digital scenes, environments, and dioramas."
            )
        if "furniture" in cat_lower:
            parts.append("It includes furniture items for interior design and architectural visualization.")
        if "character" in cat_lower:
            parts.append("This is a character asset for digital art and animation.")
        if "hair" in cat_lower:
            parts.append("This is a hairstyle asset for 3D characters.")
        if "wardrobe" in cat_lower or "clothes" in cat_lower:
            parts.append("It contains clothing or wardrobe items for 3D figures.")

    if web_desc:
        parts.append(f"Product Description: {web_desc}")

    return " ".join(parts)
