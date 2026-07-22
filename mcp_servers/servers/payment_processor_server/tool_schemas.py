"""Tool schema for the Payment Processor MCP server.

Extracted verbatim from ``handle_list_tools`` as part of migrating the server
onto ``shared_code.tool_registry.ToolRegistry``.

Plain dict rather than a ``types.Tool`` object: ``ToolRegistry.handle_list_tools``
constructs a fresh ``Tool`` per call, so sharing a model instance across calls
would let one caller's mutation reach the next.

``visible_to_customer: False`` — payment_processor is staff-only.
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "check_transaction_status",
        "description": (
            "[READ-ONLY] Check the status of a payment transaction. "
            "Provide either transaction_id (payment processor's internal ID) or tx_ref (merchant reference). "
            "Returns transaction status (successful/pending/failed), amount, currency, payment type, "
            "and customer details. This tool ONLY retrieves transaction information - it CANNOT initiate payments, "
            "refunds, or modify transactions. Useful for verifying payment completion and troubleshooting payment issues."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "transaction_id": {
                    "type": "string",
                    "description": (
                        "Payment processor transaction ID - labeled 'Transaction No.' on receipts "
                        "(e.g., '2504030201001869149859T'). "
                        "NOT the 'Session ID' which is a different identifier."
                    ),
                },
                "tx_ref": {
                    "type": "string",
                    "description": (
                        "Merchant transaction reference exactly as provided by the customer. "
                        "Do NOT construct or guess this value - it must come from the customer's receipt or records."
                    ),
                },
            },
            "oneOf": [
                {"required": ["transaction_id"]},
                {"required": ["tx_ref"]},
            ],
        },
        "visible_to_customer": False,
    },
]
