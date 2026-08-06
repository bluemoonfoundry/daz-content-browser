import os
import shutil
from unittest.mock import MagicMock, patch

import pytest
from managers.morph_index_manager import MorphIndexManager
from managers.morph_transpiler import index_library, embed_and_store_morphs
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


def test_embed_and_store_morphs_upserts_into_chroma(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")
    summary = index_library(library, tmb_dir, db)

    fake_chroma = MagicMock()
    fake_embeddings = MagicMock()
    fake_embeddings.tolist.return_value = [[0.1] * 1024, [0.2] * 1024]

    with patch("managers.morph_transpiler.generate_embeddings", return_value=fake_embeddings):
        count = embed_and_store_morphs(db, fake_chroma, summary["new_guids"])

    assert count == 2
    fake_chroma.collection.upsert.assert_called_once()
    call_kwargs = fake_chroma.collection.upsert.call_args.kwargs
    assert sorted(call_kwargs["ids"]) == sorted(summary["new_guids"])
    assert len(call_kwargs["embeddings"]) == 2
    assert len(call_kwargs["documents"]) == 2
    assert len(call_kwargs["metadatas"]) == 2
    assert "label" in call_kwargs["metadatas"][0]


def test_embed_and_store_morphs_returns_zero_for_empty_guids(tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    fake_chroma = MagicMock()
    count = embed_and_store_morphs(db, fake_chroma, [])
    assert count == 0
    fake_chroma.collection.upsert.assert_not_called()
