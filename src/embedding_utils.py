import logging
import os
from numpy import ndarray
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "mixedbread-ai/mxbai-embed-large-v1")
EMBEDDING_DEVICE = os.getenv("EMBEDDING_DEVICE", "cpu")

# Global model cache — loaded once per process.
_model = None


def get_embedding_model() -> SentenceTransformer:
    """Loads and caches the embedding model specified in the .env file.

    The device is set explicitly via EMBEDDING_DEVICE to avoid a meta-tensor
    error that occurs when 'accelerate' is installed and SentenceTransformer's
    auto device-detection initialises weights on a meta device first.
    """
    global _model
    if _model is None:
        logger.info(f"Loading embedding model: {EMBEDDING_MODEL_NAME} on device: {EMBEDDING_DEVICE}")
        _model = SentenceTransformer(EMBEDDING_MODEL_NAME, device=EMBEDDING_DEVICE)
        logger.info("Embedding model loaded.")
    return _model


def generate_embeddings(texts, is_query: bool = False) -> ndarray:
    """Generates embeddings for a given text or list of texts.

    Handles model-specific prefixes for query vs. passage.

    Args:
        texts (str or list[str]): The text(s) to embed.
        is_query (bool): True if the text is a search query, False if it's a document.

    Returns:
        numpy.ndarray: The embedding vector(s).
    """
    model = get_embedding_model()

    logger.debug(f"Generating embeddings with {EMBEDDING_MODEL_NAME}")

    if "mxbai" in EMBEDDING_MODEL_NAME and is_query:
        # Ensure that if a list is passed, we prefix each item
        if isinstance(texts, list):
            texts = [
                f"Represent this sentence for searching relevant passages: {text}"
                for text in texts
            ]
        else:
            texts = f"Represent this sentence for searching relevant passages: {texts}"

    # The .encode() method handles batching automatically for lists
    return model.encode(texts, convert_to_tensor=False)
