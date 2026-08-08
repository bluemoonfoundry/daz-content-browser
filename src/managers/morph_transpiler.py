"""Orchestrates the DAZ morph library ingest: walks a library, parses .dsf
files, writes .tmb caches, and populates the MorphIndexManager SQLite index.
"""

import hashlib
import logging
import os

from dsf_parser import parse_dsf_file, extract_referenced_guids
from embedding_utils import generate_embeddings
from tmb_format import write_tmb

logger = logging.getLogger(__name__)


def compute_content_hash(path: str) -> str:
    stat = os.stat(path)
    raw = f"{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_library_root(library_root: str) -> str:
    """Returns the library's data/ directory path, raising FileNotFoundError
    if it doesn't exist. Callers must validate before taking any destructive
    --force action (wiping the SQLite index, the .tmb cache, or the Chroma
    collection) so a typo'd path fails fast without side effects.
    """
    data_root = os.path.join(library_root, "data")
    if not os.path.isdir(data_root):
        raise FileNotFoundError(
            f"No 'data' directory found under library root: {library_root!r} (expected {data_root!r})"
        )
    return data_root


def index_library(library_root: str, tmb_output_dir: str, morph_index_manager, force: bool = False, on_progress=None) -> dict:
    data_root = validate_library_root(library_root)

    if force:
        morph_index_manager.setup_db(force_reset=True)
    else:
        morph_index_manager.setup_db(force_reset=False)

    summary = {
        "scanned": 0, "ingested": 0, "skipped_no_deltas": 0,
        "skipped_unchanged": 0, "errors": 0, "new_guids": [],
    }

    for dirpath, _dirnames, filenames in os.walk(data_root):
        for filename in filenames:
            if not filename.lower().endswith(".dsf"):
                continue
            summary["scanned"] += 1
            source_path = os.path.join(dirpath, filename)

            try:
                content_hash = compute_content_hash(source_path)

                rel_path = os.path.relpath(source_path, library_root)
                tmb_rel_path = os.path.splitext(rel_path)[0] + ".tmb"
                tmb_abs_path = os.path.join(tmb_output_dir, tmb_rel_path)

                if not force:
                    existing_hash = morph_index_manager.get_content_hash_by_source_path(source_path)
                    if existing_hash == content_hash and os.path.exists(tmb_abs_path):
                        summary["skipped_unchanged"] += 1
                        continue

                parsed = parse_dsf_file(source_path)
                if parsed is None:
                    summary["skipped_no_deltas"] += 1
                    continue

                os.makedirs(os.path.dirname(tmb_abs_path), exist_ok=True)
                write_tmb(tmb_abs_path, parsed.vertex_count, parsed.deltas)

                morph_index_manager.insert_morph({
                    "guid": parsed.guid,
                    "label": parsed.label,
                    "name": parsed.name,
                    "target_figure": parsed.target_figure,
                    "group_path": parsed.group_path,
                    "source_dsf_path": source_path,
                    "tmb_path": tmb_rel_path,
                    "vertex_count": parsed.vertex_count,
                    "delta_count": len(parsed.deltas),
                    "min_value": parsed.min_value,
                    "max_value": parsed.max_value,
                    "is_clamped": parsed.is_clamped,
                    "formulas_json": parsed.formulas_json,
                    "content_hash": content_hash,
                })
                summary["ingested"] += 1
                summary["new_guids"].append(parsed.guid)

            except Exception:
                logger.warning(f"Failed to ingest {source_path!r}, skipping.", exc_info=True)
                summary["errors"] += 1

            if on_progress and summary["scanned"] % 500 == 0:
                on_progress("scan", summary["scanned"], None, source_path)

    edge_count = morph_index_manager.rebuild_dependencies(extract_referenced_guids)
    logger.info(f"Rebuilt dependency graph: {edge_count} edges.")
    logger.info(f"Index run complete: {summary}")
    return summary


def _build_embedding_text(row) -> str:
    label = row["label"] or ""
    group_path = row["group_path"] or ""
    return f"{label}. Category: {group_path}." if group_path else f"{label}."


def embed_and_store_morphs(morph_index_manager, chroma_manager, guids: list, on_progress=None) -> tuple:
    if not guids:
        return 0, []

    batch_size = int(os.getenv("BATCH_SIZE", "512"))
    total = len(guids)
    embedded = 0
    failed_guids = []

    for i in range(0, total, batch_size):
        batch_guids = guids[i:i + batch_size]
        rows = morph_index_manager.get_morphs_by_guids(batch_guids)
        if not rows:
            continue

        documents = [_build_embedding_text(row) for row in rows]
        metadatas = [
            {
                "guid": row["guid"],
                "label": row["label"] or "",
                "name": row["name"] or "",
                "target_figure": row["target_figure"] or "",
                "group_path": row["group_path"] or "",
            }
            for row in rows
        ]
        ids = [row["guid"] for row in rows]

        embeddings = generate_embeddings(documents, is_query=False).tolist()
        try:
            chroma_manager.collection.upsert(
                ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents,
            )
        except Exception as e:
            logger.warning(f"ChromaDB upsert failed ({e}), reconnecting and retrying...")
            try:
                chroma_manager.reconnect()
                chroma_manager.collection.upsert(
                    ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents,
                )
            except Exception as e2:
                logger.error(f"Error publishing batch {i // batch_size + 1} to ChromaDB: {e2}")
                failed_guids.extend(ids)
                continue
        embedded += len(ids)

        if on_progress:
            on_progress("embed", min(i + batch_size, total), total, f"batch {i // batch_size + 1}")

    return embedded, failed_guids
