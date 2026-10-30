"""Request and response models of the HTTP API (the OpenAPI contract).

Every endpoint declares its response model, so the OpenAPI schema and its examples are
what the API actually returns (tests/test_api_contract.py checks the examples). Request
models bound every size, count and score a client can send.
"""
from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from services.jobs import UpdateMode
from services.search import MAX_RESULTS, QUERY_POOL

_MAX_FILTER_VALUES = 50
_MAX_QUERY_CHARS = 1000

FilterValues = Union[str, List[str]]


# ── Products ───────────────────────────────────────────────────────────────────

_PRODUCT_EXAMPLE = {
    "asset_id": "daz:85227", "sku": "85227", "name": "Genesis 9 Starter Essentials",
    "artist": ["Daz Originals"], "category": "character", "subcategories": [],
    "compatible_figures": ["Genesis 9"], "tags": ["genesis 9", "character", "essentials"],
    "store_url": "https://www.daz3d.com/?sku=85227", "image_url": None, "store": "daz",
    "price": None, "description": None, "formats": None, "poly_count": None,
    "textures_info": None, "required_products": None, "compatible_software": None,
    "styles": None, "inferred_tags": None, "mature": False,
    "last_updated": "2023-10-28T10:00:00Z", "enriched_at": "2023-10-28T10:00:00Z",
    "install_date": "2023-10-28T10:00:00Z", "is_installed": True, "asset_count": 0,
}


class Product(BaseModel):
    """A catalog product. ``is_installed``: currently present in the DAZ CMS (products
    removed from it stay listed). ``relevance_score`` (1 - cosine distance) is set on
    search results only."""
    model_config = ConfigDict(json_schema_extra={"examples": [_PRODUCT_EXAMPLE]})

    asset_id: Optional[str] = None
    sku: Optional[str] = None
    name: Optional[str] = None
    artist: List[str] = []
    category: Optional[str] = None
    subcategories: List[str] = []
    compatible_figures: List[str] = []
    tags: List[str] = []
    store_url: str = ""
    image_url: Optional[str] = None
    store: Optional[str] = None
    price: Optional[str] = None
    description: Optional[str] = None
    formats: Optional[str] = None
    poly_count: Optional[str] = None
    textures_info: Optional[str] = None
    required_products: Optional[str] = None
    compatible_software: Optional[str] = None
    styles: Optional[str] = None
    inferred_tags: Optional[str] = None
    mature: Optional[bool] = None
    last_updated: Optional[str] = None
    enriched_at: Optional[str] = None
    install_date: Optional[str] = None
    is_installed: bool = True
    asset_count: int = 0
    relevance_score: Optional[float] = None


class ProductPage(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "products": [_PRODUCT_EXAMPLE], "total": 1, "page": 1, "page_size": 25,
        "total_pages": 1}]})

    products: List[Product]
    total: int
    page: int
    page_size: int
    total_pages: int


class FilterValuesResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "categories": ["character", "clothes"], "artists": ["Daz Originals", "Val-Z"],
        "compatible_figures": ["Genesis 9"]}]})

    categories: List[str]
    artists: List[str]
    compatible_figures: List[str]


# ── Search ─────────────────────────────────────────────────────────────────────

class UISearchFilters(BaseModel):
    """Exact-match filters: OR within a list, AND across filters. A single string is a
    one-value list. ``is_installed`` filters on presence in the DAZ CMS."""
    category: Optional[FilterValues] = None
    artist: Optional[FilterValues] = None
    compatible_figures: Optional[FilterValues] = None
    tags: Optional[FilterValues] = None
    is_installed: Optional[bool] = None


class UISearchRequest(BaseModel):
    """``min_relevance`` (1 - cosine distance) of 0 means no cut."""
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "query": "fantasy dress", "filters": {"compatible_figures": "Genesis 9",
                                              "is_installed": True},
        "max_results": 500, "min_relevance": 0.0}]})

    query: str = Field(min_length=1, max_length=_MAX_QUERY_CHARS)
    filters: Optional[UISearchFilters] = None
    max_results: int = Field(500, ge=1, le=MAX_RESULTS)
    min_relevance: float = Field(0.0, ge=0.0, le=1.0)


class SearchResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "results": [{**_PRODUCT_EXAMPLE, "relevance_score": 0.9}], "total": 1,
        "query": "genesis 9 character", "took_ms": 12}]})

    results: List[Product]
    total: int
    query: str
    took_ms: int


_MCP_SORT_FIELDS = Literal["relevance", "sku", "url", "image_url", "name", "artist",
                           "compatible_figures", "tags", "category", "subcategories",
                           "asset_count"]


class QueryRequest(BaseModel):
    """MCP-oriented search over a fixed pool of the 500 nearest products, paged with
    offset/limit. Same filters and relevance as /search. ``score_threshold`` is the legacy
    cut as a maximum cosine distance (``min_relevance = 1 - score_threshold``); when both
    are given the stricter one applies."""
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "prompt": "sci-fi outfit", "limit": 10, "offset": 0, "compatible_figures": ["Genesis 9"],
        "is_installed": True, "sort_by": "relevance", "sort_order": "descending"}]})

    prompt: str = Field(min_length=1, max_length=_MAX_QUERY_CHARS)
    limit: int = Field(10, ge=1, le=100)
    offset: int = Field(0, ge=0, le=QUERY_POOL)
    tags: Optional[List[str]] = Field(None, max_length=_MAX_FILTER_VALUES)
    artists: Optional[List[str]] = Field(None, max_length=_MAX_FILTER_VALUES)
    categories: Optional[List[str]] = Field(None, max_length=_MAX_FILTER_VALUES)
    compatible_figures: Optional[List[str]] = Field(None, max_length=_MAX_FILTER_VALUES)
    is_installed: Optional[bool] = None
    score_threshold: float = Field(1.0, ge=0.0, le=2.0)
    min_relevance: Optional[float] = Field(None, ge=-1.0, le=1.0)
    sort_by: _MCP_SORT_FIELDS = "relevance"
    sort_order: Literal["ascending", "descending"] = "descending"


class QueryHitMetadata(BaseModel):
    sku: str
    url: str
    image_url: str
    name: str
    artist: str
    compatible_figures: str
    tags: str
    category: str
    subcategories: str
    asset_count: int


class QueryHit(BaseModel):
    id: str
    distance: float
    relevance_score: float
    metadata: QueryHitMetadata


class QueryResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "total_hits": 1, "limit": 10, "offset": 0, "results": [{
            "id": "91123", "distance": 0.1, "relevance_score": 0.9, "metadata": {
                "sku": "91123", "url": "https://www.daz3d.com/?sku=91123", "image_url": "",
                "name": "Cyberpunk Gunslinger Outfit", "artist": "Val-Z",
                "compatible_figures": "Genesis 9", "tags": "sci-fi, cyberpunk, outfit",
                "category": "clothes", "subcategories": "", "asset_count": 0}}]}]})

    total_hits: int
    limit: int
    offset: int
    results: List[QueryHit]


# ── Status, health, info ───────────────────────────────────────────────────────

class StatusResponse(BaseModel):
    """Legacy top-level keys (kept) plus ``index``, the reconciliation-backed detail
    (null in demo mode)."""
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "status": "ok", "postgres_connected": True, "sqlite_connected": True,
        "chromadb_connected": True, "postgres_count": 4, "sqlite_count": 4,
        "chromadb_count": 4, "new_products": 0, "update_available": False, "repair": None,
        "index": None}]})

    status: Literal["ok", "degraded"]
    postgres_connected: bool
    sqlite_connected: bool
    chromadb_connected: bool
    postgres_count: int
    sqlite_count: int
    chromadb_count: int
    new_products: int
    update_available: bool
    repair: Optional[str] = None
    index: Optional[dict] = None


class HealthResponse(BaseModel):
    """Per-component health; each component has ``ok`` and, when not ok, ``repair``."""
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "status": "ok", "mode": "demo", "components": {}}]})

    status: Literal["ok", "degraded"]
    mode: str
    components: dict[str, dict[str, Any]]


class InfoResponse(BaseModel):
    """Totals and filter values (``histograms`` lists each filter's values)."""
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "total_docs": 4, "last_update": "2023-10-28T10:00:00Z",
        "histograms": {"categories": ["character"], "artists": ["Daz Originals"],
                       "compatible_figures": ["Genesis 9"]},
        "total_products_postgres": 4, "total_products_sqlite": 4, "new_products": 0}]})

    total_docs: int
    last_update: str
    histograms: FilterValuesResponse
    total_products_postgres: int
    total_products_sqlite: int
    new_products: int


# ── Index jobs ─────────────────────────────────────────────────────────────────

class UpdateRequest(BaseModel):
    """``mode``: update (incremental), rebuild (new index generation from SQLite, built
    beside the live one), or repair (quarantine an unopenable store, then rebuild from
    SQLite). ``force: true`` with no mode re-fetches every product's store data, then
    rebuilds; it never deletes SQLite."""
    force: bool = False
    mode: Optional[UpdateMode] = None


class Job(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: str
    params: Any = None
    source: str
    status: str
    stage: str = ""
    progress: str = ""
    error: Optional[str] = None
    cancel_requested: bool = False
    attempt: int = 0
    max_attempts: int
    worker_id: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    heartbeat_at: Optional[str] = None


class JobList(BaseModel):
    jobs: List[Job]


class UpdateStatus(BaseModel):
    """The job in the legacy polling shape: ``running``/``progress``/``stage``/``error``/
    ``last_run``, plus the job's fields (``task_id``, ``status``, ...) once one exists."""
    model_config = ConfigDict(extra="allow", json_schema_extra={"examples": [{
        "running": False, "progress": "", "stage": "", "error": None, "last_run": None}]})

    running: bool
    progress: str = ""
    stage: str = ""
    error: Optional[str] = None
    last_run: Optional[str] = None
    task_id: Optional[str] = None
    task_status: Optional[int] = None
    status: Optional[str] = None


class UpdateStarted(BaseModel):
    """``mode`` is null when a job was already active (its id is ``task_id``)."""
    message: str
    task_id: Optional[str] = None
    mode: Optional[UpdateMode] = None


class CancelResult(BaseModel):
    message: str
    task_id: Optional[str] = None
    status: Optional[str] = None


# ── Settings ───────────────────────────────────────────────────────────────────

class SettingsPayload(BaseModel):
    """Settings to save; omitted (or null) fields are unchanged, and so is a blank
    password. ``query_model``, ``cms_schema`` and ``daz_script_server_enabled`` are
    deprecated: accepted, ignored, and listed in ``ignored``."""
    cms_host: Optional[str] = Field(None, max_length=255)
    cms_port: Optional[int] = Field(None, ge=1, le=65535)
    cms_db: Optional[str] = Field(None, max_length=255)
    cms_user: Optional[str] = Field(None, max_length=255)
    cms_password: Optional[str] = Field(None, max_length=1024)
    embedding_model: Optional[str] = Field(None, min_length=1, max_length=255)
    daz_script_server_url: Optional[str] = Field(None, max_length=2048)
    query_model: Optional[str] = Field(None, max_length=255)
    cms_schema: Optional[str] = Field(None, max_length=255)
    daz_script_server_enabled: Optional[bool] = None


_SETTINGS_EXAMPLE = {
    "cms_host": "127.0.0.1", "cms_port": 17237, "cms_db": "Content", "cms_user": "dzcms",
    "cms_password_set": False, "embedding_model": "BAAI/bge-large-en-v1.5",
    "daz_script_server_url": "http://127.0.0.1:18811", "query_model": "BAAI/bge-large-en-v1.5",
    "applies": {"cms_host": "immediate", "cms_port": "immediate", "cms_db": "immediate",
                "cms_user": "immediate", "cms_password": "immediate",
                "embedding_model": "next_index_job", "daz_script_server_url": "immediate"},
    "deprecated": {"query_model": "...", "cms_schema": "...", "daz_script_server_enabled": "..."},
}


class SettingsView(BaseModel):
    """The effective settings (env + settings.json). ``applies`` says when a saved change
    takes effect: ``immediate``, ``next_index_job`` or ``restart``. ``query_model`` is
    read-only: the active index generation's model, which every query embeds with."""
    model_config = ConfigDict(json_schema_extra={"examples": [_SETTINGS_EXAMPLE]})

    cms_host: str
    cms_port: int
    cms_db: str
    cms_user: str
    cms_password_set: bool
    embedding_model: str
    daz_script_server_url: str
    query_model: str
    applies: dict[str, Literal["immediate", "next_index_job", "restart"]]
    deprecated: dict[str, str]


class SettingsSaved(SettingsView):
    """The saved settings, plus which ones changed and how each change takes effect."""
    model_config = ConfigDict(json_schema_extra={"examples": [{
        **_SETTINGS_EXAMPLE, "changed": ["cms_host"], "applied": ["cms_host"],
        "applies_on_next_index_job": [], "restart_required": [], "ignored": []}]})

    changed: List[str]
    applied: List[str]
    applies_on_next_index_job: List[str]
    restart_required: List[str]
    ignored: List[str]


class SettingsTestResult(BaseModel):
    success: bool
    message: str
    latency_ms: Optional[int] = None


# ── DAZ Studio, assets, files ──────────────────────────────────────────────────

class DazStudioStatus(BaseModel):
    model_config = ConfigDict(extra="allow", json_schema_extra={"examples": [{
        "plugin_detected": False, "plugin_url": None, "version": None}]})

    plugin_detected: bool
    plugin_url: Optional[str] = None
    version: Optional[str] = None
    auth_enabled: Optional[bool] = None
    active_requests: int = 0


class ContentDirs(BaseModel):
    dirs: List[str]
    count: int
    source: Literal["plugin", "demo"]


class ContentRoots(BaseModel):
    content_roots: List[str]


class AssetFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str
    filename: str
    content_type: Optional[str] = None
    resolved_path: Optional[str] = None


class AssetFiles(BaseModel):
    sku: str
    files: List[AssetFile]


class PathRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class ActionResult(BaseModel):
    success: bool
    message: Optional[str] = None
    via: Optional[Literal["plugin", "subprocess", "demo"]] = None
    detail: Any = None


class SessionResult(BaseModel):
    authenticated: bool
