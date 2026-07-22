"""MCP Payment Processor Server - Handles payment transaction status checks."""

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import mcp.types as types
from dotenv import load_dotenv
from mcp.server import Server

# Load environment variables from .env file BEFORE importing shared_code
load_dotenv()

from shared_code.stdio_runner import run_stdio_server
from shared_code.tool_registry import ToolRegistry

from shared.utils.http_client import HTTPClientMixin
from shared.utils.response_formatters import compose_error_response, compose_json_response

from .tool_schemas import TOOL_SCHEMAS

# Configure logging to stderr for Claude Desktop visibility
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("payment-processor-server")

# Startup messages to stderr
print("🚀 Payment Processor MCP Server starting...", file=sys.stderr)
print(f"📍 Python path: {sys.path}", file=sys.stderr)
print(f"📂 Working directory: {os.getcwd()}", file=sys.stderr)

# Initialize MCP server
server = Server("payment-processor-server")

# Payment processor API configuration
PAYMENT_PROCESSOR_API_URL = os.getenv("PAYMENT_PROCESSOR_API_URL", "")
PAYMENT_PROCESSOR_SECRET_KEY = os.getenv("PAYMENT_PROCESSOR_SECRET_KEY")


class PaymentProcessorClient(HTTPClientMixin):
    """Client for interacting with payment processor API."""

    def __init__(self):
        super().__init__()
        self.base_url: Optional[str] = None
        self.secret_key: Optional[str] = None
        self._auto_configure_from_env()

    def _auto_configure_from_env(self):
        """Load credentials from environment variables."""
        if PAYMENT_PROCESSOR_API_URL and PAYMENT_PROCESSOR_SECRET_KEY:
            self.base_url = PAYMENT_PROCESSOR_API_URL
            self.secret_key = PAYMENT_PROCESSOR_SECRET_KEY
            logger.info("Payment processor client auto-configured from environment")
        else:
            logger.warning(
                "Payment processor credentials not found in environment. "
                "Set PAYMENT_PROCESSOR_API_URL and PAYMENT_PROCESSOR_SECRET_KEY."
            )

    async def _make_request(self, endpoint: str) -> Dict[str, Any]:
        """
        Make authenticated request to payment processor API.

        Args:
            endpoint: API endpoint path (e.g., "/transactions/123/verify")

        Returns:
            API response data

        Raises:
            Exception: If request fails or credentials missing
        """
        if not self.base_url or not self.secret_key:
            raise Exception(
                "Payment processor client not configured. "
                "Set PAYMENT_PROCESSOR_API_URL and PAYMENT_PROCESSOR_SECRET_KEY environment variables."
            )

        url = f"{self.base_url}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.secret_key}",
        }

        client = await self._get_client()

        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return dict(await response.json())
        except Exception as e:
            logger.error(f"Payment processor API request failed: {e}")
            raise

    async def verify_transaction_by_id(self, transaction_id: str) -> Dict[str, Any]:
        """
        Verify transaction using payment processor transaction ID.

        Args:
            transaction_id: Payment processor transaction ID

        Returns:
            Transaction verification response
        """
        logger.info(f"Verifying transaction by ID: {transaction_id}")
        endpoint = f"/transactions/{transaction_id}/verify"
        return await self._make_request(endpoint)

    async def verify_transaction_by_reference(self, tx_ref: str) -> Dict[str, Any]:
        """
        Verify transaction using merchant transaction reference.

        Args:
            tx_ref: Merchant transaction reference (e.g., from Platform orders.external_reference)

        Returns:
            Transaction verification response
        """
        logger.info(f"Verifying transaction by reference: {tx_ref}")
        from urllib.parse import quote

        endpoint = f"/transactions/verify_by_reference?tx_ref={quote(tx_ref, safe='')}"
        return await self._make_request(endpoint)

    async def close(self):
        """Close HTTP session."""
        await self.close_session()


# Global client instance
payment_processor_client = PaymentProcessorClient()

registry = ToolRegistry("payment_processor")
_SCHEMAS_BY_NAME = {s["name"]: s for s in TOOL_SCHEMAS}


@registry.tool("check_transaction_status", _SCHEMAS_BY_NAME["check_transaction_status"])
async def check_transaction_status(args: Dict[str, Any]) -> List[types.TextContent]:
    """
    Check payment transaction status.

    Args:
        args: Tool arguments containing transaction_id or tx_ref

    Returns:
        Transaction status details
    """
    transaction_id = args.get("transaction_id")
    tx_ref = args.get("tx_ref")

    # Validate inputs
    if not transaction_id and not tx_ref:
        return [
            types.TextContent(
                type="text",
                text="Error: Either transaction_id or tx_ref must be provided",
            )
        ]

    try:
        # Choose verification method based on provided identifier
        if transaction_id:
            result = await payment_processor_client.verify_transaction_by_id(transaction_id)
        else:
            result = await payment_processor_client.verify_transaction_by_reference(tx_ref)

        # Check if verification was successful
        if result.get("status") == "error":
            error_message = result.get("message", "Unknown error")
            return list(
                compose_error_response(Exception(f"Payment verification failed: {error_message}"))
            )

        # Extract transaction data
        transaction_data = result.get("data", {})

        # Format response with key transaction details
        response_data = {
            "verification_status": result.get("status"),
            "message": result.get("message"),
            "transaction": {
                "id": transaction_data.get("id"),
                "tx_ref": transaction_data.get("tx_ref"),
                "flw_ref": transaction_data.get("flw_ref"),
                "status": transaction_data.get("status"),
                "amount": transaction_data.get("amount"),
                "currency": transaction_data.get("currency"),
                "charged_amount": transaction_data.get("charged_amount"),
                "app_fee": transaction_data.get("app_fee"),
                "merchant_fee": transaction_data.get("merchant_fee"),
                "amount_settled": transaction_data.get("amount_settled"),
                "payment_type": transaction_data.get("payment_type"),
                "processor_response": transaction_data.get("processor_response"),
                "created_at": transaction_data.get("created_at"),
                "customer": transaction_data.get("customer", {}),
                "card": transaction_data.get("card", {}),
            },
        }

        logger.info(
            f"Transaction verified - ID: {transaction_data.get('id')}, "
            f"Status: {transaction_data.get('status')}, "
            f"Amount: {transaction_data.get('amount')} {transaction_data.get('currency')}"
        )

        return list(compose_json_response(response_data))

    except Exception as e:
        logger.error(f"Error checking transaction status: {e}")
        return list(
            compose_error_response(Exception(f"Failed to check transaction status: {str(e)}"))
        )


handle_list_tools = server.list_tools()(registry.handle_list_tools)
handle_call_tool = server.call_tool()(registry.handle_call_tool)


@server.list_resources()
async def handle_list_resources() -> List[types.Resource]:
    """List available resources."""
    return [
        types.Resource(
            uri="payment_processor://config",
            name="Payment Processor Configuration",
            description="Current payment processor server configuration",
            mimeType="application/json",
        ),
        types.Resource(
            uri="payment_processor://status",
            name="Connection Status",
            description="Payment processor API connection status",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Read resource content."""
    if uri == "payment_processor://config":
        config = {
            "api_url": PAYMENT_PROCESSOR_API_URL,
            "configured": bool(PAYMENT_PROCESSOR_SECRET_KEY),
            "server_name": "payment-processor-server",
            "server_version": "1.0.0",
        }
        return json.dumps(config, indent=2)
    elif uri == "payment_processor://status":
        status = {
            "configured": bool(PAYMENT_PROCESSOR_API_URL and PAYMENT_PROCESSOR_SECRET_KEY),
            "api_url": PAYMENT_PROCESSOR_API_URL,
            "has_secret_key": bool(PAYMENT_PROCESSOR_SECRET_KEY),
        }
        return json.dumps(status, indent=2)
    else:
        raise ValueError(f"Unknown resource: {uri}")


async def main():
    """Main entry point."""
    logger.info("Starting Payment Processor MCP Server...")
    await run_stdio_server(
        server,
        name="payment-processor-server",
        label="Payment processor",
        on_cleanup=payment_processor_client.close,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Payment processor server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Payment processor server crashed: {e}", file=sys.stderr)
        sys.exit(1)
