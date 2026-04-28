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
