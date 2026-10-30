"""Guards against tests touching the user's real index (see tests/conftest.py)."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_session_does_not_point_at_repo_db():
    import managers.managers as mm
    real = (ROOT / "enriched_products.db").resolve()
    assert Path(os.environ["SQLITE_DB_PATH"]).resolve() != real
    assert Path(mm.catalog.db_path).resolve() != real
    assert Path(os.environ["CHROMA_PATH"]).resolve() != (ROOT / "chroma_db").resolve()


def test_importing_managers_does_no_sqlite_io(tmp_path):
    target = tmp_path / "must_not_exist.db"
    env = dict(os.environ, SQLITE_DB_PATH=str(target), SQLITE_TABLE_NAME="products",
               JOBS_DB_PATH=str(tmp_path / "jobs.db"), CHROMA_PATH=str(tmp_path / "chroma"),
               PYTHONPATH=str(ROOT / "src"))
    r = subprocess.run([sys.executable, "-c", "import managers.managers"], env=env,
                       capture_output=True, text=True, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert not target.exists()
