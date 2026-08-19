"""Settings management service for Anansi App.

Thin adapter over the shared, host-agnostic settings layer:

* :mod:`shared.config.flag_registry` is the single source of truth for which
  flags exist, their types, defaults, service scope, and editability.
* :mod:`shared.config.settings_backends` persists changes through an explicit
  backend (DigitalOcean App Platform or a local-development env file), and is
  read-only when no writable backend is configured.

The public API (``get_current_settings`` / ``update_settings`` /
``get_available_models`` / ``get_log_levels``) is unchanged so the Streamlit
settings UI keeps working as-is.
"""

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from shared.config import flag_registry as registry
from shared.config.settings_backends import MAX_ENV_VAR_SIZE, get_backend

# Backwards-compatible re-exports (derived from the registry, no longer hand-maintained).
DO_NOT_SAVE_TO_DO = registry.non_editable_settings()
__all__ = [
    "SettingsService",
    "DO_NOT_SAVE_TO_DO",
    "MAX_ENV_VAR_SIZE",
    "ValueSource",
    "SettingValue",
]


class ValueSource(Enum):
    """Where a setting's effective value came from."""

    DEFAULT = "default"
    ENVIRONMENT = "environment"
    BACKEND = "backend"


@dataclass(frozen=True)
class SettingValue:
    name: str
    value: Any
    source: ValueSource
    secret_is_set: bool = False

GEMINI_MODEL_FALLBACKS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-flash-latest",
    "gemini-pro-latest",
]

OPENROUTER_MODEL_FALLBACKS = [
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-lite",
    "google/gemini-2.5-pro",
    "openai/gpt-oss-20b:free",
]

OPENROUTER_PROVIDER_ROUTE_FALLBACKS = {
    "google": {
        "google-vertex": "Google Vertex",
        "google-ai-studio": "Google AI Studio",
    },
    "openai": {"openai": "OpenAI"},
    "anthropic": {"anthropic": "Anthropic"},
    "amazon": {"amazon-bedrock": "Amazon Bedrock"},
}


class SettingsService:
    """Manage bot settings via the configured deployment backend."""

    def __init__(self):
        """Initialize the settings service with the active backend."""
        self.backend = get_backend()

    def get_current_settings(self, fetch_from_do: bool = False) -> Dict[str, Any]:
        """Get current settings as a typed dict for the admin UI.

        Args:
            fetch_from_do: If True, overlay fresh values from the deployment
                backend (e.g. DigitalOcean) on top of the local environment.

        Returns:
            Dict of setting name -> typed current value.
        """
        # Typed defaults sourced from the local environment via the registry.
        settings: Dict[str, Any] = registry.settings_defaults()

        if fetch_from_do and self.backend.available():
            remote = self.backend.get_all()
            for name, raw in remote.items():
                flag = registry.FLAGS.get(name)
                if flag is not None and flag.show_in_settings:
                    settings[name] = flag.coerce(raw)

        return settings

    def update_settings(
        self, settings: Dict[str, Any], restart_bot: bool = True
    ) -> Tuple[bool, Optional[str]]:
        """Persist settings via the active backend.

        Read-only (non-editable) and oversized values are filtered out by the
        backend. Returns ``(success, error_message)``.
        """
        success, error = self.backend.update(settings, restart=restart_bot)
        return success, error

    def get_settings_with_provenance(
        self, fetch_from_do: bool = False
    ) -> Dict[str, SettingValue]:
        """Current values annotated with where each one came from.

        ``get_current_settings`` collapses "unset" into "default", so the UI
        cannot show whether a value was chosen or merely inherited. Secrets
        report only whether they are set; their value never leaves this method.
        """
        remote: Dict[str, str] = {}
        if fetch_from_do and self.backend.available():
            remote = self.backend.get_all()

        out: Dict[str, SettingValue] = {}
        for name, flag in registry.FLAGS.items():
            if not flag.show_in_settings:
                continue
            if name in remote:
                raw, source = remote[name], ValueSource.BACKEND
            elif name in os.environ:
                raw, source = os.environ[name], ValueSource.ENVIRONMENT
            else:
                raw, source = None, ValueSource.DEFAULT

            is_set = bool((raw or "").strip())
            if flag.secret:
                out[name] = SettingValue(name, "", source, secret_is_set=is_set)
            else:
                out[name] = SettingValue(name, flag.coerce(raw), source)
        return out

    def get_available_models(self) -> List[str]:
        """Get list of available Gemini models."""
        return self.get_gemini_models()

    def get_llm_provider_options(self) -> Dict[str, str]:
        """Get provider options for the settings UI."""
        return {
            "gemini": "Gemini (Google direct)",
            "openrouter": "OpenRouter",
        }

    def get_gemini_models(self) -> List[str]:
        """Fetch Gemini model ids from Google AI Studio, with safe fallbacks."""
        api_key = os.getenv("GOOGLE_API_KEY", "").strip()
        if not api_key:
            return list(GEMINI_MODEL_FALLBACKS)
        try:
            import requests

            response = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": api_key},
                timeout=8,
            )
            response.raise_for_status()
            models = []
            for item in response.json().get("models", []):
                methods = item.get("supportedGenerationMethods") or []
                name = str(item.get("name") or "").removeprefix("models/")
                if name and "generateContent" in methods:
                    models.append(name)
            return sorted(dict.fromkeys(models)) or list(GEMINI_MODEL_FALLBACKS)
        except Exception:
            return list(GEMINI_MODEL_FALLBACKS)

    def get_openrouter_models(self) -> List[str]:
        """Fetch OpenRouter model ids, with safe fallbacks."""
        try:
            import requests

            headers = {}
            api_key = (
                os.getenv("OPENROUTER_API_KEY", "").strip()
                or os.getenv("OPEN_ROUTER_BEARER_TOKEN", "").strip()
            )
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            response = requests.get(
                "https://openrouter.ai/api/v1/models",
                headers=headers or None,
                timeout=8,
            )
            response.raise_for_status()
            models = [
                str(item.get("id") or "")
                for item in response.json().get("data", [])
                if item.get("id")
            ]
            return sorted(dict.fromkeys(models)) or list(OPENROUTER_MODEL_FALLBACKS)
        except Exception:
            return list(OPENROUTER_MODEL_FALLBACKS)

    def get_openrouter_provider_routes(self, model: str) -> Dict[str, str]:
        """Fetch provider endpoint slugs available for an OpenRouter model."""
        if not model:
            return {}
        try:
            import requests

            headers = {}
            api_key = (
                os.getenv("OPENROUTER_API_KEY", "").strip()
                or os.getenv("OPEN_ROUTER_BEARER_TOKEN", "").strip()
            )
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            response = requests.get(
                f"https://openrouter.ai/api/v1/models/{model}/endpoints",
                headers=headers or None,
                timeout=8,
            )
            response.raise_for_status()
            routes = self._parse_openrouter_provider_routes(response.json())
            return routes or _openrouter_provider_route_fallbacks(model)
        except Exception:
            return _openrouter_provider_route_fallbacks(model)

    @staticmethod
    def _parse_openrouter_provider_routes(payload: Any) -> Dict[str, str]:
        if isinstance(payload, dict):
            data = payload.get("data", payload)
        else:
            data = payload
        if isinstance(data, dict):
            endpoints = data.get("endpoints", [])
        elif isinstance(data, list):
            endpoints = data
        else:
            endpoints = []

        routes: Dict[str, str] = {}
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            tag = str(endpoint.get("tag") or "").strip()
            if not tag:
                continue
            label = str(
                endpoint.get("name")
                or endpoint.get("provider_name")
                or endpoint.get("providerName")
                or tag
            )
            routes[tag] = label
        return dict(sorted(routes.items()))

    def get_log_levels(self) -> List[str]:
        """Get list of available log levels."""
        return ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _openrouter_provider_route_fallbacks(model: str) -> Dict[str, str]:
    provider_prefix = model.split("/", 1)[0].lower()
    return OPENROUTER_PROVIDER_ROUTE_FALLBACKS.get(provider_prefix, {})
