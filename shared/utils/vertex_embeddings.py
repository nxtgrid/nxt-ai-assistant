"""
Vertex AI Embeddings Helper

Uses Google's embedding model via Vertex AI API.
Model is configurable via EMBEDDING_MODEL env var (default: gemini-embedding-001).

Usage:
    from shared.utils.vertex_embeddings import get_embeddings, get_embedding

    # Single text
    embedding = await get_embedding("Hello world")

    # Batch texts
    embeddings = await get_embeddings(["Hello", "World"], task_type="RETRIEVAL_DOCUMENT")
"""

import os
from typing import List, Optional

from shared.utils.google_auth import get_service_account_json
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Default embedding model - configurable via environment variable
DEFAULT_EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")

# Cache for initialized state
_vertex_initialized = False


def _ensure_vertex_initialized():
    """Initialize Vertex AI with service account credentials."""
    global _vertex_initialized
    if _vertex_initialized:
        return

    import vertexai
    from google.oauth2 import service_account

    # Get service account info
    sa_info = get_service_account_json()
    project_id = sa_info.get("project_id")

    if not project_id:
        raise ValueError("project_id not found in service account JSON")

    # Create credentials
    credentials = service_account.Credentials.from_service_account_info(sa_info)

    # Initialize Vertex AI
    location = os.getenv("VERTEX_AI_LOCATION", "us-central1")
    vertexai.init(project=project_id, location=location, credentials=credentials)

    LOGGER.info(f"Vertex AI initialized: project={project_id}, location={location}")
    _vertex_initialized = True


async def get_embeddings(
    texts: List[str],
    task_type: str = "RETRIEVAL_DOCUMENT",
    model_name: str = "",
    output_dimensionality: int = 768,
) -> List[List[float]]:
    """
    Generate embeddings for multiple texts using Vertex AI.

    Args:
        texts: List of texts to embed
        task_type: Embedding task type (RETRIEVAL_DOCUMENT, RETRIEVAL_QUERY,
                   QUESTION_ANSWERING, FACT_VERIFICATION, etc.)
        model_name: Model to use (default: EMBEDDING_MODEL env var or gemini-embedding-001)
        output_dimensionality: Output embedding dimensions (default: 768)

    Returns:
        List of embedding vectors
    """
    if not texts:
        return []

    _ensure_vertex_initialized()

    from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel

    model = TextEmbeddingModel.from_pretrained(model_name or DEFAULT_EMBEDDING_MODEL)

    # Create inputs with task type
    inputs = [TextEmbeddingInput(text=t, task_type=task_type) for t in texts]

    # Get embeddings (Vertex AI handles batching internally)
    embeddings = model.get_embeddings(
        inputs,
        output_dimensionality=output_dimensionality,
    )

    return [e.values for e in embeddings]


async def get_embedding(
    text: str,
    task_type: str = "RETRIEVAL_QUERY",
    model_name: str = "",
    output_dimensionality: int = 768,
) -> Optional[List[float]]:
    """
    Generate embedding for a single text using Vertex AI.

    Args:
        text: Text to embed
        task_type: Embedding task type (default: RETRIEVAL_QUERY for queries)
        model_name: Model to use (default: EMBEDDING_MODEL env var or gemini-embedding-001)
        output_dimensionality: Output embedding dimensions (default: 768)

    Returns:
        Embedding vector, or None on failure
    """
    try:
        embeddings = await get_embeddings(
            [text],
            task_type=task_type,
            model_name=model_name,
            output_dimensionality=output_dimensionality,
        )
        return embeddings[0] if embeddings else None
    except Exception as e:
        LOGGER.error(f"Embedding failed: {e}")
        return None


__all__ = ["get_embeddings", "get_embedding", "DEFAULT_EMBEDDING_MODEL"]
