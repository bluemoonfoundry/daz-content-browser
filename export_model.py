#!/usr/bin/env python
"""Export the embedding model to ONNX format for bundling in the DIM package.

Downloads BAAI/bge-large-en-v1.5 from HuggingFace, converts it to ONNX via
optimum, and writes the output to models/bge-large-en-v1.5/ (the location
build_dim.py expects by default).

Run once before `make release-dim`. Requires an internet connection on first
run; subsequent runs skip the download if the model is already present.

Usage:
    python export_model.py [--force] [--out-dir PATH]

Options:
    --force         Re-export even if the model already exists
    --out-dir PATH  Output directory (default: models/)

Makefile: make export-model
Prereqs:  pip install -e ".[local_llm]"
"""
import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
DEFAULT_MODEL_DIR = ROOT / "models"

HF_MODEL_ID = "BAAI/bge-large-en-v1.5"
MODEL_NAME = "bge-large-en-v1.5"


def dir_size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1_048_576


def export(out_dir: Path, force: bool):
    model_dir = out_dir / MODEL_NAME
    onnx_path = model_dir / "model.onnx"

    if onnx_path.exists() and not force:
        log.info(f"Model already exported at {model_dir} ({dir_size_mb(model_dir):.0f} MB)")
        log.info("Pass --force to re-export.")
        return

    try:
        from optimum.onnxruntime import ORTModelForFeatureExtraction
        from transformers import AutoTokenizer
    except ImportError:
        sys.exit(
            "ERROR: optimum and transformers are required.\n"
            "  Run: pip install -e '.[local_llm]'"
        )

    if force and model_dir.exists():
        import shutil
        log.info(f"Removing existing model at {model_dir}")
        shutil.rmtree(model_dir)

    model_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Exporting {HF_MODEL_ID} → {model_dir}")
    log.info("This may take several minutes on first run (downloads ~1.3 GB from HuggingFace).")

    model = ORTModelForFeatureExtraction.from_pretrained(HF_MODEL_ID, export=True)
    model.save_pretrained(str(model_dir))

    tokenizer = AutoTokenizer.from_pretrained(HF_MODEL_ID)
    tokenizer.save_pretrained(str(model_dir))

    size_mb = dir_size_mb(model_dir)
    log.info(f"Done. Exported to {model_dir} ({size_mb:.0f} MB)")
    log.info("Ready for: make release-dim VERSION=x.y.z")


def main():
    parser = argparse.ArgumentParser(description="Export ONNX embedding model for DIM packaging")
    parser.add_argument("--force", action="store_true", help="Re-export even if model already exists")
    parser.add_argument("--out-dir", default=str(DEFAULT_MODEL_DIR),
                        help=f"Parent directory for the exported model (default: {DEFAULT_MODEL_DIR})")
    args = parser.parse_args()

    export(Path(args.out_dir), args.force)


if __name__ == "__main__":
    main()
