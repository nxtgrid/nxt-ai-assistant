"""Expert subagent system for multi-session work packets.

This package provides the infrastructure for expert subagents that work on
durable "work packets" persisting across chat sessions. Experts handle
specialized tasks like grid analysis, KPI reports, and ticket triage.

Architecture:
    - StepContext/StepResult: Context and result types for step execution
    - StepHandlerRegistry: Registry for function step handlers
    - WorkflowExecutor: Executes hybrid LLM/function workflows
    - Handlers: Concrete step implementations per expert

Usage:
    from orchestrator.experts import (
        StepContext,
        StepResult,
        register_step,
        get_step_handler,
    )

    # Define a step handler
    @register_step("my_step")
    async def my_step(context: StepContext) -> StepResult:
        data = await context.mcp_executor.call_tool("some_tool", {...})
        return StepResult.success(data={"result": data})
"""

# Import handlers to register them
from orchestrator.experts import handlers  # noqa: F401
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import (
    StepHandler,
    StepHandlerRegistry,
    get_step_handler,
    get_step_registry,
    register_step,
)

__all__ = [
    "StepContext",
    "StepResult",
    "StepHandler",
    "StepHandlerRegistry",
    "get_step_handler",
    "get_step_registry",
    "register_step",
]
