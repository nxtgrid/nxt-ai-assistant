"""
Telegram Debug Logger
Sends debug messages to a Telegram chat when DEBUG=true
Auto-captures file, line, and function context
"""

import asyncio
import inspect
import os
import traceback

from telegram import Bot


async def tele_debug(message: str, include_traceback: bool = False):
    """
    Send debug message to Telegram if DEBUG=true
    Automatically captures caller's file, line number, and function name

    Args:
        message: Your custom debug message
        include_traceback: Whether to include full stack trace

    Example:
        await tele_debug("API call failed: timeout")
        await tele_debug(f"Unexpected result: {result}", include_traceback=True)
    """
    # Only run if DEBUG is enabled
    if os.getenv("DEBUG", "false").lower() != "true":
        return

    # Auto-capture caller context
    frame = inspect.currentframe().f_back
    filename = frame.f_code.co_filename
    line_number = frame.f_lineno
    function_name = frame.f_code.co_name

    # Format message with context
    debug_msg = f"🐛 {os.path.basename(filename)}:{line_number} in {function_name}()\n{message}"

    if include_traceback:
        debug_msg += f"\n\nStack:\n{traceback.format_exc()}"

    # Get Telegram credentials
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("DEBUG_TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print(f"[DEBUG] {debug_msg}")  # Fallback to console
        return

    try:
        bot = Bot(token=bot_token)
        # Telegram has a 4096 character limit per message
        await bot.send_message(chat_id=chat_id, text=debug_msg[:4096])
    except Exception as e:
        # Fallback to console if Telegram send fails
        print(f"[DEBUG SEND FAILED] {debug_msg}\nError: {e}")


def tele_debug_sync(message: str, include_traceback: bool = False):
    """
    Synchronous wrapper for tele_debug
    Use this in non-async contexts

    Args:
        message: Your custom debug message
        include_traceback: Whether to include full stack trace

    Example:
        tele_debug_sync("Connection failed")
    """
    try:
        # Try to get existing event loop
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If we're in an async context, create a task
            asyncio.create_task(tele_debug(message, include_traceback))
        else:
            # If no loop is running, use asyncio.run
            asyncio.run(tele_debug(message, include_traceback))
    except RuntimeError:
        # No event loop exists, create one
        asyncio.run(tele_debug(message, include_traceback))
