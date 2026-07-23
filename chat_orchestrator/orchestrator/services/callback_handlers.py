"""Telegram callback_query handlers (inline button clicks).

Split out of chat_orchestrator/handler.py as part of the Phase 5 file split.
Imports from handler.py are done lazily inside function bodies (matching the
existing pattern in orchestrator/experts/step_context.py and
orchestrator/graphs/nodes/prepare_media.py) to avoid a circular import --
handler.py imports this module, never the reverse.
"""

from __future__ import annotations

import os
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from orchestrator.models.schemas import UserContext
from orchestrator.services.supabase_client import get_supabase_client
from orchestrator.services.telegram_transport import (
    _answer_callback_query,
    _edit_message_remove_buttons,
    _edit_message_text,
    _send_telegram_response,
)
from orchestrator.utils.session_id import generate_session_id
from shared.auth import get_auth_service
from shared.auth.auth_service import STAFF_ORG_ID as _STAFF_ORG_ID
from shared.utils.logging import get_logger
from shared.utils.telegram_buttons import (
    ESCALATION_CLOSE_NOTIFY_PREFIX,
    ESCALATION_CLOSE_SILENT_PREFIX,
    ESCALATION_OFFER_PREFIX,
    ESCALATION_TRACK_CALLBACK_PREFIX,
    PROCEDURE_CALLBACK_PREFIX,
    STEP_INPUT_CALLBACK_PREFIX,
    is_procedure_buttons_enabled,
    parse_callback_data,
    parse_procedure_buttons,
)

LOGGER = get_logger(__name__)


async def _handle_callback_query(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle Telegram callback_query updates (inline button clicks).

    Telegram webhook format for callback queries:
    {
        "update_id": 123456789,
        "callback_query": {
            "id": "unique_callback_id",
            "from": {"id": 987654321, "username": "user", "first_name": "Name"},
            "message": {
                "message_id": 42,
                "chat": {"id": -100123456789, "type": "group"},
                "text": "Original message text",
                "reply_markup": {...}
            },
            "chat_instance": "...",
            "data": "pd:abc12345:run_new"
        }
    }

    Args:
        args: Telegram webhook payload with callback_query

    Returns:
        Response indicating success/failure
    """
    try:
        from orchestrator.services.pending_decision_service import PendingDecisionService

        callback_query = args.get("callback_query", {})

        # Extract relevant fields
        callback_id = callback_query.get("id")
        callback_data = callback_query.get("data", "")
        from_user = callback_query.get("from", {})
        user_id = str(from_user.get("id", ""))
        user_name = from_user.get("first_name") or from_user.get("username") or "User"
        message = callback_query.get("message", {})
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        message_id = message.get("message_id")
        topic_id = message.get("message_thread_id")

        LOGGER.info(
            f"Processing callback query: id={callback_id}, data={callback_data}, "
            f"user={user_name} ({user_id}), chat={chat_id}"
        )

        # Parse the callback data
        parsed = parse_callback_data(callback_data)
        if not parsed:
            LOGGER.warning(f"Failed to parse callback_data: {callback_data}")
            await _answer_callback_query(callback_id, "Invalid button data")
            return {"success": True, "message": "Invalid callback data", "statusCode": 200}

        callback_type = parsed["type"]

        # =================================================================
        # PROCEDURE CALLBACKS (pc:choice) - LLM-generated options
        # =================================================================
        if callback_type == PROCEDURE_CALLBACK_PREFIX:
            return await _handle_procedure_callback(
                callback_id=callback_id,
                choice=parsed["choice"],
                user_id=user_id,
                user_name=user_name,
                chat_id=chat_id,
                topic_id=topic_id,
                message_id=message_id,
                original_text=message.get("text", ""),
            )

        # =================================================================
        # STEP INPUT CALLBACKS (si:number) - Expert step handler options
        # =================================================================
        if callback_type == STEP_INPUT_CALLBACK_PREFIX:
            return await _handle_step_input_callback(
                callback_id=callback_id,
                choice=parsed["choice"],
                user_id=user_id,
                user_name=user_name,
                chat_id=chat_id,
                topic_id=topic_id,
                message_id=message_id,
                original_text=message.get("text", ""),
            )

        # =================================================================
        # ESCALATION TRACKING CALLBACKS (es:mapping_id) - Track as JIRA ticket
        # =================================================================
        if callback_type == ESCALATION_TRACK_CALLBACK_PREFIX:
            return await _handle_escalation_track_callback(
                callback_id=callback_id,
                mapping_id=parsed["mapping_id"],
                chat_id=chat_id,
                message_id=message_id,
                original_text=message.get("text", ""),
                clicker_telegram_id=user_id,
            )

        # =================================================================
        # ESCALATION CLOSE CALLBACKS (ec/en:mapping_id)
        # =================================================================
        if callback_type in (ESCALATION_CLOSE_SILENT_PREFIX, ESCALATION_CLOSE_NOTIFY_PREFIX):
            notify_customer = callback_type == ESCALATION_CLOSE_NOTIFY_PREFIX
            return await _handle_escalation_close_callback(
                callback_id=callback_id,
                mapping_id=parsed["mapping_id"],
                chat_id=chat_id,
                message_id=message_id,
                original_text=message.get("text", ""),
                notify_customer=notify_customer,
                clicker_telegram_id=user_id,
            )

        # =================================================================
        # ESCALATION OFFER CALLBACKS (eo:session_id) - Customer requests support
        # =================================================================
        if callback_type == ESCALATION_OFFER_PREFIX:
            return await _handle_escalation_offer_callback(
                callback_id=callback_id,
                session_id=parsed["session_id"],
                chat_id=chat_id,
                message_id=message_id,
                topic_id=topic_id,
                from_user=from_user,
            )

        # =================================================================
        # DECISION CALLBACKS (pd:id:action) - Expert workflow decisions
        # =================================================================
        id_prefix = parsed["id_prefix"]
        action = parsed["action"]

        # Find the pending decision by ID prefix
        decision_service = PendingDecisionService()

        # Look up the pending decision by full ID (callback_data now carries the full UUID)
        decision_result = await decision_service.get_decision_by_id(id_prefix)

        if not decision_result or decision_result.get("resolved_at"):
            LOGGER.warning(f"No pending decision found for id: {id_prefix}")
            await _answer_callback_query(callback_id, "This option has expired")
            # Edit message to indicate expired
            await _edit_message_remove_buttons(chat_id, message_id, topic_id)
            return {"success": True, "message": "Decision expired", "statusCode": 200}

        decision = decision_result
        decision_id = decision["id"]
        decision_context = decision.get("context") or {}

        # =================================================================
        # AUTHORIZATION: Validate who can click buttons
        # - Anyone can click "cancel" (prevents group chat lockup)
        # - Original user who triggered the decision
        # - Staff members (organization_id matches STAFF_ORG_ID)
        # =================================================================
        is_cancel_action = action in ("cancel", "abandon")
        original_user_id = decision_context.get("original_user_id")
        # Check if clicker is the original user
        is_original_user = user_id == original_user_id

        # Check if clicker is staff (need to look up their organization)
        is_staff = False
        if not is_original_user and not is_cancel_action:
            try:
                auth_service = get_auth_service()
                clicker_permissions = await auth_service.resolve_user_permissions(
                    user_id, source="telegram"
                )
                if clicker_permissions and clicker_permissions.organization_id == _STAFF_ORG_ID:
                    is_staff = True
                    LOGGER.info(f"Staff user {user_name} ({user_id}) clicking button")
            except Exception as auth_error:
                LOGGER.warning(f"Could not check staff status for {user_id}: {auth_error}")

        # Validate authorization
        if not (is_cancel_action or is_original_user or is_staff):
            LOGGER.info(
                f"Unauthorized button click: user={user_id}, original={original_user_id}, "
                f"action={action}, is_staff={is_staff}"
            )
            await _answer_callback_query(
                callback_id,
                "Only the person who started this or staff can select this option",
                show_alert=True,
            )
            return {"success": True, "message": "Unauthorized", "statusCode": 200}

        # NOTE: Do NOT resolve the decision here. The expert_router will find
        # it via get_pending_decision() and resolve it after handling the action.
        # Resolving early causes the router to miss the decision and fall through
        # to normal Gemini processing (e.g., "1" treated as a regular message).
        LOGGER.info(
            f"Keeping decision {decision_id} pending for graph processing (action={action})"
        )

        # Answer the callback query (removes loading indicator)
        # Extract the full button label from the inline keyboard for human-readable display
        action_display = action.replace("_", " ").title()
        reply_markup = message.get("reply_markup", {})
        for row in reply_markup.get("inline_keyboard", []):
            for btn in row:
                if btn.get("callback_data") == callback_data:
                    action_display = btn["text"]
                    break
        await _answer_callback_query(callback_id, f"Selected: {action_display[:50]}")

        # Edit the original message to show selection and remove buttons
        original_text = message.get("text", "")
        updated_text = f"{original_text}\n\n✓ *Selected: {action_display}*"
        await _edit_message_text(
            chat_id, message_id, updated_text, topic_id, reply_markup={"inline_keyboard": []}
        )

        # Trigger processing immediately — don't wait for the user's next message.
        # Map the action back to the number the expert_router expects (e.g., run_new → "1").
        is_resumable = decision_context.get("is_resumable", False)
        action_to_number = {
            "run_new": "1",
            "resume": "2" if is_resumable else "1",
            "start_fresh": "2",
            "cancel": "3" if is_resumable else "2",
            "abandon": "3",
        }
        user_input = action_to_number.get(action, action)

        # Build user context and process through the graph
        auth_service = get_auth_service()
        try:
            permissions = await auth_service.resolve_user_permissions(user_id, source="telegram")
            user_email = permissions.email
            organization_ids = (
                [str(permissions.organization_id)] if permissions.organization_id else []
            )
        except Exception as auth_error:
            LOGGER.warning(f"Could not resolve permissions for {user_id}: {auth_error}")
            user_email = f"telegram_{user_id}"
            organization_ids = []

        user_context = UserContext(
            user_id=user_id,
            user_email=user_email,
            username=user_name,
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            is_group=chat_id.startswith("-"),
            organization_ids=organization_ids,
        )

        session_id = generate_session_id(
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            user_id=user_id,
        )

        from orchestrator.services.webhook_processor import process_webhook_with_graph

        response_text, tool_results, reply_markup = await process_webhook_with_graph(
            user_input=user_input,
            user_context=user_context,
            session_id=session_id,
            metadata={
                "original_chat_id": chat_id,
                "topic_id": str(topic_id) if topic_id else None,
                "from_decision_button": True,
                "decision_action": action,
            },
        )

        # Send the response
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_token and response_text:
            webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            await _send_telegram_response(
                webhook_url=webhook_url,
                chat_id=chat_id,
                topic_id=str(topic_id) if topic_id else None,
                text=response_text,
                reply_markup=reply_markup,
            )

        return {
            "success": True,
            "message": f"Decision resolved: {action}",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling callback query: {e}")
        # Try to answer the callback to remove loading state
        try:
            callback_id = args.get("callback_query", {}).get("id")
            if callback_id:
                await _answer_callback_query(callback_id, "An error occurred")
        except Exception:
            pass
        return {"success": False, "error": str(e), "statusCode": 500}


def _extract_button_text(original_text: str, choice: str) -> str:
    """Extract the full text of a button option from the original message.

    The original message contains button options in one of these formats:
    - Numbered: "1. Check all grid statuses" or "1) Check all grid statuses"
    - Unnumbered (parsed by line position): Just "Check all grid statuses"

    Args:
        original_text: The original message text containing button options
        choice: The choice number as string (e.g., "1", "2")

    Returns:
        The full text of the selected option, or a descriptive fallback
    """
    import re

    try:
        choice_num = int(choice)
    except ValueError:
        return f"Option {choice}"

    # Try to find numbered option pattern: "1. Option text" or "1) Option text"
    # Look for the pattern at the start of a line
    pattern = rf"^\s*{choice_num}[.\)]\s*(.+?)\s*$"
    match = re.search(pattern, original_text, re.MULTILINE)
    if match:
        return match.group(1).strip()

    # Fallback: Try to find options in a BUTTONS block and get by position
    # Look for lines that might be button options (non-empty, not the BUTTONS markers)
    lines = original_text.split("\n")
    button_lines = []
    in_buttons_block = False

    for line in lines:
        line_stripped = line.strip().lower()
        # Detect start of buttons section
        if "buttons" in line_stripped and "/" not in line_stripped:
            in_buttons_block = True
            continue
        # Detect end of buttons section
        if "/buttons" in line_stripped:
            in_buttons_block = False
            continue
        # Collect button option lines
        if in_buttons_block and line.strip():
            # Remove any leading number if present
            clean_line = re.sub(r"^\s*\d+[.\)]\s*", "", line.strip())
            if clean_line:
                button_lines.append(clean_line)

    # Return the option at the selected index (1-based)
    if 0 < choice_num <= len(button_lines):
        return button_lines[choice_num - 1]

    # Final fallback
    return f"Option {choice}"


async def _handle_procedure_callback(
    callback_id: str,
    choice: str,
    user_id: str,
    user_name: str,
    chat_id: str,
    topic_id: int | None,
    message_id: int,
    original_text: str,
) -> Dict[str, Any]:
    """Handle procedure choice callback (pc:choice).

    When user clicks a procedure button, we:
    1. Answer the callback query
    2. Edit the message to show the selection
    3. Process the choice as a user message
    4. Send the LLM's response

    Args:
        callback_id: Telegram callback query ID
        choice: The choice number ("1", "2", etc.)
        user_id: Telegram user ID
        user_name: User display name
        chat_id: Telegram chat ID
        topic_id: Optional topic/thread ID
        message_id: Original message ID
        original_text: Original message text

    Returns:
        Response dict
    """
    try:
        LOGGER.info(
            f"Handling procedure callback: choice={choice}, user={user_name} ({user_id}), "
            f"chat={chat_id}"
        )

        # Extract the full text of the selected option from the original message
        # Buttons were formatted as "1. Option text" or just "Option text" on separate lines
        selected_text = _extract_button_text(original_text, choice)
        LOGGER.info(f"Extracted button text for choice {choice}: {selected_text}")

        # Answer the callback query immediately
        await _answer_callback_query(callback_id, f"Selected: {selected_text[:50]}")

        # Edit the message to show selection and remove buttons
        updated_text = f"{original_text}\n\n✓ *Selected: {selected_text}*"
        await _edit_message_text(
            chat_id, message_id, updated_text, topic_id, reply_markup={"inline_keyboard": []}
        )

        # Get user context and process the choice as a message
        auth_service = get_auth_service()

        # Resolve user permissions
        try:
            permissions = await auth_service.resolve_user_permissions(user_id, source="telegram")
            user_email = permissions.email
            organization_ids = (
                [str(permissions.organization_id)] if permissions.organization_id else []
            )
        except Exception as auth_error:
            LOGGER.warning(f"Could not resolve permissions for {user_id}: {auth_error}")
            user_email = f"telegram_{user_id}"
            organization_ids = []

        # Build user context
        user_context = UserContext(
            user_id=user_id,
            user_email=user_email,
            username=user_name,
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            is_group=chat_id.startswith("-"),
            organization_ids=organization_ids,
        )

        # Generate session ID
        session_id = generate_session_id(
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            user_id=user_id,
        )

        # Process the choice as a user message through the graph
        # The choice number becomes the user's input
        from orchestrator.services.webhook_processor import process_webhook_with_graph

        response_text, tool_results, reply_markup = await process_webhook_with_graph(
            user_input=selected_text,  # Send the full selected option text
            user_context=user_context,
            session_id=session_id,
            metadata={
                "original_chat_id": chat_id,
                "topic_id": str(topic_id) if topic_id else None,
                "from_procedure_button": True,
                "original_choice_number": choice,
            },
        )

        # Send the response
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_token and response_text:
            # Check for new procedure buttons in the response
            if is_procedure_buttons_enabled():
                clean_text, proc_keyboard, _ = parse_procedure_buttons(response_text)
                if proc_keyboard:
                    response_text = clean_text
                    # Procedure buttons take precedence over decision buttons
                    reply_markup = proc_keyboard

            webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            await _send_telegram_response(
                webhook_url=webhook_url,
                chat_id=chat_id,
                topic_id=str(topic_id) if topic_id else None,
                text=response_text,
                reply_markup=reply_markup,
            )

        return {
            "success": True,
            "message": f"Processed procedure choice: {choice}",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling procedure callback: {e}")
        # Try to send error message
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_token:
            try:
                webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                await _send_telegram_response(
                    webhook_url=webhook_url,
                    chat_id=chat_id,
                    topic_id=str(topic_id) if topic_id else None,
                    text="Sorry, I encountered an error processing your selection. Please try again.",
                )
            except Exception:
                pass
        return {"success": False, "error": str(e), "statusCode": 500}


async def _handle_step_input_callback(
    callback_id: str,
    choice: str,
    user_id: str,
    user_name: str,
    chat_id: str,
    topic_id: int | None,
    message_id: int,
    original_text: str,
) -> Dict[str, Any]:
    """Handle step input callback (si:choice).

    Similar to procedure callback but sends the choice NUMBER as user_input,
    since step handlers parse by number (e.g., "1", "2", "3") or keyword.

    Args:
        callback_id: Telegram callback query ID
        choice: The choice number ("1", "2", etc.)
        user_id: Telegram user ID
        user_name: User display name
        chat_id: Telegram chat ID
        topic_id: Optional topic/thread ID
        message_id: Original message ID
        original_text: Original message text

    Returns:
        Response dict
    """
    try:
        LOGGER.info(
            f"Handling step input callback: choice={choice}, user={user_name} ({user_id}), "
            f"chat={chat_id}"
        )

        # Extract the full text of the selected option from the original message
        selected_text = _extract_button_text(original_text, choice)
        LOGGER.info(f"Extracted step input text for choice {choice}: {selected_text}")

        # Answer the callback query immediately
        await _answer_callback_query(callback_id, f"Selected: {selected_text[:50]}")

        # Edit the message to show selection and remove buttons
        updated_text = f"{original_text}\n\n✓ *Selected: {selected_text}*"
        await _edit_message_text(
            chat_id, message_id, updated_text, topic_id, reply_markup={"inline_keyboard": []}
        )

        # Get user context and process the choice as a message
        auth_service = get_auth_service()

        # Resolve user permissions
        try:
            permissions = await auth_service.resolve_user_permissions(user_id, source="telegram")
            user_email = permissions.email
            organization_ids = (
                [str(permissions.organization_id)] if permissions.organization_id else []
            )
        except Exception as auth_error:
            LOGGER.warning(f"Could not resolve permissions for {user_id}: {auth_error}")
            user_email = f"telegram_{user_id}"
            organization_ids = []

        # Build user context
        user_context = UserContext(
            user_id=user_id,
            user_email=user_email,
            username=user_name,
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            is_group=chat_id.startswith("-"),
            organization_ids=organization_ids,
        )

        # Generate session ID
        session_id = generate_session_id(
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            user_id=user_id,
        )

        # Process the choice NUMBER through graph (step handlers parse numbers)
        # KEY DIFFERENCE from procedure callback: send "1" not the full option text
        from orchestrator.services.webhook_processor import process_webhook_with_graph

        response_text, tool_results, reply_markup = await process_webhook_with_graph(
            user_input=choice,
            user_context=user_context,
            session_id=session_id,
            metadata={
                "original_chat_id": chat_id,
                "topic_id": str(topic_id) if topic_id else None,
                "from_step_input_button": True,
                "original_choice_number": choice,
            },
        )

        # Send the response
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_token and response_text:
            # Check for new procedure buttons in the response
            if is_procedure_buttons_enabled():
                clean_text, proc_keyboard, _ = parse_procedure_buttons(response_text)
                if proc_keyboard:
                    response_text = clean_text
                    reply_markup = proc_keyboard

            webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            await _send_telegram_response(
                webhook_url=webhook_url,
                chat_id=chat_id,
                topic_id=str(topic_id) if topic_id else None,
                text=response_text,
                reply_markup=reply_markup,
            )

        return {
            "success": True,
            "message": f"Processed step input choice: {choice}",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling step input callback: {e}")
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_token:
            try:
                webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                await _send_telegram_response(
                    webhook_url=webhook_url,
                    chat_id=chat_id,
                    topic_id=str(topic_id) if topic_id else None,
                    text="Sorry, I encountered an error processing your selection. Please try again.",
                )
            except Exception:
                pass
        return {"success": False, "error": str(e), "statusCode": 500}


async def _handle_escalation_offer_callback(
    callback_id: str,
    session_id: str,
    chat_id: str,
    message_id: int,
    topic_id: Optional[int],
    from_user: Dict[str, Any],
) -> Dict[str, Any]:
    """Handle escalation offer callback (eo:session_id).

    Customer clicked "Contact support" after a system error.
    Triggers escalation, removes the button, confirms to customer.
    """
    from orchestrator.services.escalation_service import EscalationService
    from orchestrator.services.supabase_client import SupabaseClient

    try:
        await _answer_callback_query(callback_id, "Contacting support...")
        await _edit_message_remove_buttons(chat_id, message_id, topic_id)

        first_name = from_user.get("first_name", "")
        last_name = from_user.get("last_name", "")
        customer_username = (
            from_user.get("username") or f"{first_name} {last_name}".strip() or "Customer"
        )

        # Look up organization from session for proper escalation routing
        organization_id = None
        try:
            supabase_client = SupabaseClient(
                url=os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", ""),
                key=os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", ""),
            )
            session_obj = await supabase_client.get_session(session_id)
            if session_obj:
                organization_id = session_obj.organization_id
        except Exception as e:
            LOGGER.warning(f"Could not look up org for escalation offer: {e}")

        escalation_service = EscalationService()
        if escalation_service.is_enabled():
            result = await escalation_service.escalate_to_support(
                question_summary="Customer requested support after system error",
                session_id=session_id,
                organization_id=organization_id,
                customer_chat_id=str(chat_id),
                customer_topic_id=str(topic_id) if topic_id else None,
                customer_username=customer_username,
                reason="user_requested",
            )
            if result.get("success"):
                LOGGER.info(f"Escalation offer accepted for session={session_id}")
            else:
                LOGGER.error(f"Escalation offer failed: {result.get('error')}")
        else:
            LOGGER.warning("Escalation service not enabled for offer callback")

        return {"success": True, "message": "Escalation offer handled", "statusCode": 200}

    except Exception as e:
        LOGGER.exception(f"Error handling escalation offer callback: {e}")
        try:
            await _answer_callback_query(callback_id, "An error occurred", show_alert=True)
        except Exception:
            pass
        return {"success": False, "error": "Internal error", "statusCode": 500}


async def _handle_escalation_track_callback(
    callback_id: str,
    mapping_id: str,
    chat_id: str,
    message_id: int,
    original_text: str,
    clicker_telegram_id: str = "",
) -> Dict[str, Any]:
    """Handle escalation tracking callback (es:mapping_id).

    Atomically claims the escalation, creates a JIRA ticket, edits the
    escalation message to show the ticket key, and notifies the customer.
    """
    from orchestrator.services.escalation_service import EscalationService

    try:
        # Authorization: only allow from escalation group
        escalation_group_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
        if chat_id != escalation_group_id:
            await _answer_callback_query(callback_id, "Unauthorized", show_alert=True)
            return {"success": True, "message": "Unauthorized", "statusCode": 403}

        try:
            uuid_mod.UUID(mapping_id)
        except ValueError:
            await _answer_callback_query(callback_id, "Invalid escalation ID", show_alert=True)
            return {"success": True, "message": "Invalid mapping_id", "statusCode": 400}

        supabase_client = get_supabase_client()

        # Atomic claim: only first click wins
        escalation = await supabase_client.claim_escalation_for_tracking(mapping_id)
        if not escalation:
            await _answer_callback_query(
                callback_id, "Escalation already closed or tracked", show_alert=True
            )
            await _edit_message_remove_buttons(chat_id, message_id)
            return {"success": True, "message": "Already claimed", "statusCode": 200}

        # Resolve clicker's email for JIRA assignment (non-blocking)
        clicker_email = None
        if clicker_telegram_id:
            try:
                from shared.auth import get_auth_service

                clicker_email = await get_auth_service().get_user_email(
                    clicker_telegram_id, source="telegram"
                )
            except Exception as e:
                LOGGER.debug(f"Could not resolve clicker email: {e}")

        # Show "Creating ticket..." toast immediately
        await _answer_callback_query(callback_id, "Creating JIRA ticket...")

        # Create ticket + notify customer + close escalation
        escalation_service = EscalationService()
        result = await escalation_service.track_as_ticket(
            escalation_mapping=escalation,
            assignee_email=clicker_email,
        )

        if result.get("success"):
            jira_key = result["jira_ticket_key"]
            # Edit escalation message: append ticket ref, remove button
            updated_text = f"{original_text}\n\n\u2705 Tracked as {jira_key}"
            await _edit_message_text(
                chat_id,
                message_id,
                updated_text,
                reply_markup={"inline_keyboard": []},
            )
            LOGGER.info(f"Escalation {mapping_id} tracked as {jira_key}")
        else:
            # Revert: re-activate the escalation since ticket creation failed
            await supabase_client.reactivate_escalation(mapping_id)
            error_msg = result.get("error", "Unknown error")
            LOGGER.error(f"Failed to track escalation {mapping_id}: {error_msg}")
            # Edit message to show failure (can't answer callback twice)
            failure_text = f"{original_text}\n\n\u274c Ticket creation failed — try again"
            await _edit_message_text(chat_id, message_id, failure_text)

        return {
            "success": True,
            "message": f"Escalation tracking: {result.get('jira_ticket_key', 'failed')}",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling escalation track callback: {e}")
        try:
            await _answer_callback_query(callback_id, "An error occurred", show_alert=True)
        except Exception:
            pass
        return {"success": False, "error": "Internal error", "statusCode": 500}


async def _handle_escalation_close_callback(
    callback_id: str,
    mapping_id: str,
    chat_id: str,
    message_id: int,
    original_text: str,
    notify_customer: bool = False,
    clicker_telegram_id: str = "",
) -> Dict[str, Any]:
    """Handle escalation close callbacks (ec/en:mapping_id).

    Closes the escalation. If notify_customer is True, sends a resolution
    message to the customer. Otherwise closes silently.
    """
    from orchestrator.services.escalation_service import EscalationService

    claimed = False
    supabase_client = None
    try:
        # Authorization: only allow from escalation group
        escalation_group_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
        if chat_id != escalation_group_id:
            await _answer_callback_query(callback_id, "Unauthorized", show_alert=True)
            return {"success": True, "message": "Unauthorized", "statusCode": 403}

        try:
            uuid_mod.UUID(mapping_id)
        except ValueError:
            await _answer_callback_query(callback_id, "Invalid escalation ID", show_alert=True)
            return {"success": True, "message": "Invalid mapping_id", "statusCode": 400}

        supabase_client = get_supabase_client()

        # Atomic claim: only first click wins
        escalation = await supabase_client.claim_escalation_for_tracking(mapping_id)
        if not escalation:
            await _answer_callback_query(
                callback_id, "Escalation already closed or tracked", show_alert=True
            )
            await _edit_message_remove_buttons(chat_id, message_id)
            return {"success": True, "message": "Already claimed", "statusCode": 200}

        claimed = True

        # Set resolved_at immediately so orphan recovery (which reactivates
        # is_active=False rows without resolved_at) never re-opens this intentional close.
        try:
            _db = supabase_client._get_client()
            _db.table("escalation_mappings").update(
                {"resolved_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", mapping_id).execute()
        except Exception:
            LOGGER.warning("Could not set resolved_at for mapping %s", mapping_id, exc_info=True)

        session_id = escalation.get("session_id")
        if not session_id:
            await _answer_callback_query(callback_id, "No session found", show_alert=True)
            return {"success": False, "error": "No session_id", "statusCode": 500}

        action_label = "Closing & notifying..." if notify_customer else "Closing silently..."
        await _answer_callback_query(callback_id, action_label)

        # The claim already set this mapping to is_active=false.
        # Only release the session if no other blocking escalations remain.
        # Non-blocking ones (safety_escalation) never set is_escalated=True,
        # so they shouldn't prevent session release.
        remaining = await supabase_client.count_active_blocking_escalations(session_id)
        if remaining == 0:
            await supabase_client.update_session_escalation_status(
                session_id=session_id, is_escalated=False
            )

        # Notify customer if requested
        if notify_customer:
            customer_chat_id = escalation.get("customer_chat_id", "")
            customer_topic_id = escalation.get("customer_topic_id")
            if customer_chat_id:
                escalation_service = EscalationService()
                await escalation_service.notify_customer_resolved(
                    customer_chat_id, customer_topic_id
                )

        # Update message: show what happened, remove buttons
        if notify_customer:
            status_text = f"{original_text}\n\n\u2705 Closed — customer notified"
        else:
            status_text = f"{original_text}\n\n\U0001f507 Closed silently"

        await _edit_message_text(
            chat_id,
            message_id,
            status_text,
            reply_markup={"inline_keyboard": []},
        )
        claimed = False  # Success — no rollback needed

        # Transition Jira to Done if the escalation had a tracked ticket.
        # Non-fatal — failure is logged inside _transition_jira_to_done.
        jira_key = escalation.get("jira_ticket_key")
        if jira_key:
            escalation_svc = EscalationService()
            await escalation_svc._transition_jira_to_done(jira_key)

        LOGGER.info(
            f"Escalation {mapping_id} closed by {clicker_telegram_id} (notify={notify_customer})"
        )

        return {
            "success": True,
            "message": f"Escalation closed (notify={notify_customer})",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling escalation close callback: {e}")
        if claimed and supabase_client:
            try:
                await supabase_client.reactivate_escalation(mapping_id)
            except Exception:
                LOGGER.error(f"CRITICAL: Failed to reactivate escalation {mapping_id} after error")
        try:
            await _answer_callback_query(callback_id, "An error occurred", show_alert=True)
        except Exception:
            pass
        return {"success": False, "error": "Internal error", "statusCode": 500}


