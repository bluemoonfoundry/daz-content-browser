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
_PROFILE_PROVIDERS = os.getenv("EMBEDDING_PROFILE_PROVIDERS", "") == "1"

_session = None
_tokenizer = None
_profile_logged = False
_embed_call_count = 0


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

    sess_options = ort.SessionOptions()
    if _PROFILE_PROVIDERS:
        # Diagnostic only (EMBEDDING_PROFILE_PROVIDERS=1): session.get_providers() reports
        # which providers are *available* to a session, not which one actually executes
        # each node. ORT's own profiler is the authoritative source for that.
        sess_options.enable_profiling = True

    session = ort.InferenceSession(str(_MODEL_DIR / "model.onnx"), sess_options=sess_options, providers=providers)

    tokenizer = Tokenizer.from_file(str(_MODEL_DIR / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding()

    # session.get_providers() reflects what ORT actually selected, which can silently
    # differ from `providers` above (e.g. DmlExecutionProvider requested but the
    # installed onnxruntime package doesn't include it, so it falls back to CPU).
    logger.info(f"[embedding] ONNX model loaded. Available providers: {available}; "
                f"active providers: {session.get_providers()}")
    return session, tokenizer


def load_embedding_model():
    """Load (and if necessary export) the model. Intended to be called at server startup."""
    import sys
    global _session, _tokenizer

    onnx_path = _MODEL_DIR / "model.onnx"
    if not onnx_path.exists():
        if getattr(sys, 'frozen', False):
            raise RuntimeError(
                f"ONNX model not found at '{_MODEL_DIR}'. "
                f"Download and export the model with the export tool before switching models."
            )
        _export_model()

    _session, _tokenizer = _load_from_cache()
    return _session, _tokenizer


def get_embedding_model():
    """Return the cached session + tokenizer, raising RuntimeError if the model is unavailable."""
    global _session, _tokenizer
    if _session is None:
        load_embedding_model()
    if _session is None:
        raise RuntimeError(
            f"Embedding model is not loaded. "
            f"Ensure the ONNX model files are present at '{_MODEL_DIR}'."
        )
    return _session, _tokenizer


def _mean_pool(last_hidden_state: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    """Mean-pool token embeddings weighted by the attention mask, then L2-normalise."""
    mask_exp = np.expand_dims(attention_mask, -1).astype(np.float32)  # (batch, seq, 1)
    pooled = (last_hidden_state * mask_exp).sum(axis=1) / mask_exp.sum(axis=1).clip(min=1e-9)
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    return (pooled / np.maximum(norms, 1e-9)).astype(np.float32)


def _log_provider_profile(session) -> None:
    """One-shot diagnostic: end ORT profiling and log node count/duration per provider.

    Only runs when EMBEDDING_PROFILE_PROVIDERS=1 is set, and only after the
    second inference call (the first call includes one-time graph compilation
    that would otherwise dominate the numbers).
    """
    import json
    global _profile_logged
    _profile_logged = True
    try:
        profile_path = session.end_profiling()
        with open(profile_path) as f:
            events = json.load(f)
        totals = {}
        counts = {}
        for e in events:
            provider = e.get("args", {}).get("provider") if e.get("cat") == "Node" else None
            if provider:
                totals[provider] = totals.get(provider, 0) + e.get("dur", 0)
                counts[provider] = counts.get(provider, 0) + 1
        summary = ", ".join(f"{p}: {counts[p]} nodes/{totals[p]}us" for p in totals)
        logger.info(f"[embedding] Provider profile (one-shot): {summary}")
    except Exception:
        logger.exception("[embedding] Failed to read provider profile")


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

    if _PROFILE_PROVIDERS and not _profile_logged:
        global _embed_call_count
        _embed_call_count += 1
        # Skip the first call: it includes one-time graph compilation that would
        # otherwise dominate the per-provider timing.
        if _embed_call_count >= 2:
            _log_provider_profile(session)

    embeddings = np.concatenate(chunks, axis=0)
    return embeddings[0] if single else embeddings
