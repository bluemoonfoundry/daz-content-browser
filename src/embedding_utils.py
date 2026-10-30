import logging
import os
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
load_dotenv()

from catalog.model_config import configured_model_id

logger = logging.getLogger(__name__)

_INFERENCE_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))
_PROFILE_PROVIDERS = os.getenv("EMBEDDING_PROFILE_PROVIDERS", "") == "1"

_MODELS_ROOT = Path(__file__).parent.parent / "models"

# model_id -> (session, tokenizer)
_sessions: dict = {}
# "Configured model loaded" flag for /health; set by load_embedding_model() for the configured model
_session = None
_tokenizer = None
_profile_logged = False
_embed_call_count = 0


class ModelUnavailable(RuntimeError):
    """The requested embedding model's ONNX files are missing and cannot be exported."""


def model_dir(model_id: str) -> Path:
    """Directory holding a model's ONNX export. EMBEDDING_MODEL_DIR applies only to the configured model."""
    env_dir = os.getenv("EMBEDDING_MODEL_DIR", "")
    if env_dir and model_id == configured_model_id():
        return Path(env_dir)
    return _MODELS_ROOT / model_id.split("/")[-1]


def model_available(model_id: str) -> bool:
    return (model_dir(model_id) / "model.onnx").exists()


def model_loaded(model_id: str | None = None) -> bool:
    return (model_id or configured_model_id()) in _sessions


def _export_model(model_id: str | None = None):
    """Download a model from HuggingFace, export to ONNX, and cache locally.

    Uses optimum + transformers. Only via load_embedding_model (server startup, index
    jobs); never inside a request.
    """
    from optimum.onnxruntime import ORTModelForFeatureExtraction
    from transformers import AutoTokenizer

    model_id = model_id or configured_model_id()
    target = model_dir(model_id)
    logger.info(f"[embedding] First run — exporting {model_id} to ONNX. This may take a few minutes.")
    target.mkdir(parents=True, exist_ok=True)

    model = ORTModelForFeatureExtraction.from_pretrained(model_id, export=True)
    model.save_pretrained(str(target))

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    tokenizer.save_pretrained(str(target))

    logger.info(f"[embedding] Model exported and saved to {target}")


def _load_from_cache(model_id: str):
    """Load the ONNX session and tokenizer directly — no torch or transformers required."""
    import onnxruntime as ort
    from tokenizers import Tokenizer

    directory = model_dir(model_id)
    logger.info(f"[embedding] Loading ONNX model from {directory}")

    available = ort.get_available_providers()
    providers = [p for p in ["DmlExecutionProvider", "CPUExecutionProvider"] if p in available]

    sess_options = ort.SessionOptions()
    if _PROFILE_PROVIDERS:
        # Diagnostic only (EMBEDDING_PROFILE_PROVIDERS=1): session.get_providers() reports
        # which providers are *available* to a session, not which one actually executes
        # each node. ORT's own profiler is the authoritative source for that.
        sess_options.enable_profiling = True

    session = ort.InferenceSession(str(directory / "model.onnx"), sess_options=sess_options, providers=providers)

    tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding()

    # session.get_providers() reflects what ORT actually selected, which can silently
    # differ from `providers` above (e.g. DmlExecutionProvider requested but the
    # installed onnxruntime package doesn't include it, so it falls back to CPU).
    logger.info(f"[embedding] ONNX model loaded. Available providers: {available}; "
                f"active providers: {session.get_providers()}")
    return session, tokenizer


def _load_session(model_id: str):
    directory = model_dir(model_id)
    if not (directory / "model.onnx").exists():
        raise ModelUnavailable(f"ONNX model for '{model_id}' not found at '{directory}'.")
    pair = _load_from_cache(model_id)
    _sessions[model_id] = pair
    return pair


def load_embedding_model(model_id: str | None = None, allow_export: bool | None = None):
    """Load a model, exporting it first when its files are missing and export is allowed.

    ``allow_export=None`` (server startup) exports only the configured model; index jobs
    pass ``True`` for the model they build or embed with. A frozen build never exports
    (raises ModelUnavailable). Request paths never call this: generate_embeddings and
    get_embedding_model never export.
    """
    import sys
    global _session, _tokenizer

    configured = configured_model_id()
    model_id = model_id or configured
    if allow_export is None:
        allow_export = model_id == configured
    if allow_export and not (model_dir(model_id) / "model.onnx").exists():
        if getattr(sys, 'frozen', False):
            raise ModelUnavailable(
                f"ONNX model not found at '{model_dir(model_id)}'. "
                f"Download and export the model with the export tool before switching models."
            )
        _export_model(model_id)

    pair = _load_session(model_id)
    if model_id == configured:
        _session, _tokenizer = pair
    return pair


def get_embedding_model(model_id: str | None = None):
    """Return the cached session + tokenizer for a model (None = configured), loading it on first use.

    Never exports: raises ModelUnavailable naming the model and directory if its files are missing.
    """
    model_id = model_id or configured_model_id()
    if model_id not in _sessions:
        _load_session(model_id)
    return _sessions[model_id]


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


def generate_embeddings(texts, is_query: bool = False, model_id: str | None = None) -> np.ndarray:
    """Tokenise, run ONNX inference, mean-pool, and L2-normalise.

    Processes texts in sub-batches of EMBEDDING_BATCH_SIZE (default 32) to
    keep peak memory reasonable on CPU.

    model_id=None uses the configured model. Returns float32 ndarray of shape (dim,)
    for a single string, or (N, dim) for a list.
    """
    session, tokenizer = get_embedding_model(model_id)

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
