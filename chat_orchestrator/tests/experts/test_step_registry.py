"""Tests for StepHandlerRegistry.

Tests handler registration, retrieval, and the @register_step decorator.
"""

import pytest

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import StepHandlerRegistry, get_step_registry, register_step


class TestStepHandlerRegistry:
    """Test StepHandlerRegistry class."""

    def test_register_and_get_handler(self):
        """Can register and retrieve a handler."""
        registry = StepHandlerRegistry()

        async def my_handler(ctx: StepContext) -> StepResult:
            return StepResult.success()

        registry.register("my_step", my_handler)
        retrieved = registry.get_handler("my_step")
        assert retrieved is my_handler

    def test_get_handler_raises_for_unknown(self):
        """get_handler raises KeyError for unregistered handler."""
        registry = StepHandlerRegistry()

        with pytest.raises(KeyError, match="No step handler registered"):
            registry.get_handler("unknown_step")

    def test_has_handler(self):
        """has_handler returns True for registered handlers."""
        registry = StepHandlerRegistry()

        async def my_handler(ctx: StepContext) -> StepResult:
            return StepResult.success()

        assert registry.has_handler("my_step") is False
        registry.register("my_step", my_handler)
        assert registry.has_handler("my_step") is True

    def test_list_handlers(self):
        """list_handlers returns all registered names."""
        registry = StepHandlerRegistry()

        async def handler1(ctx: StepContext) -> StepResult:
            return StepResult.success()

        async def handler2(ctx: StepContext) -> StepResult:
            return StepResult.success()

        registry.register("step_a", handler1)
        registry.register("step_b", handler2)

        handlers = registry.list_handlers()
        assert "step_a" in handlers
        assert "step_b" in handlers
        assert len(handlers) == 2

    def test_overwrite_handler(self):
        """Registering same name overwrites previous handler."""
        registry = StepHandlerRegistry()

        async def handler1(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"version": 1})

        async def handler2(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"version": 2})

        registry.register("my_step", handler1)
        registry.register("my_step", handler2)

        retrieved = registry.get_handler("my_step")
        assert retrieved is handler2


class TestGlobalRegistry:
    """Test the global registry and decorator."""

    def test_get_step_registry_returns_singleton(self):
        """get_step_registry returns the same instance."""
        registry1 = get_step_registry()
        registry2 = get_step_registry()
        assert registry1 is registry2

    def test_register_step_decorator(self):
        """@register_step decorator registers handler to global registry."""
        # Note: This modifies global state, but that's the intended behavior

        @register_step("test_decorator_step")
        async def test_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(message="Decorated handler")

        registry = get_step_registry()
        assert registry.has_handler("test_decorator_step")
        retrieved = registry.get_handler("test_decorator_step")
        assert retrieved is test_handler

    def test_register_step_decorator_preserves_function(self):
        """@register_step decorator returns original function."""

        @register_step("test_preserve_step")
        async def my_function(ctx: StepContext) -> StepResult:
            return StepResult.success()

        # Function should still be callable directly
        assert my_function.__name__ == "my_function"
        assert callable(my_function)
