"""GTR Analysis Conversation step handler.

Conversational analysis of historical GTR data with live Grafana timeseries deep dives.
Uses the existing needs_user_input pattern to loop:
- First call: loads past year's historical reviews from sheet, discovers timeseries tools
- Subsequent calls: LLM responds to user question with optional Grafana tool calls
- Exit: user says done/finish/exit or cancel keywords → skip_remaining
"""

import json
import os
import re
from typing import Any, Dict, List

from orchestrator.config.settings import get_settings
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Exit phrases (exact match, case-insensitive)
ANALYSIS_EXIT_PHRASES = {"done", "finish", "exit analysis", "end analysis"}
CANCEL_KEYWORDS = {"cancel", "abort", "quit", "exit", "stop", "no", "n"}


async def _discover_timeseries_tools() -> List[Dict[str, str]]:
    """Filter Grafana tools to only those backed by timeseries panels.

    Looks up panel_type via TOOL_NAME_TO_PANEL_KEY -> PANELS_METADATA chain,
    since panel_type is NOT included in the tool listing dicts.

    Returns:
        List of dicts with name, display_name, dashboard keys
    """
    try:
        from mcp_servers.server_registry import list_tools as registry_list_tools
        from mcp_servers.servers.grafana_server.grafana_mcp_server import (
            PANELS_METADATA,
            TOOL_NAME_TO_PANEL_KEY,
        )
    except ImportError:
        LOGGER.warning("Grafana server not available for timeseries tool discovery")
        return []

    try:
        all_tools = await registry_list_tools("grafana")
        timeseries_tools: List[Dict[str, str]] = []

        for tool in all_tools:
            tool_name = str(tool.get("name", ""))
            panel_key = TOOL_NAME_TO_PANEL_KEY.get(tool_name)
            if panel_key:
                panel_info = PANELS_METADATA.get(panel_key, {})
                panel_type = panel_info.get("panel_type", "timeseries")
                if panel_type in ("timeseries", "graph"):
                    timeseries_tools.append(
                        {
                            "name": f"grafana_{tool_name}",
                            "display_name": panel_info.get("title", tool_name),
                            "dashboard": panel_info.get("dashboard_title", ""),
                        }
                    )

        LOGGER.info(
            f"Discovered {len(timeseries_tools)} timeseries Grafana tools "
            f"out of {len(all_tools)} total"
        )
        return timeseries_tools

    except Exception as e:
        LOGGER.warning(f"Failed to discover timeseries tools: {e}")
        return []


def _build_welcome_message(
    historical_md: str,
    timeseries_tools: List[Dict[str, str]],
    grids_to_review: List[Dict[str, Any]],
) -> str:
    """Build the first-turn welcome message with summary and examples.

    Args:
        historical_md: Full historical markdown document
        timeseries_tools: Available timeseries Grafana tools
        grids_to_review: List of grid dicts

    Returns:
        Welcome message string
    """
    grid_names = [g["name"] for g in grids_to_review]
    grids_str = ", ".join(grid_names)

    # Count months with data
    months_with_data = len(re.findall(r"^### \w+ \d{4}$", historical_md, re.MULTILINE))
    months_no_data = len(re.findall(r"_No review data found", historical_md))

    parts = [
        f"**GTR Analysis Mode** for {grids_str}",
        f"Loaded {months_with_data} months of historical reviews"
        + (f" ({months_no_data} months with no data)" if months_no_data else ""),
        "",
    ]

    if timeseries_tools:
        parts.append(f"{len(timeseries_tools)} Grafana timeseries charts available for deep dives")
        parts.append("")

    parts.extend(
        [
            "You can ask questions like:",
            '- "What\'s the trend in CUF over the past 6 months?"',
            '- "Which months had the most technical downtime?"',
            '- "Show me the battery usage trend"',
            '- "Are there any recurring pending issues?"',
            "",
            "Type **done** or **exit** when finished.",
        ]
    )

    return "\n".join(parts)


def _build_tool_declarations(
    timeseries_tools: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """Build Gemini function declarations for available Grafana tools.

    Args:
        timeseries_tools: Available timeseries tools

    Returns:
        List of function declaration dicts for Gemini API
    """
    declarations = []
    for tool in timeseries_tools:
        declarations.append(
            {
                "name": tool["name"],
                "description": f"Fetch timeseries data for {tool['display_name']} from Grafana",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "Grid": {
                            "type": "string",
                            "description": "Grid name to query data for",
                        },
                        "time_from": {
                            "type": "string",
                            "description": "Start time in ISO 8601 (e.g., 2025-07-01T00:00:00Z)",
                        },
                        "time_to": {
                            "type": "string",
                            "description": "End time in ISO 8601 (e.g., 2025-07-31T23:59:59Z)",
                        },
                    },
                    "required": ["Grid", "time_from", "time_to"],
                },
            }
        )
    return declarations


async def _execute_tool_call(
    context: StepContext,
    tool_name: str,
    tool_args: Dict[str, Any],
) -> str:
    """Execute a Grafana tool call via MCP executor.

    Args:
        context: Step context with mcp_executor
        tool_name: Full tool name (e.g., grafana_battery_usage)
        tool_args: Tool arguments (Grid, time_from, time_to)

    Returns:
        Tool result as string (JSON)
    """
    if not context.mcp_executor:
        return json.dumps({"error": "MCP executor not available"})

    try:
        result = await context.mcp_executor.call_tool(tool_name, tool_args)

        if isinstance(result, str):
            return result
        return json.dumps(result, default=str)

    except Exception as e:
        LOGGER.warning(f"Tool call {tool_name} failed: {e}")
        return json.dumps({"error": sanitize_error_for_user(str(e), "Grafana data retrieval")})


async def _run_analysis_turn(
    context: StepContext,
    conversation_turns: List[Dict[str, str]],
) -> str:
    """Execute one analysis conversation turn: prompt Gemini, handle tool calls, return response.

    Args:
        context: Step context
        conversation_turns: Previous conversation turns

    Returns:
        LLM response text
    """
    try:
        from google import genai
    except ImportError:
        return "Gemini API not available. Cannot process analysis questions."

    historical_md = context.get_state("historical_reviews_md", "")
    timeseries_tools = context.get_state("available_timeseries_tools", [])
    user_question = context.user_input or ""

    # Build system prompt
    system_prompt = (
        "You are a grid technical reviewer analyzing historical performance data.\n\n"
        "CRITICAL: ONLY analyze and reference data from the Historical Reviews markdown below "
        "and from Grafana tool call results. Never invent, estimate, or assume KPI values. "
        "If data is missing for a period, say so explicitly.\n\n"
        "When calling Grafana tools, use ISO 8601 format: "
        'time_from="2025-07-01T00:00:00Z", time_to="2025-07-31T23:59:59Z".\n'
        "For full year trends, use a single call with the full analysis period range "
        "rather than individual monthly calls.\n\n"
        "KPI relationships:\n"
        "- Financial CUF depends on: uncurtailed loss, battery usage, solar production\n"
        "- Service uptime: FS Hours (target ≥12h), HPS Hours (target ≥22h)\n"
        "- Financial CUF target: ≥55%\n"
        "- Technical Downtime target: 0 days\n\n"
        "Format responses clearly using markdown. Include specific numbers and trends."
    )

    # Build conversation contents for Gemini
    contents = []

    # Build context block: historical reviews + recent chat chronology
    context_text = f"Here is the historical review data to analyze:\n\n{historical_md}\n\n"

    chat_chronology_md = context.get_state("chat_chronology_md", "")
    if chat_chronology_md:
        context_text += (
            "Recent customer and staff communications about these grids:\n\n"
            f"{chat_chronology_md}\n\n"
            "Consider these communications when analyzing grid performance — "
            "they may highlight customer complaints, outages, or staff observations "
            "that correlate with the KPI data.\n\n"
        )

    context_text += (
        "I'll now ask you questions about this data. "
        "You can also call Grafana tools for live timeseries deep dives."
    )

    # First message: historical context + chat chronology
    contents.append(
        {
            "role": "user",
            "parts": [{"text": context_text}],
        }
    )
    contents.append(
        {
            "role": "model",
            "parts": [
                {
                    "text": (
                        "I've loaded the historical review data. I can analyze KPI trends, "
                        "identify recurring issues, and pull live Grafana charts for deeper analysis. "
                        "What would you like to explore?"
                    )
                }
            ],
        }
    )

    # Add previous conversation turns (last 10 exchanges = 20 entries)
    recent_turns = conversation_turns[-20:]
    for turn in recent_turns:
        role = "user" if turn["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": turn["content"]}]})

    # Add current user question
    contents.append({"role": "user", "parts": [{"text": user_question}]})

    # Build Gemini request
    client = genai.Client(
        api_key=os.getenv("GOOGLE_API_KEY"),
        http_options={"timeout": 30_000},
    )
    model = get_settings().gemini.model

    # Build config
    config: Dict[str, Any] = {
        "temperature": 0.3,
        "max_output_tokens": int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "8192")),
        "system_instruction": system_prompt,
    }

    # Add tool declarations if timeseries tools are available
    tool_declarations = _build_tool_declarations(timeseries_tools)
    if tool_declarations:
        config["tools"] = [{"function_declarations": tool_declarations}]

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

        # Build whitelist of declared tool names for validation
        declared_tool_names = {t["name"] for t in timeseries_tools}

        # Handle function calls (multi-turn tool use)
        max_tool_rounds = 3
        for _ in range(max_tool_rounds):
            if not response.candidates:
                break

            # Check if the response contains function calls
            has_function_call = False
            function_call_results = []

            for part in response.candidates[0].content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    has_function_call = True
                    fc = part.function_call
                    tool_name = fc.name
                    tool_args = dict(fc.args) if fc.args else {}

                    LOGGER.info(f"LLM requested tool call: {tool_name}({tool_args})")

                    # Validate tool name against declared whitelist
                    if tool_name not in declared_tool_names:
                        LOGGER.warning(f"LLM requested undeclared tool: {tool_name}")
                        tool_result = json.dumps({"error": f"Tool {tool_name} not available"})
                    else:
                        tool_result = await _execute_tool_call(context, tool_name, tool_args)

                    function_call_results.append(
                        {
                            "function_response": {
                                "name": tool_name,
                                "response": {"result": tool_result},
                            }
                        }
                    )

            if not has_function_call:
                break

            # Feed tool results back to Gemini
            contents.append(
                {
                    "role": "model",
                    "parts": [
                        {
                            "function_call": {
                                "name": fc.name,
                                "args": dict(fc.args) if fc.args else {},
                            }
                        }
                        for part in response.candidates[0].content.parts
                        if hasattr(part, "function_call") and part.function_call
                        for fc in [part.function_call]
                    ],
                }
            )
            contents.append({"role": "user", "parts": function_call_results})

            # Call Gemini again with tool results
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

        # Extract final text response
        if response.text:
            return str(response.text)
        elif response.candidates:
            parts = response.candidates[0].content.parts
            text_parts = [p.text for p in parts if hasattr(p, "text") and p.text]
            if text_parts:
                return "\n".join(text_parts)

        return "I couldn't generate a response. Please try rephrasing your question."

    except Exception as e:
        LOGGER.error(f"Gemini call failed in analysis turn: {e}", exc_info=True)
        return "Sorry, I encountered an error processing your question. Please try again."


@register_step("gtr_analysis_conversation")
async def gtr_analysis_conversation(context: StepContext) -> StepResult:
    """Conversational analysis of historical GTR data with Grafana deep dives.

    Uses needs_user_input to loop:
    - First call: loads historical data, sends welcome, pauses
    - Subsequent calls: LLM responds to user question, pauses
    - Exit: user says done/finish/exit → skip_remaining

    Args:
        context: Step execution context

    Returns:
        StepResult
    """
    # Skip if not in analysis mode
    if not context.get_state("analysis_mode"):
        return StepResult(data={"skipped": True}, progress_message="Skipped (not analysis mode)")

    # --- Exit detection (MUST be first, per CLAUDE.md cancel handling) ---
    if context.user_input:
        cleaned = context.user_input.strip().lower()
        if cleaned in ANALYSIS_EXIT_PHRASES:
            conversation_turns = context.get_state("conversation_turns", [])
            return StepResult(
                skip_remaining=True,
                data={"summary": "Analysis session completed", "turns": len(conversation_turns)},
                progress_message="Analysis session complete.",
            )
        if cleaned in CANCEL_KEYWORDS:
            return StepResult(
                skip_remaining=True,
                progress_message="Cancelled.",
            )

    # --- First call: load historical data ---
    if not context.get_state("conversation_started"):
        await context.send_progress_to_user("Loading historical reviews...")

        grids_to_review = context.get_state("grids_to_review", [])

        # Load historical reviews from sheets (shared reader)
        from shared.utils.gtr_sheet_reader import load_grid_review_history

        historical_md = await load_grid_review_history(grids_to_review)

        # Discover timeseries Grafana tools
        timeseries_tools = await _discover_timeseries_tools()

        # Build welcome message
        welcome = _build_welcome_message(historical_md, timeseries_tools, grids_to_review)

        return StepResult(
            state_updates={
                "conversation_started": True,
                "historical_reviews_md": historical_md,
                "available_timeseries_tools": timeseries_tools,
                "conversation_turns": [],
            },
            needs_user_input=True,
            user_prompt=welcome,
        )

    # --- Subsequent calls: LLM conversation turn ---
    conversation_turns = context.get_state("conversation_turns", [])
    max_turns = int(os.getenv("GTR_ANALYSIS_MAX_TURNS", "30"))

    # Each exchange = 2 entries (user + assistant)
    if len(conversation_turns) >= max_turns * 2:
        return StepResult(
            skip_remaining=True,
            data={"summary": "Session limit reached", "turns": len(conversation_turns) // 2},
            progress_message=f"Analysis session limit reached ({max_turns} exchanges).",
        )

    # Run the analysis turn
    response = await _run_analysis_turn(context, conversation_turns)

    # Update conversation history (cap at last 10 exchanges = 20 entries in state)
    conversation_turns.append({"role": "user", "content": context.user_input})
    conversation_turns.append({"role": "assistant", "content": response})
    capped_turns = conversation_turns[-20:]

    return StepResult(
        data={"llm_response": response},
        state_updates={"conversation_turns": capped_turns},
        needs_user_input=True,
        user_prompt=response,
    )
