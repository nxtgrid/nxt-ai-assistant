"""Tests for expert_router LangGraph node.

Tests routing decisions based on active packets and expert commands.
"""

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.graphs.nodes.expert_router import (
    EXPERT_COMMANDS,
    expert_router,
    parse_expert_command,
)


class TestExpertCommands:
    """Test EXPERT_COMMANDS mapping."""

    def test_analyze_command_maps_to_grid_analysis(self):
        """'/analyze' maps to grid_analysis packet type."""
        assert EXPERT_COMMANDS["/analyze"] == "grid_analysis"
        assert EXPERT_COMMANDS["/analyse"] == "grid_analysis"

    def test_kpi_command_maps_to_kpi_report(self):
        """'/kpi' maps to kpi_report packet type."""
        assert EXPERT_COMMANDS["/kpi"] == "kpi_report"
        assert EXPERT_COMMANDS["/report"] == "kpi_report"


class TestParseExpertCommand:
    """Test parse_expert_command helper function."""

    def test_parse_analyze_with_grid(self):
        """Parse '/analyze grid ExampleGrid' command."""
        result = parse_expert_command("/analyze grid ExampleGrid")
        assert result["command"] == "/analyze"
        assert result["packet_type"] == "grid_analysis"
        assert result["raw_args"] == "grid ExampleGrid"

    def test_parse_kpi_weekly(self):
        """Parse '/kpi weekly' command."""
        result = parse_expert_command("/kpi weekly")
        assert result["command"] == "/kpi"
        assert result["packet_type"] == "kpi_report"
        assert result["raw_args"] == "weekly"

    def test_parse_command_only(self):
        """Parse command without arguments."""
        result = parse_expert_command("/analyze")
        assert result["command"] == "/analyze"
        assert result["packet_type"] == "grid_analysis"
        assert result["raw_args"] == ""

    def test_parse_unknown_command(self):
        """Unknown command returns None for packet_type."""
        result = parse_expert_command("/unknown something")
        assert result["command"] == "/unknown"
        assert result["packet_type"] is None

    def test_parse_preserves_case_in_args(self):
        """Arguments preserve original case."""
        result = parse_expert_command("/analyze Grid BELEL")
        assert result["raw_args"] == "Grid BELEL"

    def test_parse_handles_extra_whitespace(self):
        """Parser handles extra whitespace."""
        result = parse_expert_command("  /analyze   grid ExampleGrid  ")
        assert result["command"] == "/analyze"
        assert result["raw_args"] == "grid ExampleGrid"


class TestExpertRouter:
    """Test expert_router node function."""

    @pytest.fixture
    def base_state(self) -> Dict[str, Any]:
        """Create base state for testing."""
        return {
            "session_id": "session_abc123",
            "user_input": "Hello, how are you?",
            "user_context": MagicMock(
                user_email="test@example.com",
                organization_ids=["1"],
            ),
        }

    @pytest.fixture
    def mock_pending_decision_service(self):
        """Mock PendingDecisionService that returns no pending decisions."""
        with patch("orchestrator.graphs.nodes.expert_router.PendingDecisionService") as mock_class:
            mock_service = MagicMock()
            mock_service.get_pending_decision = AsyncMock(return_value=None)
            mock_class.return_value = mock_service
            yield mock_service

    @pytest.mark.asyncio
    async def test_no_expert_routing_for_normal_message(
        self, base_state, mock_pending_decision_service
    ):
        """Normal messages don't trigger expert routing."""
        with patch(
            "orchestrator.graphs.nodes.expert_router.WorkPacketService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.get_active_packets_for_session = AsyncMock(return_value=[])
            mock_service_class.return_value = mock_service

            result = await expert_router(base_state)

        assert result["expert_routing_decision"] == "continue"
        assert result["active_work_packet"] is None
        assert result["matched_expert_id"] is None

    @pytest.mark.asyncio
    async def test_routes_to_expert_for_active_packet(
        self, base_state, mock_pending_decision_service
    ):
        """Routes to expert when active packet exists."""
        active_packet = {
            "packet_id": "grid_analysis_20260120",
            "packet_status": "in_progress",
            "assigned_expert": "grid_analyst",
        }

        with patch(
            "orchestrator.graphs.nodes.expert_router.WorkPacketService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.get_active_packets_for_session = AsyncMock(return_value=[active_packet])
            mock_service_class.return_value = mock_service

            result = await expert_router(base_state)

        assert result["expert_routing_decision"] == "expert"
        assert result["active_work_packet"] == active_packet
        assert result["matched_expert_id"] == "grid_analyst"

    @pytest.mark.asyncio
    async def test_routes_to_expert_for_analyze_command(
        self, base_state, mock_pending_decision_service
    ):
        """Routes to expert when /analyze command is used."""
        base_state["user_input"] = "/analyze grid ExampleGrid last 7 days"

        with (
            patch(
                "orchestrator.graphs.nodes.expert_router.WorkPacketService"
            ) as mock_service_class,
            patch(
                "orchestrator.graphs.nodes.expert_router.ExpertInstructionsProvider"
            ) as mock_provider_class,
        ):
            mock_service = MagicMock()
            mock_service.get_active_packets_for_session = AsyncMock(return_value=[])
            mock_service.get_resumable_packets_for_session = AsyncMock(return_value=[])
            mock_service.find_similar_completed = AsyncMock(return_value=[])
            mock_service_class.return_value = mock_service

            mock_provider = MagicMock()
            mock_provider.get_expert_for_packet_type = AsyncMock(return_value="grid_analyst")
            mock_provider_class.return_value = mock_provider

            result = await expert_router(base_state)

        assert result["expert_routing_decision"] == "expert"
        assert result["matched_expert_id"] == "grid_analyst"
        # expert_command stores the FULL user input, not just the command
        assert result["expert_command"] == "/analyze grid ExampleGrid last 7 days"
        assert result["expert_packet_type"] == "grid_analysis"

    @pytest.mark.asyncio
    async def test_routes_to_expert_for_kpi_command(
        self, base_state, mock_pending_decision_service
    ):
        """Routes to expert when /kpi command is used."""
        base_state["user_input"] = "/kpi weekly"

        with (
            patch(
                "orchestrator.graphs.nodes.expert_router.WorkPacketService"
            ) as mock_service_class,
            patch(
                "orchestrator.graphs.nodes.expert_router.ExpertInstructionsProvider"
            ) as mock_provider_class,
        ):
            mock_service = MagicMock()
            mock_service.get_active_packets_for_session = AsyncMock(return_value=[])
            mock_service.get_resumable_packets_for_session = AsyncMock(return_value=[])
            mock_service.find_similar_completed = AsyncMock(return_value=[])
            mock_service_class.return_value = mock_service

            mock_provider = MagicMock()
            mock_provider.get_expert_for_packet_type = AsyncMock(return_value="kpi_reporter")
            mock_provider_class.return_value = mock_provider

            result = await expert_router(base_state)

        assert result["expert_routing_decision"] == "expert"
        assert result["matched_expert_id"] == "kpi_reporter"
        assert result["expert_packet_type"] == "kpi_report"

    @pytest.mark.asyncio
    async def test_no_routing_without_session_id(self, base_state):
        """No expert routing when session_id is missing."""
        base_state["session_id"] = None

        result = await expert_router(base_state)

        assert result["expert_routing_decision"] == "continue"

    @pytest.mark.asyncio
    async def test_continues_on_error(self, base_state, mock_pending_decision_service):
        """Returns 'continue' on error (fail-safe)."""
        with patch(
            "orchestrator.graphs.nodes.expert_router.WorkPacketService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.get_active_packets_for_session = AsyncMock(
                side_effect=Exception("Database error")
            )
            mock_service_class.return_value = mock_service

            result = await expert_router(base_state)

        assert result["expert_routing_decision"] == "continue"

    @pytest.mark.asyncio
    async def test_new_command_overrides_active_packet(
        self, base_state, mock_pending_decision_service
    ):
        """New command overrides active packet (routes as new command)."""
        base_state["user_input"] = "/analyze different grid"

        active_packet = {
            "packet_id": "existing_packet",
            "packet_status": "in_progress",
            "assigned_expert": "grid_analyst",
        }

        with (
            patch(
                "orchestrator.graphs.nodes.expert_router.WorkPacketService"
            ) as mock_service_class,
            patch(
                "orchestrator.graphs.nodes.expert_router.ExpertInstructionsProvider"
            ) as mock_provider_class,
        ):
            mock_service = MagicMock()
            mock_service.get_active_packets_for_session = AsyncMock(return_value=[active_packet])
            mock_service.get_resumable_packets_for_session = AsyncMock(return_value=[])
            mock_service.find_similar_completed = AsyncMock(return_value=[])
            mock_service_class.return_value = mock_service

            mock_provider = MagicMock()
            mock_provider.get_expert_for_packet_type = AsyncMock(return_value="grid_analyst")
            mock_provider_class.return_value = mock_provider

            result = await expert_router(base_state)

        # New command overrides active packet
        assert result["expert_routing_decision"] == "expert"
        assert result["expert_command"] == "/analyze different grid"
        assert result["expert_packet_type"] == "grid_analysis"


class TestPendingDecisions:
    """Test pending decision handling (Check -1)."""

    @pytest.fixture
    def base_state(self) -> Dict[str, Any]:
        """Create base state for testing."""
        return {
            "session_id": "session_abc123",
            "user_input": "2",  # User choosing option 2
            "user_context": MagicMock(
                user_email="test@example.com",
                organization_ids=["1"],
            ),
        }

    @pytest.mark.asyncio
    async def test_handles_duplicate_decision_cancel(self, base_state):
        """User choosing '2' for non-resumable duplicate decision cancels.

        New behavior: 1 = run_new, 2 = cancel (for non-resumable).
        """
        # base_state already has user_input = "2"
        pending_decision = {
            "id": "decision-123",
            "decision_type": "duplicate",
            "context": {
                "similar_work_packet": {
                    "packet_id": "lpp_123",
                    "external_url": "https://example.com/report",
                    "packet_outputs": {"summary": "Existing report summary"},
                },
                "matched_expert_id": "lpp_expert",
                "expert_command": "/lpp ExampleGrid",
                "expert_packet_type": "light_preliminary_package",
                "expert_key_entity": "ExampleGrid",
                "is_resumable": False,  # Non-resumable: 2 = cancel
            },
            "prompt": "Would you like to run new or cancel?",
        }

        with patch(
            "orchestrator.graphs.nodes.expert_router.PendingDecisionService"
        ) as mock_decision_class:
            mock_decision_service = MagicMock()
            mock_decision_service.get_pending_decision = AsyncMock(return_value=pending_decision)
            mock_decision_service.resolve_decision = AsyncMock()
            mock_decision_class.return_value = mock_decision_service

            result = await expert_router(base_state)

        # Should cancel and return to normal flow
        assert result["expert_routing_decision"] == "continue"
        assert "what would you like to do" in result["final_response"].lower()
        assert result["awaiting_duplicate_decision"] is False

        # Should have resolved the decision as "cancel"
        mock_decision_service.resolve_decision.assert_called_once_with("decision-123", "cancel")

    @pytest.mark.asyncio
    async def test_handles_duplicate_decision_run_new_option1(self, base_state):
        """User choosing '1' for duplicate decision runs new analysis.

        New behavior: 1 = run_new, 2 = cancel (for non-resumable).
        """
        base_state["user_input"] = "1"  # Run new

        pending_decision = {
            "id": "decision-123",
            "decision_type": "duplicate",
            "context": {
                "similar_work_packet": {
                    "packet_id": "lpp_123",
                    "external_url": "https://example.com/report",
                    "packet_outputs": {"summary": "Existing report summary"},
                },
                "matched_expert_id": "lpp_expert",
                "expert_command": "/lpp ExampleGrid",
                "expert_packet_type": "light_preliminary_package",
                "is_resumable": False,
            },
            "prompt": "Would you like to run new or cancel?",
        }

        with patch(
            "orchestrator.graphs.nodes.expert_router.PendingDecisionService"
        ) as mock_decision_class:
            mock_decision_service = MagicMock()
            mock_decision_service.get_pending_decision = AsyncMock(return_value=pending_decision)
            mock_decision_service.resolve_decision = AsyncMock()
            mock_decision_class.return_value = mock_decision_service

            result = await expert_router(base_state)

        # Should route to expert (run new)
        assert result["expert_routing_decision"] == "expert"
        assert result["matched_expert_id"] == "lpp_expert"
        assert result["awaiting_duplicate_decision"] is False
        mock_decision_service.resolve_decision.assert_called_once_with("decision-123", "run_new")

    @pytest.mark.asyncio
    async def test_reprompts_on_invalid_duplicate_decision(self, base_state):
        """Invalid response to duplicate decision shows re-prompt."""
        base_state["user_input"] = "maybe?"  # Invalid response

        pending_decision = {
            "id": "decision-123",
            "decision_type": "duplicate",
            "context": {
                "similar_work_packet": {"packet_id": "lpp_123"},
                "matched_expert_id": "lpp_expert",
            },
            "prompt": "Would you like to view existing or run new?",
        }

        with patch(
            "orchestrator.graphs.nodes.expert_router.PendingDecisionService"
        ) as mock_decision_class:
            mock_decision_service = MagicMock()
            mock_decision_service.get_pending_decision = AsyncMock(return_value=pending_decision)
            mock_decision_class.return_value = mock_decision_service

            result = await expert_router(base_state)

        # Should re-prompt
        assert result["expert_routing_decision"] == "continue"
        assert "didn't understand" in result["final_response"].lower()

    @pytest.mark.asyncio
    async def test_pending_decision_takes_priority(self, base_state):
        """Pending decision is checked before active packets (Check -1 before Check 0)."""
        # Override to use "1" (run_new) instead of default "2"
        base_state["user_input"] = "1"

        pending_decision = {
            "id": "decision-123",
            "decision_type": "duplicate",
            "context": {
                "similar_work_packet": {"packet_id": "lpp_123"},
                "matched_expert_id": "lpp_expert",
                "expert_command": "/lpp Test",
                "expert_packet_type": "light_preliminary_package",
                "is_resumable": False,
            },
            "prompt": "Prompt",
        }

        with (
            patch(
                "orchestrator.graphs.nodes.expert_router.PendingDecisionService"
            ) as mock_decision_class,
            patch("orchestrator.graphs.nodes.expert_router.WorkPacketService") as mock_packet_class,
        ):
            mock_decision_service = MagicMock()
            mock_decision_service.get_pending_decision = AsyncMock(return_value=pending_decision)
            mock_decision_service.resolve_decision = AsyncMock()
            mock_decision_class.return_value = mock_decision_service

            # Active packet exists, but pending decision should take priority
            mock_packet_service = MagicMock()
            mock_packet_service.get_active_packets_for_session = AsyncMock(
                return_value=[{"packet_id": "other_packet"}]
            )
            mock_packet_class.return_value = mock_packet_service

            result = await expert_router(base_state)

        # Pending decision should be handled, not the active packet
        assert result["matched_expert_id"] == "lpp_expert"
        mock_decision_service.resolve_decision.assert_called_once()
