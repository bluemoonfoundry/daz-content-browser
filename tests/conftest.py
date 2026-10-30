import atexit
import os
import shutil
import tempfile

import pytest


# The test environment must exist before any test module (and so ``managers.managers``)
# is imported: pytest imports conftest first, while a session fixture would run too late
# and ``load_dotenv()`` would already have filled in the real paths from ``.env``.
_TMP = tempfile.mkdtemp(prefix="vab-tests-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["APP_MODE"] = "demo"
os.environ["SQLITE_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["SQLITE_TABLE_NAME"] = "products"
os.environ["CHROMA_PATH"] = os.path.join(_TMP, "chroma")
os.environ["CHROMA_COLLECTION"] = "test_collection"
os.environ["JOBS_DB_PATH"] = os.path.join(_TMP, "index_jobs.db")
# Blank (not unset) so load_dotenv() can't fill them in from .env: tests must never
# reach a real DAZ CMS PostgreSQL, running or not
for _key in ("DB_NAME", "DB_HOST"):
    os.environ[_key] = ""
# Fixed API token; keep the token file out of the real ~/.daz3d
os.environ["VAB_API_TOKEN"] = "test-token"
os.environ["VAB_API_TOKEN_FILE"] = os.path.join(_TMP, "vab_api_token.txt")


@pytest.fixture
def fake_embedder(monkeypatch):
    """Replace real ONNX embedding with a deterministic per-model fake (fake/* models only)."""
    import embedding_utils
    from catalog_fakes import fake_embed
    monkeypatch.setattr(embedding_utils, "generate_embeddings", fake_embed)
    monkeypatch.setattr(embedding_utils, "model_available", lambda model_id: model_id.startswith("fake/"))
