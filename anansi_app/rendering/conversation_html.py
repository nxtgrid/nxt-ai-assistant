"""Pure HTML builders for the conversation viewer (framework-agnostic).

Extracted from ``components/conversation_view.py`` so both the Streamlit and the
NiceGUI front ends render identical message HTML. The only former Streamlit
coupling — the feedback user-name cache — is now an explicit ``cache`` argument
(a plain ``dict[str, str]``) threaded through the render chain.

No ``streamlit`` or ``nicegui`` imports here on purpose.
"""

from __future__ import annotations

import html as html_module
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

# Shared CSS for the scrollable message container + Telegram-markdown + thread
# lanes. Both UIs wrap the message HTML in ``MESSAGES_CONTAINER_CSS`` +
# ``<div class="messages-scroll-container" id="msg-container">`` ... ``</div>``.
MESSAGES_CONTAINER_CSS = """
<style>
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
    pre code { background-color: transparent; padding: 0; color: #333; }
    strong { font-weight: 700; color: #000; }
    em { font-style: italic; }
    s { text-decoration: line-through; opacity: 0.7; }
    a { color: #0088cc; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .chat-msg { position: relative; box-sizing: border-box; flex: 1; min-width: 0; }
    .thread-region { margin: 4px 0; }
    .thread-msg-row { display: flex; align-items: stretch; }
    .thread-lanes { position: relative; flex-shrink: 0; }
    .thread-lane-segment { position: absolute; top: 0; bottom: 0; width: 12px; }
    .thread-line { position: absolute; left: 4px; width: 3px; border-radius: 1.5px; }
    .thread-connector {
        position: absolute; top: 50%; height: 3px; border-radius: 1.5px;
        transform: translateY(-50%); z-index: 1;
    }
</style>
"""

# JS that keeps the container scrolled to the newest message.
SCROLL_TO_BOTTOM_SCRIPT = """
<script>
    function scrollToBottom() {
        var c = document.getElementById('msg-container');
        if (c) c.scrollTop = c.scrollHeight;
    }
    scrollToBottom();
    setTimeout(scrollToBottom, 100);
    setTimeout(scrollToBottom, 500);
    window.addEventListener('load', scrollToBottom);
</script>
"""

_THREAD_COLORS = ["#9c27b0", "#1565c0", "#2e7d32", "#e65100", "#c62828", "#00838f"]
_LANE_WIDTH = 12  # pixels per thread lane


def is_internal_message(message: Dict[str, Any]) -> bool:
    """Whether a message is internal (tool calls/results, command templates)."""
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


def format_tokens(count: int) -> str:
    """Format a token count with a k/M suffix."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


def escape_markdown(text: Optional[str]) -> str:
    """Parse Telegram markdown into sanitized inline HTML (XSS-safe)."""
    if not text:
        return ""

    import markdown  # type: ignore[import-untyped]

    text = html_module.escape(text)
    text = re.sub(
        r"\|\|(.+?)\|\|",
        r'<span style="background-color: #000; color: #000;" title="Spoiler - hover to reveal">\1</span>',
        text,
    )
    text = re.sub(r"^(#{1,6})(\w)", r"\\\1\2", text, flags=re.MULTILINE)
    html: str = markdown.markdown(text, extensions=["fenced_code", "nl2br", "sane_lists"])
    html = re.sub(r"^<p>(.*)</p>$", r"\1", html.strip(), flags=re.DOTALL)
    return html


def render_feedback_html(metadata: Dict[str, Any], cache: Dict[str, str]) -> str:
    """Render feedback emoji spans with user-name tooltips (from ``cache``)."""
    feedback = metadata.get("feedback", [])
    if not feedback:
        return ""
    if isinstance(feedback, dict):
        feedback = [feedback]
    if not feedback:
        return ""

    emoji_spans = []
    for fb in feedback:
        emoji = fb.get("emoji", "👍")
        telegram_id = fb.get("telegram_user_id", "")
        user_name = fb.get("user_name") or cache.get(telegram_id) or telegram_id or "Unknown"
        user_name = html_module.escape(str(user_name), quote=True)
        feedback_type = fb.get("type", "unknown")
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


def find_multi_thread_regions(thread_ids: List[str]) -> List[Dict[str, Any]]:
    """Find contiguous regions containing 2+ distinct thread IDs."""
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


def render_message_html(message: Dict[str, Any], cache: Dict[str, str]) -> str:
    """Render a single message dict as an HTML string."""
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
    content_html = escape_markdown(content) if content else "<i>No content</i>"

    if role == "user":
        return f"""
        <div class="chat-msg" data-thread-id="{thread_id}" style="background-color: #e3f2fd; padding: 10px; border-radius: 10px; margin: 5px 0;">
            <div style="font-size: 0.8em; color: #666; margin-bottom: 5px;">
                👤 User · {time_str}{thread_display}
            </div>
            <div>{content_html}</div>
        </div>
        """

    if role == "model":
        if function_call and not content:
            func_name = function_call.get("name", "unknown")
            func_args = function_call.get("arguments", {})
            arg_summary = ", ".join([f"{k}={v}" for k, v in list(func_args.items())[:3]])
            if len(func_args) > 3:
                arg_summary += "..."
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
                    <pre style="background: #f5f5f5; padding: 8px; border-radius: 4px; overflow-x: auto; font-size: 0.85em;">{escape_markdown(func_json)}</pre>
                </details>
            </div>
            """

        metadata = message.get("metadata", {})
        token_info = ""
        if metadata and metadata.get("total_tokens"):
            input_tokens = metadata.get("input_tokens", 0)
            output_tokens = metadata.get("output_tokens", 0)
            token_info = f' <span style="color: #ff6b35; font-weight: bold;">Input Tokens: {format_tokens(input_tokens)} Output Tokens: {format_tokens(output_tokens)}</span>'

        feedback_html = render_feedback_html(metadata, cache) if metadata else ""
        is_blocked = metadata.get("blocked", False) if metadata else False
        is_placeholder = metadata.get("placeholder_for_blocked", False) if metadata else False

        if is_blocked:
            verification_feedback = metadata.get("verification_feedback", "")
            verification_categories = metadata.get("verification_categories", [])
            categories_str = ", ".join(verification_categories) if verification_categories else ""
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
                        <div style="margin-top: 4px;"><strong>Feedback:</strong> {escape_markdown(verification_feedback) or "N/A"}</div>
                    </div>
                </details>
                {feedback_html}
            </div>
            """
        if is_placeholder:
            return f"""
            <div class="chat-msg" data-thread-id="{thread_id}" style="background-color: #fff3e0; padding: 10px; border-radius: 10px; margin: 5px 0; border-left: 4px solid #ff9800;">
                <div style="font-size: 0.8em; color: #e65100; margin-bottom: 5px;">
                    🔄 Bot (Placeholder) · {time_str}{thread_display}
                </div>
                <div>{content_html}</div>
                {feedback_html}
            </div>
            """
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

    if role == "tool" and tool_result:
        result_name = tool_result.get("name", "unknown")
        result_success = tool_result.get("success", False)
        result_output = tool_result.get("output")
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
            <div style="font-weight: bold;">{result_name}</div>
            <div style="font-size: 0.9em; color: #666; margin-top: 5px;">{escape_markdown(output_preview)}</div>
            <details style="margin-top: 8px;">
                <summary style="cursor: pointer; color: #666; font-size: 0.9em;">📋 View full result</summary>
                <pre style="background: #f5f5f5; padding: 8px; border-radius: 4px; overflow-x: auto; font-size: 0.85em;">{escape_markdown(tool_json)}</pre>
            </details>
        </div>
        """

    return f"""
    <div class="chat-msg" data-thread-id="{thread_id}" style="background-color: #fff3cd; padding: 10px; border-radius: 10px; margin: 5px 0;">
        <div style="font-size: 0.8em; color: #666; margin-bottom: 5px;">
            ⚠️ {role.upper()} · {time_str}{thread_display}
        </div>
        <div>{content_html}</div>
    </div>
    """


def render_messages_with_thread_spread(
    messages: List[Dict[str, Any]], cache: Dict[str, str]
) -> str:
    """Render messages with vertical thread lanes connecting shared threads."""
    rendered = []
    for msg in messages:
        html = render_message_html(msg, cache)
        tid = msg.get("thread_id") or "none"
        rendered.append((html, tid))

    regions = find_multi_thread_regions([tid for _, tid in rendered])
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

            first_occ: Dict[str, int] = {}
            last_occ: Dict[str, int] = {}
            for j in range(start, end + 1):
                _, t = rendered[j]
                if t != "none":
                    if t not in first_occ:
                        first_occ[t] = j
                    last_occ[t] = j

            lane_assignment: Dict[str, int] = {}
            row_lanes: Dict[int, Dict[str, int]] = {}
            free_lanes: list[int] = []
            next_lane = 0
            for j in range(start, end + 1):
                _, tid = rendered[j]
                if tid != "none" and tid not in lane_assignment:
                    if free_lanes:
                        lane = min(free_lanes)
                        free_lanes.remove(lane)
                    else:
                        lane = next_lane
                        next_lane += 1
                    lane_assignment[tid] = lane
                row_lanes[j] = dict(lane_assignment)
                for thread_id, last_j in last_occ.items():
                    if last_j == j and thread_id in lane_assignment:
                        free_lanes.append(lane_assignment[thread_id])
                        del lane_assignment[thread_id]

            thread_order = []
            for j in range(start, end + 1):
                _, t = rendered[j]
                if t != "none" and t not in thread_order:
                    thread_order.append(t)
            thread_color: Dict[str, str] = {}
            for ci, t in enumerate(thread_order):
                thread_color[t] = _THREAD_COLORS[ci % len(_THREAD_COLORS)]

            output.append('<div class="thread-region">')
            for j in range(start, end + 1):
                html, tid = rendered[j]
                active_lanes = row_lanes[j]
                max_lane = max(active_lanes.values()) + 1 if active_lanes else 0
                lanes_width = max_lane * _LANE_WIDTH

                lanes_html = ""
                for lt, lane_idx in active_lanes.items():
                    color = thread_color[lt]
                    left = lane_idx * _LANE_WIDTH
                    is_msg_thread = lt == tid
                    is_first = j == first_occ.get(lt)
                    is_last = j == last_occ.get(lt)
                    if is_msg_thread:
                        if is_first and is_last:
                            vline = ""
                        elif is_first:
                            vline = f'<div class="thread-line" style="background:{color}; top:50%; bottom:0;"></div>'
                        elif is_last:
                            vline = f'<div class="thread-line" style="background:{color}; top:0; bottom:50%;"></div>'
                        else:
                            vline = f'<div class="thread-line" style="background:{color}; top:0; bottom:0;"></div>'
                        conn_left = left + 4
                        conn_width = lanes_width - conn_left
                        lanes_html += (
                            f'<div class="thread-lane-segment" style="left:{left}px;">{vline}</div>'
                            f'<div class="thread-connector" style="background:{color}; left:{conn_left}px; width:{conn_width}px;"></div>'
                        )
                    else:
                        lanes_html += (
                            f'<div class="thread-lane-segment" style="left:{left}px;">'
                            f'<div class="thread-line" style="background:{color}; top:0; bottom:0; opacity:0.3;"></div>'
                            f"</div>"
                        )

                if tid in thread_color:
                    color = thread_color[tid]
                    html = html.replace('style="', f'style="border-left: 3px solid {color}; ', 1)

                output.append(
                    f'<div class="thread-msg-row">'
                    f'<div class="thread-lanes" style="width:{lanes_width}px; min-width:{lanes_width}px;">'
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


def render_session_html(
    session_id: str,
    messages: List[Dict[str, Any]],
    show_internal: bool,
    cache: Dict[str, str],
) -> str:
    """Render a single conversation session (header + threaded messages)."""
    first_msg = messages[0]
    timestamp = datetime.fromisoformat(first_msg["created_at"])

    session_input_tokens = 0
    session_output_tokens = 0
    for msg in messages:
        metadata = msg.get("metadata", {})
        if metadata and metadata.get("total_tokens"):
            session_input_tokens += metadata.get("input_tokens", 0)
            session_output_tokens += metadata.get("output_tokens", 0)

    token_display = ""
    if session_input_tokens > 0 or session_output_tokens > 0:
        token_display = f' · <span style="color: #ff6b35; font-weight: bold;">Input Tokens: {format_tokens(session_input_tokens)} Output Tokens: {format_tokens(session_output_tokens)}</span>'

    visible_count = sum(1 for m in messages if not is_internal_message(m))
    session_html = f"""
    <div class="session-block">
        <div class="session-header">
            🗨️ Session {session_id[:8]}... · {timestamp.strftime("%Y-%m-%d %H:%M")} · {visible_count} messages{token_display}
        </div>
        <div class="session-messages">
    """
    visible_msgs = [msg for msg in messages if show_internal or not is_internal_message(msg)]
    session_html += render_messages_with_thread_spread(visible_msgs, cache)
    session_html += """
        </div>
    </div>
    """
    return session_html


def build_conversation_html(
    messages: List[Dict[str, Any]],
    show_internal: bool,
    cache: Dict[str, str],
) -> str:
    """Group messages by session and return the full inner HTML (no wrapper div)."""
    sessions: Dict[str, List[Dict[str, Any]]] = {}
    for msg in messages:
        session_id = msg.get("session_id", "unknown")
        sessions.setdefault(session_id, []).append(msg)
    return "".join(
        render_session_html(sid, msgs, show_internal, cache) for sid, msgs in sessions.items()
    )
