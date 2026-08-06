"""Orchestrates the DAZ morph library ingest: walks a library, parses .dsf
files, writes .tmb caches, and populates the MorphIndexManager SQLite index.
"""

import hashlib
import logging
import os

from dsf_parser import parse_dsf_file, extract_referenced_guids
from tmb_format import write_tmb

logger = logging.getLogger(__name__)


def compute_content_hash(path: str) -> str:
    stat = os.stat(path)
    raw = f"{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def index_library(library_root: str, tmb_output_dir: str, morph_index_manager, force: bool = False, on_progress=None) -> dict:
    if force:
        morph_index_manager.setup_db(force_reset=True)
    else:
        morph_index_manager.setup_db(force_reset=False)

    summary = {
        "scanned": 0, "ingested": 0, "skipped_no_deltas": 0,
        "skipped_unchanged": 0, "errors": 0, "new_guids": [],
    }

    data_root = os.path.join(library_root, "data")
    for dirpath, _dirnames, filenames in os.walk(data_root):
        for filename in filenames:
            if not filename.lower().endswith(".dsf"):
                continue
            summary["scanned"] += 1
            source_path = os.path.join(dirpath, filename)

            try:
                content_hash = compute_content_hash(source_path)

                parsed = parse_dsf_file(source_path)
                if parsed is None:
                    summary["skipped_no_deltas"] += 1
                    continue

                if not force:
                    existing_hash = morph_index_manager.get_content_hash(parsed.guid)
                    if existing_hash == content_hash:
                        summary["skipped_unchanged"] += 1
                        continue

                rel_path = os.path.relpath(source_path, library_root)
                tmb_rel_path = os.path.splitext(rel_path)[0] + ".tmb"
                tmb_abs_path = os.path.join(tmb_output_dir, tmb_rel_path)
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

                if on_progress and summary["scanned"] % 500 == 0:
                    on_progress("scan", summary["scanned"], None, source_path)

            except Exception:
                logger.warning(f"Failed to ingest {source_path!r}, skipping.", exc_info=True)
                summary["errors"] += 1

    edge_count = morph_index_manager.rebuild_dependencies(extract_referenced_guids)
    logger.info(f"Rebuilt dependency graph: {edge_count} edges.")
    logger.info(f"Index run complete: {summary}")
    return summary
