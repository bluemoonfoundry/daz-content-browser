"""Browsing the catalog: listing, details, filter values, product shape."""
from dataclasses import dataclass
from typing import Literal, Optional

from services.catalog_reads import catalog_reads
from services.errors import NotFound

SortBy = Literal["name", "install_date", "last_updated", "artist"]
SortDir = Literal["asc", "desc"]

_INTERNAL_FIELDS = ("embedding_text", "doc_hash", "doc_format_version", "distance", "installed")
_LIST_FIELDS = ("compatible_figures", "subcategories", "tags", "artist")


def split_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def format_product(row: dict) -> dict:
    """A catalog row as the API's Product: internal fields stripped, comma-separated fields
    as lists, ``url`` as ``store_url``, ``installed`` as ``is_installed``."""
    result = {k: v for k, v in row.items() if k not in _INTERNAL_FIELDS}
    for field in _LIST_FIELDS:
        result[field] = split_list(result.get(field))
    result["store_url"] = result.pop("url", None) or ""
    result["install_date"] = result.get("enriched_at")
    result["is_installed"] = bool(row["installed"]) if "installed" in row else True
    result["asset_count"] = result.get("asset_count") or 0
    if "mature" in result:
        result["mature"] = bool(result["mature"])
    return result


@dataclass(frozen=True)
class ProductQuery:
    page: int = 1
    page_size: int = 25
    category: Optional[str] = None
    artist: Optional[str] = None
    compatible_figure: Optional[str] = None
    name_query: Optional[str] = None
    sort_by: SortBy = "name"
    sort_dir: SortDir = "asc"
    installed: Optional[bool] = None


class ProductService:
    """Reads products from a catalog (``CatalogStore``, or the demo catalog)."""

    def __init__(self, catalog):
        self.catalog = catalog

    def list(self, q: ProductQuery) -> dict:
        with catalog_reads(self.catalog):
            result = self.catalog.get_products(
                page=q.page, page_size=q.page_size, category=q.category or None,
                artist=q.artist or None, compatible_figure=q.compatible_figure or None,
                name_query=q.name_query or None, sort_by=q.sort_by, sort_dir=q.sort_dir,
                installed=q.installed)
        result["products"] = [format_product(r) for r in result["products"]]
        return result

    def get(self, sku: str) -> dict:
        with catalog_reads(self.catalog):
            row = self.catalog.get_by_sku(sku)
        if not row:
            raise NotFound(f"Product '{sku}' not found in local index.")
        return format_product(row)

    def name_for(self, sku: str) -> Optional[str]:
        """The product's name, or None when the SKU is unknown."""
        with catalog_reads(self.catalog):
            row = self.catalog.get_by_sku(sku)
        return (row.get("name") or sku) if row else None

    def filters(self) -> dict:
        with catalog_reads(self.catalog):
            return self.catalog.get_filter_values()
