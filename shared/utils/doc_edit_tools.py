"""The tool seam for document editing.

generate_replacement_markdown used to be one untooled LLM call, so a comment
asking for "the current power levels per phase" got confident invented
numbers. This gives it a bounded, read-only tool loop instead.

Two things are deliberate:

- The runner is *injected*. This module lives in shared/, called from both an
  expert step (which has a StepContext and a ToolExecutor) and an MCP handler
  (which does not). Importing either one here would couple shared/ to
  orchestrator/ and break the moment mcp_servers is deployed on its own.
- Images never enter the model's context. generate_power_chart returns a
  base64 PNG that is hundreds of kilobytes; feeding it back as a tool result
  would blow max_output_tokens on the spot. ToolOutcome carries the text the
  model reads and the images it does not, separately.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Sequence, Tuple

from shared.llm.types import ToolSpec

LOGGER = logging.getLogger(__name__)

# Every server this module may address, longest-prefix first. Server names
# contain underscores, so the split cannot be done on the first one -- see
# ToolExecutor's own multi_word_servers list, which this mirrors.
_SERVERS: Tuple[str, ...] = (
    "equipment_diagnostics",
    "grid_design",
    "customer",
    "knowledge",
)

# What a document edit is allowed to call. Read-only by construction: this
# runs unattended inside a write to somebody's document, so the blast radius
# of a confused model has to stay at "wrote the wrong paragraph".
#
# Pinned against mcp_servers/tool_definitions.json by
# mcp_servers/tests/servers/knowledge_server/test_doc_edit_tool_whitelist.py --
# a rename that misses this list fails there rather than silently removing a
# capability at runtime.
DOC_EDIT_TOOLS: Tuple[str, ...] = (
    "grid_design_find_grid",
    "customer_customer_get_grid_status",
    "equipment_diagnostics_get_historical_power_data",
    "equipment_diagnostics_generate_power_chart",
    "knowledge_get_knowledge_module",
)

MAX_TOOL_ROUNDS = 3


@dataclass(frozen=True)
class ToolOutcome:
    """One tool call's result, split by who may see it.

    ``text`` goes back to the model as a ToolResult. ``images`` are base64
    payloads held aside for the caller to place in the document.
    """

    text: str
    images: Tuple[str, ...] = ()
    is_error: bool = False


# (tool_name, arguments) -> outcome. Never raises: a failed tool is a result
# the model can react to, not an exception that loses the whole edit.
ToolRunner = Callable[[str, Dict[str, Any]], Awaitable[ToolOutcome]]


def split_tool_name(full_name: str) -> Tuple[str, str]:
    """('customer_customer_get_grid_status') -> ('customer', 'customer_get_grid_status')."""
    for server in _SERVERS:
        prefix = f"{server}_"
        if full_name.startswith(prefix):
            return server, full_name[len(prefix) :]
    raise ValueError(f"Unrecognised tool name: {full_name}")


def build_tool_specs(manifest: Mapping[str, Mapping[str, Any]]) -> list[ToolSpec]:
    """Provider-neutral specs for the whitelisted tools present in ``manifest``.

    A tool missing from the manifest is skipped rather than fatal -- the
    editor degrades to fewer capabilities, which is the right failure for a
    feature that is additive.
    """
    specs = []
    for name in DOC_EDIT_TOOLS:
        entry = manifest.get(name)
        if not entry:
            LOGGER.warning(f"Doc-edit tool '{name}' is not in the manifest; skipping")
            continue
        specs.append(
            ToolSpec(
                name=name,
                description=str(entry.get("description", "")),
                parameters_json_schema=dict(entry.get("inputSchema") or {}),
            )
        )
    return specs


def _outcome_from_mcp_content(content: Sequence[Any]) -> ToolOutcome:
    """Split an MCP content list into model-visible text and held-back images.

    Mirrors envelope.attachments_from_tool_results' extraction -- same
    ``type == "image"`` / ``.data`` shape. Keep the two in sync if the MCP
    content shape ever changes.
    """
    texts, images = [], []
    for item in content or []:
        if not isinstance(item, dict):
            texts.append(str(item))
            continue
        if item.get("type") == "image":
            data = item.get("data")
            if data:
                images.append(str(data))
            continue
        texts.append(str(item.get("text", "")))
    return ToolOutcome(text="\n".join(t for t in texts if t), images=tuple(images))


def registry_tool_runner(call_tool) -> ToolRunner:
    """A runner over mcp_servers.server_registry.call_tool(server, tool, args).

    ``call_tool`` is passed in rather than imported so this module stays
    importable (and testable) without mcp_servers on the path.
    """

    async def _run(full_name: str, arguments: Dict[str, Any]) -> ToolOutcome:
        try:
            server, tool = split_tool_name(full_name)
        except ValueError as e:
            return ToolOutcome(text=json.dumps({"error": str(e)}), is_error=True)

        try:
            response = await call_tool(server, tool, arguments)
        except Exception as e:
            LOGGER.warning(f"Doc-edit tool {full_name} failed: {e}", exc_info=True)
            from shared.utils.error_sanitizer import sanitize_error_for_tool_result

            return ToolOutcome(
                text=json.dumps({"error": sanitize_error_for_tool_result(str(e), full_name)}),
                is_error=True,
            )

        if not response.get("success"):
            return ToolOutcome(
                text=json.dumps({"error": response.get("error", "Tool failed")}),
                is_error=True,
            )
        return _outcome_from_mcp_content(response.get("result", []))

    return _run


def executor_tool_runner(executor) -> ToolRunner:
    """A runner over an orchestrator ToolExecutor.

    Uses ``execute`` rather than ``call_tool``: call_tool returns only
    ``result.output``, dropping ``raw_response`` -- which is where a chart's
    base64 lives.
    """

    async def _run(full_name: str, arguments: Dict[str, Any]) -> ToolOutcome:
        from orchestrator.models.schemas import FunctionCall

        try:
            result = await executor.execute(FunctionCall(name=full_name, arguments=arguments), {})
        except Exception as e:
            LOGGER.warning(f"Doc-edit tool {full_name} failed: {e}", exc_info=True)
            return ToolOutcome(text=json.dumps({"error": "Tool call failed"}), is_error=True)

        if not result.success:
            return ToolOutcome(
                text=json.dumps({"error": result.error or "Tool failed"}), is_error=True
            )

        raw = getattr(result, "raw_response", None) or {}
        images = tuple(
            str(item.get("data"))
            for item in (raw.get("result") or [])
            if isinstance(item, dict) and item.get("type") == "image" and item.get("data")
        )
        return ToolOutcome(text=str(result.output or ""), images=images)

    return _run


def default_tool_runner() -> "ToolRunner | None":
    """The in-process registry runner, or None where mcp_servers is absent.

    Production runs the MCP servers inside chat-orchestrator (see
    .do/app.example.yaml -- chat_orchestrator/Dockerfile copies mcp_servers/
    in for its own in-process use), so this resolves there. The standalone
    mcp-gateway service (same .do/app.example.yaml) has its own separate
    Dockerfile and process; it doesn't change this function's own resolution
    path. Returning None elsewhere keeps the untooled path working instead
    of failing the edit.
    """
    try:
        from mcp_servers.server_registry import call_tool
    except ImportError:
        LOGGER.info("mcp_servers is not importable; doc edits will run without tools")
        return None
    return registry_tool_runner(call_tool)
