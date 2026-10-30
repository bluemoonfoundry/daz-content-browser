import hashlib

import numpy as np

_DIMS = {"fake/model-a": 8, "fake/model-b": 12}


def fake_embed(texts, is_query=False, model_id=None) -> np.ndarray:
    """Deterministic stand-in for embedding_utils.generate_embeddings."""
    dim = _DIMS.get(model_id, 8)
    single = isinstance(texts, str)
    if single:
        texts = [texts]
    rows = []
    for text in texts:
        digest = hashlib.sha256(f"{model_id}{text}".encode("utf-8")).digest()
        vec = np.array([digest[i % len(digest)] - 127.5 for i in range(dim)], dtype=np.float32)
        rows.append(vec / max(float(np.linalg.norm(vec)), 1e-9))
    out = np.stack(rows).astype(np.float32)
    return out[0] if single else out


# --- DAZ CMS / store scraping fakes (reconciler tests) -------------------------------

FIGS = ["Genesis 8 Female", "Genesis 8.1 Female", "Genesis 9"]


def cms_row(sku, name=None, *, date="2024-01-01T00:00:00", artists="Ann",
            categories="Props, Decor", compat="Genesis 9", content_types="Props",
            count=3, product_id=None) -> dict:
    """One row shaped like DazDBAnalyzer.get_all_products() output (dates as ISO strings)."""
    return {"product_id": product_id if product_id is not None else abs(hash(sku)) % 10_000,
            "product_name": name or f"Product {sku}", "artists": artists, "sku": sku,
            "last_modified_date": date, "product_compatibility": compat,
            "content_item_count": count, "categories": categories,
            "content_types": content_types}


def web_payload(sku, description=None) -> dict:
    return {"url": f"https://www.daz3d.com/{sku.lower()}", "image_url": f"https://img/{sku}.jpg",
            "price": "$9.99", "mature": False,
            "description": description if description is not None else f"About {sku}.",
            "tags": "keyword"}


class FakeScraper:
    """Callable ``scrape(skus) -> {sku: dict | Exception}``; records every call."""

    def __init__(self, fail=(), pages=None):
        self.fail = set(fail)
        self.pages = dict(pages or {})
        self.calls: list = []

    @property
    def scraped(self) -> list:
        return [s for call in self.calls for s in call]

    def __call__(self, skus):
        skus = list(skus)
        self.calls.append(skus)
        return {s: (RuntimeError(f"simulated failure for {s}") if s in self.fail
                    else self.pages.get(s) or web_payload(s)) for s in skus}
