
from pydantic import BaseModel, Field


class UpdateRequest(BaseModel):
    force: bool = False


class SearchFilters(BaseModel):
    category: str | None = None
    artist: str | None = None
    compatible_figures: str | None = None
    is_installed: bool | None = None


class UISearchRequest(BaseModel):
    query: str
    filters: SearchFilters | None = None
    limit: int = 25
    min_relevance: float = 0.0


class QueryRequest(BaseModel):
    prompt: str
    limit: int = 10
    offset: int = 0
    tags: list[str] | None = None
    artists: list[str] | None = None
    categories: list[str] | None = None
    compatible_figures: list[str] | None = None
    score_threshold: float = 1.0
    sort_by: str = "relevance"
    sort_order: str = Field("descending", pattern="^(ascending|descending)$")


class SettingsPayload(BaseModel):
    cms_host: str | None = None
    cms_port: int | None = None
    cms_db: str | None = None
    cms_user: str | None = None
    cms_password: str | None = None
    cms_schema: str | None = None
    embedding_model: str | None = None
    query_model: str | None = None
    daz_script_server_url: str | None = None
    daz_script_server_enabled: bool | None = None


class LoadAssetRequest(BaseModel):
    path: str


class RevealRequest(BaseModel):
    path: str
