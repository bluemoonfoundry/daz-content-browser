import os
import shutil
from unittest.mock import MagicMock, patch

import pytest
from managers.morph_index_manager import MorphIndexManager
from managers.morph_transpiler import index_library, embed_and_store_morphs
from tmb_format import read_tmb

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "dsf")


def test_full_pipeline_end_to_end(tmp_path):
    # Arrange a fake library with both fixtures plus one corrupt file.
    lib_root = tmp_path / "library"
    data_dir = lib_root / "data" / "Vendor"
    data_dir.mkdir(parents=True)
    shutil.copy(os.path.join(FIXTURES, "Billow.dsf"), data_dir / "Billow.dsf")
    shutil.copy(os.path.join(FIXTURES, "pJCMCloakBend_m90.dsf"), data_dir / "pJCMCloakBend_m90.dsf")
    (data_dir / "Corrupt.dsf").write_text("{not valid json")

    db = MorphIndexManager(str(tmp_path / "morph_index.db"))
    tmb_dir = str(tmp_path / "morph_cache")
    fake_chroma = MagicMock()
    fake_embeddings = MagicMock()
    fake_embeddings.tolist.return_value = [[0.1] * 1024, [0.2] * 1024]

    # Act
    summary = index_library(str(lib_root), tmb_dir, db)
    with patch("managers.morph_transpiler.generate_embeddings", return_value=fake_embeddings):
        embedded_count = embed_and_store_morphs(db, fake_chroma, summary["new_guids"])

    # Assert: SQLite
    assert summary["ingested"] == 2
    assert summary["errors"] == 1
    stats = db.get_stats()
    assert stats["morph_count"] == 2
    assert stats["dependency_count"] == 0  # neither fixture depends on another indexed morph

    # Assert: .tmb files on disk
    billow_tmb = os.path.join(tmb_dir, "data", "Vendor", "Billow.tmb")
    assert os.path.exists(billow_tmb)
    vertex_count, deltas = read_tmb(billow_tmb)
    assert vertex_count == 23369
    assert len(deltas) == 18503

    # Assert: ChromaDB upsert happened with matching count
    assert embedded_count == 2
    fake_chroma.collection.upsert.assert_called_once()

    # Act again: re-run should be a no-op (incremental)
    second_summary = index_library(str(lib_root), tmb_dir, db)
    assert second_summary["ingested"] == 0
    assert second_summary["skipped_unchanged"] == 2
