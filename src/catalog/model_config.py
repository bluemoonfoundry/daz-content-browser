import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "BAAI/bge-large-en-v1.5"
SETTINGS_PATH = Path(__file__).parent.parent.parent / "settings.json"


def configured_model_id(settings_path: str | None = None) -> str:
    """Resolve the embedding model: CLI override -> settings.json -> EMBEDDING_MODEL
    -> EMBEDDING_MODEL_ID -> default.

    `vab ... --model` sets EMBEDDING_MODEL_OVERRIDE, which beats settings.json for that process.
    """
    override = os.getenv("EMBEDDING_MODEL_OVERRIDE")
    if override:
        return override
    path = Path(settings_path) if settings_path else SETTINGS_PATH
    try:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8")).get("embedding_model")
            if isinstance(value, str) and value:
                return value
    except Exception as e:
        logger.warning(f"Could not read embedding_model from {path}: {e}")
    return os.getenv("EMBEDDING_MODEL") or os.getenv("EMBEDDING_MODEL_ID") or DEFAULT_MODEL_ID
