"""Persistent Agents management page for Anansi App.

Displays all persistent agent instances with controls for
pausing, resuming, restarting, and terminating individual agents
or bulk-managing by expert type.
"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import streamlit as st

_DEFAULT_TZ = ZoneInfo(os.getenv("DEFAULT_TIMEZONE", "UTC"))


def _fmt_local(iso: str | None) -> str:
    """Convert an ISO datetime string (UTC-aware) to local timezone display string."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(_DEFAULT_TZ).strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, TypeError):
        return str(iso)[:16]


def _fetch_system_jobs() -> list[dict]:
    """Call GET /api/v1/jobs on the orchestrator and return the job list."""
    base_url = os.getenv("CHAT_ORCHESTRATOR_URL", "http://localhost:8000/chat").rstrip("/")
    api_key = os.getenv("API_KEY", "")
    try:
        resp = requests.get(
            f"{base_url}/api/v1/jobs",
            headers={"X-Api-Key": api_key},
            timeout=5,
        )
        resp.raise_for_status()
        data: dict = resp.json()
        return list(data.get("jobs", []))
    except Exception as e:
        return [{"_error": str(e)}]


def render_scheduled_jobs_section():
    """Render the Scheduled Jobs section (system jobs + user schedules)."""
    with st.expander("🗓️ Scheduled Jobs (non-agent)", expanded=False):
        col_refresh, _ = st.columns([1, 5])
        with col_refresh:
            if st.button("↻ Refresh", key="refresh_scheduled_jobs"):
                st.cache_data.clear()

        # ── System jobs ──────────────────────────────────────────────────────
        st.markdown("#### System Jobs")
        jobs = _fetch_system_jobs()

        if jobs and "_error" in jobs[0]:
            st.warning(f"Could not fetch system jobs: {jobs[0]['_error']}")
        elif not jobs:
            st.caption("No system jobs registered (all feature flags may be off).")
        else:
            rows = []
            for j in jobs:
                rows.append(
                    {
                        "Name": j.get("name", j.get("id", "?")),
                        "Trigger": j.get("trigger", "—"),
                        "Next Run (WAT)": _fmt_local(j.get("next_run_time")),
                    }
                )
            st.dataframe(rows, use_container_width=True, hide_index=True)

        st.divider()

        # ── User schedules ────────────────────────────────────────────────────
        st.markdown("#### User Schedules")
        from services.supabase_reader import SupabaseReader

        reader = SupabaseReader()
        if not reader.is_configured():
            st.warning("Database not configured — cannot load user schedules.")
        else:
            schedules = reader.get_all_user_schedules()
            if not schedules:
                st.caption("No user schedules found.")
            else:
                rows = []
                for s in schedules:
                    rows.append(
                        {
                            "Name": s.get("friendly_name") or s.get("command", "")[:40],
                            "Command": s.get("command", "")[:60],
                            "Type": s.get("schedule_type", "—"),
                            "Next Run (WAT)": _fmt_local(s.get("next_run_at")),
                            "Status": s.get("status", "—"),
                            "Created By": s.get("created_by_email") or "—",
                            "Chat ID": str(s.get("chat_id", "—")),
                        }
                    )
                st.dataframe(rows, use_container_width=True, hide_index=True)


def render_agents_page():
    """Render the persistent agents management page."""
    from services.agent_management_service import AgentManagementService

    st.title("Persistent Agents")

    render_scheduled_jobs_section()

    svc = AgentManagementService()
    if not svc.is_configured():
        st.error("Database not configured. Check CHAT_DB_URL and CHAT_DB_SERVICE_KEY.")
        return

    # Global kill switch indicator — fetch from DO since the var lives on anansi-bot
    from services.settings_service import SettingsService

    settings_svc = SettingsService()
    remote_settings = settings_svc.get_current_settings(fetch_from_do=True)
    enabled = remote_settings.get("PERSISTENT_AGENTS_ENABLED", False)
    if not enabled:
        st.warning(
            "Persistent agents are **disabled** globally. "
            "Enable them in **Settings > Bot Behavior > Persistent Agents**."
        )

    # Load all instances (single query)
    instances = svc.list_instances()

    # Load all expert configs once for the page
    expert_configs = svc.get_persistent_expert_configs()
    expert_config_map = {c["expert_id"]: c for c in expert_configs}

    # Coverage count for grid_monitor agents (derived from full list, no extra query)
    # Only show coverage warning for auto-provisioned (non-user-startable) agents
    auto_provisioned_experts = [
        c["expert_id"] for c in expert_configs if not c.get("is_user_startable")
    ]
    if "grid_monitor" in auto_provisioned_experts:
        eligible_count = svc.get_eligible_grid_count()
        if eligible_count > 0:
            active_gm = sum(
                1 for i in instances if i["expert_id"] == "grid_monitor" and i["status"] == "active"
            )
            if active_gm < eligible_count:
                st.warning(
                    f"{active_gm} of {eligible_count} eligible grids have an active grid_monitor agent"
                )
            else:
                st.success(
                    f"{active_gm} of {eligible_count} eligible grids have an active grid_monitor agent"
                )

    if not instances:
        st.info("No persistent agent instances found. Create one below.")
        _render_create_button(svc)
        return

    # Group by expert_id
    expert_groups = {}
    for inst in instances:
        eid = inst["expert_id"]
        if eid not in expert_groups:
            expert_groups[eid] = []
        expert_groups[eid].append(inst)

    # Render each expert group
    for expert_id, group_instances in expert_groups.items():
        config = expert_config_map.get(expert_id, {})
        _render_expert_group(svc, expert_id, group_instances, config)

    st.divider()
    _render_create_button(svc)


def _render_expert_group(svc, expert_id: str, instances: list, config: dict):
    """Render a collapsible group of instances for one expert type."""
    is_user_startable = config.get("is_user_startable", False)
    is_known = bool(config)

    # Hide terminated instances unless auto-provisioned (those represent grids
    # and may be restarted) or recently terminated (5-min grace window).
    now = datetime.now(timezone.utc)
    filtered_instances = []
    for i in instances:
        status = i["status"]
        if status != "terminated":
            filtered_instances.append(i)
            continue

        # For terminated instances:
        if is_user_startable or not is_known:
            # For user-startable or unknown experts: hide auto-provisioned placeholders.
            # Only show manually terminated ones if they are very recent.
            if (i.get("created_by") or "").startswith("auto:"):
                continue
            updated_at = i.get("updated_at")
            if updated_at:
                delta = now - datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if delta.total_seconds() < 300:
                    filtered_instances.append(i)
        else:
            # For known auto-provisioned experts: keep placeholders (created_by=auto:*)
            if (i.get("created_by") or "").startswith("auto:"):
                filtered_instances.append(i)
            else:
                # Keep manually created ones if very recent
                updated_at = i.get("updated_at")
                if updated_at:
                    delta = now - datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    if delta.total_seconds() < 300:
                        filtered_instances.append(i)

    if not filtered_instances:
        if is_user_startable or not is_known:
            # User-startable or unknown experts with no active instances are hidden completely
            return
        # Known auto-provisioned agents with no instances (unlikely) should still return.
        return

    active_count = sum(1 for i in filtered_instances if i["status"] == "active")
    paused_count = sum(1 for i in filtered_instances if i["status"] == "paused")
    error_count = sum(1 for i in filtered_instances if i["status"] == "error")

    label = (
        f"{expert_id.replace('_', ' ').title()} ({len(filtered_instances)}) — "
        f"{active_count} active · {paused_count} paused · {error_count} error"
    )

    with st.expander(label, expanded=(error_count > 0 or active_count > 0)):
        # Bulk controls
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("Pause All", key=f"pause_all_{expert_id}"):
                count = svc.update_status_by_expert(expert_id, "paused")
                st.toast(f"Paused {count} instance(s)")
                st.rerun()
        with col2:
            if st.button("Resume All", key=f"resume_all_{expert_id}"):
                count = svc.update_status_by_expert(expert_id, "active")
                st.toast(f"Resumed {count} instance(s)")
                st.rerun()

        # Instance rows
        for inst in filtered_instances:
            _render_instance_row(svc, inst)


def _build_view_state_url(instance_id: str) -> str | None:
    """Build the View State mini app URL for an agent instance."""
    try:
        from orchestrator.mini_app.schemas import build_agent_state_url

        result: str | None = build_agent_state_url(instance_id)
        return result
    except ImportError:
        # anansi_app runs as a separate service without chat_orchestrator on PYTHONPATH.
        # Fall back to building the URL directly.
        import hashlib
        import hmac

        base_url = os.getenv("MINI_APP_BASE_URL", "http://localhost:8000/mini-app").rstrip("/")
        if base_url.startswith("http://"):
            base_url = "https://" + base_url[7:]
        secret = os.getenv("TELEGRAM_BOT_TOKEN", "fallback").encode()
        sig = hmac.new(secret, instance_id.encode(), hashlib.sha256).hexdigest()[:16]
        return f"{base_url}/?instance_id={instance_id}&view=agent_state&sig={sig}"


def _render_instance_row(svc, inst: dict):
    """Render one agent instance as a row with controls."""
    status = inst["status"]
    status_colors = {
        "active": ":green[active]",
        "executing": ":blue[executing]",
        "paused": ":orange[paused]",
        "error": ":red[error]",
        "terminated": ":gray[terminated]",
        "initializing": ":gray[initializing]",
    }
    status_display = status_colors.get(status, status)

    # Format last wake time
    last_woke = inst.get("last_woke_at")
    if last_woke:
        try:
            dt = datetime.fromisoformat(last_woke.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = now - dt
            if delta.total_seconds() < 3600:
                wake_str = f"{int(delta.total_seconds() / 60)}m ago"
            elif delta.total_seconds() < 86400:
                wake_str = f"{int(delta.total_seconds() / 3600)}h ago"
            else:
                wake_str = f"{delta.days}d ago"
        except (ValueError, TypeError):
            wake_str = str(last_woke)[:16]
    else:
        wake_str = "never"

    inst_id = str(inst["id"])

    # Row layout: name + details on left, buttons on right
    col_info, col_buttons = st.columns([5, 3])
    with col_info:
        view_url = _build_view_state_url(inst_id)
        if view_url:
            st.markdown(f"**{inst['instance_name']}** · [View State]({view_url})")
        else:
            st.markdown(f"**{inst['instance_name']}**")
        creator = inst.get("created_by") or ""
        creator_suffix = (
            f" · by {creator}" if creator and inst.get("expert_id") == "user_agent" else ""
        )
        st.caption(
            f"{status_display} · Wake: {wake_str} · #{inst.get('wake_count', 0)}{creator_suffix}"
        )
    with col_buttons:
        bcol1, bcol2, bcol3, bcol4 = st.columns(4)
        with bcol1:
            if status in ("active", "executing"):
                if st.button("Pause", key=f"pause_{inst_id}", help="Pause this instance"):
                    svc.update_status(inst_id, "paused")
                    st.rerun()
            elif status in ("paused", "error"):
                if st.button("Resume", key=f"resume_{inst_id}", help="Resume this instance"):
                    svc.update_status(inst_id, "active")
                    st.rerun()
        with bcol2:
            if status in ("active", "error"):
                if st.button("Wake", key=f"wake_{inst_id}", help="Trigger immediate wake"):
                    svc.queue_manual_wake(inst_id)
                    if status == "error":
                        svc.update_status(inst_id, "active")
                    st.toast(f"Queued wake for {inst['instance_name']}")
                    st.rerun()
        with bcol3:
            if st.button("Restart", key=f"restart_{inst_id}", help="Restart (reset metadata)"):
                svc.restart_instance(inst_id)
                st.rerun()
        with bcol4:
            if status != "terminated":
                if st.button("Stop", key=f"term_{inst_id}", help="Terminate"):
                    svc.terminate_instance(inst_id)
                    st.rerun()

    # Expandable detail
    with st.expander(f"Details: {inst['instance_name']}", expanded=False):
        detail_col1, detail_col2 = st.columns(2)
        with detail_col1:
            st.markdown("**Instance Info**")
            st.text(f"ID: {inst['id']}")
            st.text(f"Thread: {inst['thread_id']}")
            st.text(f"Entity: {inst['anchor_entity_type']} / {inst['anchor_entity_id']}")
            st.text(f"Schedule: {inst.get('wake_schedule', 'none')}")
            if inst.get("created_by"):
                st.text(f"Created by: {inst['created_by']}")
            if inst.get("created_by_user_id"):
                st.text(f"Telegram user: {inst['created_by_user_id']}")
            if inst.get("error_message"):
                st.error(f"Error: {inst['error_message']}")

            # User agent prompts
            if inst.get("check_prompt"):
                st.markdown("**Check Prompt**")
                st.caption(inst["check_prompt"])
            if inst.get("response_prompt"):
                st.markdown("**Response Prompt**")
                st.caption(inst["response_prompt"])
            if inst.get("auto_complete") is not None:
                st.text(f"Auto-complete: {'Yes' if inst['auto_complete'] else 'No'}")
        with detail_col2:
            st.markdown("**Metadata**")
            metadata = inst.get("metadata", {})
            if metadata:
                st.json(metadata)
            else:
                st.text("(empty)")

        # Recent events
        st.markdown("**Recent Events (last 10)**")
        try:
            events = svc.get_recent_events(inst_id, limit=10)
        except Exception as ev_err:
            st.warning(f"Could not load events: {ev_err}")
            events = []
        if events:
            for ev in events:
                ev_type = ev.get("event_type", "?")
                ev_status = ev.get("status", "?")
                ev_time = str(ev.get("created_at", ""))[:19]
                ev_text = (ev.get("event_data", {}) or {}).get("text", "")[:100]
                st.text(f"  {ev_time} [{ev_type}] {ev_status}")
                if ev_status == "failed" and ev.get("error"):
                    st.caption(f"    {ev['error'][:200]}")
                elif ev_text:
                    st.caption(f"    {ev_text}")
        else:
            st.text("  (no events)")


def _render_create_button(svc):
    """Render the create instance button/dialog."""
    if st.button("+ Create Agent Instance", type="primary"):
        st.session_state["show_create_agent_dialog"] = True

    if st.session_state.get("show_create_agent_dialog"):
        _render_create_dialog(svc)


@st.dialog("Create Agent Instance")
def _render_create_dialog(svc):
    """Dialog for creating a new persistent agent instance."""
    # Step 1: Select expert from persistent experts in Google Doc
    expert_configs = svc.get_persistent_expert_configs()
    expert_ids = [c["expert_id"] for c in expert_configs]

    if not expert_ids:
        st.warning("No persistent experts found in expert definitions.")
        expert_id = st.text_input("Expert ID", value="grid_monitor")
        selected = {"expert_id": expert_id, "anchor_entity_type": "grid"}
        anchor_entity_type = "grid"
        default_schedule = ""
    else:
        expert_id = st.selectbox("Expert", expert_ids)
        selected = next((c for c in expert_configs if c["expert_id"] == expert_id), {})
        anchor_entity_type = selected.get("anchor_entity_type", "")
        default_schedule = selected.get("wake_schedule") or ""
        st.caption(f"Entity type: **{anchor_entity_type}**")

    # Step 2: Select entity based on type
    anchor_entity_id = None
    anchor_metadata = {}
    organization_id = int(os.getenv("STAFF_ORG_ID", "2"))
    instance_name = ""

    if anchor_entity_type == "grid":
        grids = svc.get_eligible_grids()
        if grids:
            # Build display options: "Grid Name (ID)"
            grid_options = {f"{g['name']} (ID: {g['id']})": g for g in grids}
            selected_label = st.selectbox("Grid", list(grid_options.keys()))
            grid = grid_options[selected_label]

            anchor_entity_id = str(grid["id"])
            organization_id = grid.get("organization_id") or int(os.getenv("STAFF_ORG_ID", "2"))
            instance_name = f"{grid['name']} ({expert_id})"
            anchor_metadata = svc.build_anchor_metadata(anchor_entity_type, grid)

            # Special handling for site visits: allow selecting multiple sites
            if selected.get("is_user_startable") and expert_id == "site_visit_tracker":
                sites = svc.get_all_sites()
                if sites:
                    site_options = {f"{s['site_name']} (ID: {s['id']})": s for s in sites}
                    selected_site_labels = st.multiselect(
                        "Sites to Visit", list(site_options.keys())
                    )
                    if selected_site_labels:
                        selected_sites = [site_options[label] for label in selected_site_labels]
                        anchor_metadata["sites"] = selected_sites

                        # Generate unique ID for this visit so multiple visits can happen for one grid
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        anchor_entity_id = f"{grid['id']}:visit_{timestamp}"
                        instance_name = f"{grid['name']} Visit ({len(selected_sites)} sites)"
                    else:
                        st.info("Select one or more sites to visit.")
        else:
            st.warning("No eligible grids found in Auth DB.")
            anchor_entity_id = st.text_input("Entity ID", placeholder="Grid ID")
    else:
        anchor_entity_id = st.text_input("Entity ID", placeholder="Entity ID from Auth DB")
        organization_id = st.number_input(
            "Organization ID", min_value=1, value=int(os.getenv("STAFF_ORG_ID", "2")), step=1
        )

    # Step 3: Editable instance name and schedule
    instance_name = st.text_input("Instance Name", value=instance_name)
    wake_schedule = st.text_input("Wake Schedule (cron)", value=default_schedule)

    # Show metadata preview (read-only)
    if anchor_metadata:
        with st.expander("Anchor Metadata (auto-filled)", expanded=False):
            st.json(anchor_metadata)

    if st.button("Create", type="primary"):
        if not instance_name or not anchor_entity_id:
            st.error("Instance name and entity are required")
            return

        try:
            result = svc.create_instance(
                expert_id=expert_id,
                instance_name=instance_name,
                anchor_entity_type=anchor_entity_type,
                anchor_entity_id=anchor_entity_id,
                anchor_metadata=anchor_metadata,
                organization_id=int(organization_id),
                wake_schedule=wake_schedule or None,
            )
            st.success(f"Created: {result.get('instance_name', instance_name)}")
            st.session_state["show_create_agent_dialog"] = False
            st.rerun()
        except Exception as e:
            st.error(f"Failed to create: {e}")
