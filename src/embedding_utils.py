import logging
import os
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

_HF_MODEL_ID = os.getenv("EMBEDDING_MODEL_ID", "BAAI/bge-large-en-v1.5")
_model_slug  = _HF_MODEL_ID.split("/")[-1]   # e.g. "bge-large-en-v1.5"
_env_model_dir = os.getenv("EMBEDDING_MODEL_DIR", "")
_MODEL_DIR = Path(_env_model_dir) if _env_model_dir else (Path(__file__).parent.parent / "models" / _model_slug)
_INFERENCE_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))

_session = None
_tokenizer = None


def _export_model():
    """Download BAAI/bge-large-en-v1.5 from HuggingFace, export to ONNX, and cache locally.

    Uses optimum + transformers — only called from export_model.py, never at server runtime.
    """
    from optimum.onnxruntime import ORTModelForFeatureExtraction
    from transformers import AutoTokenizer

    logger.info(f"[embedding] First run — exporting {_HF_MODEL_ID} to ONNX. This may take a few minutes.")
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)

    model = ORTModelForFeatureExtraction.from_pretrained(_HF_MODEL_ID, export=True)
    model.save_pretrained(str(_MODEL_DIR))

    tokenizer = AutoTokenizer.from_pretrained(_HF_MODEL_ID)
    tokenizer.save_pretrained(str(_MODEL_DIR))

    logger.info(f"[embedding] Model exported and saved to {_MODEL_DIR}")


def _load_from_cache():
    """Load the ONNX session and tokenizer directly — no torch or transformers required."""
    import onnxruntime as ort
    from tokenizers import Tokenizer

    logger.info(f"[embedding] Loading ONNX model from {_MODEL_DIR}")

    available = ort.get_available_providers()
    providers = [p for p in ["DmlExecutionProvider", "CPUExecutionProvider"] if p in available]
    session = ort.InferenceSession(str(_MODEL_DIR / "model.onnx"), providers=providers)

    tokenizer = Tokenizer.from_file(str(_MODEL_DIR / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding()

    logger.info("[embedding] ONNX model loaded.")
    return session, tokenizer


def load_embedding_model():
    """Load (and if necessary export) the model. Intended to be called at server startup."""
    global _session, _tokenizer

    onnx_path = _MODEL_DIR / "model.onnx"
    if not onnx_path.exists():
        _export_model()

    _session, _tokenizer = _load_from_cache()
    return _session, _tokenizer


def get_embedding_model():
    """Return the cached session + tokenizer, initialising them on first call."""
    global _session, _tokenizer
    if _session is None:
        load_embedding_model()
    return _session, _tokenizer


def _mean_pool(last_hidden_state: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    """Mean-pool token embeddings weighted by the attention mask, then L2-normalise."""
    mask_exp = np.expand_dims(attention_mask, -1).astype(np.float32)  # (batch, seq, 1)
    pooled = (last_hidden_state * mask_exp).sum(axis=1) / mask_exp.sum(axis=1).clip(min=1e-9)
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    return (pooled / np.maximum(norms, 1e-9)).astype(np.float32)


def generate_embeddings(texts, is_query: bool = False) -> np.ndarray:
    """Tokenise, run ONNX inference, mean-pool, and L2-normalise.

    Processes texts in sub-batches of EMBEDDING_BATCH_SIZE (default 32) to
    keep peak memory reasonable on CPU.

    Returns float32 ndarray of shape (1024,) for a single string,
    or (N, 1024) for a list.
    """
    session, tokenizer = get_embedding_model()

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

        encoded = tokenizer.encode_batch(batch)
        input_ids      = np.array([e.ids        for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        token_type_ids = np.array([e.type_ids   for e in encoded], dtype=np.int64)

        outputs = session.run(None, {
            "input_ids":      input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        })

        chunks.append(_mean_pool(outputs[0], attention_mask))

    embeddings = np.concatenate(chunks, axis=0)
    return embeddings[0] if single else embeddings
