"""Tool schemas for the Reference MCP server.

Extracted verbatim from ``handle_list_tools`` as part of migrating the server
onto ``shared_code.tool_registry.ToolRegistry``.

Plain dicts rather than ``types.Tool`` objects: ``ToolRegistry.handle_list_tools``
constructs a fresh ``Tool`` per call, so sharing model instances across calls
would let one caller's mutation reach the next.

All three tools set ``visible_to_customer: False`` — reference lookups
(Nigerian import tariff/prohibition/standards data) are staff-only.
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "lookup_tariff",
        "description": (
            "[READ-ONLY] Look up Nigeria import tariff by item description or CET code. "
            "Accepts fuzzy item descriptions (e.g. 'breeding horses') or CET codes with or "
            "without dots (e.g. '0101.21.00.00' or '0101210000'). Returns VAT, levy (LVY), "
            "excise (EXC), and date of validity (DOV) from the Nigeria Customs import tariff schedule."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Item description or CET code (dots optional)",
                }
            },
            "required": ["query"],
        },
        "visible_to_customer": False,
    },
    {
        "name": "get_import_prohibition_list",
        "description": (
            "[READ-ONLY] Fetch the current Nigeria import prohibition list from the Nigeria "
            "Customs Service website. Returns all categories of absolutely prohibited imports."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "visible_to_customer": False,
    },
    {
        "name": "lookup_import_standard",
        "description": (
            "[READ-ONLY] Look up Nigeria import standards by item name. Fuzzy-matches against "
            "the Nigeria Import Standards document and returns the applicable HS codes and remarks "
            "(standard reference, e.g. NIS 54:2017)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Item name to search for",
                }
            },
            "required": ["query"],
        },
        "visible_to_customer": False,
    },
]
