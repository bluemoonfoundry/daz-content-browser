#!/usr/bin/env python
"""vab_export — one-time ONNX export of a HuggingFace embedding model.

The BMF Content Browser plugin launches this binary when the user selects
a model whose ONNX files have not yet been downloaded.

Usage (as invoked by the plugin):
    vab_export.exe --model BAAI/bge-m3 --output-dir <path>

All output is written to stdout so the plugin can stream it into a
progress log.  The final line on success is "[EXPORT DONE]"; the plugin
also checks for exit code 0.
"""

import argparse
import logging
import sys
from pathlib import Path

# Route all logging to stdout — the plugin captures stdout only.
logging.basicConfig(level=logging.INFO, format="[EXPORT] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

# Merge stderr into stdout so tqdm download bars appear in the plugin log.
sys.stderr = sys.stdout


def export(model_id: str, output_dir: Path) -> None:
    logger.info(f"Starting export of {model_id}")
    logger.info(f"Output directory: {output_dir}")
    sys.stdout.flush()

    try:
        from optimum.onnxruntime import ORTModelForFeatureExtraction
        from transformers import AutoTokenizer
    except ImportError as exc:
        logger.error(f"Required package not available: {exc}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Downloading model from HuggingFace and converting to ONNX.")
    logger.info("This may take several minutes depending on your connection speed.")
    sys.stdout.flush()

    try:
        model = ORTModelForFeatureExtraction.from_pretrained(model_id, export=True)
    except Exception as exc:
        logger.error(f"Download/conversion failed: {exc}")
        sys.exit(1)

    logger.info("Saving ONNX model files...")
    sys.stdout.flush()
    try:
        model.save_pretrained(str(output_dir))
    except Exception as exc:
        logger.error(f"Failed to save model files: {exc}")
        sys.exit(1)

    logger.info("Saving tokenizer...")
    sys.stdout.flush()
    try:
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        tokenizer.save_pretrained(str(output_dir))
    except Exception as exc:
        logger.error(f"Failed to save tokenizer: {exc}")
        sys.exit(1)

    logger.info(f"All files saved to {output_dir}")
    print("[EXPORT DONE]", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download and export a HuggingFace embedding model to ONNX format."
    )
    parser.add_argument(
        "--model", required=True, metavar="MODEL_ID",
        help="HuggingFace model ID (e.g. BAAI/bge-m3)",
    )
    parser.add_argument(
        "--output-dir", required=True, metavar="PATH", type=Path,
        help="Directory to save the exported ONNX model and tokenizer files",
    )
    args = parser.parse_args()
    export(args.model, args.output_dir)


if __name__ == "__main__":
    main()
