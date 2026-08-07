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


def test_index_library_raises_for_missing_data_dir(tmp_path):
    lib_root = tmp_path / "not_a_real_library"
    lib_root.mkdir()  # exists, but has no "data" subdirectory

    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")

    with pytest.raises(FileNotFoundError):
        index_library(str(lib_root), tmb_dir, db)


def test_index_library_validates_path_before_force_wipes_existing_data(library, tmp_path):
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    tmb_dir = str(tmp_path / "morph_cache")

    # Populate the index with real data first.
    first = index_library(library, tmb_dir, db)
    assert first["ingested"] == 2
    assert db.get_stats()["morph_count"] == 2

    # A --force run against a bad path must fail before wiping anything.
    bad_root = str(tmp_path / "not_a_real_library")
    os.makedirs(bad_root)
    with pytest.raises(FileNotFoundError):
        index_library(bad_root, tmb_dir, db, force=True)

    assert db.get_stats()["morph_count"] == 2


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
        count, failed_guids = embed_and_store_morphs(db, fake_chroma, summary["new_guids"])

    assert count == 2
    assert failed_guids == []
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
    count, failed_guids = embed_and_store_morphs(db, fake_chroma, [])
    assert count == 0
    assert failed_guids == []
    fake_chroma.collection.upsert.assert_not_called()


def test_embed_and_store_morphs_returns_failed_guids_on_double_failure(library, tmp_path):
    """When both the initial upsert and the reconnect-retry fail, the batch's
    guids should be reported back via failed_guids instead of silently
    dropped, so the caller can decide what to do (e.g. warn, retry later).
    """
    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()
    tmb_dir = str(tmp_path / "morph_cache")
    summary = index_library(library, tmb_dir, db)

    fake_chroma = MagicMock()
    fake_chroma.collection.upsert.side_effect = RuntimeError("chroma is down")
    fake_embeddings = MagicMock()
    fake_embeddings.tolist.return_value = [[0.1] * 1024, [0.2] * 1024]

    with patch("managers.morph_transpiler.generate_embeddings", return_value=fake_embeddings):
        count, failed_guids = embed_and_store_morphs(db, fake_chroma, summary["new_guids"])

    assert count == 0
    assert sorted(failed_guids) == sorted(summary["new_guids"])
    # Both the initial attempt and the retry (after reconnect) were tried.
    assert fake_chroma.collection.upsert.call_count == 2
    fake_chroma.reconnect.assert_called_once()


def test_embed_and_store_morphs_batches_correctly_across_multiple_iterations(tmp_path, monkeypatch):
    """Test that batching loop and upsert calls work correctly with multiple batches."""
    # Set BATCH_SIZE to 2 so 5 guids will result in 3 batches (2, 2, 1)
    monkeypatch.setenv("BATCH_SIZE", "2")

    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    db.setup_db()

    # Insert 5 morphs using the make_record pattern
    from test_morph_index_manager import make_record
    guids = []
    for i in range(5):
        guid = f"guid-{i}"
        guids.append(guid)
        db.insert_morph(make_record(guid=guid, label=f"Morph {i}"))

    fake_chroma = MagicMock()

    # Mock generate_embeddings to return embeddings for however many documents are passed
    def mock_generate_embeddings(documents, is_query=False):
        # Return a mock with tolist() that returns embeddings matching document count
        mock_embeddings = MagicMock()
        mock_embeddings.tolist.return_value = [[0.1 + j * 0.01] * 1024 for j in range(len(documents))]
        return mock_embeddings

    with patch("managers.morph_transpiler.generate_embeddings", side_effect=mock_generate_embeddings):
        count, failed_guids = embed_and_store_morphs(db, fake_chroma, guids)

    # Total count should be 5 (all guids embedded)
    assert count == 5
    assert failed_guids == []

    # upsert should be called 3 times (batch 1: 2, batch 2: 2, batch 3: 1)
    assert fake_chroma.collection.upsert.call_count == 3

    # Verify the batches were correctly sized
    calls = fake_chroma.collection.upsert.call_args_list
    batch_sizes = [len(call.kwargs["ids"]) for call in calls]
    assert batch_sizes == [2, 2, 1], f"Expected batch sizes [2, 2, 1], got {batch_sizes}"

    # Verify all guids appear in the upserts (across all batches)
    all_upserted_ids = []
    for call in calls:
        all_upserted_ids.extend(call.kwargs["ids"])
    assert sorted(all_upserted_ids) == sorted(guids)
