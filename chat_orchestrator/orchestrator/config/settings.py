"""Application configuration for the Anansi Chat Orchestrator service."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_optional_float(value: Optional[str]) -> Optional[float]:
    """Parse optional float from env var, returning None if unset or 'auto'."""
    if value is None or value.lower() in ("", "auto", "none", "default"):
        return None
    return float(value)


class GeminiModelConfig(BaseModel):
    """Configuration for Gemini generateContent calls."""

    model: str = Field(
        default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        description="Primary model for the selected generation provider.",
    )
    fallback_model: str = Field(
        default_factory=lambda: os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite"),
        description="Fallback model for rate limit recovery.",
    )
    agent_pro_model: str = Field(
        default_factory=lambda: os.environ.get("GEMINI_AGENT_PRO_MODEL", "gemini-2.5-pro"),
        description="Pro model for complex agent tasks (analysis, multi-step reasoning, regulatory interpretation)",
    )
    deep_thinking_model: str = Field(
        default_factory=lambda: os.environ.get("GEMINI_DEEP_THINKING_MODEL", "gemini-pro-latest"),
        description="Model for deep thinking tasks (document editing, complex analysis). No thinking budget cap applied.",
    )
    endpoint_template: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        description="Template for the Gemini endpoint; {model} will be replaced automatically",
    )
    candidate_count: int = Field(default=1, ge=1, le=8)
    temperature: Optional[float] = Field(
        default_factory=lambda: _parse_optional_float(os.environ.get("GEMINI_TEMPERATURE")),
        ge=0.0,
        le=2.0,
        description="Temperature for generation. If None, uses model default (recommended for Gemini 3+)",
    )
    top_k: int = Field(default=40, ge=1, le=100)
    top_p: float = Field(default=0.95, ge=0.1, le=1.0)
    max_output_tokens: int = Field(
        default_factory=lambda: int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "8192")),
        ge=64,
        le=65536,  # Gemini Flash 2.5 supports up to 65,536 output tokens
        description="Max output tokens (includes thinking tokens for thinking models)",
    )
    thinking_budget: int = Field(
        default_factory=lambda: int(os.environ.get("GEMINI_THINKING_BUDGET", "4096")),
        ge=-1,
        le=24576,
        description="Thinking token budget for Gemini 2.5 models (-1=dynamic/omit, 0=off, >0=cap)",
    )
    google_search_grounding: bool = Field(
        default_factory=lambda: os.environ.get("GOOGLE_SEARCH_GROUNDING", "true").lower() == "true",
        description="Enable Google Search grounding for staff users (set via GOOGLE_SEARCH_GROUNDING env var)",
    )

    def endpoint(self) -> str:
        """Return the fully formatted endpoint URL for the primary model."""
        return self.endpoint_template.format(model=self.model)

    def fallback_endpoint(self) -> str:
        """Return the fully formatted endpoint URL for the fallback model."""
        return self.endpoint_template.format(model=self.fallback_model)

    def get_effective_temperature(self, model_name: Optional[str] = None) -> Optional[float]:
        """Get the effective temperature for a model.

        Gemini 3 models recommend using the default temperature (1.0) and
        changing it "may cause looping or degraded performance". This method
        returns None for Gemini 3+ models unless explicitly configured.

        Args:
            model_name: Model name to check. If None, uses self.model.

        Returns:
            Temperature value to use, or None to use model default.
        """
        # If explicitly configured via env var, respect that
        if self.temperature is not None:
            return self.temperature

        # For Gemini 3+ models, use default (None = model decides)
        # For older models, use 0.2 for backward compatibility
        model = model_name or self.model
        if self._is_gemini_3_or_later(model):
            return None  # Use model default (1.0 for Gemini 3)
        return 0.2  # Backward compatible default for Gemini 2.x

    @staticmethod
    def _is_gemini_3_or_later(model_name: str) -> bool:
        """Check if model is Gemini 3 or later (where temp=1.0 is recommended)."""
        model_lower = model_name.lower()
        # Match patterns like: gemini-3.0-flash, gemini-3-pro, flash-3, etc.
        # Also catches 'flash-latest' since it will soon point to Gemini 3
        gemini_3_patterns = (
            "gemini-3",
            "gemini-4",  # Future-proof
            "-3.0-",
            "-3-",
        )
        return any(pattern in model_lower for pattern in gemini_3_patterns)


class ToolParameterSchema(BaseModel):
    """JSON schema snippet describing tool parameters."""

    type: str
    description: Optional[str] = None
    enum: Optional[List[str]] = None
    items: Optional[Dict[str, Any]] = None
    properties: Optional[Dict[str, Any]] = None
    required: Optional[List[str]] = None


# Reasoning parameter injected into every tool for transparency
# Note: Must use uppercase types to match Gemini format used by _convert_property_type
REASONING_PARAM = {
    "type": "STRING",
    "description": "Brief explanation of why you are calling this tool and what you expect to learn or accomplish.",
}


def inject_reasoning_param(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Inject a 'reasoning' parameter into a tool's parameter schema.

    This enables logging of the model's reasoning for each tool call
    without requiring a full ReAct implementation.

    Args:
        parameters: The original tool parameters schema (Gemini format)

    Returns:
        Modified schema with 'reasoning' as a required parameter
    """
    # Deep copy to avoid mutating the original
    import copy

    params = copy.deepcopy(parameters)

    # Ensure properties dict exists
    if "properties" not in params:
        params["properties"] = {}

    # Add reasoning parameter
    params["properties"]["reasoning"] = REASONING_PARAM

    # Make reasoning required
    if "required" not in params:
        params["required"] = []
    if "reasoning" not in params["required"]:
        params["required"] = ["reasoning"] + list(params["required"])

    return params


class ToolServiceConfig(BaseModel):
    """Configuration used to expose and invoke a custom MCP-like service."""

    name: str
    description: str
    url: str = Field(description="The HTTP endpoint the service expects to receive calls on")
    method: str = Field(default="POST", description="HTTP method to use when invoking the service")
    arguments_schema: Dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "OBJECT",
            "properties": {},
            "required": [],
        },
        description="JSON schema describing tool arguments in Gemini format",
    )
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    include_raw_response: bool = Field(
        default=False,
        description="If True, include the literal HTTP response payload in the tool result",
    )
    forward_headers: Dict[str, str] = Field(
        default_factory=dict,
        description="Static headers to include when invoking the service",
    )
    payload_mode: str = Field(
        default="json",
        description="Controls how arguments are attached to the request: 'json', 'query', or 'form'",
    )

    def as_function_declaration(self, include_reasoning: bool = True) -> Dict[str, Any]:
        """Convert the config into Gemini function declaration format.

        Args:
            include_reasoning: If True, inject a 'reasoning' parameter for logging.
        """
        parameters = self.arguments_schema
        if include_reasoning:
            parameters = inject_reasoning_param(parameters)

        return {
            "name": self.name,
            "description": self.description,
            "parameters": parameters,
        }


class RagConfig(BaseModel):
    """Configuration for optional retrieval augmented generation."""

    enabled: bool = False
    top_k: int = Field(default=3, ge=1, le=10)
    collection_path: Path = Field(
        default=Path("storage/rag/knowledge_base.json"),
        description="Path to the local knowledge base file used for retrieval",
    )


class DspyConfig(BaseModel):
    """Configuration switches for DSPy integration."""

    enabled: bool = False
    program_path: Optional[Path] = Field(
        default=None,
        description="Optional path to a DSPy program definition that overrides defaults",
    )


class AppSettings(BaseSettings):
    """Top-level application settings."""

    debug: bool = Field(default=False, alias="DEBUG", description="Enable debug mode")
    llm_provider: str = Field(default="gemini", alias="LLM_PROVIDER")
    google_api_key: str = Field(default="", alias="GOOGLE_API_KEY")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    gemini: GeminiModelConfig = Field(default_factory=GeminiModelConfig)
    known_services: List[ToolServiceConfig] = Field(default_factory=list)
    allow_parallel_calls: bool = True
    max_tool_rounds: int = Field(default=3, ge=1, le=50)
    rag: RagConfig = Field(default_factory=RagConfig)
    dspy: DspyConfig = Field(default_factory=DspyConfig)
    bridge_url: Optional[str] = Field(
        default=None, description="URL to MCP bridge service for dynamic tool loading"
    )

    # Google Docs system instructions
    google_service_account_json: Optional[str] = Field(
        default=None,
        alias="GOOGLE_SERVICE_ACCOUNT_JSON",
        description="Google Cloud service account credentials JSON",
    )
    customer_support_doc_id: Optional[str] = Field(
        default=None,
        alias="CUSTOMER_SUPPORT_DOC_ID",
        description="Google Doc ID for customer support mode system instructions",
    )
    staff_support_doc_id: Optional[str] = Field(
        default=None,
        alias="STAFF_SUPPORT_DOC_ID",
        description="Google Doc ID for staff mode system instructions",
    )
    troubleshooting_procedures_doc_id: Optional[str] = Field(
        default=None,
        alias="TROUBLESHOOTING_PROCEDURES_DOC_ID",
        description="Google Doc ID for troubleshooting procedures shared between customer and staff modes",
    )

    # LangGraph orchestration (Phase 1 rollout)
    use_langgraph: bool = Field(
        default=True,
        alias="USE_LANGGRAPH",
        description="Use LangGraph-based orchestration instead of imperative loop",
    )

    # Response verification (LLM-as-judge for customer mode)
    verification_enabled: bool = Field(
        default=False,
        alias="VERIFICATION_ENABLED",
        description="Enable response verification for customer mode",
    )
    verification_doc_id: Optional[str] = Field(
        default=None,
        alias="VERIFICATION_DOC_ID",
        description="Google Doc ID for verification criteria",
    )
    verification_model: str = Field(
        default="gemini-2.5-flash-lite",
        alias="VERIFICATION_MODEL",
        description="Model to use for response verification",
    )

    # Langfuse LLM observability
    langfuse_enabled: bool = Field(
        default=False,
        alias="LANGFUSE_ENABLED",
        description="Enable Langfuse LLM observability tracing",
    )
    langfuse_secret_key: str = Field(
        default="",
        alias="LANGFUSE_SECRET_KEY",
    )
    langfuse_public_key: str = Field(
        default="",
        alias="LANGFUSE_PUBLIC_KEY",
    )
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com",
        alias="LANGFUSE_HOST",
        description="Langfuse server URL (cloud or self-hosted)",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
        populate_by_name=True,
    )


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Return cached application settings."""

    settings = AppSettings()
    if not settings.known_services:
        inferred = _load_default_services()
        settings.known_services.extend(inferred)
    return settings


def _load_default_services() -> List[ToolServiceConfig]:
    """Load default services defined in config/services.yaml if it exists."""

    config_path = Path(__file__).resolve().parent.parent / "config" / "services.yaml"
    if not config_path.exists():
        return []

    import yaml  # type: ignore[import-untyped]  # imported lazily to avoid dependency

    with config_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}

    services_raw = payload.get("services", [])
    services: List[ToolServiceConfig] = []
    for raw in services_raw:
        services.append(ToolServiceConfig(**raw))
    return services


__all__ = [
    "AppSettings",
    "GeminiModelConfig",
    "ToolServiceConfig",
    "get_settings",
    "inject_reasoning_param",
]
