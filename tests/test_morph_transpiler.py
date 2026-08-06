import os
import shutil

import pytest
from managers.morph_index_manager import MorphIndexManager
from managers.morph_transpiler import index_library
from tmb_format import read_tmb

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "dsf")


@pytest.fixture
def library(tmp_path):
    lib_root = tmp_path / "library"
    data_dir = lib_root / "data" / "SomeVendor"
    data_dir.mkdir(parents=True)
    shutil.copy(os.path.join(FIXTURES, "Billow.dsf"), data_dir / "Billow.dsf")
    shutil.copy(os.path.join(FIXTURES, "pJCMCloakBend_m90.dsf"), data_dir / "pJCMCloakBend_m90.dsf")
    return str(lib_root)


def test_index_library_ingests_both_fixtures(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    summary = index_library(library, tmb_dir, db)

    assert summary["scanned"] == 2
    assert summary["ingested"] == 2
    assert summary["errors"] == 0
    assert db.get_stats()["morph_count"] == 2
    assert len(summary["new_guids"]) == 2


def test_index_library_writes_tmb_files_matching_source_layout(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    index_library(library, tmb_dir, db)

    expected = os.path.join(tmb_dir, "data", "SomeVendor", "Billow.tmb")
    assert os.path.exists(expected)
    vertex_count, deltas = read_tmb(expected)
    assert vertex_count == 23369
    assert len(deltas) == 18503


def test_index_library_is_incremental_by_default(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    first = index_library(library, tmb_dir, db)
    second = index_library(library, tmb_dir, db)

    assert first["ingested"] == 2
    assert second["ingested"] == 0
    assert second["skipped_unchanged"] == 2


def test_index_library_force_reingests_everything(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    index_library(library, tmb_dir, db)
    second = index_library(library, tmb_dir, db, force=True)

    assert second["ingested"] == 2


def test_index_library_skips_bad_json_without_aborting(library, tmp_path):
    bad_path = os.path.join(library, "data", "SomeVendor", "Corrupt.dsf")
    with open(bad_path, "w") as f:
        f.write("{not valid json")

    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    summary = index_library(library, tmb_dir, db)

    assert summary["errors"] == 1
    assert summary["ingested"] == 2  # the two good files still succeed


def test_index_library_rebuilds_dependencies(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    index_library(library, tmb_dir, db)

    # Neither fixture references another *morph* (the JCM references a bone
    # rotation), so the dependency graph should be empty but not error.
    assert db.get_stats()["dependency_count"] == 0
