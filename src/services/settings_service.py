import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SETTINGS_PATH = Path(__file__).parent.parent.parent / "settings.json"
SETTINGS_DEFAULTS = {
    "cms_host": os.getenv("DB_HOST", "localhost"),
    "cms_port": int(os.getenv("DB_PORT", "17237")),
    "cms_db": os.getenv("DB_NAME", "dzcms"),
    "cms_user": os.getenv("DB_USER", ""),
    "cms_password": os.getenv("DB_PASS", ""),
    "cms_schema": os.getenv("DB_SCHEMA", "dzcontent"),
    "embedding_model": os.getenv("EMBEDDING_MODEL", "mixedbread-ai/mxbai-embed-large-v1"),
    "query_model": os.getenv("QUERY_MODEL", "mixedbread-ai/mxbai-embed-large-v1"),
    "daz_script_server_url": os.getenv("DAZ_SCRIPT_SERVER_URL", "http://localhost:18811"),
    "daz_script_server_enabled": os.getenv("DAZ_SCRIPT_SERVER_ENABLED", "false").lower() == "true",
}


def load_settings() -> dict:
    base = dict(SETTINGS_DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            overrides = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            base.update(overrides)
        except Exception as e:
            logger.warning(f"Could not load settings.json: {e}")
    return base


def save_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def update_settings(patch: dict) -> dict:
    existing = {}
    if SETTINGS_PATH.exists():
        try:
            existing = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    updates = {k: v for k, v in patch.items() if v is not None}
    existing.update(updates)
    save_settings(existing)
    return load_settings()
