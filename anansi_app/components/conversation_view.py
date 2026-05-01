"""
Conversation view component for displaying message threads.

Shows messages in a Telegram-like interface from the bot's perspective.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import streamlit as st
from services.supabase_reader import SupabaseReader


def _is_internal_message(message: Dict[str, Any]) -> bool:
    """Check if a message is internal (not visible to the end user).

    Internal messages include:
    - Tool results (role="tool")
    - Bot tool calls (role="model" with function_call but no text content)
    - Command prompt templates (role="user" with message_type="command_result" or "scheduled")
    """
    role = message.get("role", "")
    if role == "tool":
        return True
    if role == "model" and message.get("function_call") and not message.get("content"):
        return True
    if role == "user":
        metadata = message.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("message_type") in (
            "command_result",
            "scheduled",
        ):
            return True
    return False


def _format_tokens(count: int) -> str:
    """Format token count with k/M suffix for readability."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    elif count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


def _get_user_name_cache() -> Dict[str, str]:
    """Get or initialize the user name cache from session state."""
    if "feedback_user_names" not in st.session_state:
        st.session_state.feedback_user_names = {}
    cache: Dict[str, str] = st.session_state.feedback_user_names
    return cache


def _collect_feedback_user_ids(messages: List[Dict[str, Any]]) -> List[str]:
    """
    Collect all telegram_user_ids from feedback entries that need name lookup.

    Args:
        messages: List of message dicts

    Returns:
        List of telegram_user_ids that don't have user_name set
    """
    user_ids = set()
    cache = _get_user_name_cache()

    for msg in messages:
        metadata = msg.get("metadata", {})
        if not metadata:
            continue

        feedback = metadata.get("feedback", [])
        if isinstance(feedback, dict):
            feedback = [feedback]

        for fb in feedback:
            # Only collect IDs that don't have user_name and aren't already cached
            if not fb.get("user_name"):
                telegram_id = fb.get("telegram_user_id")
                if telegram_id and telegram_id not in cache:
                    user_ids.add(telegram_id)

    return list(user_ids)


def _lookup_and_cache_user_names(db: "SupabaseReader", user_ids: List[str]) -> None:
    """
    Look up user names from auth DB and cache them in session state.

    Args:
        db: SupabaseReader instance
        user_ids: List of telegram_user_ids to look up
    """
    if not user_ids:
        return

    import asyncio

    cache = _get_user_name_cache()

    try:
        # Use the existing batch lookup method
        name_map = asyncio.run(db._batch_lookup_user_names(user_ids))
        cache.update(name_map)
    except Exception as e:
        print(f"Error looking up feedback user names: {e}")


def _render_feedback_html(metadata: Dict[str, Any]) -> str:
    """
    Render feedback emojis with hover tooltips showing user names.

    Uses cached user names from session state for telegram_user_ids.

    Args:
        metadata: Message metadata dict containing feedback array

    Returns:
        HTML string for feedback display, or empty string if no feedback
    """
    feedback = metadata.get("feedback", [])
    if not feedback:
        return ""

    # Handle legacy single-object format
    if isinstance(feedback, dict):
        feedback = [feedback]

    if not feedback:
        return ""

    # Get cached user names
    cache = _get_user_name_cache()

    import html as html_module

    # Build emoji spans with tooltips
    emoji_spans = []
    for fb in feedback:
        emoji = fb.get("emoji", "👍")
        telegram_id = fb.get("telegram_user_id", "")

        # Priority: stored user_name > cached lookup > telegram_id > Unknown
        user_name = fb.get("user_name") or cache.get(telegram_id) or telegram_id or "Unknown"
        # Escape to prevent HTML injection via display names
        user_name = html_module.escape(str(user_name), quote=True)

        feedback_type = fb.get("type", "unknown")

        # Color based on feedback type
        bg_color = "#e8f5e9" if feedback_type == "thumbs_up" else "#ffebee"

        emoji_spans.append(
            f'<span style="cursor: help; padding: 2px 4px; margin: 0 2px; '
            f'background-color: {bg_color}; border-radius: 4px; font-size: 1.1em;" '
            f'title="{user_name}">{emoji}</span>'
        )

    return f"""
        <div style="margin-top: 6px; display: flex; align-items: center; gap: 4px;">
            <span style="font-size: 0.8em; color: #888;">Reactions:</span>
            {"".join(emoji_spans)}
        </div>
    """


def _is_expired_for_deletion(msg: Dict[str, Any], is_group: bool) -> bool:
    """Check if a message is past the 48h Telegram deletion window.

    Telegram bots can only delete messages up to 48 hours old in groups.
    In private chats bots can delete any message at any time.
    """
    if not is_group:
        return False
    msg_time = datetime.fromisoformat(msg["created_at"])
    # Ensure timezone-naive comparison
    if msg_time.tzinfo is not None:
        msg_time = msg_time.replace(tzinfo=None)
    age = datetime.utcnow() - msg_time
    return age > timedelta(hours=48)


def _render_inline_delete_ui(
    messages: List[Dict[str, Any]], context: Dict[str, Any], db: SupabaseReader
):
    """Render a compact inline delete row below the messages."""
    # Reset selection when user switches to a different chat
    current_chat = context.get("chat_id", "")
    if st.session_state.get("_delete_ui_chat") != current_chat:
        st.session_state["_delete_ui_chat"] = current_chat
        st.session_state.pop("delete_msg_select", None)

    is_group = context.get("is_group", False)

    # Only show messages that have a telegram_message_id (actually deletable from TG)
    deletable = []
    for msg in messages:
        if (
            msg.get("role") == "model"
            and msg.get("content")
            and msg.get("telegram_message_id")
            and not (msg.get("metadata") or {}).get("deleted")
        ):
            ts = datetime.fromisoformat(msg["created_at"]).strftime("%H:%M")
            tg_id = msg["telegram_message_id"]
            preview = (msg["content"] or "")[:60].replace("\n", " ")
            expired = _is_expired_for_deletion(msg, is_group)
            label = f"[{ts}] #{tg_id} {preview}..."
            if expired:
                label += " (expired >48h)"
            deletable.append({"label": label, "msg": msg, "expired": expired})

    if not deletable:
        return

    with st.expander("🗑️ Delete a bot message"):
        selected = st.selectbox(
            "Select message to delete",
            range(len(deletable)),
            format_func=lambda i: deletable[i]["label"],
            key="delete_msg_select",
        )
        entry = deletable[selected]
        msg = entry["msg"]

        if entry["expired"]:
            st.warning("This message is older than 48 hours — Telegram will reject deletion.")

        confirm_key = f"confirm_delete_{msg['id']}"
        if not st.session_state.get(confirm_key):
            if st.button(
                "Delete from Telegram",
                type="primary",
                key="delete_msg_btn",
                disabled=entry["expired"],
            ):
                st.session_state[confirm_key] = True
                st.rerun()
        else:
            st.warning("Are you sure? This cannot be undone.")
            col_yes, col_no = st.columns(2)
            with col_yes:
                if st.button("Yes, delete", type="primary", key="confirm_del_yes"):
                    chat_id = (
                        msg.get("from_chat_id")
                        or context.get("telegram_chat_id")
                        or context.get("chat_id", "")
                    )
                    result = db.delete_bot_message(
                        message_id=msg["id"],
                        chat_id=chat_id,
                        telegram_message_id=msg.get("telegram_message_id"),
                    )
                    st.session_state.pop(confirm_key, None)
                    if result["success"]:
                        st.success("Message deleted")
                        st.rerun()
                    else:
                        st.error(f"Failed: {result.get('error', 'Unknown error')}")
            with col_no:
                if st.button("Cancel", key="confirm_del_no"):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()


def render_conversation(context: Dict[str, Any], db: SupabaseReader, days_back: int = 2):
    """
    Render conversation view for a selected chat context.

    Args:
        context: Selected chat context dict
        db: Supabase reader instance
        days_back: Number of days to look back for messages (from sidebar slider)
    """
    # Simple header with just message count
    st.markdown(f"**Messages:** {context['message_count']}")

    # Use date range from sidebar slider
    date_from_dt = datetime.utcnow() - timedelta(days=days_back)
    date_to_dt = datetime.utcnow()

    # Fetch messages
    with st.spinner("Loading messages..."):
        messages = db.get_conversation_messages(
            chat_id=context["chat_id"],
            group_id=context.get("group_id"),
            telegram_chat_id=(context.get("telegram_chat_id") if context.get("is_group") else None),
            telegram_topic_id=context.get("telegram_topic_id"),
            date_from=date_from_dt,
            date_to=date_to_dt,
            limit=500,
        )

    if not messages:
        st.info("No messages found in this date range")
        return

    # Controls row
    show_internal = st.toggle("Show internal messages to LLM", value=False)

    visible_count = sum(1 for m in messages if not _is_internal_message(m))
    if show_internal:
        st.caption(f"Showing {len(messages)} messages ({visible_count} conversation)")
    else:
        st.caption(f"Showing {visible_count} messages")

    # Look up user names for feedback entries (uses cache to avoid repeated lookups)
    feedback_user_ids = _collect_feedback_user_ids(messages)
    if feedback_user_ids:
        _lookup_and_cache_user_names(db, feedback_user_ids)

    # Group messages by session for better context
    sessions: Dict[str, List[Dict[str, Any]]] = {}
    for msg in messages:
        session_id = msg.get("session_id", "unknown")
        if session_id not in sessions:
            sessions[session_id] = []
        sessions[session_id].append(msg)

    # Build HTML for all messages
    messages_html = []
    for session_id, session_messages in sessions.items():
        messages_html.append(_render_session_html(session_id, session_messages, show_internal))

    # Render in scrollable div
    full_html = """
    <style>
        .main .block-container {
            padding-top: 1rem;
            padding-bottom: 1rem;
        }
        .messages-scroll-container {
            height: 800px;
            overflow-y: auto;
            overflow-x: hidden;
            border: 1px solid #e0e0e0;
            border-radius: 8px;
            padding: 15px;
            background-color: #fafafa;
            scroll-behavior: smooth;
        }
        .session-block {
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 10px;
            margin-bottom: 15px;
            background-color: white;
        }
        .session-header {
            font-weight: bold;
            color: #333;
            margin-bottom: 10px;
            padding: 8px;
            background-color: #f8f9fa;
            border-radius: 4px;
            cursor: pointer;
        }
        .session-messages {
            /* No max-height — single scroll container (#msg-container) so
               the JS thread-spread scroll engine receives all scroll events. */
        }
        /* Telegram markdown styles */
        code {
            background-color: #f4f4f4;
            border-radius: 3px;
            padding: 2px 4px;
            font-family: 'Courier New', Courier, monospace;
            font-size: 0.9em;
            color: #c7254e;
        }
        pre {
            background-color: #f4f4f4;
            border-radius: 4px;
            padding: 10px;
            overflow-x: auto;
            border-left: 3px solid #0088cc;
            margin: 8px 0;
        }
        pre code {
            background-color: transparent;
            padding: 0;
            color: #333;
        }
        strong {
            font-weight: 700;
            color: #000;
        }
        em {
            font-style: italic;
        }
        s {
            text-decoration: line-through;
            opacity: 0.7;
        }
        a {
            color: #0088cc;
            text-decoration: none;
        }
        a:hover {
            text-decoration: underline;
        }
        /* Thread line styles */
        .chat-msg {
            position: relative;
            box-sizing: border-box;
            flex: 1;
            min-width: 0;
        }
        .thread-region {
            margin: 4px 0;
        }
        .thread-msg-row {
            display: flex;
            align-items: stretch;
        }
        .thread-lanes {
            position: relative;
            flex-shrink: 0;
        }
        .thread-lane-segment {
            position: absolute;
            top: 0;
            bottom: 0;
            width: 12px;
        }
        .thread-line {
            position: absolute;
            left: 4px;
            width: 3px;
            border-radius: 1.5px;
        }
        .thread-connector {
            position: absolute;
            top: 50%;
            height: 3px;
            border-radius: 1.5px;
            transform: translateY(-50%);
            z-index: 1;
        }
    </style>
    <div class="messages-scroll-container" id="msg-container">
    """

    # Add each session's HTML directly (not pre-joined to avoid escaping issues)
    st.components.v1.html(
        full_html
        + "".join(messages_html)
        + """
    </div>
    <script>
        // --- Auto-scroll to bottom ---
        function scrollToBottom() {
            var c = document.getElementById('msg-container');
            if (c) c.scrollTop = c.scrollHeight;
        }
        scrollToBottom();
        setTimeout(scrollToBottom, 100);
        setTimeout(scrollToBottom, 500);
        window.addEventListener('load', scrollToBottom);
    </script>
    """,
        height=850,
        scrolling=True,
    )

    # Inline delete UI below messages
    _render_inline_delete_ui(messages, context, db)


def _render_messages_with_thread_spread(messages: List[Dict[str, Any]]) -> str:
    """Render messages with vertical thread lines on the left.

    Detects contiguous regions where 2+ thread IDs appear and renders
    vertical colored lines in a lane area to the left of messages,
    connecting messages that share the same thread.

    Uses lane packing: once a thread's last message passes, its lane is
    freed for reuse by the next thread that needs one. Horizontal connector
    lines span from the thread's lane all the way to the message border.
    """
    THREAD_COLORS = ["#9c27b0", "#1565c0", "#2e7d32", "#e65100", "#c62828", "#00838f"]
    LANE_WIDTH = 12  # pixels per thread lane

    # Build list of (html, thread_id) tuples
    rendered = []
    for msg in messages:
        html = _render_message_html(msg)
        tid = msg.get("thread_id") or "none"
        rendered.append((html, tid))

    # Find multi-thread regions
    regions = _find_multi_thread_regions([tid for _, tid in rendered])

    # Build index -> region lookup
    region_set: Dict[int, Dict[str, Any]] = {}
    for region in regions:
        for idx in range(region["start"], region["end"] + 1):
            region_set[idx] = region

    output: list[str] = []
    i = 0
    while i < len(rendered):
        if i in region_set and i == region_set[i]["start"]:
            region = region_set[i]
            start, end = region["start"], region["end"]

            # Track first/last occurrence of each thread in the region
            first_occ: Dict[str, int] = {}
            last_occ: Dict[str, int] = {}
            for j in range(start, end + 1):
                _, t = rendered[j]
                if t != "none":
                    if t not in first_occ:
                        first_occ[t] = j
                    last_occ[t] = j

            # Assign lanes with packing: reuse freed lanes
            # lane_assignment[thread_id] = lane_index (only while thread is active)
            lane_assignment: Dict[str, int] = {}
            # Per-row snapshot of active lanes: row_lanes[j] = dict(thread_id -> lane_idx)
            row_lanes: Dict[int, Dict[str, int]] = {}
            free_lanes: list[int] = []  # reusable lane indices
            next_lane = 0

            for j in range(start, end + 1):
                _, tid = rendered[j]

                # Allocate lane for thread on its first appearance
                if tid != "none" and tid not in lane_assignment:
                    if free_lanes:
                        lane = min(free_lanes)
                        free_lanes.remove(lane)
                    else:
                        lane = next_lane
                        next_lane += 1
                    lane_assignment[tid] = lane

                # Snapshot current active lanes for this row
                row_lanes[j] = dict(lane_assignment)

                # Free lanes for threads whose last message is this row
                for thread_id, last_j in last_occ.items():
                    if last_j == j and thread_id in lane_assignment:
                        free_lanes.append(lane_assignment[thread_id])
                        del lane_assignment[thread_id]

            # Color assignment per thread (stable by first-appearance order)
            thread_order = []
            for j in range(start, end + 1):
                _, t = rendered[j]
                if t != "none" and t not in thread_order:
                    thread_order.append(t)
            thread_color: Dict[str, str] = {}
            for ci, t in enumerate(thread_order):
                thread_color[t] = THREAD_COLORS[ci % len(THREAD_COLORS)]

            output.append('<div class="thread-region">')

            for j in range(start, end + 1):
                html, tid = rendered[j]
                active_lanes = row_lanes[j]

                # Max lane index active on this row determines width
                if active_lanes:
                    max_lane = max(active_lanes.values()) + 1
                else:
                    max_lane = 0
                lanes_width = max_lane * LANE_WIDTH

                # Build lane segments
                lanes_html = ""
                for lt, lane_idx in active_lanes.items():
                    color = thread_color[lt]
                    left = lane_idx * LANE_WIDTH
                    is_msg_thread = lt == tid
                    is_first = j == first_occ.get(lt)
                    is_last = j == last_occ.get(lt)

                    if is_msg_thread:
                        # Vertical line segment
                        if is_first and is_last:
                            vline = ""
                        elif is_first:
                            vline = (
                                f'<div class="thread-line" style="background:{color};'
                                f' top:50%; bottom:0;"></div>'
                            )
                        elif is_last:
                            vline = (
                                f'<div class="thread-line" style="background:{color};'
                                f' top:0; bottom:50%;"></div>'
                            )
                        else:
                            vline = (
                                f'<div class="thread-line" style="background:{color};'
                                f' top:0; bottom:0;"></div>'
                            )

                        # Horizontal connector: from lane center to right edge of lanes area
                        conn_left = left + 4  # center of lane
                        conn_width = lanes_width - conn_left
                        lanes_html += (
                            f'<div class="thread-lane-segment" style="left:{left}px;">'
                            f"{vline}</div>"
                            f'<div class="thread-connector" style="background:{color};'
                            f' left:{conn_left}px; width:{conn_width}px;"></div>'
                        )
                    else:
                        # Pass-through: thread is active but message belongs to another
                        lanes_html += (
                            f'<div class="thread-lane-segment" style="left:{left}px;">'
                            f'<div class="thread-line" style="background:{color};'
                            f' top:0; bottom:0; opacity:0.3;"></div>'
                            f"</div>"
                        )

                # Color the message's left border
                if tid in thread_color:
                    color = thread_color[tid]
                    html = html.replace(
                        'style="',
                        f'style="border-left: 3px solid {color}; ',
                        1,
                    )

                output.append(
                    f'<div class="thread-msg-row">'
                    f'<div class="thread-lanes" style="width:{lanes_width}px;'
                    f' min-width:{lanes_width}px;">'
                    f"{lanes_html}"
                    f"</div>"
                    f"{html}"
                    f"</div>"
                )

            output.append("</div>")
            i = end + 1
        else:
            output.append(rendered[i][0])
            i += 1

    return "".join(output)


def _find_multi_thread_regions(thread_ids: List[str]) -> List[Dict[str, Any]]:
    """Find contiguous regions with 2+ distinct thread IDs.

    Returns list of dicts with start and end (inclusive).
    Thread lists are computed dynamically during rendering with lane packing.
    """
    regions = []
    i = 0

    while i < len(thread_ids):
        tid = thread_ids[i]
        region_threads = set()
        if tid != "none":
            region_threads.add(tid)
        j = i + 1

        while j < len(thread_ids):
            next_tid = thread_ids[j]
            if next_tid != "none":
                region_threads.add(next_tid)

            if len(region_threads) >= 2:
                j += 1
                continue

            # Lookahead: check if next few messages bring another thread
            look_ahead = min(j + 4, len(thread_ids))
            found = False
            for k in range(j, look_ahead):
                if thread_ids[k] != "none":
                    region_threads.add(thread_ids[k])
                if len(region_threads) >= 2:
                    found = True
                    break
            if found:
                j += 1
                continue
            break

        if len(region_threads) >= 2:
            regions.append({"start": i, "end": j - 1})
            i = j
        else:
            i += 1

    return regions


def _render_session_html(
    session_id: str, messages: List[Dict[str, Any]], show_internal: bool = False
) -> str:
    """
    Render a single conversation session as HTML.

    Args:
        session_id: Session identifier
        messages: List of messages in this session
        show_internal: Whether to show internal messages (tool calls, prompts)

    Returns:
        HTML string for the session
    """
    first_msg = messages[0]
    timestamp = datetime.fromisoformat(first_msg["created_at"])

    # Calculate total tokens for this session from model messages with metadata
    session_input_tokens = 0
    session_output_tokens = 0
    for msg in messages:
        metadata = msg.get("metadata", {})
        if metadata and metadata.get("total_tokens"):
            session_input_tokens += metadata.get("input_tokens", 0)
            session_output_tokens += metadata.get("output_tokens", 0)

    # Build token display if we have token data
    token_display = ""
    if session_input_tokens > 0 or session_output_tokens > 0:
        token_display = f' · <span style="color: #ff6b35; font-weight: bold;">Input Tokens: {_format_tokens(session_input_tokens)} Output Tokens: {_format_tokens(session_output_tokens)}</span>'

    # Count visible (non-internal) messages for display
    visible_count = sum(1 for m in messages if not _is_internal_message(m))

    # Build session header
    session_html = f"""
    <div class="session-block">
        <div class="session-header">
            🗨️ Session {session_id[:8]}... · {timestamp.strftime("%Y-%m-%d %H:%M")} · {visible_count} messages{token_display}
        </div>
        <div class="session-messages">
    """

    # Filter messages based on show_internal toggle
    visible_msgs = [msg for msg in messages if show_internal or not _is_internal_message(msg)]

    # Detect multi-thread regions and render with CSS grid
    session_html += _render_messages_with_thread_spread(visible_msgs)

    session_html += """
        </div>
    </div>
    """

    return session_html


def _render_message_html(message: Dict[str, Any]) -> str:
    """
    Render a single message as HTML string.

    Args:
        message: Message dict from database

    Returns:
        HTML string for the message
    """
    role = message.get("role", "unknown")
    content = message.get("content")
    function_call = message.get("function_call")
    tool_result = message.get("tool_result")
    timestamp = datetime.fromisoformat(message["created_at"])
    time_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    thread_id = message.get("thread_id") or "none"
    thread_display = (
        f' · <span style="color: #9c27b0;">thread:{thread_id[:8]}</span>'
        if thread_id != "none"
        else ""
    )

    # Escape content for HTML
    content_html = _escape_markdown(content) if content else "<i>No content</i>"

    if role == "user":
        return f"""
        <div class="chat-msg" data-thread-id="{thread_id}" style="background-color: #e3f2fd; padding: 10px; border-radius: 10px; margin: 5px 0;">
            <div style="font-size: 0.8em; color: #666; margin-bottom: 5px;">
                👤 User · {time_str}{thread_display}
            </div>
            <div>{content_html}</div>
        </div>
        """

    elif role == "model":
        if function_call and not content:
            # Function call display
            func_name = function_call.get("name", "unknown")
            func_args = function_call.get("arguments", {})
            arg_summary = ", ".join([f"{k}={v}" for k, v in list(func_args.items())[:3]])
            if len(func_args) > 3:
                arg_summary += "..."

            # JSON details for expandable view
            func_json = json.dumps(function_call, indent=2)

            return f"""
            <div class="chat-msg" data-thread-id="{thread_id}" style="background-color: #fff9e6; padding: 10px; border-radius: 10px; margin: 5px 0; border-left: 4px solid #ffc107;">
                <div style="font-size: 0.8em; color: #666; margin-bottom: 5px;">
                    🔧 Bot Tool Call · {time_str}{thread_display}
                </div>
                <div style="font-weight: bold; color: #ff6f00;">
                    {func_name}({arg_summary})
                </div>
                <details style="margin-top: 8px;">
                    <summary style="cursor: pointer; color: #666; font-size: 0.9em;">📋 View full details</summary>
                    <pre style="background: #f5f5f5; padding: 8px; border-radius: 4px; overflow-x: auto; font-size: 0.85em;">{_escape_markdown(func_json)}</pre>
                </details>
            </div>
            """
        else:
            # Regular bot response - check for token metadata
            metadata = message.get("metadata", {})
            token_info = ""
            if metadata and metadata.get("total_tokens"):
                input_tokens = metadata.get("input_tokens", 0)
                output_tokens = metadata.get("output_tokens", 0)
                token_info = f' <span style="color: #ff6b35; font-weight: bold;">Input Tokens: {_format_tokens(input_tokens)} Output Tokens: {_format_tokens(output_tokens)}</span>'

            # Render feedback if present
            feedback_html = _render_feedback_html(metadata) if metadata else ""

            # Check if message was blocked by verification
            is_blocked = metadata.get("blocked", False) if metadata else False
            is_placeholder = metadata.get("placeholder_for_blocked", False) if metadata else False

            if is_blocked:
                # Blocked message - show with red background and warning
                verification_feedback = metadata.get("verification_feedback", "")
                verification_categories = metadata.get("verification_categories", [])
                categories_str = (
                    ", ".join(verification_categories) if verification_categories else ""
                )

                return f"""
                <div class="chat-msg" data-thread-id="{thread_id}" style="background-color: #ffebee; padding: 10px; border-radius: 10px; margin: 5px 0; border-left: 4px solid #f44336;">
                    <div style="font-size: 0.8em; color: #c62828; margin-bottom: 5px;">
                        🚫 Bot (BLOCKED) · {time_str}{thread_display}{token_info}
                    </div>
                    <div style="color: #b71c1c;">{content_html}</div>
                    <details style="margin-top: 8px;">
                        <summary style="cursor: pointer; color: #c62828; font-size: 0.9em;">⚠️ Verification failure details</summary>
                        <div style="background: #fff5f5; padding: 8px; border-radius: 4px; margin-top: 4px; font-size: 0.85em;">
                            <div><strong>Categories:</strong> {categories_str or "N/A"}</div>
                            <div style="margin-top: 4px;"><strong>Feedback:</strong> {_escape_markdown(verification_feedback) or "N/A"}</div>
                        </div>
                    </details>
                    {feedback_html}
                </div>
                """
            elif is_placeholder:
                # Placeholder message sent instead of blocked content
                return f"""
                <div class="chat-msg" data-thread-id="{thread_id}" style="background-color: #fff3e0; padding: 10px; border-radius: 10px; margin: 5px 0; border-left: 4px solid #ff9800;">
                    <div style="font-size: 0.8em; color: #e65100; margin-bottom: 5px;">
                        🔄 Bot (Placeholder) · {time_str}{thread_display}
                    </div>
                    <div>{content_html}</div>
                    {feedback_html}
                </div>
                """
            else:
                # Normal bot response — show telegram_message_id for identification
                tg_msg_id = message.get("telegram_message_id")
                tg_id_display = (
                    f' · <span style="color: #999; font-weight: normal;">#{tg_msg_id}</span>'
                    if tg_msg_id
                    else ""
                )
                return f"""
                <div class="chat-msg" data-thread-id="{thread_id}" style="background-color: #f5f5f5; padding: 10px; border-radius: 10px; margin: 5px 0;">
                    <div style="font-size: 0.8em; color: #666; margin-bottom: 5px;">
                        🤖 Bot · {time_str}{thread_display}{token_info}{tg_id_display}
                    </div>
                    <div>{content_html}</div>
                    {feedback_html}
                </div>
                """

    elif role == "tool":
        if tool_result:
            result_name = tool_result.get("name", "unknown")
            result_success = tool_result.get("success", False)
            result_output = tool_result.get("output")

            # Create preview
            if isinstance(result_output, str):
                output_preview = result_output[:100] + ("..." if len(result_output) > 100 else "")
            elif isinstance(result_output, dict):
                output_preview = f"{len(result_output)} fields"
            else:
                output_preview = str(result_output)[:100]

            status_icon = "✅" if result_success else "❌"
            bg_color = "#e8f5e9" if result_success else "#ffebee"
            border_color = "#4caf50" if result_success else "#f44336"

            tool_json = json.dumps(tool_result, indent=2)

            return f"""
            <div class="chat-msg" data-thread-id="{thread_id}" style="background-color: {bg_color}; padding: 10px; border-radius: 10px; margin: 5px 0; border-left: 4px solid {border_color};">
                <div style="font-size: 0.8em; color: #666; margin-bottom: 5px;">
                    {status_icon} Tool Result · {time_str}{thread_display}
                </div>
                <div style="font-weight: bold;">
                    {result_name}
                </div>
                <div style="font-size: 0.9em; color: #666; margin-top: 5px;">
                    {_escape_markdown(output_preview)}
                </div>
                <details style="margin-top: 8px;">
                    <summary style="cursor: pointer; color: #666; font-size: 0.9em;">📋 View full result</summary>
                    <pre style="background: #f5f5f5; padding: 8px; border-radius: 4px; overflow-x: auto; font-size: 0.85em;">{_escape_markdown(tool_json)}</pre>
                </details>
            </div>
            """

    # Unknown role
    return f"""
    <div class="chat-msg" data-thread-id="{thread_id}" style="background-color: #fff3cd; padding: 10px; border-radius: 10px; margin: 5px 0;">
        <div style="font-size: 0.8em; color: #666; margin-bottom: 5px;">
            ⚠️ {role.upper()} · {time_str}{thread_display}
        </div>
        <div>{content_html}</div>
    </div>
    """


def _escape_markdown(text: Optional[str]) -> str:
    """
    Parse Telegram markdown and convert to safe HTML using the markdown library.

    Sanitizes HTML to prevent XSS from user-supplied content.

    Args:
        text: Text with Telegram markdown

    Returns:
        Sanitized HTML formatted text
    """
    if not text:
        return ""

    import html as html_module
    import re

    import markdown  # type: ignore[import-untyped]

    # First, escape any raw HTML in user content to prevent XSS
    text = html_module.escape(text)

    # Handle Telegram-specific features before standard markdown processing
    # Spoiler tags ||text|| -> special span (safe — we control the HTML)
    text = re.sub(
        r"\|\|(.+?)\|\|",
        r'<span style="background-color: #000; color: #000;" title="Spoiler - hover to reveal">\1</span>',
        text,
    )

    # Escape hashtags used as Telegram tags (e.g., #BotAction) before markdown
    # processing, so they don't become headings. Only escape # followed by a
    # word character (not # followed by space, which could be intentional markdown).
    text = re.sub(r"^(#{1,6})(\w)", r"\\\1\2", text, flags=re.MULTILINE)

    # Process standard markdown (supports bold, italic, code, links, etc.)
    html: str = markdown.markdown(text, extensions=["fenced_code", "nl2br", "sane_lists"])

    # Remove wrapping <p> tags if present (for inline rendering)
    html = re.sub(r"^<p>(.*)</p>$", r"\1", html.strip(), flags=re.DOTALL)

    return html


# Import for date handling
from datetime import timedelta

__all__ = ["render_conversation"]
