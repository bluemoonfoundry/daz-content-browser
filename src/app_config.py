"""Typed application configuration, loaded through one object.

Precedence, lowest to highest:

  1. Built-in defaults (the field defaults below)
  2. ``.env`` in the working directory (``load_dotenv``; never overrides a variable that is
     already set in the process environment)
  3. The process environment
  4. ``settings.json`` beside the source tree (or ``VAB_SETTINGS_PATH``) — only for the
     user-editable settings in ``EDITABLE_SETTINGS`` (written by ``PUT /api/v1/settings``)
  5. ``vab ... --model`` (``EMBEDDING_MODEL_OVERRIDE``), for ``embedding_model`` only

``get_config()`` returns the cached ``AppConfig``. The cache is keyed on the settings.json
stamp (mtime, size) and the snapshot of the environment variables listed here, so a saved
Setting, a hand-edited settings.json, or a changed variable is picked up by the next call
without re-reading anything in between.

When a change takes effect is part of the contract (``EDITABLE_SETTINGS[name].applies``):
``immediate`` settings are applied by the API as soon as they are saved, ``next_index_job``
settings are read when the next index job starts (the status reports a rebuild when the
embedding model changes), and every environment-only field needs a restart of the process
that reads it (``vab server`` / ``vab worker``).
"""
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "BAAI/bge-large-en-v1.5"
SETTINGS_PATH = Path(__file__).parent.parent / "settings.json"
_SETTINGS_ENV = "VAB_SETTINGS_PATH"


def settings_path() -> Path:
    """The settings.json in use: ``VAB_SETTINGS_PATH`` if set, else beside the source tree."""
    override = os.environ.get(_SETTINGS_ENV)
    return Path(override) if override else SETTINGS_PATH


class CmsConfig(BaseModel):
    """DAZ CMS (PostgreSQL) connection. Read-only access; DAZ Studio's ``dzcms`` account."""
    model_config = ConfigDict(frozen=True)

    host: str = "127.0.0.1"
    port: int = 17237
    db: str = "Content"
    user: str = "dzcms"
    password: str = ""
    pool_max: int = 5

    def missing(self) -> list[str]:
        """Settings without which no connection is attempted (empty means configured)."""
        return [name for name, value in (("host", self.host), ("db", self.db),
                                         ("user", self.user)) if not value]


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_mode: Literal["production", "demo"] = "production"
    settings_path: Path = SETTINGS_PATH

    # Storage (environment only; restart required)
    sqlite_db_path: Optional[str] = None
    sqlite_table_name: Optional[str] = None
    chroma_path: str = "chroma_db"
    chroma_collection: str = "daz_products2"
    jobs_db_path: str = "index_jobs.db"

    # Embedding
    embedding_model: str = DEFAULT_MODEL_ID
    embedding_model_dir: Optional[str] = None
    embedding_batch_size: int = 32
    batch_size: int = 512

    # Sources and DAZ Studio
    cms: CmsConfig = CmsConfig()
    daz_script_server_url: str = "http://127.0.0.1:18811"
    daz_studio_exe_path: Optional[str] = None
    scrape_concurrency: int = 10

    # Index jobs
    job_lease_seconds: float = 60.0
    job_max_attempts: int = 3

    # Morphs
    morph_library_path: Optional[str] = None
    morph_index_db_path: str = "morph_index.db"
    morph_cache_path: str = "morph_cache"

    # API security
    api_token: Optional[str] = None
    api_token_file: Optional[str] = None
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    require_token_for_reads: bool = False


# ── User-editable settings ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class EditableSetting:
    path: tuple            # where the value lives in AppConfig
    applies: Literal["immediate", "next_index_job", "restart"]
    description: str


EDITABLE_SETTINGS: dict[str, EditableSetting] = {
    "cms_host": EditableSetting(("cms", "host"), "immediate", "DAZ CMS PostgreSQL host."),
    "cms_port": EditableSetting(("cms", "port"), "immediate", "DAZ CMS PostgreSQL port."),
    "cms_db": EditableSetting(("cms", "db"), "immediate", "DAZ CMS database name."),
    "cms_user": EditableSetting(("cms", "user"), "immediate", "DAZ CMS user."),
    "cms_password": EditableSetting(("cms", "password"), "immediate",
                                    "DAZ CMS password (write-only)."),
    "embedding_model": EditableSetting(
        ("embedding_model",), "next_index_job",
        "Model new index generations are built with. Search keeps using the active "
        "generation's model until a rebuild with the new model is promoted."),
    "daz_script_server_url": EditableSetting(("daz_script_server_url",), "immediate",
                                             "DAZ Script Server plugin URL."),
}

# Accepted in a settings write for older clients, never stored, never applied.
DEPRECATED_SETTINGS: dict[str, str] = {
    "query_model": "Queries always embed with the active index generation's model; "
                   "change embedding_model and rebuild instead. Read-only.",
    "cms_schema": "The DAZ CMS schema is fixed (dzcontent).",
    "daz_script_server_enabled": "The DAZ Script Server plugin is detected automatically.",
}


# ── Loading ────────────────────────────────────────────────────────────────────

# Environment variable -> AppConfig field (dotted for nested), plus a parser.
_ENV: dict[str, tuple[str, type]] = {
    "APP_MODE": ("app_mode", str),
    "SQLITE_DB_PATH": ("sqlite_db_path", str),
    "SQLITE_TABLE_NAME": ("sqlite_table_name", str),
    "CHROMA_PATH": ("chroma_path", str),
    "CHROMA_COLLECTION": ("chroma_collection", str),
    "JOBS_DB_PATH": ("jobs_db_path", str),
    "EMBEDDING_MODEL_ID": ("embedding_model", str),
    "EMBEDDING_MODEL": ("embedding_model", str),     # listed later: wins over _ID
    "EMBEDDING_MODEL_DIR": ("embedding_model_dir", str),
    "EMBEDDING_BATCH_SIZE": ("embedding_batch_size", int),
    "BATCH_SIZE": ("batch_size", int),
    "DB_HOST": ("cms.host", str),
    "DB_PORT": ("cms.port", int),
    "DB_NAME": ("cms.db", str),
    "DB_USER": ("cms.user", str),
    "DB_PASS": ("cms.password", str),
    "DB_POOL_MAX": ("cms.pool_max", int),
    "DAZ_SCRIPT_SERVER_URL": ("daz_script_server_url", str),
    "DAZ_STUDIO_EXE_PATH": ("daz_studio_exe_path", str),
    "ETL_SCRAPE_CONCURRENCY": ("scrape_concurrency", int),
    "VAB_JOB_LEASE_SECONDS": ("job_lease_seconds", float),
    "VAB_JOB_MAX_ATTEMPTS": ("job_max_attempts", int),
    "MORPH_LIBRARY_PATH": ("morph_library_path", str),
    "MORPH_INDEX_DB_PATH": ("morph_index_db_path", str),
    "MORPH_CACHE_PATH": ("morph_cache_path", str),
    "VAB_API_TOKEN": ("api_token", str),
    "VAB_API_TOKEN_FILE": ("api_token_file", str),
    "VAB_ALLOWED_HOSTS": ("allowed_hosts", tuple),
    "VAB_ALLOWED_ORIGINS": ("allowed_origins", tuple),
    "VAB_REQUIRE_TOKEN_FOR_READS": ("require_token_for_reads", bool),
}
_OVERRIDE_ENV = "EMBEDDING_MODEL_OVERRIDE"
# Variables that may be set but blank on purpose (tests blank DB_HOST/DB_NAME so .env
# can't point them at a real CMS); a blank value counts as "set to empty".
_BLANK_ALLOWED = {"DB_HOST", "DB_NAME", "DB_USER", "DB_PASS"}


def _parse(raw: str, kind: type):
    raw = raw.strip()
    if kind is bool:
        return raw.lower() in ("1", "true", "yes")
    if kind is tuple:
        return tuple(x.strip() for x in raw.split(",") if x.strip())
    return kind(raw)


def _set(data: dict, dotted: str, value) -> None:
    *parents, leaf = dotted.split(".")
    for p in parents:
        data = data.setdefault(p, {})
    data[leaf] = value


def read_settings_file(path: Path) -> dict:
    """settings.json as a dict; {} when missing or unreadable (logged)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"Could not read {path}: {e}")
        return {}


def load_config(environ=None, settings_path: Optional[Path] = None) -> AppConfig:
    """Builds an AppConfig from the layers above (no caching)."""
    environ = os.environ if environ is None else environ
    path = Path(settings_path) if settings_path else _settings_path_in(environ)
    data: dict = {"settings_path": path}
    for name, (field, kind) in _ENV.items():
        raw = environ.get(name)
        if raw is None or (raw.strip() == "" and name not in _BLANK_ALLOWED):
            continue
        try:
            _set(data, field, _parse(raw, kind))
        except ValueError:
            logger.warning(f"Ignoring {name}={raw!r}: not a valid {kind.__name__}.")
    env_only = AppConfig.model_validate(_with_override(data, environ))
    for name, value in read_settings_file(path).items():
        setting = EDITABLE_SETTINGS.get(name)
        if setting is None or value is None or value == "":
            continue
        _set(data, ".".join(setting.path), value)
    try:
        return AppConfig.model_validate(_with_override(data, environ))
    except ValidationError as e:
        logger.warning(f"Ignoring {path}: invalid setting(s): {e}")
        return env_only


def _settings_path_in(environ) -> Path:
    override = environ.get(_SETTINGS_ENV)
    return Path(override) if override else SETTINGS_PATH


def _with_override(data: dict, environ) -> dict:
    override = environ.get(_OVERRIDE_ENV)
    if override:
        data = {**data, "embedding_model": override}
    return data


_cache: dict = {}


def _stamp(path: Path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def get_config() -> AppConfig:
    """The current AppConfig; rebuilt only when settings.json or a listed variable changed."""
    path = settings_path()
    key = (str(path), _stamp(path),
           tuple(os.environ.get(name) for name in (*_ENV, _OVERRIDE_ENV)))
    cached = _cache.get("entry")
    if cached is None or cached[0] != key:
        cached = (key, load_config(settings_path=path))
        _cache["entry"] = cached
    return cached[1]


def save_settings(updates: dict, path: Optional[Path] = None) -> None:
    """Merges editable settings into settings.json; unknown and deprecated keys are dropped."""
    path = Path(path) if path else settings_path()
    data = {k: v for k, v in read_settings_file(path).items() if k in EDITABLE_SETTINGS}
    data.update({k: v for k, v in updates.items() if k in EDITABLE_SETTINGS})
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _cache.clear()


def setting_value(config: AppConfig, name: str):
    value = config
    for part in EDITABLE_SETTINGS[name].path:
        value = getattr(value, part)
    return value
