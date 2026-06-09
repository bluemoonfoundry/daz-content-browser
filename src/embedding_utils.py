import logging
import os
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

_HF_MODEL_ID = "BAAI/bge-large-en-v1.5"
_env_model_dir = os.getenv("EMBEDDING_MODEL_DIR", "")
_MODEL_DIR = Path(_env_model_dir) if _env_model_dir else (Path(__file__).parent.parent / "models" / "bge-large-en-v1.5")
_INFERENCE_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))

_model = None
_tokenizer = None


def _export_model():
    """Download BAAI/bge-large-en-v1.5 from HuggingFace, export to ONNX, and cache locally."""
    from optimum.onnxruntime import ORTModelForFeatureExtraction
    from transformers import AutoTokenizer

    logger.info(f"[embedding] First run — exporting {_HF_MODEL_ID} to ONNX. This may take a few minutes.")
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)

    model = ORTModelForFeatureExtraction.from_pretrained(_HF_MODEL_ID, export=True)
    model.save_pretrained(str(_MODEL_DIR))

    tokenizer = AutoTokenizer.from_pretrained(_HF_MODEL_ID)
    tokenizer.save_pretrained(str(_MODEL_DIR))

    logger.info(f"[embedding] Model exported and saved to {_MODEL_DIR}")
    return model, tokenizer


def _load_from_cache():
    """Load the ONNX model and tokenizer from the local cache directory."""
    from optimum.onnxruntime import ORTModelForFeatureExtraction
    from transformers import AutoTokenizer

    logger.info(f"[embedding] Loading ONNX model from local cache: {_MODEL_DIR}")
    model = ORTModelForFeatureExtraction.from_pretrained(
        str(_MODEL_DIR),
        provider="CPUExecutionProvider",
    )
    tokenizer = AutoTokenizer.from_pretrained(str(_MODEL_DIR))
    logger.info("[embedding] ONNX model loaded.")
    return model, tokenizer


def load_embedding_model():
    """Load (and if necessary export) the model. Intended to be called at server startup."""
    global _model, _tokenizer

    onnx_path = _MODEL_DIR / "model.onnx"
    if onnx_path.exists():
        _model, _tokenizer = _load_from_cache()
    else:
        _model, _tokenizer = _export_model()

    return _model, _tokenizer


def get_embedding_model():
    """Return the cached model + tokenizer, initialising them on first call."""
    global _model, _tokenizer
    if _model is None:
        load_embedding_model()
    return _model, _tokenizer


def _mean_pool(last_hidden_state, attention_mask) -> np.ndarray:
    """Mean-pool token embeddings weighted by the attention mask, then L2-normalise."""
    hidden = last_hidden_state.numpy() if hasattr(last_hidden_state, "numpy") else np.asarray(last_hidden_state)
    mask = attention_mask.numpy() if hasattr(attention_mask, "numpy") else np.asarray(attention_mask)

    mask_exp = np.expand_dims(mask, -1).astype(np.float32)      # (batch, seq, 1)
    pooled = (hidden * mask_exp).sum(axis=1) / mask_exp.sum(axis=1).clip(min=1e-9)  # (batch, dim)

    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    return (pooled / np.maximum(norms, 1e-9)).astype(np.float32)


def generate_embeddings(texts, is_query: bool = False) -> np.ndarray:
    """Tokenise, run ONNX inference, mean-pool, and L2-normalise.

    Processes texts in sub-batches of EMBEDDING_BATCH_SIZE (default 32) to
    keep peak memory reasonable on CPU.

    Returns float32 ndarray of shape (1024,) for a single string,
    or (N, 1024) for a list.
    """
    model, tokenizer = get_embedding_model()

    single = isinstance(texts, str)
    if single:
        texts = [texts]

    logger.debug(f"[embedding] Generating embeddings for {len(texts)} text(s)")

    total = len(texts)
    n_batches = (total + _INFERENCE_BATCH_SIZE - 1) // _INFERENCE_BATCH_SIZE
    chunks = []
    for idx, start in enumerate(range(0, total, _INFERENCE_BATCH_SIZE)):
        batch = texts[start: start + _INFERENCE_BATCH_SIZE]
        logger.info(f"[embedding] Sub-batch {idx + 1}/{n_batches} ({len(batch)} texts)")
        inputs = tokenizer(
            batch,
            max_length=512,
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        outputs = model(
            input_ids=inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
        )
        chunks.append(_mean_pool(outputs.last_hidden_state, inputs["attention_mask"]))

    embeddings = np.concatenate(chunks, axis=0)
    return embeddings[0] if single else embeddings
