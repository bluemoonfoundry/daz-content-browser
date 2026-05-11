import json
from pathlib import Path

from services import settings_service


def test_load_settings_defaults_when_file_missing(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_service, "SETTINGS_PATH", settings_file)

    loaded = settings_service.load_settings()
    assert loaded["cms_host"]
    assert "embedding_model" in loaded


def test_save_and_load_roundtrip(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_service, "SETTINGS_PATH", settings_file)

    payload = {"cms_host": "db.local", "cms_port": 17237, "query_model": "demo-model"}
    settings_service.save_settings(payload)

    loaded = settings_service.load_settings()
    assert loaded["cms_host"] == "db.local"
    assert loaded["query_model"] == "demo-model"


def test_update_settings_ignores_none_values(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_service, "SETTINGS_PATH", settings_file)

    settings_service.save_settings({"cms_host": "old-host", "cms_user": "old-user"})
    updated = settings_service.update_settings({"cms_host": None, "cms_user": "new-user"})

    assert updated["cms_host"] == "old-host"
    assert updated["cms_user"] == "new-user"


def test_load_settings_invalid_json_falls_back_to_defaults(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{not-valid-json", encoding="utf-8")
    monkeypatch.setattr(settings_service, "SETTINGS_PATH", settings_file)

    loaded = settings_service.load_settings()
    assert loaded["cms_db"]


def test_update_settings_when_existing_json_is_invalid(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{bad-json", encoding="utf-8")
    monkeypatch.setattr(settings_service, "SETTINGS_PATH", settings_file)

    updated = settings_service.update_settings({"cms_schema": "dzcontent"})
    on_disk = json.loads(Path(settings_file).read_text(encoding="utf-8"))

    assert updated["cms_schema"] == "dzcontent"
    assert on_disk["cms_schema"] == "dzcontent"
