"""Executes tool calls requested by Gemini.

IMPORTANT: All errors returned to the LLM are sanitized to prevent
technical details from leaking to end users.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

import httpx

from orchestrator.config.settings import AppSettings
from orchestrator.models.schemas import FunctionCall, ToolCallResult
from orchestrator.services.tool_registry import ToolRegistry
from shared.utils.error_sanitizer import sanitize_error_for_tool_result
from shared.utils.langfuse_utils import langfuse_observe, update_span
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Conditional import for direct registry access (when mcp_servers is available)
try:
    from mcp_servers.server_registry import call_tool as registry_call_tool

    DIRECT_REGISTRY_AVAILABLE = True
    LOGGER.info("Direct MCP registry available - will use direct calls instead of HTTP bridge")
except ImportError:
    DIRECT_REGISTRY_AVAILABLE = False
    registry_call_tool = None  # type: ignore[assignment, misc]
    LOGGER.info("Direct MCP registry not available - will use HTTP bridge for tool calls")


class ToolExecutor:
    """Invokes configured MCP-like services via HTTP or MCP bridge."""

    def __init__(
        self,
        registry: Optional[ToolRegistry],
        settings: AppSettings,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        timeout = httpx.Timeout(30.0)
        self._client = client or httpx.AsyncClient(timeout=timeout)

    @langfuse_observe(name="tool-execution")
    async def execute(self, call: FunctionCall, metadata: Dict[str, Any]) -> ToolCallResult:
        """Execute a single tool call and capture the result."""
        update_span(input={"tool": call.name, "args": call.arguments})

        # Check if this is a bridge-based tool (format: servername_toolname)
        # Server names can contain underscores (e.g., "equipment_control", "payment_processor")
        # So we match against known multi-word server prefixes first
        # Try MCP tools if we have either direct registry OR HTTP bridge configured
        if "_" in call.name and (DIRECT_REGISTRY_AVAILABLE or self._settings.bridge_url):
            # Known servers with underscores in their names (longest first)
            multi_word_servers = [
                "equipment_diagnostics",
                "equipment_control",
                "payment_processor",
                "grid_design",
            ]

            server_name = None
            tool_name = None

            # Check multi-word servers first
            for srv in multi_word_servers:
                prefix = f"{srv}_"
                if call.name.startswith(prefix):
                    server_name = srv
                    tool_name = call.name[len(prefix) :]
                    break

            # Fall back to splitting on first underscore for single-word servers
            if not server_name:
                parts = call.name.split("_", 1)
                if len(parts) == 2:
                    server_name = parts[0]
                    tool_name = parts[1]

            if server_name and tool_name:
                # Attempt to call via bridge
                result = await self._execute_bridge_tool(
                    server_name=server_name,
                    tool_name=tool_name,
                    arguments=call.arguments,
                    metadata=metadata,
                )
                if result is not None:
                    return result

        # Fall back to registry-based execution
        if not self._registry:
            LOGGER.error("No registry available and tool %s is not a bridge tool", call.name)
            return ToolCallResult(
                name=call.name,
                success=False,
                output=None,
                error="No tool registry available",
            )

        try:
            service_config = self._registry.get_service(call.name)
        except KeyError as exc:  # pragma: no cover - defensive
            LOGGER.error("Attempted to call unknown service %s", call.name)
            sanitized = sanitize_error_for_tool_result(str(exc), call.name)
            return ToolCallResult(name=call.name, success=False, output=None, error=sanitized)

        method = service_config.method.upper()
        headers = dict(service_config.forward_headers)
        if metadata:
            headers.setdefault("X-Borg-Metadata", json.dumps(metadata))

        request_kwargs: Dict[str, Any] = {"headers": headers}
        payload_mode = service_config.payload_mode.lower()
        arguments = call.arguments or {}

        if payload_mode == "json":
            request_kwargs["json"] = arguments
        elif payload_mode == "query":
            request_kwargs["params"] = arguments
        elif payload_mode == "form":
            request_kwargs["data"] = arguments
        else:
            return ToolCallResult(
                name=call.name,
                success=False,
                output=None,
                error=f"Unsupported payload mode '{service_config.payload_mode}'",
            )

        LOGGER.info("Calling service %s using %s", call.name, method)
        try:
            response = await self._client.request(
                method,
                service_config.url,
                timeout=service_config.timeout_seconds,
                **request_kwargs,
            )
        except httpx.HTTPError as exc:
            LOGGER.error("Service %s request failed: %s", call.name, exc)
            sanitized = sanitize_error_for_tool_result(str(exc), call.name)
            return ToolCallResult(name=call.name, success=False, output=None, error=sanitized)

        parsed_body: Any
        try:
            parsed_body = response.json()
        except ValueError:
            parsed_body = response.text

        output = parsed_body
        raw_response = parsed_body if service_config.include_raw_response else None

        success = response.is_success
        error_text: Optional[str] = None
        if not success:
            error_text = parsed_body if isinstance(parsed_body, str) else json.dumps(parsed_body)

        return ToolCallResult(
            name=call.name,
            success=success,
            output=output,
            status_code=response.status_code,
            raw_response=raw_response,
            error=error_text,
        )

    async def _execute_bridge_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
        metadata: Dict[str, Any],
    ) -> Optional[ToolCallResult]:
        """
        Execute a tool via direct registry import or HTTP bridge.

        Prefers direct import (no HTTP overhead) when available and no bridge_url is set.
        Falls back to HTTP bridge for debugging or gradual rollout.

        Args:
            server_name: Name of the MCP server
            tool_name: Name of the tool to execute
            arguments: Tool arguments
            metadata: Request metadata (contains user_email and permissions)

        Returns:
            ToolCallResult if successful, None if tool call failed
        """
        # Direct call if registry is available and no bridge URL configured
        if DIRECT_REGISTRY_AVAILABLE and not self._settings.bridge_url:
            return await self._execute_direct_tool(server_name, tool_name, arguments, metadata)

        # Fall back to HTTP bridge
        if not self._settings.bridge_url:
            return None

        full_name = f"{server_name}_{tool_name}"
        LOGGER.info(f"Calling MCP tool via bridge: {server_name}.{tool_name}")

        # CRITICAL: Inject user context into arguments for MCP server auth
        # These values come from the webhook request and CANNOT be controlled by the LLM
        # The LLM only provides the tool-schema-defined parameters (command, time_expression, etc.)
        # organization_id is the primary identifier, user_email is optional
        user_permissions = metadata.get("user_permissions", {})
        organization_ids = user_permissions.get("organization_ids", [])
        organization_id = int(organization_ids[0]) if organization_ids else None

        enriched_arguments = {
            **arguments,
            "user_email": metadata.get("user_email"),
            "user_name": metadata.get("user_name"),  # Display name for audit trails
            "organization_id": organization_id,
            "session_id": metadata.get("session_id"),
            "chat_id": metadata.get("original_chat_id"),  # From webhook, NOT LLM-controllable
            "topic_id": metadata.get("topic_id"),  # From webhook, NOT LLM-controllable
        }

        LOGGER.info(
            f"Injecting user_email={metadata.get('user_email')}, "
            f"organization_id={organization_id}, "
            f"session_id={metadata.get('session_id')}, "
            f"chat_id={metadata.get('original_chat_id')}, "
            f"topic_id={metadata.get('topic_id')} into {server_name}.{tool_name} call"
        )

        try:
            # Call the MCP bridge (RESTful API)
            # GET API key for authentication
            api_key = os.getenv("API_KEY", "")
            headers = {"X-API-Key": api_key} if api_key else {}

            url = f"{self._settings.bridge_url}/servers/{server_name}/tools/{tool_name}"
            response = await self._client.post(
                url,
                json=enriched_arguments,  # Arguments go directly in body
                headers=headers,
                timeout=300.0,  # MCP tools can take 3-5 min (design_and_bom: 5s+30s+120s waits)
            )

            if not response.is_success:
                # Log the full technical error for debugging
                technical_error = f"Bridge returned {response.status_code}: {response.text}"
                LOGGER.error(technical_error)

                # Return sanitized error to prevent technical details reaching users
                sanitized = sanitize_error_for_tool_result(technical_error, full_name)
                return ToolCallResult(
                    name=full_name,
                    success=False,
                    output=None,
                    status_code=response.status_code,
                    error=sanitized,
                )

            result_data = response.json()

            # Bridge response format: {success: bool, result: any, error: str}
            if not result_data.get("success"):
                # The error from bridge is already sanitized by server_registry
                # but we log and sanitize again for defense-in-depth
                bridge_error = result_data.get("error", "Unknown bridge error")
                LOGGER.warning(f"Bridge tool {full_name} returned error: {bridge_error}")
                return ToolCallResult(
                    name=full_name,
                    success=False,
                    output=None,
                    error=bridge_error,  # Already sanitized by server_registry
                )

            # Extract result content
            tool_result = result_data.get("result", {})

            # MCP results are Content objects with text or other data
            # Simplify for Gemini
            if isinstance(tool_result, list):
                # Multiple content parts - concatenate text, skip image items
                output = "\n".join(
                    [
                        item.get("text", str(item))
                        for item in tool_result
                        if isinstance(item, dict) and item.get("type") != "image"
                    ]
                )
            elif isinstance(tool_result, dict):
                output = tool_result.get("text", json.dumps(tool_result))
            else:
                output = str(tool_result)

            # Check for application-level failure inside the tool result.
            # Some MCP servers return {success: false, error: "..."} as their
            # result payload even when the HTTP/bridge call succeeded.
            if isinstance(tool_result, dict) and tool_result.get("success") is False:
                app_error = tool_result.get("error", "Tool returned failure")
                LOGGER.warning(f"Tool {full_name} returned application error: {app_error}")
                return ToolCallResult(
                    name=full_name,
                    success=False,
                    output=None,
                    status_code=200,
                    error=app_error,
                )

            # IMPORTANT: Preserve raw_response for image extraction
            # The handler needs access to image data for sending to Telegram
            return ToolCallResult(
                name=full_name,
                success=True,
                output=output,
                status_code=200,
                raw_response=result_data,
            )

        except Exception as e:
            # Log full technical error for debugging
            LOGGER.exception(f"Error calling bridge tool {full_name}: {e}")

            # Return sanitized error to prevent technical details reaching users
            sanitized = sanitize_error_for_tool_result(str(e), full_name)
            return ToolCallResult(name=full_name, success=False, output=None, error=sanitized)

    async def _execute_direct_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
        metadata: Dict[str, Any],
    ) -> ToolCallResult:
        """Execute MCP tool via direct registry import (no HTTP overhead).

        Args:
            server_name: Name of the MCP server
            tool_name: Name of the tool to execute
            arguments: Tool arguments
            metadata: Request metadata (contains user_email and permissions)

        Returns:
            ToolCallResult with success/failure status
        """
        full_name = f"{server_name}_{tool_name}"
        LOGGER.info(f"Calling MCP tool directly: {server_name}.{tool_name}")

        # Inject user context into arguments (same as HTTP path)
        user_permissions = metadata.get("user_permissions", {})
        organization_ids = user_permissions.get("organization_ids", [])
        organization_id = int(organization_ids[0]) if organization_ids else None

        enriched_arguments = {
            **arguments,
            "user_email": metadata.get("user_email"),
            "user_name": metadata.get("user_name"),
            "organization_id": organization_id,
            "session_id": metadata.get("session_id"),
            "chat_id": metadata.get("original_chat_id"),
            "topic_id": metadata.get("topic_id"),
        }

        LOGGER.info(
            f"Injecting user_email={metadata.get('user_email')}, "
            f"organization_id={organization_id}, "
            f"session_id={metadata.get('session_id')}, "
            f"chat_id={metadata.get('original_chat_id')}, "
            f"topic_id={metadata.get('topic_id')} into {server_name}.{tool_name} call"
        )

        try:
            t0 = time.monotonic()
            result = await registry_call_tool(server_name, tool_name, enriched_arguments)
            duration_ms = int((time.monotonic() - t0) * 1000)
            LOGGER.info(f"Tool {server_name}.{tool_name}: {duration_ms}ms")

            if not result.get("success"):
                return ToolCallResult(
                    name=full_name,
                    success=False,
                    output=None,
                    error=result.get("error", "Unknown error"),
                )

            # Extract text from MCP result format
            # Skip image items to avoid dumping base64 data into text output
            tool_result = result.get("result", [])
            if isinstance(tool_result, list):
                text_parts = []
                for item in tool_result:
                    if isinstance(item, dict):
                        if item.get("type") == "image":
                            continue
                        text_parts.append(item.get("text", str(item)))
                output = "\n".join(text_parts)
            else:
                output = str(tool_result)

            # Preserve raw_response for image extraction
            return ToolCallResult(
                name=full_name,
                success=True,
                output=output,
                status_code=200,
                raw_response=result,
            )

        except Exception as e:
            LOGGER.exception(f"Direct tool call failed: {full_name}")
            sanitized = sanitize_error_for_tool_result(str(e), full_name)
            return ToolCallResult(name=full_name, success=False, output=None, error=sanitized)

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Convenience method for calling a tool by name.

        This is a simplified interface for step handlers that wraps the
        execute() method.

        Args:
            tool_name: Name of the tool to call (e.g., "grafana_query")
            arguments: Tool arguments
            metadata: Optional metadata for auth context

        Returns:
            Tool output on success

        Raises:
            Exception: If tool call fails
        """
        call = FunctionCall(name=tool_name, arguments=arguments)
        result = await self.execute(call, metadata or {})

        if not result.success:
            raise Exception(result.error or f"Tool {tool_name} failed")

        return result.output

    async def close(self) -> None:
        """Close the underlying HTTP client."""

        await self._client.aclose()


__all__ = ["ToolExecutor"]
