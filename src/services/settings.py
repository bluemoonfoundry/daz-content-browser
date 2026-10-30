"""User-editable settings: read, save, and when each one takes effect (see app_config)."""
import time
from typing import Callable, Optional

import app_config
from app_config import DEPRECATED_SETTINGS, EDITABLE_SETTINGS, CmsConfig, setting_value


class SettingsService:
    def __init__(self, config: Callable[[], app_config.AppConfig] = app_config.get_config,
                 active_model: Optional[Callable[[], Optional[str]]] = None,
                 save: Callable[[dict], None] = app_config.save_settings,
                 connect=None):
        """``active_model()`` is the active index generation's model (reported as the
        read-only ``query_model``); ``connect(CmsConfig)`` opens and closes a test
        connection to the DAZ CMS."""
        self._config = config
        self._active_model = active_model or (lambda: None)
        self._save = save
        self._connect = connect or _connect_postgres

    def public(self) -> dict:
        """The effective settings, minus the CMS password (``cms_password_set`` reports
        whether one is configured), with how each one is applied."""
        config = self._config()
        out = {name: setting_value(config, name) for name in EDITABLE_SETTINGS
               if name != "cms_password"}
        out["cms_password_set"] = bool(config.cms.password)
        out["query_model"] = self._active_model() or config.embedding_model
        out["applies"] = {name: s.applies for name, s in EDITABLE_SETTINGS.items()}
        out["deprecated"] = dict(DEPRECATED_SETTINGS)
        return out

    def save(self, updates: dict) -> dict:
        """Saves the given settings; None means "unchanged", and so does a blank password
        (it is write-only, so the UI can't round-trip it). Returns the public settings plus
        which saved settings changed and how each change takes effect."""
        before = self._config()
        updates = {k: v for k, v in updates.items() if v is not None}
        if updates.get("cms_password") == "":
            del updates["cms_password"]
        ignored = sorted(k for k in updates if k in DEPRECATED_SETTINGS)
        editable = {k: v for k, v in updates.items() if k in EDITABLE_SETTINGS}
        self._save(editable)
        after = self._config()
        changed = sorted(k for k in editable
                         if setting_value(before, k) != setting_value(after, k))
        result = self.public()
        result["changed"] = changed
        result["applied"] = [k for k in changed if EDITABLE_SETTINGS[k].applies == "immediate"]
        result["applies_on_next_index_job"] = [
            k for k in changed if EDITABLE_SETTINGS[k].applies == "next_index_job"]
        result["restart_required"] = [
            k for k in changed if EDITABLE_SETTINGS[k].applies == "restart"]
        result["ignored"] = ignored
        return result

    def test_cms(self, overrides: dict) -> dict:
        """Tries a DAZ CMS connection with the saved settings overlaid with ``overrides``."""
        cms = self._config().cms
        fields = {"cms_host": "host", "cms_port": "port", "cms_db": "db", "cms_user": "user",
                  "cms_password": "password"}
        values = {fields[k]: v for k, v in overrides.items()
                  if k in fields and v is not None and not (k == "cms_password" and v == "")}
        try:
            candidate = CmsConfig.model_validate({**cms.model_dump(), **values})
            started = time.monotonic()
            self._connect(candidate)
            ms = int((time.monotonic() - started) * 1000)
            return {"success": True, "message": f"Connected in {ms}ms", "latency_ms": ms}
        except Exception as e:
            return {"success": False, "message": str(e), "latency_ms": None}


def _connect_postgres(cms: CmsConfig) -> None:
    import psycopg2
    conn = psycopg2.connect(host=cms.host, port=cms.port, dbname=cms.db, user=cms.user,
                            password=cms.password, connect_timeout=5)
    conn.close()
