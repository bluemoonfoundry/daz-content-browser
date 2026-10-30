"""Typed configuration: layering, caching, and saving settings."""
import json

import app_config
from app_config import EDITABLE_SETTINGS, get_config, load_config, save_settings


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_defaults_env_settings_override_precedence(tmp_path):
    s = tmp_path / "settings.json"
    assert load_config({}, s).cms.port == 17237
    assert load_config({}, s).embedding_model == app_config.DEFAULT_MODEL_ID

    env = {"DB_PORT": "5432", "EMBEDDING_MODEL_ID": "x/id", "EMBEDDING_MODEL": "x/env"}
    assert load_config(env, s).cms.port == 5432
    assert load_config(env, s).embedding_model == "x/env"  # EMBEDDING_MODEL beats _ID

    _write(s, {"cms_port": 6000, "embedding_model": "x/settings"})
    cfg = load_config(env, s)
    assert cfg.cms.port == 6000 and cfg.embedding_model == "x/settings"

    cfg = load_config({**env, "EMBEDDING_MODEL_OVERRIDE": "x/cli"}, s)
    assert cfg.embedding_model == "x/cli"


def test_settings_file_only_layers_editable_settings(tmp_path):
    s = _write(tmp_path / "settings.json",
               {"chroma_path": "elsewhere", "query_model": "ignored", "cms_host": "db.local"})
    cfg = load_config({}, s)
    assert cfg.chroma_path == "chroma_db"   # storage paths are environment-only
    assert cfg.cms.host == "db.local"


def test_invalid_settings_file_falls_back_to_environment(tmp_path):
    s = _write(tmp_path / "settings.json", {"cms_port": "not a port"})
    assert load_config({"DB_PORT": "5432"}, s).cms.port == 5432
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert load_config({"DB_PORT": "5432"}, tmp_path / "broken.json").cms.port == 5432


def test_blank_cms_variables_mean_unconfigured(tmp_path):
    cfg = load_config({"DB_HOST": "", "DB_NAME": ""}, tmp_path / "none.json")
    assert set(cfg.cms.missing()) == {"host", "db"}


def test_invalid_env_values_are_ignored(tmp_path):
    assert load_config({"DB_PORT": "abc"}, tmp_path / "none.json").cms.port == 17237


def test_get_config_is_cached_until_something_changes(tmp_path, monkeypatch):
    s = _write(tmp_path / "settings.json", {"cms_host": "a"})
    monkeypatch.setenv("VAB_SETTINGS_PATH", str(s))
    first = get_config()
    assert first.cms.host == "a" and get_config() is first

    save_settings({"cms_host": "b"})
    assert get_config().cms.host == "b"

    monkeypatch.setenv("DB_PORT", "1234")
    assert get_config().cms.port == 1234


def test_save_settings_keeps_only_editable_keys(tmp_path, monkeypatch):
    s = _write(tmp_path / "settings.json", {"query_model": "old", "cms_user": "u"})
    save_settings({"cms_host": "h", "cms_schema": "x", "bogus": 1}, s)
    assert json.loads(s.read_text(encoding="utf-8")) == {"cms_user": "u", "cms_host": "h"}


def test_every_editable_setting_says_when_it_applies():
    assert {s.applies for s in EDITABLE_SETTINGS.values()} <= {"immediate", "next_index_job",
                                                               "restart"}
    assert EDITABLE_SETTINGS["embedding_model"].applies == "next_index_job"
