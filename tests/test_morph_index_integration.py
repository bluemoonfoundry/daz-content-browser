import os
import shutil
from unittest.mock import MagicMock, patch

from managers.morph_index_manager import MorphIndexManager
from managers.morph_transpiler import index_library, embed_and_store_morphs

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "dsf")


def test_full_pipeline_end_to_end(tmp_path):
    """End-to-end chain: index_library -> embed_and_store_morphs -> index_library.

    Verifies the sqlite->chroma handoff works when functions are chained together,
    and that incremental re-runs don't accidentally re-embed/re-ingest.
    Per-stage behaviors (tmb file content, dependency counts, etc.) are tested
    independently in test_morph_transpiler.py.
    """
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

    # Act: run the full chain
    summary = index_library(str(lib_root), tmb_dir, db)
    with patch("managers.morph_transpiler.generate_embeddings", return_value=fake_embeddings):
        embedded_count = embed_and_store_morphs(db, fake_chroma, summary["new_guids"])

    # Assert: first run completes without exception
    # (ingested==2 proves pipeline ran; errors==1 proves error handling worked)
    assert summary["ingested"] == 2
    assert summary["errors"] == 1

    # Assert: sqlite->chroma handoff works (embedded_count matches new_guids from index_library)
    assert embedded_count == 2

    # Act again: re-run the full chain to prove incremental behavior holds end-to-end
    second_summary = index_library(str(lib_root), tmb_dir, db)

    # Assert: no re-ingestion on second run (proves the chain doesn't accidentally re-process)
    assert second_summary["ingested"] == 0
