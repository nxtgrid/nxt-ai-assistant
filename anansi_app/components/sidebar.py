"""
Sidebar navigation component for chat viewer.

Displays list of chat contexts (groups and users) with filtering options.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import streamlit as st
from services.supabase_reader import SupabaseReader


def _format_tokens(count: int) -> str:
    """Format token count with k/M suffix for readability."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    elif count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


def render_sidebar(
    db: SupabaseReader, user_email: str
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], int]:
    """
    Render sidebar with chat context navigation.

    Args:
        db: Supabase reader instance
        user_email: Email of authenticated user (for cache isolation)

    Returns:
        Tuple of (selected chat context dict or None, stats data dict, days_back int)
    """
    # Check URL parameters for chat selection on page load
    _restore_from_url_params(db)

    # Red border for escalated chat containers
    st.markdown(
        "<style>"
        "section[data-testid='stSidebar'] "
        "[data-testid='stVerticalBlockBorderWrapper'] "
        "{ border-color: #e74c3c !important; border-width: 2px !important; }"
        "</style>",
        unsafe_allow_html=True,
    )

    st.subheader("Date Range")
    days_back = st.slider("Days to look back", 1, 31, 2, key="chat_viewer_days")

    # Search/filter input (placed before fetching to avoid layout shift)
    search = st.text_input("🔍 Search conversations", "", key="search_conversations")

    st.divider()

    # Fetch chat contexts with loading indicator
    # Include user_email in query to ensure cache isolation per user
    with st.spinner("Loading conversations..."):
        contexts = db.get_chat_contexts(user_email=user_email, days_back=days_back)

        # If search term provided, also search by message content
        content_search_contexts = []
        if search and search.strip():
            content_search_contexts = db.search_conversations_by_content(
                search_term=search.strip(), days_back=days_back, user_email=user_email
            )

    # Combine and deduplicate contexts from both searches
    if search and search.strip():
        # Merge contexts from title search and content search
        search_lower = search.lower()
        title_matches = [c for c in contexts if search_lower in c["display_name"].lower()]

        # Deduplicate by creating a dict keyed by (chat_id, group_id)
        combined_contexts = {}
        for ctx in title_matches + content_search_contexts:
            key = (ctx["chat_id"], ctx.get("group_id"))
            if key not in combined_contexts:
                combined_contexts[key] = ctx

        contexts = list(combined_contexts.values())
        # Re-sort by last message
        contexts = sorted(contexts, key=lambda x: x["last_message"], reverse=True)

    # Refresh selected context with updated data (e.g., new token counts for changed time range)
    selected_context = st.session_state.get("selected_context")
    if selected_context:
        for ctx in contexts:
            if ctx["chat_id"] == selected_context["chat_id"] and ctx.get(
                "group_id"
            ) == selected_context.get("group_id"):
                st.session_state["selected_context"] = ctx
                break

    # Separate groups and direct messages
    groups = [c for c in contexts if c["is_group"]]
    direct_messages = [c for c in contexts if not c["is_group"]]

    if not contexts:
        if search and search.strip():
            st.info(f"No conversations found matching '{search}'")
        else:
            st.warning(f"No chat activity found in the last {days_back} days")
            st.info(
                "💡 Try increasing the date range or check if messages exist in the database "
                "by looking at the statistics below."
            )
    else:
        # Display all contexts (already filtered by search if applicable)
        filtered_groups = groups
        filtered_dms = direct_messages

        # Sort: escalated first, then most recent first
        # (not escalated = True sorts after False, so escalated comes first)
        # Negate timestamp not possible with strings, so use two-pass stable sort
        filtered_groups.sort(key=lambda c: c.get("last_message", ""), reverse=True)
        filtered_groups.sort(key=lambda c: not c.get("is_escalated", False))
        filtered_dms.sort(key=lambda c: c.get("last_message", ""), reverse=True)
        filtered_dms.sort(key=lambda c: not c.get("is_escalated", False))

        # Groups section
        if filtered_groups:
            st.subheader(f"📊 Groups ({len(filtered_groups)})")
            for context in filtered_groups:
                _render_chat_button(context, prefix="group")

        # Direct messages section
        if filtered_dms:
            st.subheader(f"Direct Messages ({len(filtered_dms)})")
            for context in filtered_dms:
                _render_chat_button(context, prefix="dm")

    st.divider()

    # Calculate stats for the selected date range (will be shown in main area)
    # Include user_email in query to ensure cache isolation per user
    with st.spinner("Calculating statistics..."):
        start_date = datetime.utcnow() - timedelta(days=days_back)
        range_stats = db.get_period_stats(
            user_email=user_email, start_date=start_date, end_date=datetime.utcnow()
        )

    # Prepare stats data to return
    stats_data = {
        "days_back": days_back,
        "total_conversations": len(contexts),
        "groups": len(groups),
        "direct_messages": len(direct_messages),
        "total_messages": range_stats.get("messages", 0),
        "unique_users": range_stats.get("users", 0),
        "input_tokens": range_stats.get("input_tokens", 0),
        "output_tokens": range_stats.get("output_tokens", 0),
        "median_response_time": range_stats.get("median_response_time"),
    }

    # Return selected context, stats, and days_back
    selected: Optional[Dict[str, Any]] = st.session_state.get("selected_context")
    return selected, stats_data, days_back


def _render_chat_button(context: Dict[str, Any], prefix: str) -> None:
    """Render a single chat button, with a red border if the session is escalated."""
    last_msg = datetime.fromisoformat(context["last_message"])
    time_ago = _format_time_ago(last_msg)

    input_tokens = context.get("input_tokens", 0)
    output_tokens = context.get("output_tokens", 0)
    token_line = ""
    if input_tokens > 0 or output_tokens > 0:
        token_line = f"\nTokens In: {_format_tokens(input_tokens)} Tokens Out: {_format_tokens(output_tokens)}"

    escalation_tag = "🚨 " if context.get("is_escalated") else ""
    display_name = context["display_name"]
    # Truncate long names with ellipsis
    if len(display_name) > 30:
        display_name = display_name[:28] + "…"
    label = f"{escalation_tag}{display_name} · {time_ago}{token_line}"

    selected_context = st.session_state.get("selected_context")
    if prefix == "group":
        topic_id = context.get("telegram_topic_id")
        is_selected = (
            selected_context
            and selected_context["chat_id"] == context["chat_id"]
            and selected_context.get("group_id") == context.get("group_id")
            and selected_context.get("telegram_topic_id") == topic_id
        )
        # Include topic_id in button key for uniqueness
        topic_suffix = f"_t{topic_id}" if topic_id else ""
        button_key = f"group_{context.get('telegram_chat_id', context['chat_id'])}{topic_suffix}"
    else:
        is_selected = (
            selected_context
            and selected_context["chat_id"] == context["chat_id"]
            and not context.get("group_id")
        )
        button_key = f"dm_{context['chat_id']}"

    # Escalated chats: wrap in a bordered container, styled red via CSS
    if context.get("is_escalated"):
        container = st.container(border=True)
        with container:
            clicked = st.button(
                label,
                key=button_key,
                use_container_width=True,
                type="primary" if is_selected else "secondary",
            )
    else:
        clicked = st.button(
            label,
            key=button_key,
            use_container_width=True,
            type="primary" if is_selected else "secondary",
        )

    if clicked:
        st.session_state["selected_context"] = context
        st.query_params["chat_id"] = context["chat_id"]
        if prefix == "group" and context.get("group_id"):
            st.query_params["group_id"] = context["group_id"]
        elif prefix == "dm" and "group_id" in st.query_params:
            del st.query_params["group_id"]
        st.rerun()


def _format_time_ago(dt: datetime) -> str:
    """
    Format datetime as relative time string.

    Args:
        dt: Datetime to format

    Returns:
        Human-readable time ago string
    """
    # Handle both timezone-aware and naive datetimes
    now = datetime.utcnow()
    if dt.tzinfo is not None:
        # If dt has timezone, make now timezone-aware too
        from datetime import timezone

        now = datetime.now(timezone.utc)
        # Remove timezone info for comparison
        dt = dt.replace(tzinfo=None)
        now = now.replace(tzinfo=None)

    delta = now - dt

    if delta < timedelta(minutes=1):
        return "just now"
    elif delta < timedelta(hours=1):
        minutes = int(delta.total_seconds() / 60)
        return f"{minutes}m ago"
    elif delta < timedelta(days=1):
        hours = int(delta.total_seconds() / 3600)
        return f"{hours}h ago"
    elif delta < timedelta(days=7):
        days = delta.days
        return f"{days}d ago"
    else:
        return dt.strftime("%b %d")


def _restore_from_url_params(db: SupabaseReader):
    """
    Restore selected conversation from URL query parameters.

    Args:
        db: Supabase reader instance
    """
    # Only restore if not already selected and URL params exist
    if "selected_context" not in st.session_state:
        chat_id = st.query_params.get("chat_id")
        group_id = st.query_params.get("group_id")

        if chat_id:
            # Fetch the conversation context from the database
            try:
                # Convert to int if needed
                chat_id_int = int(chat_id)
                group_id_int = int(group_id) if group_id else None

                # Fetch conversation details
                context = db.get_chat_context_by_id(chat_id_int, group_id_int)
                if context:
                    st.session_state["selected_context"] = context
            except (ValueError, TypeError):
                # Invalid URL parameters, ignore
                pass


__all__ = ["render_sidebar"]
