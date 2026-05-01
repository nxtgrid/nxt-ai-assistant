#!/usr/bin/env python3
"""
Sync Telegram Slash Commands (Standalone version for anansi_app)

Syncs command definitions to Telegram via setMyCommands API.
Works without chat_orchestrator dependency by calling Telegram API directly.

Usage:
    python sync_telegram_commands.py
    python sync_telegram_commands.py --list  # List current commands
"""

import argparse
import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

# Load environment
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


@dataclass
class CommandDefinition:
    """Telegram command definition."""

    command: str
    description: str
    linked_tool: str = ""
    requires_args: bool = False


# Staff Mode Commands (mirrored from chat_orchestrator)
# Source: chat_orchestrator/orchestrator/services/command_parser.py
# These commands are only visible to staff users in the escalation chat.
# Customers see no command autocomplete (Customer Mode has no commands).
# Keep in sync when adding new commands!
COMMANDS = [
    CommandDefinition(
        command="tickets",
        description="List my open JIRA tickets",
        linked_tool="jira_search_issues_with_comments",
        requires_args=False,
    ),
    CommandDefinition(
        command="ticket",
        description="Get JIRA ticket details (e.g., /ticket OPS-123)",
        linked_tool="jira_get_issue",
        requires_args=True,
    ),
    # Equipment control commands (staff-only, command-gated)
    CommandDefinition(
        command="inverters_restart",
        description="Restart inverter at a grid (e.g., /inverters_restart ExampleGrid)",
        linked_tool="equipment_control_restart_inverter",
        requires_args=True,
    ),
    CommandDefinition(
        command="comms_reboot",
        description="Reboot comms chain at a grid (e.g., /comms_reboot ExampleGrid)",
        linked_tool="equipment_control_restart_comms_chain",
        requires_args=True,
    ),
    # Grid status commands (accessible to customers too)
    CommandDefinition(
        command="grid",
        description="Get grid status (e.g., /grid ExampleGrid)",
        linked_tool="customer_get_grid_status",
        requires_args=False,
    ),
    CommandDefinition(
        command="grids",
        description="Get status of all accessible grids",
        linked_tool="customer_get_all_grids_status",
        requires_args=False,
    ),
]


class TelegramCommandSync:
    """Standalone Telegram command sync (no orchestrator dependency)."""

    def __init__(self, bot_token: Optional[str] = None) -> None:
        self._bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._base_url = f"https://api.telegram.org/bot{self._bot_token}"

    async def get_my_commands(self, scope_type: str = "default") -> Dict[str, Any]:
        """Get currently registered commands from Telegram."""
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
                    return {"success": True, "commands": data.get("result", [])}
                else:
                    return {"success": False, "error": data.get("description")}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def sync_commands(self) -> Dict[str, Any]:
        """Sync commands to Telegram."""
        results: Dict[str, Any] = {"success": True, "synced": []}

        # Clear default scope
        clear_result = await self._set_my_commands([], "default")
        results["synced"].append({"scope": "default", **clear_result})

        # Set staff chat commands
        staff_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID")
        if staff_chat_id:
            try:
                staff_result = await self._set_my_commands(COMMANDS, "chat", int(staff_chat_id))
                results["synced"].append({"scope": "staff_chat", **staff_result})
            except ValueError as e:
                results["synced"].append({"scope": "staff_chat", "success": False, "error": str(e)})
        else:
            results["synced"].append(
                {
                    "scope": "staff_chat",
                    "success": False,
                    "error": "ESCALATION_TELEGRAM_CHAT_ID not configured",
                }
            )

        results["success"] = all(s.get("success", False) for s in results["synced"])
        return results

    async def _set_my_commands(
        self,
        commands: List[CommandDefinition],
        scope_type: str,
        chat_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Set commands via Telegram API."""
        if not self._bot_token:
            return {"success": False, "error": "No bot token"}

        try:
            tg_commands = [
                {"command": cmd.command, "description": cmd.description} for cmd in commands
            ]

            scope: Dict[str, Any] = {"type": scope_type}
            if scope_type == "chat" and chat_id:
                scope["chat_id"] = chat_id

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self._base_url}/setMyCommands",
                    json={"commands": tg_commands, "scope": scope},
                    timeout=10.0,
                )
                data = response.json()

                if data.get("ok"):
                    return {"success": True, "commands_count": len(commands)}
                else:
                    return {"success": False, "error": data.get("description")}

        except Exception as e:
            return {"success": False, "error": str(e)}


async def list_commands() -> int:
    """List current commands registered with Telegram."""
    print("=== Staff Mode Commands ===")
    print("(Commands available to staff users in the escalation chat)")
    print()
    for cmd in COMMANDS:
        print(f"  /{cmd.command}")
        print(f"    Description: {cmd.description}")
        print(f"    Linked tool: {cmd.linked_tool}")
        print()

    print("=== Customer Mode Commands ===")
    print("(Commands visible to customers - should be empty)")
    print()

    sync = TelegramCommandSync()
    result = await sync.get_my_commands(scope_type="default")

    if result.get("success"):
        tg_commands: List[Dict[str, Any]] = result.get("commands", [])
        if tg_commands:
            for tg_cmd in tg_commands:
                print(f"  /{tg_cmd['command']} - {tg_cmd['description']}")
        else:
            print("  (none)")
    else:
        print(f"  Error: {result.get('error')}")

    print()
    return 0


async def sync_commands() -> int:
    """Sync commands to Telegram."""
    print("Syncing Telegram commands...")
    print()

    sync = TelegramCommandSync()
    result = await sync.sync_commands()

    print(f"Overall success: {result.get('success')}")
    print()

    # Map internal scope names to user-friendly mode names
    scope_labels = {
        "default": "Customer Mode",
        "staff_chat": "Staff Mode",
    }

    for synced in result.get("synced", []):
        scope = synced.get("scope", "unknown")
        label = scope_labels.get(scope, scope)
        success = synced.get("success", False)
        count = synced.get("commands_count", 0)
        error = synced.get("error")

        if success:
            if count == 0:
                print(f"  [{label}] Cleared commands (customers see no autocomplete)")
            else:
                print(f"  [{label}] Set {count} commands")
        else:
            print(f"  [{label}] FAILED: {error}")

    print()
    return 0 if result.get("success") else 1


async def main(args: argparse.Namespace) -> int:
    """Main entry point."""
    if args.list:
        return await list_commands()
    else:
        return await sync_commands()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sync Telegram slash commands",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python sync_telegram_commands.py           # Sync commands to Telegram
    python sync_telegram_commands.py --list    # List current commands
        """,
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List current commands instead of syncing",
    )

    args = parser.parse_args()

    exit_code = asyncio.run(main(args))
    exit(exit_code)
