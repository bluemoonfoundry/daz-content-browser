import numpy as np
import pytest

import embedding_utils
from catalog.model_config import configured_model_id


def test_resolver_order(tmp_path, monkeypatch):
    s = tmp_path / "settings.json"
    monkeypatch.delenv("EMBEDDING_MODEL_OVERRIDE", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL_ID", raising=False)
    assert configured_model_id(str(s)) == "BAAI/bge-large-en-v1.5"
    monkeypatch.setenv("EMBEDDING_MODEL_ID", "x/id")
    assert configured_model_id(str(s)) == "x/id"
    monkeypatch.setenv("EMBEDDING_MODEL", "x/env")
    assert configured_model_id(str(s)) == "x/env"
    s.write_text('{"embedding_model": "x/settings"}')
    assert configured_model_id(str(s)) == "x/settings"
    monkeypatch.setenv("EMBEDDING_MODEL_OVERRIDE", "x/cli")
    assert configured_model_id(str(s)) == "x/cli"


def test_resolver_ignores_unreadable_settings(tmp_path, monkeypatch):
    s = tmp_path / "settings.json"; s.write_text("{not json")
    monkeypatch.delenv("EMBEDDING_MODEL_OVERRIDE", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.setenv("EMBEDDING_MODEL_ID", "x/id")
    assert configured_model_id(str(s)) == "x/id"


def test_model_dir_uses_slug(monkeypatch):
    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    assert embedding_utils.model_dir("BAAI/bge-m3").name == "bge-m3"


def test_model_dir_env_only_for_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(embedding_utils, "configured_model_id", lambda: "a/cfg")
    assert embedding_utils.model_dir("a/cfg") == tmp_path
    assert embedding_utils.model_dir("b/other").name == "other"


def test_unavailable_model_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(embedding_utils, "model_dir", lambda m: tmp_path / "nope")
    with pytest.raises(embedding_utils.ModelUnavailable):
        embedding_utils.get_embedding_model("other/model")


def test_fake_embedder_is_deterministic_per_model(fake_embedder):
    a = embedding_utils.generate_embeddings(["x"], model_id="fake/model-a")
    assert a.shape == (1, 8)
    assert np.allclose(a, embedding_utils.generate_embeddings(["x"], model_id="fake/model-a"))
    assert embedding_utils.generate_embeddings("x", model_id="fake/model-b").shape == (12,)
