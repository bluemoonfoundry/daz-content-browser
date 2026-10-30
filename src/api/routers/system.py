"""Settings, auth session, DAZ Studio, content roots, assets and file actions."""
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from api.deps import AUTH, READ, ServicesDep
from api.schemas import (ActionResult, AssetFiles, ContentDirs, ContentRoots, DazStudioStatus,
                         PathRequest, SessionResult, SettingsPayload, SettingsSaved,
                         SettingsTestResult, SettingsView)
from security import SESSION_COOKIE, get_api_token

router = APIRouter(prefix="/api/v1")


# ── Auth ───────────────────────────────────────────────────────────────────────

@router.post("/auth/session", response_model=SessionResult)
def create_session(request: Request, response: Response):
    """Issues the HttpOnly session cookie the browser UI uses (fetches and <img> thumbnails).

    Only browser pages from an allowed origin may call this: the Host/Origin guard has
    already rejected foreign origins, and here the Origin header must be present so a
    cross-site form or <img> without one can't plant the cookie. Non-browser clients use
    `Authorization: Bearer <token>` from the token file instead.
    """
    if not request.headers.get("origin"):
        raise HTTPException(status_code=403, detail="Session requires a browser Origin header.")
    response.set_cookie(SESSION_COOKIE, get_api_token(), httponly=True, samesite="strict",
                        path="/api/")
    return {"authenticated": True}


# ── Settings ───────────────────────────────────────────────────────────────────

@router.get("/settings", dependencies=AUTH, response_model=SettingsView)
def get_settings(services: ServicesDep):
    """The effective settings (env + settings.json), minus the CMS password —
    `cms_password_set` reports whether one is configured. `applies` says when a saved
    change takes effect."""
    return services.settings.public()


@router.put("/settings", dependencies=AUTH, response_model=SettingsSaved)
def save_settings(payload: SettingsPayload, services: ServicesDep):
    """Saves settings to settings.json. CMS connection and plugin URL changes apply
    immediately; an embedding model change applies to the next index job (status then
    reports the rebuild it needs). The response lists what changed and how it applies."""
    return services.settings.save(payload.model_dump())


@router.post("/settings/test-db", dependencies=AUTH, response_model=SettingsTestResult)
def test_db_connection(payload: SettingsPayload, services: ServicesDep):
    """Tests a DAZ CMS connection with the saved settings overlaid with the given ones."""
    return services.settings.test_cms(payload.model_dump())


# ── DAZ Studio ─────────────────────────────────────────────────────────────────

@router.get("/daz-studio/status", dependencies=READ, response_model=DazStudioStatus)
def get_daz_studio_status(services: ServicesDep):
    """Probes the DAZ Script Server plugin to check if it's running."""
    return services.content.plugin_status()


@router.get("/daz-studio/content-dirs", dependencies=READ, response_model=ContentDirs)
def get_daz_content_dirs(services: ServicesDep, refresh: bool = False):
    """Content library directories from the running DAZ Studio instance."""
    return services.content.content_dirs(refresh)


@router.post("/scene/load", dependencies=AUTH, response_model=ActionResult)
def load_asset_into_scene(body: PathRequest, services: ServicesDep):
    """Loads an asset file into the current DAZ Studio scene via the DAZ Script Server
    plugin. The path must resolve beneath a registered content root."""
    return services.content.load_into_scene(body.path)


# ── Content roots, assets, files ───────────────────────────────────────────────

@router.get("/content-roots", dependencies=READ, response_model=ContentRoots)
def get_content_roots(services: ServicesDep):
    return {"content_roots": services.content.cms_content_roots()}


@router.get("/assets/thumbnail", dependencies=AUTH, response_class=FileResponse,
            responses={200: {"content": {"image/png": {}}}, 404: {"description": "No thumbnail"}})
def get_asset_thumbnail(path: str, services: ServicesDep):
    """Serves the companion thumbnail PNG for a DAZ asset file.

    DAZ Studio places thumbnails alongside content files with the same stem and a .png
    extension (e.g. 'FN Ethan.duf' → 'FN Ethan.png'). Returns 404 when no thumbnail is
    found so the UI can show its placeholder. Both the asset path and the served PNG must
    resolve beneath a registered content root.

    Declared before /assets/{sku}, which would otherwise match "thumbnail" as a SKU.
    """
    return FileResponse(str(services.content.thumbnail(path)), media_type="image/png")


@router.get("/assets/{sku}", dependencies=READ, response_model=AssetFiles)
def get_assets(sku: str, services: ServicesDep):
    """Asset file paths for a SKU, with resolved absolute paths where available."""
    return {"sku": sku, "files": services.content.asset_files(sku)}


@router.get("/browseproduct/{product_id}", dependencies=AUTH, response_model=ActionResult,
            deprecated=True)
def browse_product(product_id: str, services: ServicesDep):
    """Deprecated: use POST /products/{sku}/open."""
    return services.content.open_product(product_id, require_known=False)


@router.post("/files/reveal", dependencies=AUTH, response_model=ActionResult)
def reveal_in_explorer(body: PathRequest, services: ServicesDep):
    """Opens the system file explorer with the given file selected. The path must resolve
    beneath a registered content root."""
    return services.content.reveal(body.path)
