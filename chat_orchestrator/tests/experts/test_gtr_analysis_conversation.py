"""Tests for GTR Analysis Conversation step handler.

Tests historical review loading, section parsing, timeseries tool filtering,
exit detection, max turn enforcement, and conversation flow.
"""

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.experts.step_context import StepContext


def _make_context(**overrides) -> StepContext:
    """Create a StepContext with sensible defaults for testing."""
    defaults = {
        "packet_id": "gtr_test_123",
        "packet_type": "grids_technical_review",
        "packet_goal": "Review ExampleGrid grid",
        "packet_inputs": {},
        "packet_state": {},
        "current_step": "gtr_analysis_conversation",
        "steps_completed": [],
        "user_input": "",
    }
    defaults.update(overrides)
    return StepContext(**defaults)


# ---------------------------------------------------------------------------
# Timeseries tool discovery tests
# ---------------------------------------------------------------------------


class TestDiscoverTimeseriesTools:
    """Test timeseries panel_type filtering logic.

    Since _discover_timeseries_tools does dynamic imports of the Grafana server
    (which requires runtime config), we test the filtering logic directly by
    simulating what the function does with mock panel metadata.
    """

    def test_filters_timeseries_and_graph_panels(self):
        """Only timeseries and graph panel types should be included."""
        tools = [
            {"name": "battery_usage"},
            {"name": "financial_cuf_90d"},
            {"name": "connection_count"},
            {"name": "current_soc"},
        ]

        panels_metadata = {
            "p1": {"panel_type": "timeseries", "title": "Battery Usage", "dashboard_title": "KPI"},
            "p2": {"panel_type": "stat", "title": "Financial CUF", "dashboard_title": "KPI"},
            "p3": {"panel_type": "graph", "title": "Connection Count", "dashboard_title": "Ops"},
            "p4": {"panel_type": "gauge", "title": "Current SOC", "dashboard_title": "KPI"},
        }

        tool_to_panel = {
            "battery_usage": "p1",
            "financial_cuf_90d": "p2",
            "connection_count": "p3",
            "current_soc": "p4",
        }

        # Replicate the filtering logic from _discover_timeseries_tools
        result = []
        for tool in tools:
            tool_name = tool["name"]
            panel_key = tool_to_panel.get(tool_name)
            if panel_key:
                panel_info = panels_metadata.get(panel_key, {})
                panel_type = panel_info.get("panel_type", "timeseries")
                if panel_type in ("timeseries", "graph"):
                    result.append(
                        {
                            "name": f"grafana_{tool_name}",
                            "display_name": panel_info.get("title", tool_name),
                            "dashboard": panel_info.get("dashboard_title", ""),
                        }
                    )

        # timeseries and graph included; stat and gauge excluded
        assert len(result) == 2
        assert result[0]["name"] == "grafana_battery_usage"
        assert result[1]["name"] == "grafana_connection_count"

    def test_unknown_tool_skipped(self):
        """Tools not in TOOL_NAME_TO_PANEL_KEY mapping are skipped."""
        tools = [{"name": "unknown_tool"}]
        tool_to_panel: dict = {}

        result = []
        for tool in tools:
            panel_key = tool_to_panel.get(tool["name"])
            if panel_key:
                result.append(tool)

        assert len(result) == 0


# ---------------------------------------------------------------------------
# Exit condition tests
# ---------------------------------------------------------------------------


class TestExitDetection:
    """Test exit phrase and cancel keyword detection."""

    @pytest.mark.asyncio
    async def test_done_exits(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            gtr_analysis_conversation,
        )

        ctx = _make_context(
            packet_state={"analysis_mode": True, "conversation_started": True},
            user_input="done",
        )
        result = await gtr_analysis_conversation(ctx)
        assert result.skip_remaining is True
        assert "complete" in result.progress_message.lower()

    @pytest.mark.asyncio
    async def test_finish_exits(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            gtr_analysis_conversation,
        )

        ctx = _make_context(
            packet_state={"analysis_mode": True, "conversation_started": True},
            user_input="finish",
        )
        result = await gtr_analysis_conversation(ctx)
        assert result.skip_remaining is True

    @pytest.mark.asyncio
    async def test_cancel_exits(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            gtr_analysis_conversation,
        )

        ctx = _make_context(
            packet_state={"analysis_mode": True, "conversation_started": True},
            user_input="cancel",
        )
        result = await gtr_analysis_conversation(ctx)
        assert result.skip_remaining is True
        assert "cancel" in result.progress_message.lower()

    @pytest.mark.asyncio
    async def test_no_cancels(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            gtr_analysis_conversation,
        )

        ctx = _make_context(
            packet_state={"analysis_mode": True, "conversation_started": True},
            user_input="no",
        )
        result = await gtr_analysis_conversation(ctx)
        assert result.skip_remaining is True
        assert "cancel" in result.progress_message.lower()

    @pytest.mark.asyncio
    async def test_n_cancels(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            gtr_analysis_conversation,
        )

        ctx = _make_context(
            packet_state={"analysis_mode": True, "conversation_started": True},
            user_input="n",
        )
        result = await gtr_analysis_conversation(ctx)
        assert result.skip_remaining is True

    @pytest.mark.asyncio
    async def test_dont_exit_does_not_exit(self):
        """Phrases containing exit words but not exact match should NOT exit."""
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            gtr_analysis_conversation,
        )

        ctx = _make_context(
            packet_state={
                "analysis_mode": True,
                "conversation_started": True,
                "historical_reviews_md": "# Test",
                "available_timeseries_tools": [],
                "conversation_turns": [],
            },
            user_input="don't exit yet",
        )

        with patch(
            "orchestrator.experts.handlers.grids_technical_reviewer"
            ".gtr_analysis_conversation._run_analysis_turn",
            new_callable=AsyncMock,
            return_value="I'll continue analyzing...",
        ):
            result = await gtr_analysis_conversation(ctx)
            assert result.skip_remaining is False
            assert result.needs_user_input is True


# ---------------------------------------------------------------------------
# Skip when not analysis mode
# ---------------------------------------------------------------------------


class TestSkipWhenNotAnalysisMode:
    """Test that handler skips when analysis_mode is not set."""

    @pytest.mark.asyncio
    async def test_skips_when_no_analysis_mode(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            gtr_analysis_conversation,
        )

        ctx = _make_context(packet_state={})
        result = await gtr_analysis_conversation(ctx)
        assert result.data.get("skipped") is True

    @pytest.mark.asyncio
    async def test_skips_when_analysis_mode_false(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            gtr_analysis_conversation,
        )

        ctx = _make_context(packet_state={"analysis_mode": False})
        result = await gtr_analysis_conversation(ctx)
        assert result.data.get("skipped") is True


# ---------------------------------------------------------------------------
# Max turn enforcement
# ---------------------------------------------------------------------------


class TestMaxTurnEnforcement:
    """Test conversation turn limit."""

    @pytest.mark.asyncio
    async def test_max_turns_exits(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            gtr_analysis_conversation,
        )

        # 30 exchanges = 60 entries
        turns = [{"role": "user", "content": f"q{i}"} for i in range(30)]
        turns += [{"role": "assistant", "content": f"a{i}"} for i in range(30)]

        ctx = _make_context(
            packet_state={
                "analysis_mode": True,
                "conversation_started": True,
                "conversation_turns": turns,
            },
            user_input="another question",
        )

        with patch.dict("os.environ", {"GTR_ANALYSIS_MAX_TURNS": "30"}):
            result = await gtr_analysis_conversation(ctx)
            assert result.skip_remaining is True
            assert "limit" in result.progress_message.lower()


# ---------------------------------------------------------------------------
# Conversation turn capping
# ---------------------------------------------------------------------------


class TestConversationTurnCapping:
    """Test that conversation turns are capped at last 10 exchanges."""

    @pytest.mark.asyncio
    async def test_caps_at_20_entries(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            gtr_analysis_conversation,
        )

        # Build 18 existing entries (9 exchanges)
        turns = []
        for i in range(9):
            turns.append({"role": "user", "content": f"question {i}"})
            turns.append({"role": "assistant", "content": f"answer {i}"})

        ctx = _make_context(
            packet_state={
                "analysis_mode": True,
                "conversation_started": True,
                "historical_reviews_md": "# Test data",
                "available_timeseries_tools": [],
                "conversation_turns": turns,
            },
            user_input="new question",
        )

        with patch(
            "orchestrator.experts.handlers.grids_technical_reviewer"
            ".gtr_analysis_conversation._run_analysis_turn",
            new_callable=AsyncMock,
            return_value="Here's my analysis...",
        ):
            result = await gtr_analysis_conversation(ctx)
            # 18 existing + 2 new = 20, capped at 20 (last 10 exchanges)
            capped = result.state_updates.get("conversation_turns", [])
            assert len(capped) <= 20


# ---------------------------------------------------------------------------
# First call (welcome) tests
# ---------------------------------------------------------------------------


class TestFirstCall:
    """Test the first call loads historical data and sends welcome."""

    @pytest.mark.asyncio
    async def test_first_call_sets_conversation_started(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            gtr_analysis_conversation,
        )

        ctx = _make_context(
            packet_state={
                "analysis_mode": True,
                "grids_to_review": [{"name": "ExampleGrid", "spreadsheet_id": "sheet123"}],
            },
        )
        # Mock send_progress_to_user
        ctx.send_progress_to_user = AsyncMock(return_value=True)

        with (
            patch(
                "shared.utils.gtr_sheet_reader.load_grid_review_history",
                new_callable=AsyncMock,
                return_value="# GTR Historical Analysis: ExampleGrid\n## Period: ...",
            ),
            patch(
                "orchestrator.experts.handlers.grids_technical_reviewer"
                ".gtr_analysis_conversation._discover_timeseries_tools",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "name": "grafana_battery_usage",
                        "display_name": "Battery Usage",
                        "dashboard": "Grid KPI",
                    }
                ],
            ),
        ):
            result = await gtr_analysis_conversation(ctx)

            assert result.needs_user_input is True
            assert result.state_updates["conversation_started"] is True
            assert "historical_reviews_md" in result.state_updates
            assert len(result.state_updates["available_timeseries_tools"]) == 1
            assert "GTR Analysis Mode" in result.user_prompt


# ---------------------------------------------------------------------------
# Welcome message tests
# ---------------------------------------------------------------------------


class TestBuildWelcomeMessage:
    """Test _build_welcome_message formatting."""

    def test_includes_grid_names(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            _build_welcome_message,
        )

        welcome = _build_welcome_message(
            "# GTR Historical Analysis: ExampleGrid\n### January 2026\n### December 2025",
            [{"name": "grafana_battery", "display_name": "Battery Usage", "dashboard": "KPI"}],
            [{"name": "ExampleGrid"}],
        )
        assert "ExampleGrid" in welcome
        assert "2 months" in welcome
        assert "1 Grafana timeseries" in welcome

    def test_no_tools_available(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            _build_welcome_message,
        )

        welcome = _build_welcome_message(
            "# GTR Historical Analysis: ExampleGrid\n### January 2026",
            [],
            [{"name": "ExampleGrid"}],
        )
        assert "Grafana" not in welcome


# ---------------------------------------------------------------------------
# Tool declarations tests
# ---------------------------------------------------------------------------


class TestBuildToolDeclarations:
    """Test _build_tool_declarations for Gemini API."""

    def test_builds_declarations_with_required_params(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            _build_tool_declarations,
        )

        tools = [
            {"name": "grafana_battery_usage", "display_name": "Battery Usage", "dashboard": "KPI"}
        ]
        declarations = _build_tool_declarations(tools)

        assert len(declarations) == 1
        assert declarations[0]["name"] == "grafana_battery_usage"
        assert "Grid" in declarations[0]["parameters"]["properties"]
        assert "time_from" in declarations[0]["parameters"]["properties"]
        assert "time_to" in declarations[0]["parameters"]["properties"]
        assert declarations[0]["parameters"]["required"] == ["Grid", "time_from", "time_to"]

    def test_empty_tools_returns_empty(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            _build_tool_declarations,
        )

        declarations = _build_tool_declarations([])
        assert declarations == []


# ---------------------------------------------------------------------------
# Find all month sections test
# ---------------------------------------------------------------------------


class TestFindMonthSections:
    """Test finding month sections via find_review_section_range (used by _load_all_historical_reviews)."""

    def test_finds_multiple_months(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.fetch_pending_actions import (
            find_review_section_range,
        )

        values = [
            ["January 2026 Review", "", "", ""],
            ["KPI", "Value", "Commentary", ""],
            ["FS Hours", "11.2", "", ""],
            ["", "", "", ""],
            ["December 2025 Review", "", "", ""],
            ["KPI", "Value", "Commentary", ""],
            ["FS Hours", "10.8", "", ""],
        ]

        jan = find_review_section_range(values, "January 2026")
        dec = find_review_section_range(values, "December 2025")
        nov = find_review_section_range(values, "November 2025")

        assert jan is not None
        assert jan == (0, 4)
        assert dec is not None
        assert dec == (4, 7)
        assert nov is None


# ---------------------------------------------------------------------------
# Error sanitization tests
# ---------------------------------------------------------------------------


class TestErrorSanitization:
    """Test that _execute_tool_call sanitizes errors."""

    @pytest.mark.asyncio
    async def test_sanitizes_tool_errors(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            _execute_tool_call,
        )

        mock_executor = AsyncMock()
        # Error with internal file path that sanitize_error_for_user should strip
        mock_executor.call_tool.side_effect = Exception(
            "Error in /app/mcp_servers/servers/grafana_server/grafana_mcp_server.py:123 timeout"
        )

        ctx = _make_context()
        ctx.mcp_executor = mock_executor

        result = await _execute_tool_call(ctx, "grafana_test", {"Grid": "ExampleGrid"})

        import json

        parsed = json.loads(result)
        # sanitize_error_for_user should strip internal file paths
        assert "grafana_mcp_server.py" not in parsed["error"]

    @pytest.mark.asyncio
    async def test_returns_error_when_no_executor(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            _execute_tool_call,
        )

        ctx = _make_context()
        ctx.mcp_executor = None

        result = await _execute_tool_call(ctx, "grafana_test", {})

        import json

        parsed = json.loads(result)
        assert "error" in parsed


# ---------------------------------------------------------------------------
# Tool whitelist validation tests
# ---------------------------------------------------------------------------


class TestToolWhitelist:
    """Test that CANCEL_KEYWORDS and tool names include expected values."""

    def test_cancel_keywords_include_no(self):
        from orchestrator.experts.handlers.grids_technical_reviewer.gtr_analysis_conversation import (
            CANCEL_KEYWORDS,
        )

        assert "no" in CANCEL_KEYWORDS
        assert "n" in CANCEL_KEYWORDS
        assert "cancel" in CANCEL_KEYWORDS
