"""
Telegram Command Sync

Syncs command definitions to Telegram via setMyCommands API.
Uses BotCommandScope to show different commands to different contexts.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx

from orchestrator.services.command_parser import COMMAND_REGISTRY, CommandDefinition
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


class TelegramCommandSync:
    """
    Syncs command definitions to Telegram via setMyCommands API.

    Strategy:
    - Default scope: Clear/empty (no commands for customers)
    - Staff chat scope: Full command list (staff see all commands)
    """

    def __init__(self, bot_token: Optional[str] = None) -> None:
        """
        Initialize the sync service.

        Args:
            bot_token: Telegram bot token (defaults to TELEGRAM_BOT_TOKEN env var)
        """
        self._bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not self._bot_token:
            LOGGER.warning("No TELEGRAM_BOT_TOKEN configured")
        self._base_url = f"https://api.telegram.org/bot{self._bot_token}"

    async def sync_commands(self) -> Dict[str, Any]:
        """
        Sync commands to Telegram.

        This will:
        1. Clear commands for default scope (customers see nothing)
        2. Set full command list for staff chat scope

        Returns:
            Dict with sync results
        """
        results: Dict[str, Any] = {"success": True, "synced": []}

        # Step 1: Clear default scope (customers see no commands)
        clear_result = await self.clear_default_commands()
        results["synced"].append({"scope": "default", **clear_result})

        if not clear_result.get("success"):
            results["success"] = False

        # Step 2: Set commands for staff chat
        staff_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID")
        if staff_chat_id:
            try:
                chat_id = int(staff_chat_id)
                commands = list(COMMAND_REGISTRY.values())
                staff_result = await self._set_my_commands(
                    commands=commands,
                    scope_type="chat",
                    chat_id=chat_id,
                )
                results["synced"].append({"scope": "staff_chat", **staff_result})

                if not staff_result.get("success"):
                    results["success"] = False

            except ValueError:
                error_msg = f"Invalid ESCALATION_TELEGRAM_CHAT_ID: {staff_chat_id}"
                LOGGER.error(error_msg)
                results["synced"].append(
                    {
                        "scope": "staff_chat",
                        "success": False,
                        "error": error_msg,
                    }
                )
                results["success"] = False
        else:
            results["synced"].append(
                {
                    "scope": "staff_chat",
                    "success": False,
                    "error": "ESCALATION_TELEGRAM_CHAT_ID not configured",
                }
            )
            # Don't mark overall as failed - staff chat is optional

        return results

    async def clear_default_commands(self) -> Dict[str, Any]:
        """
        Clear commands for default scope (all users).

        This ensures customers don't see any command autocomplete.

        Returns:
            Dict with API response
        """
        return await self._set_my_commands(
            commands=[],
            scope_type="default",
        )

    async def _set_my_commands(
        self,
        commands: List[CommandDefinition],
        scope_type: str = "default",
        chat_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Call Telegram setMyCommands API.

        Args:
            commands: List of commands to register (empty list to clear)
            scope_type: "default" or "chat"
            chat_id: Required if scope_type is "chat"

        Returns:
            Dict with API response
        """
        if not self._bot_token:
            return {"success": False, "error": "No bot token configured"}

        try:
            # Build commands payload
            tg_commands = [
                {"command": cmd.command, "description": cmd.description} for cmd in commands
            ]

            # Build scope
            scope: Dict[str, Any] = {"type": scope_type}
            if scope_type == "chat" and chat_id:
                scope["chat_id"] = chat_id
            elif scope_type != "default":
                return {"success": False, "error": f"Invalid scope: {scope_type}"}

            payload = {
                "commands": tg_commands,
                "scope": scope,
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self._base_url}/setMyCommands",
                    json=payload,
                    timeout=10.0,
                )

                data = response.json()

                if data.get("ok"):
                    action = "Cleared" if not commands else f"Set {len(commands)}"
                    LOGGER.info(f"{action} commands for Telegram (scope: {scope_type})")
                    return {
                        "success": True,
                        "commands_count": len(commands),
                    }
                else:
                    error = data.get("description", "Unknown error")
                    LOGGER.error(f"Telegram API error: {error}")
                    return {"success": False, "error": error}

        except Exception as e:
            LOGGER.exception(f"Failed to sync commands: {e}")
            return {"success": False, "error": str(e)}

    async def get_my_commands(self, scope_type: str = "default") -> Dict[str, Any]:
        """
        Get currently registered commands from Telegram.

        Args:
            scope_type: "default" or "chat"

        Returns:
            Dict with commands list
        """
        if not self._bot_token:
            return {"success": False, "error": "No bot token configured"}

        try:
            payload: Dict[str, Any] = {}
            if scope_type == "default":
                payload["scope"] = {"type": "default"}

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self._base_url}/getMyCommands",
                    json=payload,
                    timeout=10.0,
                )

                data = response.json()

                if data.get("ok"):
                    return {
                        "success": True,
                        "commands": data.get("result", []),
                    }
                else:
                    return {"success": False, "error": data.get("description")}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def delete_my_commands(self, scope_type: str = "default") -> Dict[str, Any]:
        """
        Delete all commands for a scope.

        Args:
            scope_type: "default" to delete for all users

        Returns:
            Dict with API response
        """
        if not self._bot_token:
            return {"success": False, "error": "No bot token configured"}

        try:
            payload: Dict[str, Any] = {}
            if scope_type == "default":
                payload["scope"] = {"type": "default"}

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self._base_url}/deleteMyCommands",
                    json=payload,
                    timeout=10.0,
                )

                data = response.json()

                if data.get("ok"):
                    LOGGER.info(f"Deleted commands for scope: {scope_type}")
                    return {"success": True}
                else:
                    return {"success": False, "error": data.get("description")}

        except Exception as e:
            return {"success": False, "error": str(e)}


__all__ = ["TelegramCommandSync"]
