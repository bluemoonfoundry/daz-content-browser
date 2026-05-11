def format_product(row: dict) -> dict:
    """Normalize product payload shape for API responses."""
    result = {k: v for k, v in row.items() if k not in ("embedding_text",)}
    for field in ("compatible_figures", "subcategories", "tags"):
        raw = result.get(field) or ""
        if not isinstance(raw, list):
            result[field] = [x.strip() for x in raw.split(",") if x.strip()]
    raw_artist = result.get("artist") or ""
    if not isinstance(raw_artist, list):
        result["artist"] = [x.strip() for x in raw_artist.split(",") if x.strip()]
    result["store_url"] = result.pop("url", None) or ""
    result["install_date"] = result.get("enriched_at")
    result["is_installed"] = True
    result["asset_count"] = 0
    return result
