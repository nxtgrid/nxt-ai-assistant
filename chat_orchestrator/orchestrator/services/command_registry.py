"""
Unified Command Registry

Single source of truth for all Telegram slash commands.
Commands can be either:
- Tool commands: Transformed into LLM prompts that use MCP tools
- Expert commands: Routed to expert workflows for multi-step execution

This eliminates duplicate command definitions and ensures consistent
discovery, parsing, and access control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@dataclass
class CommandDefinition:
    """Definition of a slash command."""

    command: str
    """Command name without leading slash (e.g., 'tickets', 'lpp')"""

    description: str
    """Description shown in Telegram command menu"""

    command_type: Literal["tool", "expert", "direct"]
    """Type of command: 'tool' for MCP tool commands, 'expert' for workflow commands, 'direct' for handlers that bypass LLM"""

    # Tool command fields
    linked_tool: str = ""
    """Name of the linked MCP tool (for tool commands)"""

    prompt_template: str = ""
    """Natural language prompt template for tool commands. Supports {args} placeholder."""

    exclusive_tools: List[str] = field(default_factory=list)
    """Tools that this command exclusively unlocks (command-gated tools)"""

    # Expert command fields
    packet_type: str = ""
    """Packet type for expert commands (e.g., 'grid_analysis', 'light_preliminary_package')"""

    # Common fields
    requires_args: bool = False
    """Whether this command requires arguments"""

    args_hint: Optional[str] = None
    """Hint shown when args are required but missing"""

    staff_only: bool = True
    """Whether this command is restricted to staff users (default: True)"""

    model_override: str = ""
    """Env var name for model override (e.g., 'GEMINI_DEEP_THINKING_MODEL'). Resolved at runtime."""

    nl_triggers: List[str] = field(default_factory=list)
    """Natural language phrases that trigger this command (matched against user input).
    Each phrase is checked as a substring of the lowercased input."""


# =============================================================================
# PROMPT TEMPLATES
# =============================================================================

SCHEDULE_COMMAND_TEMPLATE = """
The user wants to manage scheduled messages.

**User's request:** {args}

**Instructions:**
- If the request is EMPTY or blank → Use schedule_list_user_schedules to show active schedules
- If the request contains a time and a message to schedule → Use schedule_schedule_user_command to CREATE a schedule
- If the request is "cancel <schedule_id>" → Use schedule_cancel_user_schedule with the given schedule ID
- If the request is "pause <schedule_id>" → Use schedule_pause_user_schedule with the given schedule ID
- If the request is "resume <schedule_id>" → Use schedule_resume_user_schedule with the given schedule ID

**For scheduling (schedule_schedule_user_command), parse the request:**
- time_expression: The time/recurrence part (e.g., "at 15:45", "daily at 9am", "tomorrow at 3pm")
- message: The message to send at the scheduled time. This can be a slash command (e.g., "/tickets") OR any regular text message (e.g., "show me the tickets assigned to anyone"). Pass it EXACTLY as the user wrote it.
- schedule_type: 'once' for one-time (at, tomorrow, in X hours), 'recurring' for repeating (daily, every, weekdays)

**Parsing rules:**
- The time expression is usually at the start: "daily at 9am", "at 3pm", "every monday at 10am"
- Everything AFTER the time expression is the message to schedule
- The message can be a /command OR plain text — both are valid

**Examples:**
- "at 15:45 /grids" → schedule_schedule_user_command(time_expression='at 15:45', message='/grids', schedule_type='once')
- "daily at 9am /tickets" → schedule_schedule_user_command(time_expression='daily at 9am', message='/tickets', schedule_type='recurring')
- 'daily at 9am "show me the tickets assigned to anyone"' → schedule_schedule_user_command(time_expression='daily at 9am', message='show me the tickets assigned to anyone', schedule_type='recurring')
- "every monday at 10am what is the status of all grids?" → schedule_schedule_user_command(time_expression='every monday at 10am', message='what is the status of all grids?', schedule_type='recurring')
- "cancel abc123" → schedule_cancel_user_schedule(schedule_id='abc123')
- "pause abc123" → schedule_pause_user_schedule(schedule_id='abc123')
- "resume abc123" → schedule_resume_user_schedule(schedule_id='abc123')

Default timezone: configured by DEFAULT_TIMEZONE env var (default UTC).
"""

AGENTS_COMMAND_TEMPLATE = (
    "The user wants to manage their monitoring agents. "
    "Their message: '{args}'\n\n"
    "If the message is EMPTY or blank: call schedule_list_user_agents to show their agents.\n"
    "If they want to cancel/stop/remove an agent: call schedule_list_user_agents first to show the list, "
    "then ask which one to cancel. When they confirm, call schedule_cancel_user_agent with the instance_id.\n"
    "If they want to create a new agent: call schedule_create_user_agent. "
    "IMPORTANT: The check_prompt MUST be a yes/no question (e.g., 'Have connections reached 500?'), "
    "NOT a command or creation instruction. The response_prompt is the full detail query.\n"
    "Format the response clearly for Telegram."
)

TICKETS_WITH_ARGS_TEMPLATE = (
    "Search for JIRA tickets related to '{args}'. "
    "Interpret '{args}' as follows and use the appropriate parameter:\n"
    "- If it looks like a PERSON'S NAME or email → use the 'assignee' parameter\n"
    "- If it looks like a ticket KEY (e.g., 'OPS-123', 'PD-456') → use the 'issue_key' parameter\n"
    "- If it's a GRID NAME (e.g., 'ExampleGrid', 'ExampleGrid2', 'ExampleGrid3') → use the 'grid' parameter\n"
    "- If it's a STATUS word ('to do', 'in progress', 'done', 'all') → use the 'statuses' parameter\n"
    "- Otherwise (org names, keywords) → use the 'text_search' parameter\n\n"
    "IMPORTANT: By default, show only OPEN tickets (statuses: To Do, In Progress). "
    "Only show all statuses if the argument explicitly mentions 'all', 'done', or 'won't do'. "
    "Use the jira_search_issues_with_comments tool."
)


# =============================================================================
# UNIFIED COMMAND REGISTRY
# =============================================================================

COMMAND_REGISTRY: Dict[str, CommandDefinition] = {
    # =========================================================================
    # TOOL COMMANDS - Transformed to LLM prompts
    # =========================================================================
    "help": CommandDefinition(
        command="help",
        command_type="tool",
        description="Get help and learn what the bot can do",
        prompt_template=(
            "The user wants to know what you can do. "
            "Summarize your capabilities based on your system instructions — "
            "what kind of questions you can answer, what tasks you can perform, etc. "
            "Keep it concise (5-10 lines max). "
            "Focus on what you can help with in natural language — do NOT list slash commands. "
            "Tell them they can just ask you anything in plain language."
        ),
        requires_args=False,
        staff_only=False,
    ),
    "commands": CommandDefinition(
        command="commands",
        command_type="tool",
        description="List all available slash commands",
        prompt_template=(
            "List the available slash commands that the user can run. "
            "ONLY list commands that this user has access to based on their permissions. "
            "Format each command as: /command — description. "
            "If no commands are available to this user, say so."
        ),
        requires_args=False,
        staff_only=False,
    ),
    "start": CommandDefinition(
        command="start",
        command_type="tool",
        description="Get started with the bot",
        linked_tool="",
        prompt_template=(
            "Introduce yourself and your capabilities in short to a new user. "
            "Include a couple of examples of what you can help with in natural language. "
            "Do NOT list or mention slash commands — the user can discover those later."
        ),
        requires_args=False,
        staff_only=False,
    ),
    "preferences": CommandDefinition(
        command="preferences",
        command_type="tool",
        description="See and manage your response preferences",
        prompt_template=(
            "The user wants to see their stored preferences. "
            "Call list_user_preferences to get all preferences, then present them as a "
            "numbered list. Tell the user they can say 'forget preference #N' or "
            "'change preference #N to <new value>' to modify them. "
            "If no preferences are stored, let them know they can say things like "
            "'make summaries shorter' or 'use bullet points' and you'll remember."
        ),
        requires_args=False,
        staff_only=False,
        exclusive_tools=["list_user_preferences", "delete_user_preference"],
    ),
    "tickets": CommandDefinition(
        command="tickets",
        command_type="tool",
        description="List my open JIRA tickets",
        linked_tool="jira_search_issues_with_comments",
        prompt_template=(
            "FIRST call jira_get_ticket_statistics to get 30-day ticket trends. "
            "Render a **Ticket Summary** at the top: total tickets in 30 days, "
            "grids with most tickets (highlight any from grids_above_average), "
            "and top 3 ticket types. Keep it concise (3-5 lines).\n\n"
            "THEN call jira_search_issues_with_comments with assignee='me' and statuses=['To Do', 'In Progress'] "
            "to list the current user's open tickets. "
            "Show key, summary, status, and assignee. "
            "Limit to 10 unless specified otherwise. "
            "Show the summary BEFORE the open ticket list, separated by an empty line. "
            "End with a hint: 'Use /ticket <key> for full details on any ticket.'"
        ),
        requires_args=False,
        exclusive_tools=[
            "jira_search_issues_with_comments",
            "jira_get_ticket_statistics",
            "jira_get_issue",
            "jira_add_comment",
            "jira_change_status",
            "customer_customer_get_grid_status",
            "customer_customer_get_all_grids_status",
        ],
    ),
    "ticket": CommandDefinition(
        command="ticket",
        command_type="tool",
        description="Get JIRA ticket details (e.g., /ticket OPS-123)",
        linked_tool="jira_get_issue",
        prompt_template=(
            "Get the full details for JIRA ticket {args}. "
            "Call the jira_get_issue tool with issue_key='{args}'. "
            "Show description, status, priority, assignee, and recent comments."
        ),
        requires_args=True,
        args_hint="Please provide a ticket key, e.g., /ticket OPS-123",
        exclusive_tools=["jira_get_issue"],
    ),
    "inverters_restart": CommandDefinition(
        command="inverters_restart",
        command_type="tool",
        description="Restart inverter at a grid (e.g., /inverters_restart ExampleGrid)",
        linked_tool="equipment_control_restart_inverter",
        prompt_template=(
            "The user has explicitly requested to restart the inverter at grid '{args}'. "
            "Call the equipment_control_restart_inverter tool with grid='{args}' IMMEDIATELY. "
            "CRITICAL: Do NOT ask for additional confirmation - the /command itself is the user's confirmation. "
            "The tool is only available on this turn, so you MUST call it now. Report the result."
        ),
        requires_args=True,
        args_hint="Please specify the grid name, e.g., /inverters_restart ExampleGrid",
        exclusive_tools=["equipment_control_restart_inverter"],
    ),
    "comms_reboot": CommandDefinition(
        command="comms_reboot",
        command_type="tool",
        description="Reboot communications chain at a grid (e.g., /comms_reboot ExampleGrid)",
        linked_tool="equipment_control_restart_comms_chain",
        prompt_template=(
            "The user has explicitly requested to reboot the communications chain at grid '{args}'. "
            "Call the equipment_control_restart_comms_chain tool with grid='{args}' IMMEDIATELY. "
            "CRITICAL: Do NOT ask for additional confirmation - the /command itself is the user's confirmation. "
            "The tool is only available on this turn, so you MUST call it now. "
            "When reporting the result, ALWAYS include the dcu_status_before_restart field exactly as returned "
            "(it contains DCU icons showing online/offline status). Also include follow-up info if scheduled."
        ),
        requires_args=True,
        args_hint="Please specify the grid name, e.g., /comms_reboot ExampleGrid",
        exclusive_tools=["equipment_control_restart_comms_chain"],
    ),
    "grid": CommandDefinition(
        command="grid",
        command_type="tool",
        description="Get grid status (e.g., /grid ExampleGrid2)",
        linked_tool="customer_customer_get_grid_status",
        prompt_template=(
            "IMPORTANT: This is a GRID STATUS request, NOT a JIRA ticket request. Do NOT call any jira tools. "
            "Get the current status of grid '{args}' if specified. "
            "If no grid is specified, use the most recently discussed grid from the conversation context. "
            "If no grid has been discussed recently, ask the user which grid. "
            "You MUST pass the grid_name parameter when calling customer_customer_get_grid_status. "
            "For example, if the user asked for '/grid ExampleGrid', call customer_customer_get_grid_status with grid_name='ExampleGrid'. "
            "NEVER call customer_customer_get_grid_status without the grid_name parameter. "
            "Call customer_customer_get_grid_status for all data (service status, DCU, business snapshot, real-time metrics). "
            "Optionally also call equipment_diagnostics_get_equipment_status for additional equipment details. "
            "Structure the response with these sections: "
            "Start with the grid name as a clickable link: [GridName](platform_url) "
            "**Service Status**: service (FS/HPS/Down/Unknown), weather (from live_weather.display e.g. '☀️ 32°C'), "
            "yesterday_on_hours (e.g. '18h on'), downtime_24h with minutes per cause (e.g. '2h down (90m battery, 30m grid_fault!)'). "
            "If downtime has grid_fault, include the affected phase(s) from fault_details. "
            "**Real-time Metrics** (from latest_state, sourced from VRM): "
            "Battery SOC % (battery_soc_pct), battery status (battery_status: charging/discharging/idle) "
            "with current in amps (battery_current_a), "
            "grid consumption in watts (consumption_w, real-time from VRM), "
            "solar power in watts (solar_power_w), "
            "inverter power per phase (inverter_l1/l2/l3_power_kw + total inverter_power_kw from service_status). "
            "IMPORTANT: Only show per-phase values if they are present in the data. "
            "NEVER calculate per-phase values by dividing the total - if per-phase data is null, omit L1/L2/L3. "
            "**Business Snapshot**: capacity (kwp/kwh), connections, meters, battery modules. "
            "**FS Detail** (from fs_detail): fs_schedule (current state + planned commands), "
            "last_fs_command delivery status. "
            "**FS Daily Summary** (from fs_detail.daily_summary): "
            "If present, show yesterday and today's FS data. Per day: FS ON hours, "
            "commands sent with delivery %, actual state transition times, "
            "and any discrepancies (commands without state changes or vice versa). "
            "Flag stale data if is_stale is true. "
            "If any DCU is offline, list the offline DCU names from dcus.offline_dcus[].name. "
            "Also call grafana_gridname_power_plant_actuals with Grid set to the grid name "
            "and time_from='now-24h' and time_to='now' to render the power plant performance chart "
            "for the last 24 hours. The chart image will be sent automatically — do not mention it in your text response. "
            "Also call customer_customer_get_last_gtr_summary with grid_name to get the last technical report summary. "
            "If a GTR summary is available (has kpis), show it as a final section "
            "'**Last GTR Review (Month Year)**' with KPI values and commentary, plus pending issues if any. "
            "If no GTR is available (no_gtr is true or error), omit this section silently."
        ),
        requires_args=False,
        args_hint="Optionally specify grid name, e.g., /grid ExampleGrid2",
        staff_only=False,
        exclusive_tools=[
            "customer_customer_get_grid_status",
            "equipment_diagnostics_get_equipment_status",
            "prefix:grafana_",
            "customer_customer_get_last_gtr_summary",
        ],
    ),
    "grids": CommandDefinition(
        command="grids",
        command_type="tool",
        description="Get status of all accessible grids",
        linked_tool="customer_customer_get_all_grids_status",
        prompt_template=(
            "IMPORTANT: Call the customer_customer_get_all_grids_status tool to get grid status. "
            "This is a GRID STATUS request, NOT a JIRA ticket request. Do NOT call any jira tools. "
            "Get the status of all grids accessible to the current user. "
            "\n\n"
            "FIRST, render a **Fleet Summary** from the fleet_summary data at the top:\n"
            "Line 1: Status counts (e.g., '🟢 8 FS | 🟡 2 HPS | 🔌 1 Isolated | 🔴 1 Off').\n"
            "Line 2: If grids_with_faults is non-empty, list them (e.g., '⚠️ Faults: ExampleGrid (grid_fault), ...').\n"
            "Line 3: If grids_with_downtime is non-empty, list top 3 by downtime_minutes "
            "(e.g., '🔻 Downtime: ExampleGrid 90m, ExampleGrid3 45m').\n"
            "Line 4: If low_fs_delivery is non-empty, list ONLY grids below 75% delivery "
            "(e.g., '📉 Low delivery: ExampleGrid 72%').\n"
            "Line 5: If offline_dcus is non-empty, list grids with offline DCUs "
            "(e.g., '📶 Offline DCUs: ExampleGrid (2)').\n"
            "Omit any line where the list is empty. "
            "Then add an empty line separator before the grouped grid list.\n\n"
            "Present results grouped by status (🟢 FS On, 🟡 HPS On, 🔌 Isolated, 🔴 Off, Ⅹ Unknown). "
            "Use two newlines (empty line) before each status group heading for visual separation. "
            "Format each grid on its own line with an empty line between grids. "
            "Per grid: Make the grid name a clickable link using [GridName](platform_url) format, "
            "then show inverter_power_kw as 'X.X kW' (or '—' if null), then DCU Weather Downtime on the same line. "
            "NO 'Grid Name:' prefix, NO status icon per grid (already in header). "
            "DCU: Use dcu_status.visual (e.g., 📶📶❌) or skip if N/A. "
            "Weather: Use live_weather.display (e.g., '☀️ 32°C' or '🌡️☀️ 35°C' if hot). "
            "Downtime: ⚡️ stable OR 🔻 Xh (Xm per cause). Add ! to grid_fault/unknown. "
            "Skip downtime field entirely if 0 minutes or N/A. "
            "FS: Show 'FS Xh' from fs_hours_24h (e.g., 'FS 8.5h') and delivery % from "
            "fs_delivery_24h.delivery_pct as 'delivery X%' (e.g., 'delivery 95%'). Skip FS field if fs_hours_24h is null. "
            "End with a hint: 'Use /grid <name> for detailed status of any grid.'"
        ),
        requires_args=False,
        staff_only=False,
        exclusive_tools=["customer_customer_get_all_grids_status"],
    ),
    "online": CommandDefinition(
        command="online",
        command_type="tool",
        description="Get status of all accessible grids (alias for /grids)",
        linked_tool="customer_customer_get_all_grids_status",
        prompt_template=None,  # Filled from /grids below
        requires_args=False,
        staff_only=False,
        exclusive_tools=["customer_customer_get_all_grids_status"],
    ),
    "equipment": CommandDefinition(
        command="equipment",
        command_type="tool",
        description="Get equipment status and diagnostics (e.g., /equipment ExampleGrid)",
        linked_tool="equipment_diagnostics_get_equipment_status",
        prompt_template=(
            "Get the current equipment status for grid '{args}'. "
            "Use the equipment_diagnostics_get_equipment_status tool. "
            "Report inverter power per phase, battery SOC and charging state, "
            "grid connection status, PV power, and any active alarms. "
            "Format power values in kW for readability."
        ),
        requires_args=True,
        args_hint="Please specify the grid name, e.g., /equipment ExampleGrid",
        staff_only=True,
    ),
    "equipment_history": CommandDefinition(
        command="equipment_history",
        command_type="tool",
        description="Get historical power data and analysis (e.g., /equipment_history ExampleGrid last_24h)",
        linked_tool="equipment_diagnostics_get_historical_power_data",
        prompt_template=(
            "Get historical power data for grid '{args}'. "
            "Use the equipment_diagnostics_get_historical_power_data tool. "
            "Parse the args to extract grid name and optional time range (last_hour, last_6h, last_24h, last_7d, last_30d, last_90d). "
            "Include analysis=['outages', 'peak_load', 'summary_stats'] to get insights. "
            "Report any detected grid outages with affected phases, duration, and peak load before the outage."
        ),
        requires_args=True,
        args_hint="Please specify the grid name and optionally time range, e.g., /equipment_history ExampleGrid last_7d",
        staff_only=True,
    ),
    "power_chart": CommandDefinition(
        command="power_chart",
        command_type="tool",
        description="Generate a power chart (e.g., /power_chart ExampleGrid power_timeline)",
        linked_tool="equipment_diagnostics_generate_power_chart",
        prompt_template=(
            "Generate a power chart for grid '{args}'. "
            "Use the equipment_diagnostics_generate_power_chart tool. "
            "Parse the args to extract grid name and optional chart type "
            "(power_timeline, battery_soc, grid_vs_inverter, load_distribution, outage_events). "
            "Default to power_timeline if not specified. "
            "The chart will be sent as an image."
        ),
        requires_args=True,
        args_hint="Please specify the grid name and chart type, e.g., /power_chart ExampleGrid battery_soc",
        staff_only=True,
    ),
    "schedule": CommandDefinition(
        command="schedule",
        command_type="tool",
        description="Schedule commands to run later or on a recurring basis",
        linked_tool="schedule_schedule_user_command",
        prompt_template=SCHEDULE_COMMAND_TEMPLATE,
        requires_args=False,
        staff_only=True,
        exclusive_tools=[
            "schedule_schedule_user_command",
            "schedule_list_user_schedules",
            "schedule_cancel_user_schedule",
            "schedule_pause_user_schedule",
            "schedule_resume_user_schedule",
        ],
    ),
    "agents": CommandDefinition(
        command="agents",
        command_type="tool",
        description="List, create, or cancel persistent monitoring agents",
        linked_tool="schedule_list_user_agents",
        prompt_template=AGENTS_COMMAND_TEMPLATE,
        requires_args=False,
        staff_only=True,
        exclusive_tools=[
            "schedule_create_user_agent",
            "schedule_list_user_agents",
            "schedule_cancel_user_agent",
        ],
    ),
    "meta": CommandDefinition(
        command="meta",
        command_type="tool",
        description="View bot performance analytics",
        linked_tool="meta_get_performance_report",
        prompt_template=(
            "IMPORTANT: The meta analytics tools ARE available. Call them now.\n\n"
            "Get bot performance analytics for the past 7 days. "
            "Organization filter: '{args}' (empty = all orgs). "
            "Execute these steps in sequence:\n"
            "1) Call meta_get_performance_report to get performance data\n"
            "2) Call meta_response_distribution_chart for response distribution chart\n"
            "3) Call meta_escalation_types_chart for escalation breakdown chart\n"
            "4) Call meta_issue_type_breakdown_chart for issue type breakdown chart\n"
            "5) Present summary with all charts\n\n"
            "DO NOT say tools are unavailable. They are in your function list. Use them."
        ),
        requires_args=False,
        args_hint="Optionally specify organization name, e.g., /meta ExampleGrid2",
        staff_only=True,
    ),
    "retry_commissioning": CommandDefinition(
        command="retry_commissioning",
        command_type="tool",
        description="Retry a failed meter commissioning (e.g., /retry_commissioning 12345678)",
        linked_tool="customer_retry_commissioning",
        prompt_template=(
            "The user has explicitly requested to retry commissioning for meter '{args}'. "
            "Call the customer_retry_commissioning tool with meter_number='{args}' IMMEDIATELY. "
            "CRITICAL: Do NOT ask for additional confirmation - the /command itself is the user's confirmation. "
            "The tool is only available on this turn, so you MUST call it now. "
            "Report the result. If successful, mention they can check progress with the meter_information tool."
        ),
        requires_args=True,
        args_hint="Please specify the meter number, e.g., /retry_commissioning 12345678",
        exclusive_tools=["customer_retry_commissioning"],
        staff_only=True,
    ),
    "unassign": CommandDefinition(
        command="unassign",
        command_type="tool",
        description="Unassign a meter from its connection (e.g., /unassign 12345678)",
        linked_tool="customer_unassign_meter",
        prompt_template=(
            "The user has explicitly requested to unassign meter '{args}' from its current connection. "
            "Call the customer_unassign_meter tool with meter_number='{args}' IMMEDIATELY. "
            "CRITICAL: Do NOT ask for additional confirmation - the /command itself is the user's confirmation. "
            "The tool is only available on this turn, so you MUST call it now. "
            "Report the result. If successful, mention the meter is now available for reassignment."
        ),
        requires_args=True,
        args_hint="Please specify the meter number, e.g., /unassign 12345678",
        exclusive_tools=["customer_unassign_meter"],
        staff_only=True,
    ),
    "hps_limit": CommandDefinition(
        command="hps_limit",
        command_type="tool",
        description="Set the HPS power limit for a meter (e.g., /hps_limit 12345678 200)",
        linked_tool="customer_set_meter_power_limit",
        prompt_template=(
            "The user has explicitly requested to set the power limit for a meter. "
            "Parse '{args}' as '<meter_number> <watts>' (e.g., '12345678 200'). "
            "The allowed power limit values are 200W (standard HPS) and 600W (high power). "
            "If no watts value is provided, use 200 as the default. "
            "Call the customer_set_meter_power_limit tool with the parsed meter_number and "
            "power_limit_watts (as a string: '200' or '600') IMMEDIATELY. "
            "CRITICAL: Do NOT ask for additional confirmation - the /command itself is the user's confirmation. "
            "The tool is only available on this turn, so you MUST call it now. "
            "Report the result clearly, including the new limit in watts and that the change takes effect on the next meter communication."
        ),
        requires_args=True,
        args_hint="Please specify meter number and watts, e.g., /hps_limit 12345678 200",
        exclusive_tools=["customer_set_meter_power_limit"],
        staff_only=True,
    ),
    "resend_topup_token": CommandDefinition(
        command="resend_topup_token",
        command_type="tool",
        description="Resend the last TOP_UP prepayment token to a meter (e.g., /resend_topup_token 12345678)",
        linked_tool="customer_resend_meter_token",
        prompt_template=(
            "The user has explicitly requested to resend the last top-up token for meter '{args}'. "
            "Call the customer_resend_meter_token tool with meter_number='{args}' IMMEDIATELY. "
            "CRITICAL: Do NOT ask for additional confirmation - the /command itself is the user's confirmation. "
            "The tool is only available on this turn, so you MUST call it now. "
            "Report the result. If successful, inform the user that the customer should receive the token "
            "via their registered channel (SMS or app) shortly."
        ),
        requires_args=True,
        args_hint="Please specify the meter number, e.g., /resend_topup_token 12345678",
        exclusive_tools=["customer_resend_meter_token"],
        staff_only=True,
    ),
    "resend_clear_tamper_token": CommandDefinition(
        command="resend_clear_tamper_token",
        command_type="tool",
        description="Resend the last CLEAR_TAMPER token to a meter (e.g., /resend_clear_tamper_token 12345678)",
        linked_tool="customer_resend_clear_tamper_token",
        prompt_template=(
            "The user has explicitly requested to resend the last CLEAR_TAMPER token for meter '{args}'. "
            "Call the customer_resend_clear_tamper_token tool with meter_number='{args}' IMMEDIATELY. "
            "CRITICAL: Do NOT ask for additional confirmation - the /command itself is the user's confirmation. "
            "The tool is only available on this turn, so you MUST call it now. "
            "Report the result. If successful, inform the user that the customer should receive the token "
            "via their registered channel (SMS or app) shortly."
        ),
        requires_args=True,
        args_hint="Please specify the meter number, e.g., /resend_clear_tamper_token 12345678",
        exclusive_tools=["customer_resend_clear_tamper_token"],
        staff_only=True,
    ),
    "resend_power_limit_token": CommandDefinition(
        command="resend_power_limit_token",
        command_type="tool",
        description="Resend the last PLS (power limit set) token to a meter (e.g., /resend_power_limit_token 12345678)",
        linked_tool="customer_resend_power_limit_token",
        prompt_template=(
            "The user has explicitly requested to resend the last power limit set (PLS) token for meter '{args}'. "
            "Call the customer_resend_power_limit_token tool with meter_number='{args}' IMMEDIATELY. "
            "CRITICAL: Do NOT ask for additional confirmation - the /command itself is the user's confirmation. "
            "The tool is only available on this turn, so you MUST call it now. "
            "Report the result. If successful, inform the user that the customer should receive the token "
            "via their registered channel (SMS or app) shortly."
        ),
        requires_args=True,
        args_hint="Please specify the meter number, e.g., /resend_power_limit_token 12345678",
        exclusive_tools=["customer_resend_power_limit_token"],
        staff_only=True,
    ),
    "meter_date": CommandDefinition(
        command="meter_date",
        command_type="tool",
        description="Sync a meter's date to today (deployment-local time) (e.g., /meter_date 12345678)",
        linked_tool="customer_set_meter_date",
        prompt_template=(
            "The user has explicitly requested to set the date on meter '{args}' to today. "
            "Call the customer_set_meter_date tool with meter_number='{args}' IMMEDIATELY. "
            "CRITICAL: Do NOT ask for additional confirmation - the /command itself is the user's confirmation. "
            "The tool is only available on this turn, so you MUST call it now. "
            "Report the result, including the date that was set and that the change takes effect on the next meter communication."
        ),
        requires_args=True,
        args_hint="Please specify the meter number, e.g., /meter_date 12345678",
        exclusive_tools=["customer_set_meter_date"],
        staff_only=True,
    ),
    "meter_on": CommandDefinition(
        command="meter_on",
        command_type="tool",
        description="Turn on the relay for a meter, restoring power to the customer (e.g., /meter_on 12345678)",
        linked_tool="customer_turn_meter_on",
        prompt_template=(
            "The user has explicitly requested to turn ON meter '{args}'. "
            "Call the customer_turn_meter_on tool with meter_number='{args}' IMMEDIATELY. "
            "CRITICAL: Do NOT ask for additional confirmation - the /command itself is the user's confirmation. "
            "The tool is only available on this turn, so you MUST call it now. "
            "Report the result and that the change takes effect on the next meter communication."
        ),
        requires_args=True,
        args_hint="Please specify the meter number, e.g., /meter_on 12345678",
        exclusive_tools=["customer_turn_meter_on"],
        staff_only=True,
    ),
    "meter_off": CommandDefinition(
        command="meter_off",
        command_type="tool",
        description="Turn off the relay for a meter, cutting power to the customer (e.g., /meter_off 12345678)",
        linked_tool="customer_turn_meter_off",
        prompt_template=(
            "The user has explicitly requested to turn OFF meter '{args}'. "
            "Call the customer_turn_meter_off tool with meter_number='{args}' IMMEDIATELY. "
            "CRITICAL: Do NOT ask for additional confirmation - the /command itself is the user's confirmation. "
            "The tool is only available on this turn, so you MUST call it now. "
            "Report the result and that the change takes effect on the next meter communication."
        ),
        requires_args=True,
        args_hint="Please specify the meter number, e.g., /meter_off 12345678",
        exclusive_tools=["customer_turn_meter_off"],
        staff_only=True,
    ),
    "sign": CommandDefinition(
        command="sign",
        description="Request a signature on a Drive PDF from a named person",
        command_type="expert",
        packet_type="sign_request",
        staff_only=True,
        requires_args=False,
        nl_triggers=["sign this", "get this signed", "get it signed", "needs to sign"],
    ),
    # =========================================================================
    # EXPERT COMMANDS - Routed to expert workflows
    # =========================================================================
    "analyze": CommandDefinition(
        command="analyze",
        command_type="expert",
        description="Analyze a grid's performance and issues",
        packet_type="grid_analysis",
        requires_args=False,
        args_hint="Optionally specify grid name and time range, e.g., /analyze ExampleGrid last 7 days",
        staff_only=True,
        # Fallback when expert is disabled: use equipment_diagnostics tools
        prompt_template=(
            "Analyze grid failures and outages for '{args}'. "
            "Use equipment_diagnostics_get_historical_power_data with grid_name='{args}', "
            "time_range='last_7d', and analysis=['outages', 'peak_load', 'summary_stats']. "
            "The tool returns outages already enriched with cause classification, battery SOC, "
            "and related alarms (VE.Bus errors) — do NOT re-derive causes from load patterns.\n\n"
            "Format your response as follows:\n"
            "1. **Summary** — total outages, total downtime (use Xh Ym notation, e.g. '7h 12m' "
            "not '432.3 minutes'), and peak load.\n"
            "2. **Outages by cause** — group outages by cause.category (battery_depletion, "
            "overload, high_temperature, vebus_error, etc.), largest group first. For each group "
            "show total count and total downtime.\n"
            "3. **Outage details** — within each cause group, list individual outages with: "
            "start time, duration (Xh Ym), battery SOC (battery_soc_at_outage), peak load "
            "before outage, and related_alarms (show VE.Bus error descriptions if present).\n"
            "4. **Actionable findings** — any patterns or recommendations."
        ),
        exclusive_tools=[
            "equipment_diagnostics_get_historical_power_data",
            "equipment_diagnostics_analyze_grid_outage",
        ],
    ),
    "summarize_knowledge": CommandDefinition(
        command="summarize_knowledge",
        command_type="tool",
        description="Summarize knowledge base on a topic",
        linked_tool="knowledge_summarize_knowledge",
        exclusive_tools=["knowledge_summarize_knowledge"],  # Only use knowledge tool
        prompt_template=(
            "Summarize the knowledge base information about '{args}'. "
            "Use the knowledge_summarize_knowledge tool with topic='{args}'. "
            "Present the structured summary to the user. "
            "If the summary mentions relevant tools for live data, note those to the user."
        ),
        requires_args=True,
        args_hint="Please specify a topic, e.g., /summarize_knowledge distribution design",
        staff_only=True,
    ),
    # =========================================================================
    # EXPERT COMMANDS - Routed to multi-step workflows
    # =========================================================================
    "analyse": CommandDefinition(
        command="analyse",
        command_type="expert",
        description="Analyze a grid's performance and issues (UK spelling)",
        packet_type="grid_analysis",
        requires_args=False,
        args_hint="Optionally specify grid name and time range, e.g., /analyse ExampleGrid last 7 days",
        staff_only=True,
        # Fallback when expert is disabled: same as /analyze
        prompt_template=(
            "Analyze grid failures and outages for '{args}'. "
            "Use equipment_diagnostics_get_historical_power_data with grid_name='{args}', "
            "time_range='last_7d', and analysis=['outages', 'peak_load', 'summary_stats']. "
            "The tool returns outages already enriched with cause classification, battery SOC, "
            "and related alarms (VE.Bus errors) — do NOT re-derive causes from load patterns.\n\n"
            "Format your response as follows:\n"
            "1. **Summary** — total outages, total downtime (use Xh Ym notation, e.g. '7h 12m' "
            "not '432.3 minutes'), and peak load.\n"
            "2. **Outages by cause** — group outages by cause.category (battery_depletion, "
            "overload, high_temperature, vebus_error, etc.), largest group first. For each group "
            "show total count and total downtime.\n"
            "3. **Outage details** — within each cause group, list individual outages with: "
            "start time, duration (Xh Ym), battery SOC (battery_soc_at_outage), peak load "
            "before outage, and related_alarms (show VE.Bus error descriptions if present).\n"
            "4. **Actionable findings** — any patterns or recommendations."
        ),
        exclusive_tools=[
            "equipment_diagnostics_get_historical_power_data",
            "equipment_diagnostics_analyze_grid_outage",
        ],
    ),
    "kpi": CommandDefinition(
        command="kpi",
        command_type="expert",
        description="Generate KPI report",
        packet_type="kpi_report",
        requires_args=False,
        args_hint="Optionally specify report type and grids, e.g., /kpi weekly ExampleGrid",
        staff_only=True,
    ),
    "report": CommandDefinition(
        command="report",
        command_type="expert",
        description="Generate KPI report (alias for /kpi)",
        packet_type="kpi_report",
        requires_args=False,
        args_hint="Optionally specify report type and grids, e.g., /report monthly",
        staff_only=True,
    ),
    "lpp": CommandDefinition(
        command="lpp",
        command_type="expert",
        description="Create Light Preliminary Package for a site",
        packet_type="light_preliminary_package",
        requires_args=True,
        args_hint="Specify site name(s), e.g., /lpp ExampleGrid or /lpp ExampleGrid, SiteAlpha, SiteBeta",
        staff_only=True,
    ),
    "csize": CommandDefinition(
        command="csize",
        command_type="expert",
        description="Detect community at GPS coordinates and estimate solar sizing",
        packet_type="community_sizing",
        requires_args=True,
        args_hint="Provide lat and lon, e.g. /csize 6.12345 3.98765 or /csize 6.12345 3.98765 EXAMPLE_SITE_001",
        staff_only=True,
        nl_triggers=[
            "community at",
            "kwp for",
        ],
    ),
    "editdoc": CommandDefinition(
        command="editdoc",
        command_type="tool",
        description="Edit a Google Doc based on @anansibot comments or instructions",
        prompt_template=(
            "Edit the Google Doc: {args}. "
            "Find the document, then read it to understand its structure and content. "
            "Before editing, identify contextual anchors in the document "
            "(e.g. site name, grid name, organization) and use these consistently "
            "as parameters when calling data tools. "
            "If the request includes a specific instruction (e.g. 'add X to section Y'), "
            "apply that instruction directly as an edit. "
            "Also scan for any pending @anansibot comments and apply those too. "
            "If an edit needs data (e.g. equipment status, power readings), "
            "gather only the specific data requested."
        ),
        requires_args=True,
        args_hint="Document name or URL, e.g., /editdoc Site Visit Plan ExampleGrid",
        staff_only=True,
        exclusive_tools=[
            "knowledge_find_document",
            "knowledge_read_document",
            "knowledge_scan_doc_comments",
            "knowledge_edit_doc_section",
            "equipment_diagnostics_get_equipment_status",
            "equipment_diagnostics_get_historical_power_data",
            "equipment_diagnostics_analyze_grid_outage",
            "vrm_get_inverter_power",
        ],
        model_override="GEMINI_DEEP_THINKING_MODEL",
        nl_triggers=[
            "edit this document",
            "edit the document",
            "edit this doc",
            "edit the doc",
            "help me edit",
            "update this document",
            "update the document",
            "update this doc",
            "update the doc",
            "left a comment for you",
            "left you a comment",
            "comment in the doc",
        ],
    ),
    "ingest": CommandDefinition(
        command="ingest",
        command_type="expert",
        description="Ingest documents into the knowledge base",
        packet_type="document_ingestion",
        requires_args=False,
        args_hint="Optionally provide a Google Doc ID or paste text content",
        staff_only=True,
    ),
    "learn": CommandDefinition(
        command="learn",
        command_type="expert",
        description="Teach the bot new information (alias for /ingest)",
        packet_type="document_ingestion",
        requires_args=False,
        args_hint="Optionally provide a Google Doc URL or ID",
        staff_only=True,
    ),
    "gtr": CommandDefinition(
        command="gtr",
        command_type="expert",
        description="Generate monthly technical review for grid(s)",
        packet_type="grids_technical_review",
        requires_args=False,
        args_hint="Optionally specify grid name(s), e.g., /gtr ExampleGrid",
        staff_only=True,
    ),
    "ayrton": CommandDefinition(
        command="ayrton",
        description="Investigate Skyfox codebase for an issue",
        command_type="expert",
        packet_type="code_investigation",
        requires_args=True,
        args_hint="Describe the issue, e.g., /ayrton why is meter 12345 not accepting tokens",
        staff_only=True,
    ),
    "anansi": CommandDefinition(
        command="anansi",
        description="Investigate Anansi codebase for an issue",
        command_type="expert",
        packet_type="code_investigation",
        requires_args=True,
        args_hint="Describe the issue, e.g., /anansi why is the grid command stuck",
        staff_only=True,
    ),
    # =========================================================================
    # DIRECT COMMANDS - Handled inline, bypass LLM entirely
    # =========================================================================
    "pending": CommandDefinition(
        command="pending",
        command_type="direct",
        description="Show pending workflows for this session",
        requires_args=False,
        staff_only=True,
    ),
}

# Copy prompt_template from /grids to /online alias (avoids duplicating the long string)
COMMAND_REGISTRY["online"].prompt_template = COMMAND_REGISTRY["grids"].prompt_template


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def get_command(command: str) -> Optional[CommandDefinition]:
    """
    Get command definition by name.

    Args:
        command: Command name (without slash)

    Returns:
        CommandDefinition if found, None otherwise
    """
    return COMMAND_REGISTRY.get(command)


def get_tool_commands() -> Dict[str, CommandDefinition]:
    """Get all tool-type commands."""
    return {k: v for k, v in COMMAND_REGISTRY.items() if v.command_type == "tool"}


def get_expert_commands() -> Dict[str, CommandDefinition]:
    """Get all expert-type commands."""
    return {k: v for k, v in COMMAND_REGISTRY.items() if v.command_type == "expert"}


def get_expert_command_mapping() -> Dict[str, str]:
    """
    Get mapping of expert commands to packet types.

    Returns:
        Dict mapping command with slash (e.g., "/lpp") to packet_type
    """
    return {
        f"/{cmd.command}": cmd.packet_type
        for cmd in COMMAND_REGISTRY.values()
        if cmd.command_type == "expert"
    }


def get_all_commands() -> List[CommandDefinition]:
    """Get all registered commands."""
    return list(COMMAND_REGISTRY.values())


def is_expert_command(command: str) -> bool:
    """
    Check if a command is an expert command.

    Args:
        command: Command name (with or without slash)

    Returns:
        True if this is an expert command
    """
    cmd_name = command.lstrip("/")
    cmd_def = COMMAND_REGISTRY.get(cmd_name)
    return cmd_def is not None and cmd_def.command_type == "expert"


def match_nl_trigger(text: str, is_staff: bool = False) -> Optional[CommandDefinition]:
    """Match natural language text against command nl_triggers.

    Returns the first matching CommandDefinition, or None.
    Respects staff_only restrictions.
    """
    text_lower = text.lower()
    for cmd_def in COMMAND_REGISTRY.values():
        if not cmd_def.nl_triggers:
            continue
        if cmd_def.staff_only and not is_staff:
            continue
        for trigger in cmd_def.nl_triggers:
            if trigger in text_lower:
                return cmd_def
    return None


__all__ = [
    "CommandDefinition",
    "COMMAND_REGISTRY",
    "AGENTS_COMMAND_TEMPLATE",
    "SCHEDULE_COMMAND_TEMPLATE",
    "TICKETS_WITH_ARGS_TEMPLATE",
    "get_command",
    "get_tool_commands",
    "get_expert_commands",
    "get_expert_command_mapping",
    "get_all_commands",
    "is_expert_command",
    "match_nl_trigger",
]
