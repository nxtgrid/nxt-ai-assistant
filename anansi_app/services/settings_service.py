"""Settings management service for Anansi App.

Thin adapter over the shared, host-agnostic settings layer:

* :mod:`shared.config.flag_registry` is the single source of truth for which
  flags exist, their types, defaults, service scope, and editability.
* :mod:`shared.config.settings_backends` persists changes to the deployment
  (DigitalOcean App Platform, or a portable env-file on any other host).

The public API (``get_current_settings`` / ``update_settings`` /
``get_available_models`` / ``get_log_levels``) is unchanged so the Streamlit
settings UI keeps working as-is.
"""

from typing import Any, Dict, List, Optional, Tuple

from shared.config import flag_registry as registry
from shared.config.settings_backends import MAX_ENV_VAR_SIZE, get_backend

# Backwards-compatible re-exports (derived from the registry, no longer hand-maintained).
DO_NOT_SAVE_TO_DO = registry.non_editable_settings()
__all__ = ["SettingsService", "DO_NOT_SAVE_TO_DO", "MAX_ENV_VAR_SIZE"]


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

    def get_available_models(self) -> List[str]:
        """Get list of available Gemini models."""
        return [
            "gemini-flash-latest",
            "gemini-pro-latest",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ]

    def get_log_levels(self) -> List[str]:
        """Get list of available log levels."""
        return ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
