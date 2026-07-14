"""
Vertex AI Embeddings Helper

Uses Google's embedding model on the Vertex AI backend via the Google Gen AI SDK
(``google-genai``). Model is configurable via EMBEDDING_MODEL env var
(default: gemini-embedding-001).

Migrated off the legacy Vertex AI SDK (``vertexai.language_models``), which Google
deprecated on 2025-06-24 and removed on 2026-06-24
(https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk).
The embedding model and output dimensionality are unchanged, so stored and query
vectors remain in the same space — no re-embedding of the corpus is required.

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

# Cache for the initialized google-genai client (Vertex AI backend)
_client = None


def _get_client():
    """Create (once) a google-genai client on the Vertex AI backend.

    Authenticates with the service-account JSON (GOOGLE_SERVICE_ACCOUNT_JSON) and the
    project/location from it, matching the previous Vertex AI initialization.
    """
    global _client
    if _client is not None:
        return _client

    from google import genai
    from google.oauth2 import service_account

    sa_info = get_service_account_json()
    project_id = sa_info.get("project_id")
    if not project_id:
        raise ValueError("project_id not found in service account JSON")

    credentials = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    location = os.getenv("VERTEX_AI_LOCATION", "us-central1")

    _client = genai.Client(
        vertexai=True,
        project=project_id,
        location=location,
        credentials=credentials,
    )

    LOGGER.info(
        f"google-genai (Vertex AI backend) initialized: project={project_id}, location={location}"
    )
    return _client


async def get_embeddings(
    texts: List[str],
    task_type: str = "RETRIEVAL_DOCUMENT",
    model_name: str = "",
    output_dimensionality: int = 768,
) -> List[List[float]]:
    """
    Generate embeddings for multiple texts using Vertex AI (via google-genai).

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

    from google.genai import types

    client = _get_client()

    response = await client.aio.models.embed_content(
        model=model_name or DEFAULT_EMBEDDING_MODEL,
        contents=list(texts),
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=output_dimensionality,
        ),
    )

    return [list(e.values) for e in response.embeddings]


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
