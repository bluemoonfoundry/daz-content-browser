import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from api.app_state import APP_MODE
from api.contracts import ChromaDbManagerProtocol, DazScriptServerProtocol, SQLiteDbProtocol
from api.dependencies import (
    get_chroma_db_manager,
    get_daz_script_server,
    get_sqlite_db,
)
from api.schemas.models import QueryRequest, UISearchRequest
from demo_data import (
    DUMMY_PRODUCTS,
)
from demo_data import (
    get_demo_filters as get_demo_filters_mock,
)
from demo_data import (
    get_demo_product as get_demo_product_mock,
)
from demo_data import (
    get_demo_search_results as search_mock,
)
from services.product_service import format_product
from utilities import open_daz_product

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/v1/products")
def list_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=10000),
    category: str | None = None,
    artist: str | None = None,
    compatible_figure: str | None = None,
    sort_by: str = "name",
    sort_dir: str = "asc",
    sqlite_db: SQLiteDbProtocol = Depends(get_sqlite_db),
):
    if APP_MODE == "demo":
        all_products = DUMMY_PRODUCTS
        if category:
            all_products = [p for p in all_products if p["metadata"].get("category") == category]
        if artist:
            all_products = [p for p in all_products if p["metadata"].get("artist") == artist]
        if compatible_figure:
            all_products = [p for p in all_products if compatible_figure in p["metadata"].get("compatible_figures", "")]
        total = len(all_products)
        start = (page - 1) * page_size
        page_products = all_products[start : start + page_size]
        products = [
            {
                "sku": p["id"],
                "name": p["metadata"].get("name"),
                "artist": [p["metadata"].get("artist")] if p["metadata"].get("artist") else [],
                "category": p["metadata"].get("category"),
                "subcategories": [],
                "compatible_figures": [f.strip() for f in p["metadata"].get("compatible_figures", "").split(",") if f.strip()],
                "tags": [t.strip() for t in p["metadata"].get("tags", "").split(",") if t.strip()],
                "store_url": p["metadata"].get("url", ""),
                "image_url": None,
                "is_installed": True,
                "install_date": p["metadata"].get("last_updated"),
                "asset_count": 0,
            }
            for p in page_products
        ]
        import math

        return {
            "products": products,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, math.ceil(total / page_size)),
        }

    result = sqlite_db.get_products(
        page=page,
        page_size=page_size,
        category=category,
        artist=artist,
        compatible_figure=compatible_figure,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    result["products"] = [format_product(r) for r in result["products"]]
    return result


@router.get("/api/v1/products/{sku}")
def get_product(sku: str, sqlite_db: SQLiteDbProtocol = Depends(get_sqlite_db)):
    if APP_MODE == "demo":
        product = get_demo_product_mock(sku)
        if not product:
            raise HTTPException(status_code=404, detail=f"Product '{sku}' not found.")
        return product

    row = sqlite_db.get_sku_row(sku)
    if not row:
        raise HTTPException(status_code=404, detail=f"Product '{sku}' not found in local index.")
    return format_product(row)


@router.get("/api/v1/filters")
def get_filters(sqlite_db: SQLiteDbProtocol = Depends(get_sqlite_db)):
    if APP_MODE == "demo":
        return get_demo_filters_mock()
    return sqlite_db.get_filter_values()


@router.post("/api/v1/search")
def run_search(
    request: UISearchRequest,
    chroma_db_manager: ChromaDbManagerProtocol = Depends(get_chroma_db_manager),
):
    logger.info(f"Search: query={request.query!r} filters={request.filters}")
    if APP_MODE == "demo":
        raw = search_mock(prompt=request.query, limit=request.limit)
        results = [
            {
                "sku": r["id"],
                "name": r["metadata"].get("name"),
                "artist": [r["metadata"].get("artist")] if r["metadata"].get("artist") else [],
                "category": r["metadata"].get("category"),
                "subcategories": [],
                "compatible_figures": [f.strip() for f in r["metadata"].get("compatible_figures", "").split(",") if f.strip()],
                "tags": [t.strip() for t in r["metadata"].get("tags", "").split(",") if t.strip()],
                "store_url": r["metadata"].get("url", ""),
                "image_url": None,
                "is_installed": True,
                "install_date": r["metadata"].get("last_updated"),
                "last_updated": r["metadata"].get("last_updated"),
                "relevance_score": r.get("relevance_score"),
                "asset_count": 0,
            }
            for r in raw.get("results", [])
        ]
        return {"results": results, "total": len(results), "query": request.query, "took_ms": 0}

    f = request.filters
    raw = chroma_db_manager.search(
        prompt=request.query,
        limit=request.limit,
        categories=[f.category] if f and f.category else None,
        artists=[f.artist] if f and f.artist else None,
        compatible_figures=[f.compatible_figures] if f and f.compatible_figures else None,
        score_threshold=1.0,
        sort_by="relevance",
        sort_order="descending",
    )
    results = [
        format_product({**r.get("metadata", {}), "relevance_score": r.get("relevance_score")})
        for r in raw.get("results", [])
    ]
    if request.min_relevance > 0:
        results = [r for r in results if r.get("relevance_score", 0) >= request.min_relevance]
    return {"results": results, "total": len(results), "query": request.query, "took_ms": raw.get("took_ms", 0)}


@router.post("/api/v1/query")
def run_query(
    request: QueryRequest,
    chroma_db_manager: ChromaDbManagerProtocol = Depends(get_chroma_db_manager),
):
    logger.info(f"Query: {request}")
    result = search_mock(**request.model_dump()) if APP_MODE == "demo" else chroma_db_manager.search(**request.model_dump())
    return result


@router.post("/api/v1/products/{sku}/open", status_code=200)
def open_product(
    sku: str,
    sqlite_db: SQLiteDbProtocol = Depends(get_sqlite_db),
    daz_script_server: DazScriptServerProtocol = Depends(get_daz_script_server),
):
    row = sqlite_db.get_sku_row(sku)
    if not row:
        raise HTTPException(status_code=404, detail=f"Product '{sku}' not found.")
    product_name = row.get("name") or sku
    if APP_MODE != "demo" and daz_script_server.is_available():
        try:
            result = daz_script_server.browse_to_product(product_name)
            return {"success": True, "message": f"Opened '{product_name}' via DAZ Script Server.", "via": "plugin", "detail": result}
        except Exception:
            pass

    success = open_daz_product(args=type("obj", (object,), {"product": product_name}))
    if not success:
        raise HTTPException(status_code=500, detail="Failed to open product in DAZ Studio.")
    return {"success": True, "message": f"Opened '{product_name}' via subprocess.", "via": "subprocess"}


@router.get("/api/v1/assets/thumbnail")
def get_asset_thumbnail(path: str):
    asset = Path(path)
    candidates = [asset.with_suffix(".png"), asset.parent / (asset.name + ".png")]
    for candidate in candidates:
        if candidate.exists():
            from fastapi.responses import FileResponse

            return FileResponse(str(candidate), media_type="image/png")
    raise HTTPException(status_code=404, detail="No thumbnail found for this asset.")
