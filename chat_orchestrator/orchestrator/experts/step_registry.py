"""Registry for expert step handler functions.

Follows the ToolRegistry pattern from orchestrator/services/tool_registry.py
with a dict-based registry and register/get methods.

Parameter confirmation is now handled at the expert/workflow level using
schema parsed from the Google Doc definition (### Inputs section), not at
the step/handler level. Handlers do NOT need parameter schemas - they just
use context.get_parameter_value() which automatically handles user overrides.

Usage:
    from orchestrator.experts.step_registry import register_step, get_step_registry

    # Register a step handler using decorator
    @register_step("fetch_metrics")
    async def fetch_metrics(context: StepContext) -> StepResult:
        ...

    # Get handler by name
    registry = get_step_registry()
    handler = registry.get_handler("fetch_metrics")
    result = await handler(context)

    # List all registered handlers
    handlers = registry.list_handlers()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from orchestrator.config.settings import AppSettings, get_settings
from orchestrator.experts.step_context import StepContext, StepResult
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Type for step handler functions
StepHandler = Callable[[StepContext], Awaitable[StepResult]]


# NOTE: ParameterDefinition and StepSchema are now in parameter_resolver.py
# They are kept here ONLY for backwards compatibility if any code references them
@dataclass
class ParameterDefinition:
    """DEPRECATED: Use parameter_resolver.ParameterDefinition instead.

    Parameter confirmation now uses expert-level schema from Google Doc.
    """

    name: str
    param_type: str = "string"
    description: str = ""
    required: bool = False
    default: Any = None
    source_hint: str = ""
    editable: bool = True


@dataclass
class StepSchema:
    """DEPRECATED: Use parameter_resolver.PacketParameterSchema instead.

    Parameter confirmation now uses expert-level schema from Google Doc.
    """

    handler_name: str
    parameters: List[ParameterDefinition] = field(default_factory=list)
    description: str = ""
    confirmation_required: bool = True


class StepHandlerRegistry:
    """Registry for expert step handler functions.

    Pattern from: orchestrator/services/tool_registry.py
    """

    def __init__(self, settings: Optional[AppSettings] = None) -> None:
        """Initialize the registry.

        Args:
            settings: Application settings (optional)
        """
        self._settings = settings or get_settings()
        self._handlers: Dict[str, StepHandler] = {}
        self._schemas: Dict[str, StepSchema] = {}

    def register(
        self,
        name: str,
        handler: StepHandler,
        schema: Optional[StepSchema] = None,
    ) -> None:
        """Register a step handler function with optional parameter schema.

        Args:
            name: Unique name for the handler
            handler: Async function that takes StepContext and returns StepResult
            schema: Optional parameter schema for interactive confirmation

        Raises:
            ValueError: If handler with this name already exists
        """
        if name in self._handlers:
            LOGGER.warning(f"Overwriting step handler: {name}")

        self._handlers[name] = handler
        if schema:
            self._schemas[name] = schema
        LOGGER.debug(f"Registered step handler: {name} (schema: {schema is not None})")

    def get_handler(self, name: str) -> StepHandler:
        """Get a registered step handler by name.

        Args:
            name: Handler name

        Returns:
            The registered handler function

        Raises:
            KeyError: If handler not found (matches ToolRegistry pattern)
        """
        if name not in self._handlers:
            raise KeyError(f"No step handler registered with name: {name}")
        return self._handlers[name]

    def has_handler(self, name: str) -> bool:
        """Check if a handler is registered.

        Args:
            name: Handler name

        Returns:
            True if handler exists
        """
        return name in self._handlers

    def get_schema(self, name: str) -> Optional[StepSchema]:
        """Get parameter schema for a handler.

        Args:
            name: Handler name

        Returns:
            StepSchema or None if no schema defined
        """
        return self._schemas.get(name)

    def has_schema(self, name: str) -> bool:
        """Check if a handler has a parameter schema.

        Args:
            name: Handler name

        Returns:
            True if handler has schema defined
        """
        return name in self._schemas

    def list_handlers(self) -> list[str]:
        """List all registered handler names.

        Returns:
            List of handler names
        """
        return list(self._handlers.keys())

    def unregister(self, name: str) -> bool:
        """Unregister a step handler.

        Args:
            name: Handler name

        Returns:
            True if handler was removed, False if not found
        """
        if name in self._handlers:
            del self._handlers[name]
            LOGGER.debug(f"Unregistered step handler: {name}")
            return True
        return False

    def clear(self) -> None:
        """Clear all registered handlers and schemas.

        Useful for testing.
        """
        self._handlers.clear()
        self._schemas.clear()
        LOGGER.debug("Cleared all step handlers and schemas")


# Global registry instance (lazy initialized)
_global_registry: Optional[StepHandlerRegistry] = None


def get_step_registry() -> StepHandlerRegistry:
    """Get or create global step handler registry.

    Returns:
        The global StepHandlerRegistry instance
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = StepHandlerRegistry()
    return _global_registry


def register_step(name: str):
    """Decorator to register a step handler function.

    Parameter confirmation is handled at the expert/workflow level using
    schema from the Google Doc definition, NOT at the step level.
    Handlers just use context.get_parameter_value() which automatically
    respects any user overrides from the confirmation flow.

    Usage:
        @register_step("fetch_metrics")
        async def fetch_metrics(context: StepContext) -> StepResult:
            # Get parameters - overrides from confirmation flow are automatic
            site_name = context.get_parameter_value("site_name")
            ...

    Args:
        name: Unique name for the handler (must match [function:name] in workflow)

    Returns:
        Decorator function
    """

    def decorator(func: StepHandler) -> StepHandler:
        get_step_registry().register(name, func)
        return func

    return decorator


def get_step_handler(name: str) -> Optional[StepHandler]:
    """Get a step handler by name (convenience function).

    Args:
        name: Handler name

    Returns:
        Handler function or None if not found
    """
    registry = get_step_registry()
    if registry.has_handler(name):
        return registry.get_handler(name)
    return None


def get_step_schema(name: str) -> Optional[StepSchema]:
    """DEPRECATED: Step-level schemas are no longer used.

    Parameter confirmation now uses expert-level schema from Google Doc.
    This function is kept for backwards compatibility only.

    Args:
        name: Handler name

    Returns:
        StepSchema or None (always None in new architecture)
    """
    return get_step_registry().get_schema(name)


__all__ = [
    "StepHandlerRegistry",
    "StepHandler",
    "ParameterDefinition",  # Kept for backwards compatibility
    "StepSchema",  # Kept for backwards compatibility
    "get_step_registry",
    "register_step",
    "get_step_handler",
    "get_step_schema",  # Deprecated
]
