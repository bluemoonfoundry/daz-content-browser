import subprocess
import sys
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from api.app_state import APP_MODE
from api.contracts import (
    ChromaDbManagerProtocol,
    DazPgAnalyzerProtocol,
    DazScriptServerProtocol,
    SQLiteDbProtocol,
)
from api.dependencies import (
    get_chroma_db_manager,
    get_daz_pg_analyzer,
    get_daz_script_server,
    get_sqlite_db,
)
from api.schemas.models import LoadAssetRequest, RevealRequest, SettingsPayload
from demo_data import get_demo_stats as get_demo_stats_mock
from services.settings_service import load_settings, update_settings
from utilities import open_daz_product

router = APIRouter()


@router.get("/api/v1/settings")
def get_settings():
    s = load_settings()
    s.pop("cms_password", None)
    return s


@router.put("/api/v1/settings")
def save_settings(payload: SettingsPayload):
    return update_settings(payload.model_dump())


@router.post("/api/v1/settings/test-db")
def test_db_connection(payload: SettingsPayload):
    try:
        import psycopg2

        s = load_settings()
        s.update({k: v for k, v in payload.model_dump().items() if v is not None})
        t0 = datetime.now()
        conn = psycopg2.connect(
            host=s["cms_host"],
            port=s["cms_port"],
            dbname=s["cms_db"],
            user=s["cms_user"],
            password=s.get("cms_password", ""),
            connect_timeout=5,
        )
        conn.close()
        ms = int((datetime.now() - t0).total_seconds() * 1000)
        return {"success": True, "message": f"Connected in {ms}ms", "latency_ms": ms}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.get("/api/v1/daz-studio/status")
def get_daz_studio_status(
    daz_script_server: DazScriptServerProtocol = Depends(get_daz_script_server),
):
    if APP_MODE == "demo":
        return {"plugin_detected": False, "plugin_url": None, "version": None}
    return daz_script_server.status()


@router.get("/api/v1/daz-studio/content-dirs")
def get_daz_content_dirs(
    refresh: bool = False,
    daz_script_server: DazScriptServerProtocol = Depends(get_daz_script_server),
):
    if APP_MODE == "demo":
        return {"dirs": [], "source": "demo"}
    if not daz_script_server.is_available():
        raise HTTPException(status_code=503, detail="DAZ Script Server plugin is not running.")
    dirs = daz_script_server.get_content_directories(force=refresh)
    return {"dirs": dirs, "count": len(dirs)}


@router.post("/api/v1/scene/load")
def load_asset_into_scene(
    body: LoadAssetRequest,
    daz_script_server: DazScriptServerProtocol = Depends(get_daz_script_server),
):
    if APP_MODE == "demo":
        return {"success": True, "message": "Demo mode — no action taken."}
    if not daz_script_server.is_available():
        raise HTTPException(status_code=503, detail="DAZ Script Server plugin is not running.")
    try:
        result = daz_script_server.load_asset(body.path)
        return {"success": True, "message": f"Loaded '{body.path}' into scene.", "detail": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/v1/content-roots")
def get_content_roots(
    daz_pg_analyzer: DazPgAnalyzerProtocol | None = Depends(get_daz_pg_analyzer),
):
    if APP_MODE == "demo":
        return {"content_roots": []}
    if daz_pg_analyzer is None:
        raise HTTPException(status_code=500, detail="PostgreSQL analyzer is not configured.")
    roots = daz_pg_analyzer.get_content_roots()
    return {"content_roots": roots}


@router.get("/api/v1/assets/{sku}")
def get_assets(
    sku: str,
    daz_pg_analyzer: DazPgAnalyzerProtocol | None = Depends(get_daz_pg_analyzer),
    daz_script_server: DazScriptServerProtocol = Depends(get_daz_script_server),
):
    if APP_MODE == "demo":
        return {"sku": sku, "files": []}
    if daz_pg_analyzer is None:
        raise HTTPException(status_code=500, detail="PostgreSQL analyzer is not configured.")

    files = daz_pg_analyzer.get_asset_files_by_sku(sku)
    if files is None:
        raise HTTPException(status_code=500, detail="Database error fetching asset files.")

    pg_roots = daz_pg_analyzer.get_content_roots()
    plugin_roots = daz_script_server.get_content_directories() if daz_script_server.is_available() else []
    seen = set()
    all_roots = []
    for r in pg_roots + plugin_roots:
        if r and r not in seen:
            seen.add(r)
            all_roots.append(r)

    from pathlib import Path

    for f in files:
        relative = Path(f["path"].lstrip("/")) / f["filename"]
        f["resolved_path"] = None
        for root in all_roots:
            candidate = Path(root) / relative
            if candidate.exists():
                f["resolved_path"] = candidate.as_posix()
                break

    return {"sku": sku, "files": files}


@router.get("/api/v1/products/{sku}/assets")
def get_product_assets(
    sku: str,
    daz_pg_analyzer: DazPgAnalyzerProtocol | None = Depends(get_daz_pg_analyzer),
    daz_script_server: DazScriptServerProtocol = Depends(get_daz_script_server),
):
    return get_assets(
        sku=sku,
        daz_pg_analyzer=daz_pg_analyzer,
        daz_script_server=daz_script_server,
    )


@router.get("/api/v1/browseproduct/{product_id}")
def browse_product(product_id: str, sqlite_db: SQLiteDbProtocol = Depends(get_sqlite_db)):
    if APP_MODE == "demo":
        return {"status": "ok"}
    row = sqlite_db.get_sku_row(product_id)
    product_name = (row.get("name") if row else None) or product_id
    success = open_daz_product(args=type("obj", (object,), {"product": product_name}))
    if not success:
        raise HTTPException(status_code=500, detail="Failed to open product in DAZ Studio.")
    return {"success": True, "message": f"Opened '{product_name}' in Content Library."}


@router.get("/api/v1/info")
def get_info(
    chroma_db_manager: ChromaDbManagerProtocol = Depends(get_chroma_db_manager),
    daz_pg_analyzer: DazPgAnalyzerProtocol | None = Depends(get_daz_pg_analyzer),
    sqlite_db: SQLiteDbProtocol = Depends(get_sqlite_db),
):
    if APP_MODE == "demo":
        return get_demo_stats_mock()
    if daz_pg_analyzer is None:
        raise HTTPException(status_code=500, detail="PostgreSQL analyzer is not configured.")

    stats = chroma_db_manager.get_db_stats()
    if stats is None:
        raise HTTPException(status_code=404, detail="Database collection not found or empty.")

    postgres_count = daz_pg_analyzer.count_skus()
    sqlite_count = sqlite_db.count()
    stats["total_products_postgres"] = postgres_count
    stats["total_products_sqlite"] = sqlite_count
    stats["new_products"] = max(0, postgres_count - stats["total_docs"])

    if "histograms" in stats and stats["histograms"]:
        for key, counter in stats["histograms"].items():
            stats["histograms"][key] = dict(counter)
    return stats


@router.post("/api/v1/files/reveal")
def reveal_in_explorer(body: RevealRequest):
    from pathlib import Path

    target = Path(body.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {body.path}")
    try:
        if sys.platform == "win32":
            subprocess.Popen(f'explorer /select,"{target}"', shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
