"""Semantic search, shared by the UI endpoint (/search) and the MCP endpoint (/query).

Both run the same search: the same exact filters (OR within a list, AND across lists,
``installed`` on the DAZ CMS presence flag), the same relevance (``1 - cosine distance``),
and the same optional minimum-relevance cut (relevance >= ``min_relevance``; /query's
legacy ``score_threshold``, a maximum cosine distance, is the cut ``1 - score_threshold``).
They differ only in how the result is shaped: /search returns up to ``max_results``
products in one response (the client paginates), /query pages a fixed candidate pool with
offset/limit so ``total_hits`` is stable across pages.
"""
from dataclasses import dataclass, field
from typing import Optional

from catalog.active_index import mcp_response
from services.catalog_reads import catalog_reads
from services.products import format_product

# Fixed candidate pool for /query: total_hits stays stable across offset/limit pages.
QUERY_POOL = 500
MAX_RESULTS = 1000


@dataclass(frozen=True)
class SearchFilters:
    categories: tuple = field(default_factory=tuple)
    artists: tuple = field(default_factory=tuple)
    compatible_figures: tuple = field(default_factory=tuple)
    tags: tuple = field(default_factory=tuple)
    installed: Optional[bool] = None


def _values(value) -> Optional[list]:
    return list(value) if value else None


class SearchService:
    def __init__(self, catalog, active_index):
        self.catalog = catalog
        self.active_index = active_index

    def _rows(self, query: str, filters: SearchFilters, pool: int,
              min_relevance: Optional[float]) -> list:
        with catalog_reads(self.catalog):
            rows = self.active_index.search(
                self.catalog, query, max_results=pool,
                category=_values(filters.categories), artists=_values(filters.artists),
                compatible_figures=_values(filters.compatible_figures),
                tags=_values(filters.tags), installed=filters.installed)
        if min_relevance is None:
            return rows
        return [r for r in rows if r["relevance_score"] >= min_relevance]

    def search(self, query: str, filters: SearchFilters, max_results: int,
               min_relevance: Optional[float] = None) -> list:
        """Products for the nearest assets, closest first, each with ``relevance_score``."""
        return [format_product(r) for r in self._rows(query, filters, max_results, min_relevance)]

    def query(self, prompt: str, filters: SearchFilters, *, limit: int, offset: int,
              min_relevance: Optional[float] = None, sort_by: str = "relevance",
              sort_order: str = "descending") -> dict:
        """The MCP response: hits over the fixed pool, optionally sorted, then paged."""
        rows = self._rows(prompt, filters, QUERY_POOL, min_relevance)
        # The relevance cut is already applied; 2.0 is the largest cosine distance.
        return mcp_response(rows, limit=limit, offset=offset, score_threshold=2.0,
                            sort_by=sort_by, sort_order=sort_order)
