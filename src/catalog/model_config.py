from typing import Optional

from app_config import DEFAULT_MODEL_ID, SETTINGS_PATH, get_config, load_config  # noqa: F401


def configured_model_id(settings_path: Optional[str] = None) -> str:
    """The embedding model new generations are built with: CLI override -> settings.json ->
    EMBEDDING_MODEL -> EMBEDDING_MODEL_ID -> default (see app_config for the layering).

    Cheap to call on hot paths: get_config() re-reads settings.json only when it changed.
    ``settings_path`` reads a specific settings file instead (uncached).
    """
    if settings_path:
        return load_config(settings_path=settings_path).embedding_model
    return get_config().embedding_model
