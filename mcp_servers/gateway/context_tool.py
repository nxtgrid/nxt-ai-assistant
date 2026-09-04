"""The gateway's one synthetic tool: get_operating_context.

Not backed by server_registry — a compensating path for a client that
receives InitializeResult.instructions (see instructions.py) but doesn't
surface it to its own model. That field's delivery is guaranteed by the
protocol; a client acting on it is explicitly left to client discretion (the
spec gives it no MUST). Tools are the one MCP channel every client is
guaranteed to expose to its model instead — the actual distinction between
"model-controlled" (tools) and "client-discretion" (instructions) — so a
model that never saw its connection-time instructions still has a second,
equally authoritative way to reach the same content, if its own reasoning
leads it to ask. Confirmed live and necessary, not speculative: a real A/B
test against the same gateway showed Claude (which does surface
`instructions`) resolving a site name immediately from context, and a second
client (which received the identical `instructions` payload — verified all
the way through the wire) hedging as if it had none.

Deliberately NOT a real server_registry tool: it needs the caller's own
already-resolved GatewaySession (org, staff-vs-customer), which every real
request already carries by the time transport.py's flows reach here. Going
through the registry would mean threading caller identity through
server_registry.call_tool's signature, which has no notion of "caller
identity" today — arguments only. This stays a small, self-contained
gateway-level special case instead of reshaping that shared surface for one
caller.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

TOOL_NAME = "gateway__get_operating_context"

# The "gateway__" prefix deliberately does not correspond to any real
# server_registry server name — every other exposed tool's prefix names an
# actual MCP server (customer__, knowledge__, ...); this one visibly does
# not, so a client inspecting tool names can tell it is gateway-level, not
# domain data.
DEFINITION: Dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "[READ-ONLY] Load Anansi's operating context: the domain vocabulary, "
        "site/grid/meter/ticket conventions, diagnostic reasoning, and "
        "curated knowledge for the organization you're signed in as. Call "
        "this once, early in a session, and before answering any question "
        "about a named site, grid, or community. Site names here are drawn "
        "from real-world locations, so a name can also match an unrelated "
        "public record — a utility report, a government dataset, general "
        "knowledge. A match found that way is NOT sufficient confirmation "
        "the name isn't one of ours: check here (or a grid/site lookup "
        "tool) first, and prefer that result over an external match for the "
        "same name. If your MCP client already showed you connection-time "
        "server instructions, you already have this content and do not "
        "need to call it again."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}


def is_operating_context_tool(namespaced_name: str) -> bool:
    """Whether a call/list should be routed here instead of server_registry."""
    return namespaced_name == TOOL_NAME


def operating_context_tool_result(
    session: Any, *, build: Optional[Callable[..., Optional[str]]] = None
) -> Dict[str, Any]:
    """The same call-result shape server_registry.call_tool returns —
    {"success": bool, "result": [{"type": "text", "text": ...}]} on success,
    {"success": False, "error": ...} otherwise — so app.py's _call_tool
    handler needs no special case for this tool once dispatch has already
    routed to it; it already knows how to unwrap this exact shape.

    `build` defaults to gateway.instructions.build_instructions, injectable
    so a test can supply a fake without touching the real PROMPTS singleton
    (see that function's own docstring on why: real credentials in the
    environment make it resolve live DB/Google-Doc content non-hermetically).
    """
    if build is None:
        from gateway.instructions import build_instructions

        build = build_instructions

    text = build(session)
    if not text:
        return {
            "success": False,
            "error": "No operating context is available for this session right now.",
        }
    return {"success": True, "result": [{"type": "text", "text": text}]}
