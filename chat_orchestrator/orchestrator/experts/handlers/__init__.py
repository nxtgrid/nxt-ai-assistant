"""Expert step handlers.

This package contains step handler implementations for expert subagents.
Handlers are registered via the @register_step decorator and executed
by the WorkflowExecutor.

To add a new handler:
1. Create a module under handlers/{expert_name}/
2. Implement async handler function with @register_step decorator
3. Import the module in this __init__.py

Available handlers are auto-discovered by importing handler modules.
"""

# Import handler modules to trigger registration
from orchestrator.experts.handlers import (
    community_detector,
    community_sizing,
    doc_editor,
    grid_analyst,
    grids_technical_reviewer,
    ingestion_expert,
    package_generator,
    signing,
)

__all__ = [
    "community_detector",
    "community_sizing",
    "doc_editor",
    "grid_analyst",
    "grids_technical_reviewer",
    "ingestion_expert",
    "package_generator",
    "signing",
]
