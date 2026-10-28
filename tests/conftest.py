import os
import pytest


@pytest.fixture(scope="session", autouse=True)
def demo_env(tmp_path_factory):
    """Run all tests in demo mode with throwaway DB paths."""
    tmp = tmp_path_factory.mktemp("test_data")
    os.environ["APP_MODE"] = "demo"
    os.environ.setdefault("SQLITE_DB_PATH", str(tmp / "test.db"))
    os.environ.setdefault("SQLITE_TABLE_NAME", "products")
    os.environ.setdefault("CHROMA_PATH", str(tmp / "chroma"))
    os.environ.setdefault("CHROMA_COLLECTION", "test_collection")
    os.environ.setdefault("JOBS_DB_PATH", str(tmp / "index_jobs.db"))
    # Blank (not unset) so load_dotenv() can't fill them in from .env: tests must never
    # reach a real DAZ CMS PostgreSQL, running or not
    for key in ("DB_NAME", "DB_HOST"):
        os.environ[key] = ""
    # Fixed API token; keep the token file out of the real ~/.daz3d
    os.environ["VAB_API_TOKEN"] = "test-token"
    os.environ["VAB_API_TOKEN_FILE"] = str(tmp / "vab_api_token.txt")
