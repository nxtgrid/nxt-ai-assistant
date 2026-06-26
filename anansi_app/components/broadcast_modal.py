"""
Broadcast Modal Component

Modal dialog for composing and sending broadcast messages to customer groups.
Features:
- Compose tab: Message composition with placeholders, templates, scheduling
- Templates tab: Manage reusable message templates
- Scheduled tab: View/cancel pending scheduled broadcasts
- History tab: View past broadcasts with delivery stats
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import streamlit as st
from services.broadcast_service import BroadcastService
from services.broadcast_verification_service import BroadcastVerificationService

# Timezone offsets from UTC (in hours)
TIMEZONE_OPTIONS = {
    "UTC": 0,
    "CET (UTC+1)": 1,
    "CEST (UTC+2)": 2,
    "GMT (UTC+0)": 0,
    "EST (UTC-5)": -5,
    "PST (UTC-8)": -8,
}

# Recurrence options for scheduled broadcasts. "Does not repeat" preserves the
# original one-shot behaviour; the others derive a cron pattern from the chosen
# first-send date/time. Mirrors the chat /schedule recurrence set.
REPEAT_OPTIONS = [
    "Does not repeat",
    "Weekly",
    "Every other week",
    "Monthly (same date)",
    "Monthly (same weekday)",
]


def _build_recurrence(dt_utc, frequency: str) -> Optional[dict]:
    """
    Build a recurrence config {schedule_type, cron_expression, timezone} from the
    chosen first-send time (UTC) and a REPEAT_OPTIONS frequency.

    The cron is expressed in UTC (matching the chat /schedule convention). The
    recurrence pattern is derived from the first-send date so the first occurrence
    always lands exactly on the user's chosen date/time.
    """
    if frequency == "Does not repeat":
        return None

    minute = dt_utc.minute
    hour = dt_utc.hour
    cron_dow = (dt_utc.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> cron Sun=0..Sat=6

    if frequency == "Weekly":
        cron, schedule_type = f"{minute} {hour} * * {cron_dow}", "recurring"
    elif frequency == "Every other week":
        cron, schedule_type = f"{minute} {hour} * * {cron_dow}", "biweekly"
    elif frequency == "Monthly (same date)":
        cron, schedule_type = f"{minute} {hour} {dt_utc.day} * *", "recurring"
    elif frequency == "Monthly (same weekday)":
        nth = ((dt_utc.day - 1) // 7) + 1
        cron, schedule_type = f"{minute} {hour} * * {cron_dow}#{nth}", "recurring"
    else:
        return None

    # Validate the generated cron before persisting it
    try:
        from croniter import croniter  # type: ignore[import-untyped]

        if not croniter.is_valid(cron):
            return None
    except ImportError:
        pass

    return {"schedule_type": schedule_type, "cron_expression": cron, "timezone": "UTC"}


def _describe_recurrence(schedule_type: Optional[str], cron_expression: Optional[str]) -> str:
    """Human-readable label for a recurrence, reusing the shared formatter if available."""
    if not schedule_type or not cron_expression:
        return "—"
    try:
        from shared.scheduling.recurrence import format_schedule_display

        return format_schedule_display(
            schedule_type, cron_expression, datetime.now(timezone.utc), "UTC"
        )
    except Exception:
        return "Every other week" if schedule_type == "biweekly" else "Recurring"


def _clear_broadcast_state():
    """Clear all broadcast-related session state."""
    keys_to_clear = [
        "broadcast_message",
        "broadcast_verified",
        "broadcast_verification_result",
        "broadcast_sending",
        "broadcast_selected_groups",
        "broadcast_send_complete",
        "broadcast_send_result",
        "broadcast_scheduled_time",
        "broadcast_images",
        "broadcast_template_images",
    ]
    for key in keys_to_clear:
        st.session_state.pop(key, None)


@st.dialog("Broadcast Message", width="large")
def show_broadcast_modal():
    """Main broadcast modal dialog."""
    # Initialize services
    broadcast_service = BroadcastService()
    verification_service = BroadcastVerificationService()

    if not broadcast_service.is_configured():
        st.error(
            "Broadcast service not configured. Check CHAT_DB_URL/CHAT_DB_SERVICE_KEY (or legacy SUPABASE_URL/KEY) and TELEGRAM_BOT_TOKEN."
        )
        return

    # Tab navigation within modal (4 tabs)
    tab1, tab2, tab3, tab4 = st.tabs(["Compose", "Templates", "Scheduled", "History"])

    with tab1:
        _render_compose_tab(broadcast_service, verification_service)

    with tab2:
        _render_templates_tab(broadcast_service)

    with tab3:
        _render_scheduled_tab(broadcast_service)

    with tab4:
        _render_history_tab(broadcast_service)


def _render_compose_tab(
    broadcast_service: BroadcastService,
    verification_service: BroadcastVerificationService,
):
    """Render the compose message tab."""
    st.subheader("Compose Broadcast")

    # Check if send just completed
    if st.session_state.get("broadcast_send_complete"):
        result = st.session_state.get("broadcast_send_result")
        completed_scheduled_time = st.session_state.get("broadcast_scheduled_time")
        if result:
            if completed_scheduled_time:
                # Scheduled broadcast
                st.success(
                    f"Broadcast scheduled for {completed_scheduled_time.strftime('%Y-%m-%d %H:%M')} "
                    f"to {result.total} groups!"
                )
            elif result.failed == 0:
                st.success(f"Broadcast sent successfully to {result.successful} groups!")
            else:
                st.warning(
                    f"Broadcast completed: {result.successful}/{result.total} delivered, "
                    f"{result.failed} failed"
                )
                if result.errors:
                    with st.expander("View Errors"):
                        for error in result.errors:
                            st.text(error)

        if st.button("Compose Another"):
            _clear_broadcast_state()
            # Clear widget states for fresh form
            for key in list(st.session_state.keys()):
                if key.startswith(("message_input", "template_selector", "send_option")):
                    del st.session_state[key]
            st.rerun()  # Force immediate refresh to show compose form
        return

    # Get available groups
    groups = broadcast_service.get_available_groups()

    if not groups:
        st.warning("No customer groups available. Check organization Telegram chat IDs.")
        return

    # Build options for multiselect
    group_options = {g["chat_id"]: g["name"] for g in groups}

    # Group selector
    selected_group_ids = st.multiselect(
        "Target Groups",
        options=list(group_options.keys()),
        default=[],
        format_func=lambda x: group_options.get(x, x),
        help="Select which groups will receive this broadcast",
    )

    # Template insertion
    templates = broadcast_service.get_templates()
    if templates:
        template_options = ["-- Select template --"] + [t["name"] for t in templates]
        col_tmpl, col_btn = st.columns([3, 1])
        with col_tmpl:
            selected_template = st.selectbox(
                "Insert Template",
                template_options,
                key="template_selector",
            )
        with col_btn:
            st.write("")  # Spacer
            st.write("")  # Spacer
            if selected_template != "-- Select template --":
                if st.button("Insert", key="insert_template_btn"):
                    template = next((t for t in templates if t["name"] == selected_template), None)
                    if template:
                        # Append template to existing message instead of replacing
                        current = st.session_state.get("broadcast_message", "")
                        if current:
                            st.session_state.broadcast_message = (
                                current + "\n\n" + template["content"]
                            )
                        else:
                            st.session_state.broadcast_message = template["content"]
                        # Load template images into session state
                        tmpl_images = _parse_template_images(template)
                        if tmpl_images:
                            st.session_state.broadcast_template_images = tmpl_images

    # Initialize message input from session state if not already set
    if "broadcast_message" in st.session_state and st.session_state.broadcast_message:
        default_message = st.session_state.broadcast_message
    else:
        default_message = ""

    # Message composer with placeholder support
    message = st.text_area(
        "Message",
        value=default_message,
        height=200,
        max_chars=4096,
        help="Use placeholders: <org_name>, <org_hashtag>, <org_grids>. Supports Telegram Markdown.",
    )

    # Update session state
    st.session_state.broadcast_message = message

    # Character count
    char_count = len(message)
    if char_count > 4000:
        st.warning(f"{char_count}/4096 characters - approaching limit!")
    else:
        st.caption(f"{char_count}/4096 characters")

    # Show available placeholders
    with st.expander("Available Placeholders"):
        st.markdown(
            """
- `<org_name>` - Organization's name (e.g., "Acme")
- `<org_hashtag>` - Hashtag version (e.g., "#acme")
- `<org_grids>` - Comma-separated list of grids (e.g., "GridA, GridB, GridC")

Placeholders are replaced with actual values for each recipient.
"""
        )

    # Image attachments
    uploaded_images = st.file_uploader(
        "Attach Images (optional)",
        type=["png", "jpg", "jpeg", "gif", "webp"],
        accept_multiple_files=True,
        help="Up to 10 images, max 10 MB each. Sent as a photo album before the text.",
        key="broadcast_images",
    )

    if uploaded_images:
        # Validate count
        if len(uploaded_images) > 10:
            st.error("Maximum 10 images allowed (Telegram limit).")
            uploaded_images = uploaded_images[:10]
        # Validate per-file size (Telegram limit: 10 MB)
        oversized_imgs = [f for f in uploaded_images if f.size > 10 * 1024 * 1024]
        if oversized_imgs:
            names = ", ".join(f.name for f in oversized_imgs)
            st.error(f"These images exceed Telegram's 10 MB limit: {names}")
            uploaded_images = [f for f in uploaded_images if f.size <= 10 * 1024 * 1024]
        # Show summary + thumbnails
        if uploaded_images:
            total_mb = sum(f.size for f in uploaded_images) / (1024 * 1024)
            st.caption(f"{len(uploaded_images)} image(s), {total_mb:.1f} MB total")
            cols = st.columns(min(len(uploaded_images), 5))
            for i, img in enumerate(uploaded_images[:5]):
                with cols[i]:
                    st.image(img, width=100)
            if len(uploaded_images) > 5:
                st.caption(f"...and {len(uploaded_images) - 5} more")

    # Show template images loaded via Insert
    template_images = st.session_state.get("broadcast_template_images", [])
    if template_images:
        st.caption(f"From template: {len(template_images)} image(s)")
        tmpl_cols = st.columns(min(len(template_images), 5))
        for i, img in enumerate(template_images[:5]):
            with tmpl_cols[i]:
                st.image(img.data, width=80)
        if len(template_images) > 5:
            st.caption(f"...and {len(template_images) - 5} more")

    # Preview enriched message (shows first recipient as example)
    if selected_group_ids and "<" in message:
        with st.expander("Preview (first recipient)", expanded=True):
            first_group_id = selected_group_ids[0]
            preview = broadcast_service.enrich_message(message, first_group_id)
            st.text(preview)
            st.caption(f"Preview for: {group_options.get(first_group_id, first_group_id)}")

    # Validate message length after enrichment
    if selected_group_ids and message:
        oversized = broadcast_service.validate_message_length(message, selected_group_ids)
        if oversized:
            st.error(
                f"Message too long after enrichment for {len(oversized)} group(s). "
                f"Maximum is 4096 characters."
            )

    st.divider()

    # Schedule toggle
    send_option = st.radio(
        "Delivery",
        ["Send immediately", "Schedule for later"],
        horizontal=True,
        key="send_option",
    )

    scheduled_time: Optional[datetime] = None
    recurrence: Optional[dict] = None
    if send_option == "Schedule for later":
        col_date, col_time, col_tz = st.columns([2, 2, 2])
        with col_date:
            scheduled_date = st.date_input(
                "Date",
                value=datetime.now().date() + timedelta(days=1),
                min_value=datetime.now().date(),
                key="schedule_date",
            )
        with col_time:
            scheduled_time_val = st.time_input(
                "Time",
                value=datetime.now().replace(hour=9, minute=0),
                key="schedule_time",
            )
        with col_tz:
            selected_tz = st.selectbox(
                "Timezone",
                options=list(TIMEZONE_OPTIONS.keys()),
                index=1,  # Default to CET
                key="schedule_timezone",
            )
        # Combine date and time, then convert to UTC
        local_datetime = datetime.combine(scheduled_date, scheduled_time_val)
        tz_offset_hours = TIMEZONE_OPTIONS[selected_tz]
        # Convert from selected timezone to UTC by subtracting the offset
        scheduled_time = local_datetime - timedelta(hours=tz_offset_hours)
        # Make it timezone-aware (UTC)
        scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
        st.caption(f"Will be sent at {scheduled_time.strftime('%Y-%m-%d %H:%M')} UTC")

        # Recurrence (optional) — pattern is derived from the first-send date above
        repeat = st.selectbox(
            "Repeat",
            REPEAT_OPTIONS,
            key="schedule_repeat",
            help=(
                "Recurring broadcasts re-send automatically. The message and images "
                "are read live from this broadcast at each send, so editing it later "
                "changes future sends. The pattern is based on the date/time above."
            ),
        )
        if repeat != "Does not repeat":
            recurrence = _build_recurrence(scheduled_time, repeat)
            if recurrence:
                st.caption(
                    f"🔁 Repeats {repeat.lower()} — first send "
                    f"{scheduled_time.strftime('%Y-%m-%d %H:%M')} UTC"
                )
            else:
                st.error("Could not build a valid repeat schedule from that date/time.")

    st.divider()

    # Show previous verification failure if any (user needs to edit and retry)
    verification_result = st.session_state.get("broadcast_verification_result")
    if verification_result and not verification_result.passed:
        st.error(f"⚠️ Verification failed: {verification_result.feedback}")
        if verification_result.categories:
            st.caption(f"Issues: {', '.join(verification_result.categories)}")
        st.info("Please edit your message and try again.")

    # Send button - verification happens automatically on click
    can_send = (
        message and selected_group_ids and not st.session_state.get("broadcast_sending", False)
    )

    send_label = "Sending..." if st.session_state.get("broadcast_sending") else "Send"
    if scheduled_time:
        send_label = "Schedule recurring" if recurrence else "Schedule"

    # Show verification status
    verification_configured = verification_service.is_enabled()
    if verification_configured:
        st.caption("🔍 Message will be verified before sending (if enabled in settings)")
    else:
        st.caption("⚠️ Verification service not configured")

    if st.button(
        send_label,
        type="primary",
        use_container_width=True,
        disabled=not can_send,
    ):
        _handle_send_with_verification(
            broadcast_service,
            verification_service,
            message,
            selected_group_ids,
            group_options,
            scheduled_time,
            uploaded_images=uploaded_images if uploaded_images else None,
            recurrence=recurrence,
        )


def _handle_send_with_verification(
    broadcast_service: BroadcastService,
    verification_service: BroadcastVerificationService,
    message: str,
    group_ids: List[str],
    group_options: Dict[str, str],
    scheduled_time: Optional[datetime],
    uploaded_images=None,
    recurrence: Optional[dict] = None,
):
    """
    Handle the send/schedule action with MANDATORY verification first.

    All outgoing messages MUST be verified before sending. This is a safety guardrail
    to prevent inappropriate content from reaching customers.
    """
    st.session_state.broadcast_sending = True

    # Step 1: Verify the message (if service is configured)
    # The actual VERIFICATION_ENABLED check happens server-side in chat-orchestrator.
    # If disabled, the endpoint returns passed=True with "Verification disabled" feedback.
    verification_configured = verification_service.is_enabled()

    if verification_configured:
        with st.spinner("Verifying message..."):
            group_names = [group_options.get(gid, gid) for gid in group_ids]
            verification_result = verification_service.verify_broadcast(
                message=message,
                target_groups=group_names[:5],
            )

        # Store verification result
        st.session_state.broadcast_verification_result = verification_result

        # If verification failed (not just disabled), stop here and let user edit
        if not verification_result.passed:
            st.session_state.broadcast_sending = False
            st.error(f"⚠️ Verification failed: {verification_result.feedback}")
            if verification_result.categories:
                st.caption(f"Issues: {', '.join(verification_result.categories)}")
            st.info("Please edit your message and try again.")
            return  # Don't proceed with sending
    else:
        # Verification service not configured - skip verification
        verification_result = None

    # Step 2: Verification passed (or disabled) - proceed with send
    user_email = st.session_state.get("user_info", {}).get("email", "unknown")

    # Merge template images (first) + uploaded images into a single list
    from services.broadcast_service import ImageData

    image_data_list = list(st.session_state.get("broadcast_template_images", []))
    if uploaded_images:
        image_data_list.extend(
            ImageData(filename=f.name, content_type=f.type or "image/jpeg", data=f.getvalue())
            for f in uploaded_images
        )
    if not image_data_list:
        image_data_list = None

    with st.spinner("Sending broadcast..." if not scheduled_time else "Scheduling..."):
        result = broadcast_service.send_broadcast(
            message=message,
            group_ids=group_ids,
            created_by=user_email,
            scheduled_for=scheduled_time,
            verification_passed=verification_result.passed if verification_result else None,
            verification_feedback=verification_result.feedback if verification_result else None,
            images=image_data_list,
            recurrence=recurrence,
        )

    st.session_state.broadcast_sending = False
    st.session_state.broadcast_send_complete = True
    st.session_state.broadcast_send_result = result
    st.session_state.broadcast_scheduled_time = scheduled_time  # Store for display
    # Clear verification result on successful send
    st.session_state.broadcast_verification_result = None

    # Show result immediately
    if scheduled_time:
        st.success(f"✅ Broadcast scheduled for {scheduled_time.strftime('%Y-%m-%d %H:%M')}")
    elif result.failed == 0:
        st.success(f"✅ Broadcast sent successfully to {result.successful} groups!")
    else:
        st.warning(
            f"Broadcast completed: {result.successful}/{result.total} delivered, "
            f"{result.failed} failed"
        )
        if result.errors:
            with st.expander("View Errors"):
                for error in result.errors:
                    st.text(error)


def _parse_template_images(template: dict):
    """Parse image_attachments from a template record into ImageData list."""
    from services.broadcast_service import ImageData

    raw = template.get("image_attachments")
    if not raw:
        return []
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(raw, list) or not raw:
        return []
    return [ImageData.from_dict(d) for d in raw]


def _render_templates_tab(broadcast_service: BroadcastService):
    """Render the templates management tab."""
    st.subheader("Message Templates")

    templates = broadcast_service.get_templates()

    # Add new template section
    with st.expander("Add New Template", expanded=not templates):
        new_name = st.text_input("Template Name", key="new_template_name")
        new_content = st.text_area(
            "Template Content",
            key="new_template_content",
            height=150,
            help="You can use placeholders like <org_name>, <org_hashtag>, <org_grids>",
        )

        new_template_images = st.file_uploader(
            "Attach Images (optional)",
            type=["png", "jpg", "jpeg", "gif", "webp"],
            accept_multiple_files=True,
            help="Up to 10 images, max 10 MB each. Stored with the template.",
            key="new_template_images",
        )
        if new_template_images:
            if len(new_template_images) > 10:
                st.error("Maximum 10 images allowed.")
                new_template_images = new_template_images[:10]
            oversized = [f for f in new_template_images if f.size > 10 * 1024 * 1024]
            if oversized:
                names = ", ".join(f.name for f in oversized)
                st.error(f"These images exceed 10 MB: {names}")
                new_template_images = [f for f in new_template_images if f.size <= 10 * 1024 * 1024]
            if new_template_images:
                cols = st.columns(min(len(new_template_images), 5))
                for i, img in enumerate(new_template_images[:5]):
                    with cols[i]:
                        st.image(img, width=80)

        col1, col2 = st.columns([1, 3])
        with col1:
            save_disabled = not new_name.strip() or not new_content.strip()
            if st.button("Save Template", disabled=save_disabled, key="save_new_template"):
                from services.broadcast_service import ImageData

                user_email = st.session_state.get("user_info", {}).get("email", "")
                template_imgs = None
                if new_template_images:
                    template_imgs = [
                        ImageData(
                            filename=f.name,
                            content_type=f.type or "image/jpeg",
                            data=f.getvalue(),
                        )
                        for f in new_template_images
                    ]
                success, msg = broadcast_service.save_template(
                    name=new_name.strip(),
                    content=new_content.strip(),
                    created_by=user_email,
                    images=template_imgs,
                )
                if success:
                    st.success(msg)
                    st.session_state.templates_refresh = True
                    st.rerun()
                else:
                    st.error(msg)

    if not templates:
        st.info("No templates yet. Create one above to get started.")
        return

    # List existing templates
    st.markdown("---")
    st.caption(f"{len(templates)} template(s)")

    for template in templates:
        with st.expander(f"{template['name']}"):
            edited_content = st.text_area(
                "Content",
                value=template["content"],
                key=f"template_content_{template['id']}",
                height=100,
            )

            # Parse existing template images
            existing_images = _parse_template_images(template)
            clear_images = False
            edit_template_imgs = None

            if existing_images:
                st.caption(f"{len(existing_images)} image(s) attached")
                img_cols = st.columns(min(len(existing_images), 5))
                for i, img in enumerate(existing_images[:5]):
                    with img_cols[i]:
                        st.image(img.data, width=80)
                if len(existing_images) > 5:
                    st.caption(f"...and {len(existing_images) - 5} more")
                clear_images = st.checkbox(
                    "Clear all images",
                    key=f"clear_imgs_{template['id']}",
                )

            edit_template_imgs = st.file_uploader(
                "Replace Images" if existing_images else "Attach Images",
                type=["png", "jpg", "jpeg", "gif", "webp"],
                accept_multiple_files=True,
                help="Up to 10 images, max 10 MB each.",
                key=f"edit_template_images_{template['id']}",
            )
            if edit_template_imgs:
                if len(edit_template_imgs) > 10:
                    st.error("Maximum 10 images allowed.")
                    edit_template_imgs = edit_template_imgs[:10]
                oversized = [f for f in edit_template_imgs if f.size > 10 * 1024 * 1024]
                if oversized:
                    names = ", ".join(f.name for f in oversized)
                    st.error(f"These images exceed 10 MB: {names}")
                    edit_template_imgs = [
                        f for f in edit_template_imgs if f.size <= 10 * 1024 * 1024
                    ]

            col1, col2, col3 = st.columns([1, 1, 2])
            with col1:
                if st.button("Update", key=f"update_{template['id']}"):
                    from services.broadcast_service import ImageData

                    # Determine images to save
                    images_param = None  # None = don't touch
                    if edit_template_imgs:
                        images_param = [
                            ImageData(
                                filename=f.name,
                                content_type=f.type or "image/jpeg",
                                data=f.getvalue(),
                            )
                            for f in edit_template_imgs
                        ]
                    elif clear_images:
                        images_param = []  # Empty list = clear all

                    success, msg = broadcast_service.update_template(
                        template_id=template["id"],
                        content=edited_content,
                        images=images_param,
                    )
                    if success:
                        st.success("Updated!")
                        st.session_state.templates_refresh = True
                    else:
                        st.error(msg)
            with col2:
                if st.button("Delete", key=f"delete_{template['id']}", type="secondary"):
                    success, msg = broadcast_service.delete_template(template["id"])
                    if success:
                        st.success("Deleted!")
                        st.session_state.templates_refresh = True
                    else:
                        st.error(msg)


def _render_scheduled_tab(broadcast_service: BroadcastService):
    """Render the scheduled broadcasts tab."""
    st.subheader("Scheduled Broadcasts")

    scheduled = broadcast_service.get_scheduled_broadcasts()

    if not scheduled:
        st.info("No scheduled broadcasts.")
        return

    for broadcast in scheduled:
        scheduled_for_raw = broadcast.get("scheduled_for", "Unknown")
        message_preview = broadcast.get("message", "")[:100]
        recipient_count = broadcast.get("total_recipients", 0)
        schedule_type = broadcast.get("schedule_type")
        is_recurring = schedule_type in ("recurring", "biweekly")
        recurrence_label = _describe_recurrence(schedule_type, broadcast.get("cron_expression"))

        # Format scheduled time nicely (show both UTC and CET)
        try:
            utc_dt = datetime.fromisoformat(scheduled_for_raw.replace("Z", "+00:00"))
            cet_dt = utc_dt + timedelta(hours=1)  # CET = UTC+1
            scheduled_display = (
                f"{cet_dt.strftime('%Y-%m-%d %H:%M')} CET ({utc_dt.strftime('%H:%M')} UTC)"
            )
        except (ValueError, AttributeError):
            scheduled_display = scheduled_for_raw

        title_prefix = "🔁 Recurring" if is_recurring else "Scheduled"
        with st.expander(f"{title_prefix} — next {scheduled_display}"):
            if is_recurring:
                st.markdown(f"**Repeats:** {recurrence_label}")
                st.caption("Editing this broadcast's message or images changes all future sends.")
            st.markdown(f"**Recipients:** {recipient_count} groups")
            st.markdown("**Message:**")
            st.text(message_preview + ("..." if len(broadcast.get("message", "")) > 100 else ""))

            col1, col2 = st.columns([1, 3])
            with col1:
                cancel_label = "Cancel series" if is_recurring else "Cancel"
                if st.button(cancel_label, key=f"cancel_{broadcast['id']}", type="secondary"):
                    success, msg = broadcast_service.cancel_scheduled_broadcast(broadcast["id"])
                    if success:
                        st.success(
                            "Recurring broadcast cancelled"
                            if is_recurring
                            else "Broadcast cancelled"
                        )
                        st.session_state.scheduled_refresh = True
                    else:
                        st.error(msg)


def _render_history_tab(broadcast_service: BroadcastService):
    """Render the broadcast history tab."""
    st.subheader("Broadcast History")

    history = broadcast_service.get_broadcast_history(limit=20)

    if not history:
        st.info("No broadcast history yet.")
        return

    for broadcast in history:
        status = broadcast.get("status", "unknown")
        created_at = broadcast.get("created_at", "Unknown")
        successful = broadcast.get("successful_sends", 0)
        failed = broadcast.get("failed_sends", 0)
        total = broadcast.get("total_recipients", 0)
        message_preview = broadcast.get("message", "")[:50]

        # Status badge
        status_color = {
            "completed": "green",
            "pending": "orange",
            "sending": "blue",
            "failed": "red",
            "cancelled": "gray",
        }.get(status, "gray")

        # Format timestamp
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            created_str = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            created_str = created_at

        with st.expander(f":{status_color}[{status.upper()}] {created_str} - {message_preview}..."):
            st.markdown(f"**Status:** {status}")
            st.markdown(f"**Delivered:** {successful}/{total}")
            if failed > 0:
                st.markdown(f"**Failed:** {failed}")
            st.markdown(f"**Created by:** {broadcast.get('created_by', 'Unknown')}")

            st.markdown("**Message:**")
            st.text(broadcast.get("message", "")[:500])

            metadata = broadcast.get("metadata") or {}
            image_count = metadata.get("image_count", 0)
            if image_count:
                st.caption(f"📷 {image_count} image(s) attached")

            # Show delivery logs
            if st.button("View Delivery Logs", key=f"logs_{broadcast['id']}"):
                logs = broadcast_service.get_broadcast_logs(broadcast["id"])
                if logs:
                    for log in logs:
                        status_icon = "check" if log.get("success") else "x"
                        chat_name = log.get("chat_name", log.get("chat_id", "Unknown"))
                        st.markdown(f":{status_icon}: {chat_name}")
                        if not log.get("success") and log.get("error_message"):
                            st.caption(f"  Error: {log['error_message']}")
                else:
                    st.info("No delivery logs found")

            # Retry failed sends
            if failed > 0 and status == "completed":
                if st.button("Retry Failed Sends", key=f"retry_{broadcast['id']}"):
                    user_email = st.session_state.get("user_info", {}).get("email", "")
                    result = broadcast_service.retry_failed_sends(broadcast["id"], user_email)
                    if result.successful > 0:
                        st.success(f"Retry successful: {result.successful} delivered")
                        st.session_state.history_refresh = True
                    elif result.errors:
                        st.error(f"Retry failed: {result.errors[0]}")


__all__ = ["show_broadcast_modal"]
