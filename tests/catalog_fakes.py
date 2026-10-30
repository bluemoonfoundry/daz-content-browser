import hashlib

import numpy as np

_DIMS = {"fake/model-a": 8, "fake/model-b": 12}


def fake_embed(texts, is_query=False, model_id=None) -> np.ndarray:
    """Deterministic stand-in for embedding_utils.generate_embeddings."""
    dim = _DIMS.get(model_id, 8)
    single = isinstance(texts, str)
    if single:
        texts = [texts]
    rows = []
    for text in texts:
        digest = hashlib.sha256(f"{model_id}{text}".encode("utf-8")).digest()
        vec = np.array([digest[i % len(digest)] - 127.5 for i in range(dim)], dtype=np.float32)
        rows.append(vec / max(float(np.linalg.norm(vec)), 1e-9))
    out = np.stack(rows).astype(np.float32)
    return out[0] if single else out
