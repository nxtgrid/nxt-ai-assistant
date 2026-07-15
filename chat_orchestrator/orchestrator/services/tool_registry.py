"""Tool registry keeps track of functions exposed to Gemini."""

from __future__ import annotations

from typing import Dict, Iterable, List

from orchestrator.config.settings import AppSettings, ToolServiceConfig, get_settings
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


class ToolRegistry:
    """Registers and provides access to available tool definitions."""

    def __init__(self, settings: AppSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._services: Dict[str, ToolServiceConfig] = {
            svc.name: svc for svc in self._settings.known_services
        }
        LOGGER.debug("Initialised tool registry with %d services", len(self._services))

    def register(self, config: ToolServiceConfig) -> None:
        """Register or overwrite a service configuration."""

        LOGGER.info("Registering service %s", config.name)
        self._services[config.name] = config

    def get_service(self, name: str) -> ToolServiceConfig:
        """Retrieve the configuration for a named service."""

        if name not in self._services:
            raise KeyError(f"No service registered with name {name}")
        return self._services[name]

    def tools_payload(self) -> List[Dict[str, object]]:
        """Return provider-neutral function declarations for generation."""

        if not self._services:
            return []
        return [svc.as_function_declaration() for svc in self._services.values()]

    def as_declarations(self) -> List[Dict[str, object]]:
        """Return list of function declarations for reuse in documentation/tests."""

        return [svc.as_function_declaration() for svc in self._services.values()]

    def services(self) -> Iterable[ToolServiceConfig]:
        """Iterate over registered service configs."""

        return self._services.values()


__all__ = ["ToolRegistry"]
