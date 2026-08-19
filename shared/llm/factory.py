"""Factory helpers for the default LLM gateway implementations."""

from __future__ import annotations

import os
from typing import Any

from shared.llm.gemini import GeminiGateway
from shared.llm.model_tiers import resolve_model
from shared.llm.openrouter import OpenRouterGateway
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


def _resolve_provider() -> str:
    return os.getenv("LLM_PROVIDER", "gemini").strip().lower()


def is_generation_configured() -> bool:
    """True if the currently configured LLM_PROVIDER has its required API key set.

    Use this for a fail-open pre-flight check before calling
    get_default_generation_gateway() -- when a caller wants to skip or
    degrade gracefully (return None / fall back to a non-LLM path) rather
    than construct a gateway and let it fail. Do NOT read GOOGLE_API_KEY or
    OPENROUTER_API_KEY directly for this purpose: a provider-specific check
    silently breaks the moment LLM_PROVIDER points at the other provider,
    since the key it's checking for is the wrong one for whatever's actually
    configured -- and worse, if the *other* provider's key happens to be set
    too (e.g. a leftover GOOGLE_API_KEY under LLM_PROVIDER=openrouter), the
    check passes but every real call fails, because
    get_default_generation_gateway()'s own api_key= override (if a caller
    also passes that wrong key through explicitly) takes priority over the
    correct provider's env var. Every generation-gateway call site that
    special-cased GOOGLE_API_KEY this way failed identically the moment
    OpenRouter became a possible LLM_PROVIDER value; see the grafana
    panel_description indexer incident, 2026-08-19 (all 16 enabled panels'
    descriptions silently replaced with a fallback string, in fractions of a
    second, because GOOGLE_API_KEY was forced through as if it were valid
    for whatever LLM_PROVIDER was actually configured).
    """
    if _resolve_provider() in {"openrouter", "open-router"}:
        return bool(os.getenv("OPENROUTER_API_KEY"))
    return bool(os.getenv("GOOGLE_API_KEY"))


def get_default_generation_gateway(
    *,
    api_key: str | None = None,
    default_model: str | None = None,
    fallback_model: str | None = None,
) -> GeminiGateway | OpenRouterGateway:
    provider = _resolve_provider()
    if provider in {"openrouter", "open-router"}:
        return OpenRouterGateway(
            api_key=api_key or os.getenv("OPENROUTER_API_KEY"),
            default_model=default_model or os.getenv("OPENROUTER_MODEL"),
            base_url=os.getenv("OPENROUTER_BASE_URL"),
            http_referer=os.getenv("OPENROUTER_HTTP_REFERER"),
            app_title=os.getenv("OPENROUTER_APP_TITLE"),
            require_parameters=_optional_bool_env("OPENROUTER_REQUIRE_PARAMETERS"),
        )
    if provider != "gemini":
        raise ValueError(
            f"Unsupported LLM_PROVIDER={provider!r}; expected 'gemini' or 'openrouter'"
        )
    return GeminiGateway(
        api_key=api_key or os.getenv("GOOGLE_API_KEY"),
        default_model=default_model or resolve_model("fast"),
        fallback_model=fallback_model or os.getenv("FALLBACK_MODEL"),
    )


def get_default_embedding_gateway() -> GeminiGateway:
    return GeminiGateway(
        client=_get_vertex_genai_client(),
        default_embedding_model=os.getenv("EMBEDDING_MODEL", "gemini-embedding-001"),
    )


def _optional_bool_env(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in {"true", "1", "yes", "on"}
