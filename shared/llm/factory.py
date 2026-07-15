"""Factory helpers for the default LLM gateway implementations."""

from __future__ import annotations

import os
from typing import Any

from shared.llm.gemini import GeminiGateway
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

_vertex_client: Any | None = None


def _get_vertex_genai_client() -> Any:
    global _vertex_client
    if _vertex_client is not None:
        return _vertex_client

    from google import genai
    from google.oauth2 import service_account

    from shared.utils.google_auth import get_service_account_json

    sa_info = get_service_account_json()
    project_id = sa_info.get("project_id")
    if not project_id:
        raise ValueError("project_id not found in service account JSON")

    credentials = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    location = os.getenv("VERTEX_AI_LOCATION", "us-central1")

    _vertex_client = genai.Client(
        vertexai=True,
        project=project_id,
        location=location,
        credentials=credentials,
    )
    LOGGER.info(
        f"google-genai (Vertex AI backend) initialized: project={project_id}, location={location}"
    )
    return _vertex_client


def get_default_generation_gateway() -> GeminiGateway:
    return GeminiGateway(
        api_key=os.getenv("GOOGLE_API_KEY"),
        default_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        fallback_model=os.getenv("GEMINI_FALLBACK_MODEL"),
    )


def get_default_embedding_gateway() -> GeminiGateway:
    return GeminiGateway(
        client=_get_vertex_genai_client(),
        default_embedding_model=os.getenv("EMBEDDING_MODEL", "gemini-embedding-001"),
    )
