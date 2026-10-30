"""Demo mode: an in-memory catalog and index with the same interface (and filter semantics)
as CatalogStore and ActiveIndex, so demo responses go through the same services and have
exactly the production shapes."""
import re
from typing import Optional

_ROWS = [
    {"sku": "85227", "name": "Genesis 9 Starter Essentials", "artist": "Daz Originals",
     "category": "character", "compatible_figures": "Genesis 9",
     "tags": "genesis 9, character, essentials", "last_updated": "2023-10-28T10:00:00Z",
     "installed": 1},
    {"sku": "91123", "name": "Cyberpunk Gunslinger Outfit", "artist": "Val-Z",
     "category": "clothes", "compatible_figures": "Genesis 9",
     "tags": "sci-fi, cyberpunk, outfit", "last_updated": "2023-10-27T14:30:00Z",
     "installed": 1},
    {"sku": "88441", "name": "Modern Urban Street Environment", "artist": "Stonemason",
     "category": "environments", "compatible_figures": "",
     "tags": "environment, city, urban", "last_updated": "2023-10-26T09:15:00Z",
     "installed": 0},
    {"sku": "90567", "name": "Ancient Wizard Staffs", "artist": "MerlinProps",
     "category": "props", "compatible_figures": "",
     "tags": "fantasy, wizard, props", "last_updated": "2023-10-25T11:00:00Z",
     "installed": 1},
]

_SORT = {"name": "name", "install_date": "enriched_at", "last_updated": "last_updated",
         "artist": "artist"}


def _row(r: dict) -> dict:
    return {"asset_id": f"daz:{r['sku']}", "url": f"https://www.daz3d.com/?sku={r['sku']}",
            "image_url": None, "store": "daz", "price": None, "description": None,
            "formats": None, "poly_count": None, "textures_info": None,
            "required_products": None, "compatible_software": None, "subcategories": "",
            "styles": None, "inferred_tags": None, "enriched_at": r["last_updated"],
            "mature": 0, "asset_count": 0, **r}


def _split(raw) -> list:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def _as_list(value) -> list:
    if value is None:
        return []
    return [value] if isinstance(value, str) else [v for v in value if v]


class DemoCatalog:
    db_path = None
    migration_error = None

    def __init__(self):
        self.rows = [_row(r) for r in _ROWS]

    def _matching(self, category=None, artists=None, compatible_figures=None, tags=None,
                  installed: Optional[bool] = None, name_query: Optional[str] = None) -> list:
        cats, arts, figs, tgs = map(_as_list, (category, artists, compatible_figures, tags))
        out = []
        for r in self.rows:
            if cats and r["category"] not in cats:
                continue
            if arts and not set(arts) & set(_split(r["artist"])):
                continue
            if figs and not {f.strip() for f in figs} & set(_split(r["compatible_figures"])):
                continue
            if tgs and not set(tgs) & set(_split(r["tags"])):
                continue
            if installed is not None and bool(r["installed"]) != installed:
                continue
            if name_query and name_query.lower() not in r["name"].lower():
                continue
            out.append(r)
        return out

    def asset_ids_matching(self, category=None, artists=None, compatible_figures=None,
                           tags=None, installed: Optional[bool] = None) -> Optional[set]:
        if not any((_as_list(category), _as_list(artists), _as_list(compatible_figures),
                    _as_list(tags))) and installed is None:
            return None
        return {r["asset_id"] for r in self._matching(category, artists, compatible_figures,
                                                      tags, installed)}

    def get_products(self, page=1, page_size=25, category=None, artist=None,
                     compatible_figure=None, name_query=None, sort_by="name", sort_dir="asc",
                     installed=None) -> dict:
        rows = self._matching(category, artist, compatible_figure, None, installed, name_query)
        col = _SORT.get(sort_by, "name")
        rows = sorted(rows, key=lambda r: (r[col] or "", r["asset_id"]),
                      reverse=sort_dir.lower() == "desc")
        total = len(rows)
        start = (page - 1) * page_size
        return {"products": [dict(r) for r in rows[start:start + page_size]], "total": total,
                "page": page, "page_size": page_size,
                "total_pages": max(1, (total + page_size - 1) // page_size)}

    def get_by_sku(self, sku: str) -> Optional[dict]:
        return next((dict(r) for r in self.rows if r["sku"] == sku), None)

    def get_rows(self, asset_ids: list) -> list:
        by_id = {r["asset_id"]: r for r in self.rows}
        return [dict(by_id[a]) for a in asset_ids if a in by_id]

    def get_filter_values(self) -> dict:
        return {
            "categories": sorted({r["category"] for r in self.rows if r["category"]}),
            "artists": sorted({a for r in self.rows for a in _split(r["artist"])}),
            "compatible_figures": sorted({f for r in self.rows
                                          for f in _split(r["compatible_figures"])}),
        }

    def count(self) -> int:
        return len(self.rows)

    def get_last_updated(self) -> str:
        return max(r["last_updated"] for r in self.rows)


class DemoIndex:
    """Ranks demo products by how many query words appear in their name, tags and category."""

    def search(self, catalog: DemoCatalog, query: str, *, max_results: int, category=None,
               artists=None, compatible_figures=None, tags=None,
               installed: Optional[bool] = None) -> list:
        allowed = catalog.asset_ids_matching(category=category, artists=artists,
                                             compatible_figures=compatible_figures, tags=tags,
                                             installed=installed)
        words = set(re.findall(r"\w+", query.lower()))
        scored = []
        for row in catalog.rows:
            if allowed is not None and row["asset_id"] not in allowed:
                continue
            text = set(re.findall(r"\w+", f"{row['name']} {row['tags']} {row['category']}".lower()))
            overlap = len(words & text) / len(words) if words else 0.0
            scored.append((round(0.6 - 0.5 * overlap, 4), row["asset_id"]))
        scored.sort()
        rows = catalog.get_rows([a for _, a in scored[:max_results]])
        for (distance, _), row in zip(scored, rows):
            row["distance"] = distance
            row["relevance_score"] = round(1.0 - distance, 4)
        return rows


class DemoStatus:
    """StatusService's interface over the demo catalog: always healthy, no CMS."""

    def __init__(self, catalog: DemoCatalog):
        self.catalog = catalog

    def status(self) -> dict:
        n = self.catalog.count()
        return {
            "status": "ok", "postgres_connected": False, "sqlite_connected": True,
            "chromadb_connected": True, "postgres_count": 0, "sqlite_count": n,
            "chromadb_count": n, "new_products": 0, "update_available": False,
            "repair": None, "index": None,
        }

    def health(self) -> dict:
        return {"status": "ok", "mode": "demo", "components": {}}

    def info(self) -> dict:
        n = self.catalog.count()
        return {"total_docs": n, "last_update": self.catalog.get_last_updated(),
                "histograms": self.catalog.get_filter_values(), "total_products_postgres": 0,
                "total_products_sqlite": n, "new_products": 0}
