"""State definition for persistent agent graphs.

Persistent agents are long-running LangGraph graphs that wake on events
(equipment alerts, staff messages, scheduled checks) and act on behalf
of a domain entity (e.g., a grid). State is checkpointed to PostgreSQL
between wake cycles.

All fields must be JSON-serializable (no client objects, no callables).
Transient resources (Gemini client, MCP executor) are injected at wake
time and NOT stored in state.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict


class PersistentAgentState(TypedDict, total=False):
    """State for a persistent agent's LangGraph graph.

    Split into three categories:
    - Identity: set once from the instance row, never changes
    - Context: loaded fresh by load_context on each wake
    - Execution: produced by think_and_act, consumed by save_and_wait
    """

    # ── Identity (from persistent_agent_instances row) ──────────────────
    instance_id: str  # UUID of the persistent_agent_instances row
    thread_id: str  # LangGraph thread ID: "{expert_id}:{entity_id}"
    organization_id: int

    # ── Context (loaded fresh each wake by load_context) ────────────────
    entity_data: Dict[str, Any]  # Current grid status, VRM data, etc.
    metadata: Dict[str, Any]  # Accumulated knowledge from instance row
    current_events: List[Dict[str, Any]]  # Events being processed this wake
    recent_event_history: List[Dict[str, Any]]  # Last 30 completed events

    # Telegram target (where to send staff messages)
    telegram_chat_id: str
    telegram_topic_id: Optional[str]

    # LLM config (from Expert Instructions Google Doc)
    system_instructions: str
    available_tools: List[str]

    # ── Execution output (produced by think_and_act) ────────────────────
    assessment: str  # LLM's situation assessment
    observations: List[Dict[str, Any]]  # Read-only tool calls (information gathering)
    actions_taken: List[Dict[str, Any]]  # Write/mutating tool calls (real actions)
    metadata_updates: Dict[str, Any]  # Changes to persist to instance.metadata
