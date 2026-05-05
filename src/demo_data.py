import random
from collections import Counter

DUMMY_PRODUCTS = [
    {
        "id": "85227",
        "distance": 0.15,
        "relevance_score": 0.85,
        "metadata": {
            "name": "Genesis 9 Starter Essentials",
            "artist": "Daz Originals",
            "category": "character",
            "compatible_figures": "Genesis 9",
            "url": "https://...",
            "tags": "genesis 9, character, essentials",
            "last_updated": "2023-10-28T10:00:00Z",
        },
    },
    {
        "id": "91123",
        "distance": 0.28,
        "relevance_score": 0.72,
        "metadata": {
            "name": "Cyberpunk Gunslinger Outfit",
            "artist": "Val-Z",
            "category": "clothes",
            "compatible_figures": "Genesis 9",
            "url": "https://...",
            "tags": "sci-fi, cyberpunk, outfit",
            "last_updated": "2023-10-27T14:30:00Z",
        },
    },
    {
        "id": "88441",
        "distance": 0.41,
        "relevance_score": 0.59,
        "metadata": {
            "name": "Modern Urban Street Environment",
            "artist": "Stonemason",
            "category": "environments",
            "compatible_figures": "",
            "url": "https://...",
            "tags": "environment, city, urban",
            "last_updated": "2023-10-26T09:15:00Z",
        },
    },
    {
        "id": "90567",
        "distance": 0.55,
        "relevance_score": 0.45,
        "metadata": {
            "name": "Ancient Wizard Staffs",
            "artist": "MerlinProps",
            "category": "props",
            "compatible_figures": "",
            "url": "https://...",
            "tags": "fantasy, wizard, props",
            "last_updated": "2023-10-25T11:00:00Z",
        },
    },
]


def get_demo_stats():
    tag_counter = Counter(
        tag.strip()
        for p in DUMMY_PRODUCTS
        for tag in p["metadata"]["tags"].split(",")
    )
    artist_counter = Counter(p["metadata"]["artist"] for p in DUMMY_PRODUCTS)
    category_counter = Counter(p["metadata"]["category"] for p in DUMMY_PRODUCTS)
    figure_counter = Counter(
        fig.strip()
        for p in DUMMY_PRODUCTS
        for fig in p["metadata"]["compatible_figures"].split(",")
        if fig.strip()
    )
    return {
        "total_docs": len(DUMMY_PRODUCTS),
        "last_update": max(p["metadata"]["last_updated"] for p in DUMMY_PRODUCTS),
        "total_products_postgres": len(DUMMY_PRODUCTS),
        "total_products_sqlite": len(DUMMY_PRODUCTS),
        "new_products": 0,
        "histograms": {
            "tags": dict(tag_counter),
            "artists": dict(artist_counter),
            "compatible_figures": dict(figure_counter),
            "categories": dict(category_counter),
        },
    }


def get_demo_search_results(prompt, limit=10, offset=0, **kwargs):
    shuffled = random.sample(DUMMY_PRODUCTS, k=len(DUMMY_PRODUCTS))
    paginated_results = shuffled[offset: offset + limit]
    return {
        "total_hits": len(DUMMY_PRODUCTS),
        "limit": limit,
        "offset": offset,
        "results": paginated_results,
    }


def get_demo_status():
    n = len(DUMMY_PRODUCTS)
    return {
        "status": "ok",
        "postgres_connected": False,
        "sqlite_connected": True,
        "chromadb_connected": True,
        "postgres_count": 0,
        "sqlite_count": n,
        "chromadb_count": n,
        "new_products": 0,
        "update_available": False,
    }


def get_demo_product(sku: str):
    match = next((p for p in DUMMY_PRODUCTS if p["id"] == sku), None)
    if not match:
        return None
    m = match["metadata"]
    return {
        "sku": match["id"],
        "name": m.get("name"),
        "artist": m.get("artist"),
        "category": m.get("category"),
        "subcategories": [],
        "compatible_figures": [f.strip() for f in m.get("compatible_figures", "").split(",") if f.strip()],
        "tags": [t.strip() for t in m.get("tags", "").split(",") if t.strip()],
        "url": m.get("url"),
        "image_url": None,
        "price": None,
        "description": None,
        "formats": None,
        "mature": False,
        "last_updated": m.get("last_updated"),
    }


def get_demo_filters():
    categories = sorted({p["metadata"]["category"] for p in DUMMY_PRODUCTS})
    artists = sorted({p["metadata"]["artist"] for p in DUMMY_PRODUCTS})
    figures = sorted({
        f.strip()
        for p in DUMMY_PRODUCTS
        for f in p["metadata"]["compatible_figures"].split(",")
        if f.strip()
    })
    return {"categories": categories, "artists": artists, "compatible_figures": figures}
