"""Browsing products, filter values, search."""
import logging
import time
from typing import Optional

from fastapi import APIRouter, Query

from api.deps import AUTH, READ, ServicesDep
from api.schemas import (ActionResult, AssetFiles, FilterValuesResponse, Product, ProductPage,
                         QueryRequest, QueryResponse, SearchResponse, UISearchRequest)
from services.products import ProductQuery, SortBy, SortDir
from services.search import SearchFilters

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


@router.get("/products", dependencies=READ, response_model=ProductPage)
def list_products(
    services: ServicesDep,
    page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(25, ge=1, le=10_000),
    category: Optional[str] = Query(None, max_length=255),
    artist: Optional[str] = Query(None, max_length=255),
    compatible_figure: Optional[str] = Query(None, max_length=255),
    name_query: Optional[str] = Query(None, max_length=255),
    sort_by: SortBy = "name",
    sort_dir: SortDir = "asc",
    is_installed: Optional[bool] = None,
):
    """Paginated product listing with exact filters and sort. `is_installed` filters on
    whether the product is currently in the DAZ CMS (removed products stay listed)."""
    return services.products.list(ProductQuery(
        page=page, page_size=page_size, category=category, artist=artist,
        compatible_figure=compatible_figure, name_query=name_query, sort_by=sort_by,
        sort_dir=sort_dir, installed=is_installed))


@router.get("/products/{sku}", dependencies=READ, response_model=Product)
def get_product(sku: str, services: ServicesDep):
    """Full details for a single product."""
    return services.products.get(sku)


@router.get("/products/{sku}/assets", dependencies=READ, response_model=AssetFiles)
def get_product_assets(sku: str, services: ServicesDep):
    """Asset files for a product."""
    return {"sku": sku, "files": services.content.asset_files(sku)}


@router.post("/products/{sku}/open", dependencies=AUTH, response_model=ActionResult)
def open_product(sku: str, services: ServicesDep):
    """Navigates the DAZ Studio Content Library to the product.

    Uses the DAZ Script Server plugin when available; falls back to launching DAZ Studio.
    """
    return services.content.open_product(sku)


@router.get("/filters", dependencies=READ, response_model=FilterValuesResponse)
def get_filters(services: ServicesDep):
    """All valid filter values (categories, artists, compatible figures)."""
    return services.products.filters()


def _list(value) -> tuple:
    if value is None:
        return ()
    return (value,) if isinstance(value, str) else tuple(value)


@router.post("/search", dependencies=READ, response_model=SearchResponse)
def run_search(request: UISearchRequest, services: ServicesDep):
    """UI semantic search. Returns every match up to `max_results` (relevance >=
    `min_relevance`; 0 disables the cut) in one response; the client paginates. Same
    filters and relevance as /query."""
    logger.info(f"Search: query={request.query!r} filters={request.filters}")
    f = request.filters
    filters = SearchFilters() if f is None else SearchFilters(
        categories=_list(f.category), artists=_list(f.artist),
        compatible_figures=_list(f.compatible_figures), tags=_list(f.tags),
        installed=f.is_installed)
    started = time.monotonic()
    results = services.search.search(request.query, filters, request.max_results,
                                     request.min_relevance or None)
    took_ms = int((time.monotonic() - started) * 1000)
    return {"results": results, "total": len(results), "query": request.query, "took_ms": took_ms}


@router.post("/query", dependencies=READ, response_model=QueryResponse)
def run_query(request: QueryRequest, services: ServicesDep):
    """MCP-oriented semantic search: a fixed pool of the nearest products, paged with
    offset/limit (stable `total_hits`). Same filters and relevance as /search."""
    logger.info(f"Query: {request}")
    filters = SearchFilters(
        categories=_list(request.categories), artists=_list(request.artists),
        compatible_figures=_list(request.compatible_figures), tags=_list(request.tags),
        installed=request.is_installed)
    min_relevance = 1.0 - request.score_threshold
    if request.min_relevance is not None:
        min_relevance = max(min_relevance, request.min_relevance)
    return services.search.query(request.prompt, filters, limit=request.limit,
                                 offset=request.offset, min_relevance=min_relevance,
                                 sort_by=request.sort_by, sort_order=request.sort_order)
