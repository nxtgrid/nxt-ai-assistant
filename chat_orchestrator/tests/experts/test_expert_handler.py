"""Tests for expert_handler LangGraph node.

Tests packet creation, resumption, and workflow execution.
"""

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.models.schemas import UserContext


def test_build_tool_executor_metadata_uses_current_user_context():
    from orchestrator.graphs.nodes.expert_handler import _build_tool_executor_metadata

    user_context = UserContext(
        user_id="1570892239",
        user_email="requester@example.com",
        username="Requester",
        source="telegram",
        chat_id="1570892239",
        organization_ids=["2"],
        organization_name="Staff",
    )

    metadata = _build_tool_executor_metadata(
        state={
            "session_id": "telegram_session",
            "thread_id": "thr_123",
            "user_context": user_context,
            "user_permissions": {
                "organization_ids": ["2"],
                "email": "requester@example.com",
                "is_staff": True,
            },
        },
        packet={"requested_by_email": "packet@example.com", "organization_id": 99},
    )

    assert metadata["user_email"] == "requester@example.com"
    assert metadata["user_name"] == "Requester"
    assert metadata["original_chat_id"] == "1570892239"
    assert metadata["session_id"] == "telegram_session"
    assert metadata["thread_id"] == "thr_123"
    assert metadata["organization_id"] == 2
    assert metadata["organization_name"] == "Staff"
    assert metadata["user_permissions"]["organization_ids"] == ["2"]


def test_build_tool_executor_metadata_falls_back_to_packet_requester():
    from orchestrator.graphs.nodes.expert_handler import _build_tool_executor_metadata

    metadata = _build_tool_executor_metadata(
        state={"session_id": "telegram_session"},
        packet={"requested_by_email": "packet@example.com", "organization_id": 2},
    )

    assert metadata["user_email"] == "packet@example.com"
    assert metadata["organization_id"] == 2
    assert metadata["user_permissions"]["organization_ids"] == ["2"]


class TestExpertHandler:
    """Test expert_handler node function."""

    @pytest.fixture
    def base_state(self) -> Dict[str, Any]:
        """Create base state for testing."""
        mock_settings = MagicMock()
        mock_settings.google_api_key = "test_api_key"  # pragma: allowlist secret
        mock_settings.gemini = MagicMock()

        return {
            "session_id": "session_abc123",
            "user_input": "/analyze grid ExampleGrid",
            "user_context": MagicMock(
                user_email="test@example.com",
                organization_ids=["1"],
            ),
            "matched_expert_id": "grid_analyst",
            "active_work_packet": None,
            "expert_command": "/analyze",
            "expert_packet_type": "grid_analysis",
            "settings": mock_settings,
            "tool_executor": MagicMock(),
        }

    @pytest.fixture
    def mock_expert_config(self):
        """Create mock expert configuration."""
        config = MagicMock()
        config.expert_id = "grid_analyst"
        config.display_name = "Grid Analyst"
        config.system_instructions = "You are a grid analyst."
        config.tools = ["grafana_query", "vrm_status"]
        config.packet_types = ["grid_analysis"]

        def get_workflow(packet_type):
            if packet_type == "grid_analysis":
                return ["1. [llm] analyze - Analyze the grid"]
            return None

        config.get_workflow = get_workflow
        return config

    @pytest.mark.asyncio
    async def test_returns_error_without_expert_id(self, base_state):
        """Returns error when no expert_id is matched."""
        base_state["matched_expert_id"] = None

        from orchestrator.graphs.nodes.expert_handler import expert_handler

        result = await expert_handler(base_state)

        assert "expert_error" in result
        assert result["expert_error"] == "No expert matched"
        assert "couldn't determine" in result["final_response"]

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_expert(self, base_state):
        """Returns error when expert config not found."""
        base_state["matched_expert_id"] = "unknown_expert"

        with patch(
            "orchestrator.graphs.nodes.expert_handler.ExpertInstructionsProvider"
        ) as mock_provider_class:
            mock_provider = MagicMock()
            mock_provider.get_expert_config = AsyncMock(return_value=None)
            mock_provider_class.return_value = mock_provider

            from orchestrator.graphs.nodes.expert_handler import expert_handler

            result = await expert_handler(base_state)

        assert "expert_error" in result
        assert "not found" in result["expert_error"]

    @pytest.mark.asyncio
    async def test_creates_new_packet_when_none_exists(self, base_state, mock_expert_config):
        """Creates new packet when active_work_packet is None."""
        created_packet = {
            "packet_id": "grid_analysis_20260120_abc123",
            "packet_type": "grid_analysis",
            "packet_status": "in_progress",
            "packet_goal": "/analyze grid ExampleGrid",
            "packet_inputs": {"raw_request": "/analyze grid ExampleGrid"},
            "packet_state": {},
            "steps_completed": [],
            "current_step": "analyze",
            "requested_by_email": "test@example.com",
            "organization_id": 1,
        }

        with (
            patch(
                "orchestrator.graphs.nodes.expert_handler.ExpertInstructionsProvider"
            ) as mock_provider_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkPacketService"
            ) as mock_service_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.create_chat_llm_client"
            ) as mock_create_llm_client,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkflowExecutor"
            ) as mock_executor_class,
        ):
            # Set up mocks
            mock_provider = MagicMock()
            mock_provider.get_expert_config = AsyncMock(return_value=mock_expert_config)
            mock_provider_class.return_value = mock_provider

            mock_service = MagicMock()
            mock_service.create_packet = AsyncMock(return_value=created_packet)
            mock_service.start_packet = AsyncMock(return_value=created_packet)
            mock_service.get_packet = AsyncMock(return_value=created_packet)
            mock_service.complete_packet = AsyncMock(return_value=created_packet)
            mock_service.cancel_active_packets_of_type = AsyncMock(return_value=0)
            mock_service_class.return_value = mock_service

            mock_gemini = MagicMock()
            mock_create_llm_client.return_value = mock_gemini

            mock_executor = MagicMock()
            mock_executor.execute_workflow = AsyncMock(
                return_value=("Analysis complete", {"accumulated_results": {}})
            )
            mock_executor.parse_workflow = MagicMock(return_value=[])
            mock_executor_class.return_value = mock_executor

            from orchestrator.graphs.nodes.expert_handler import expert_handler

            result = await expert_handler(base_state)

        # Should have created a packet
        mock_service.create_packet.assert_called_once()
        assert result["expert_executed"] is True

    @pytest.mark.asyncio
    async def test_resumes_existing_packet(self, base_state, mock_expert_config):
        """Resumes existing packet when one is active."""
        existing_packet = {
            "packet_id": "existing_123",
            "packet_type": "grid_analysis",
            "packet_status": "in_progress",
            "packet_goal": "Analyze ExampleGrid",
            "packet_inputs": {},
            "packet_state": {},
            "steps_completed": ["step1"],
            "current_step": "step2",
            "requested_by_email": "original@example.com",
            "organization_id": 1,
        }
        base_state["active_work_packet"] = existing_packet

        with (
            patch(
                "orchestrator.graphs.nodes.expert_handler.ExpertInstructionsProvider"
            ) as mock_provider_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkPacketService"
            ) as mock_service_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.create_chat_llm_client"
            ) as mock_create_llm_client,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkflowExecutor"
            ) as mock_executor_class,
        ):
            mock_provider = MagicMock()
            mock_provider.get_expert_config = AsyncMock(return_value=mock_expert_config)
            mock_provider_class.return_value = mock_provider

            mock_service = MagicMock()
            mock_service.get_packet = AsyncMock(return_value=existing_packet)
            mock_service.complete_packet = AsyncMock(return_value=existing_packet)
            mock_service_class.return_value = mock_service

            mock_gemini = MagicMock()
            mock_create_llm_client.return_value = mock_gemini

            mock_executor = MagicMock()
            mock_executor.execute_workflow = AsyncMock(
                return_value=("Resumed and complete", {"accumulated_results": {}})
            )
            mock_executor_class.return_value = mock_executor

            from orchestrator.graphs.nodes.expert_handler import expert_handler

            result = await expert_handler(base_state)

        # Should NOT have created a new packet
        mock_service.create_packet.assert_not_called()
        assert "Resumed" in result["final_response"] or result["expert_executed"]

    @pytest.mark.asyncio
    async def test_resumes_from_awaiting_input(self, base_state, mock_expert_config):
        """Resumes packet that was awaiting user input."""
        awaiting_packet = {
            "packet_id": "awaiting_123",
            "packet_type": "grid_analysis",
            "packet_status": "awaiting_input",
            "packet_goal": "Analyze ExampleGrid",
            "packet_inputs": {},
            "packet_state": {"awaiting_user_input": True},
            "steps_completed": [],
            "current_step": "clarify",
            "requested_by_email": "test@example.com",
            "organization_id": 1,
        }
        base_state["active_work_packet"] = awaiting_packet
        base_state["user_input"] = "ExampleGrid"

        with (
            patch(
                "orchestrator.graphs.nodes.expert_handler.ExpertInstructionsProvider"
            ) as mock_provider_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkPacketService"
            ) as mock_service_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.create_chat_llm_client"
            ) as mock_create_llm_client,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkflowExecutor"
            ) as mock_executor_class,
        ):
            mock_provider = MagicMock()
            mock_provider.get_expert_config = AsyncMock(return_value=mock_expert_config)
            mock_provider_class.return_value = mock_provider

            mock_service = MagicMock()
            mock_service.resume_from_input = AsyncMock(
                return_value={**awaiting_packet, "packet_status": "in_progress"}
            )
            mock_service.get_packet = AsyncMock(return_value=awaiting_packet)
            mock_service.complete_packet = AsyncMock(return_value=awaiting_packet)
            mock_service.cancel_active_packets_of_type = AsyncMock(return_value=0)
            mock_service_class.return_value = mock_service

            mock_gemini = MagicMock()
            mock_create_llm_client.return_value = mock_gemini

            mock_executor = MagicMock()
            mock_executor.execute_workflow = AsyncMock(
                return_value=("Completed after input", {"accumulated_results": {}})
            )
            mock_executor_class.return_value = mock_executor

            from orchestrator.graphs.nodes.expert_handler import expert_handler

            await expert_handler(base_state)

        # Should have called resume_from_input
        mock_service.resume_from_input.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_workflow_needing_input(self, base_state, mock_expert_config):
        """Returns awaiting_input state when workflow needs user input."""
        created_packet = {
            "packet_id": "new_123",
            "packet_type": "grid_analysis",
            "packet_status": "in_progress",
            "packet_goal": "/analyze",
            "packet_inputs": {},
            "packet_state": {},
            "steps_completed": [],
            "current_step": "clarify",
            "requested_by_email": "test@example.com",
            "organization_id": 1,
        }

        with (
            patch(
                "orchestrator.graphs.nodes.expert_handler.ExpertInstructionsProvider"
            ) as mock_provider_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkPacketService"
            ) as mock_service_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.create_chat_llm_client"
            ) as mock_create_llm_client,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkflowExecutor"
            ) as mock_executor_class,
        ):
            mock_provider = MagicMock()
            mock_provider.get_expert_config = AsyncMock(return_value=mock_expert_config)
            mock_provider_class.return_value = mock_provider

            mock_service = MagicMock()
            mock_service.create_packet = AsyncMock(return_value=created_packet)
            mock_service.start_packet = AsyncMock(return_value=created_packet)
            mock_service.cancel_active_packets_of_type = AsyncMock(return_value=0)
            mock_service_class.return_value = mock_service

            mock_gemini = MagicMock()
            mock_create_llm_client.return_value = mock_gemini

            mock_executor = MagicMock()
            mock_executor.execute_workflow = AsyncMock(
                return_value=(
                    "Which grid do you want me to analyze?",
                    {"needs_user_input": True},
                )
            )
            mock_executor.parse_workflow = MagicMock(return_value=[])
            mock_executor_class.return_value = mock_executor

            from orchestrator.graphs.nodes.expert_handler import expert_handler

            result = await expert_handler(base_state)

        assert result["expert_awaiting_input"] is True
        assert result["expert_executed"] is False
        assert "Which grid" in result["final_response"]

    @pytest.mark.asyncio
    async def test_handles_workflow_error(self, base_state, mock_expert_config):
        """Handles workflow execution errors gracefully."""
        created_packet = {
            "packet_id": "error_123",
            "packet_type": "grid_analysis",
            "packet_status": "in_progress",
            "packet_goal": "/analyze",
            "packet_inputs": {},
            "packet_state": {},
            "steps_completed": [],
            "current_step": "execute",
            "requested_by_email": "test@example.com",
            "organization_id": 1,
        }

        with (
            patch(
                "orchestrator.graphs.nodes.expert_handler.ExpertInstructionsProvider"
            ) as mock_provider_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkPacketService"
            ) as mock_service_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.create_chat_llm_client"
            ) as mock_create_llm_client,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkflowExecutor"
            ) as mock_executor_class,
        ):
            mock_provider = MagicMock()
            mock_provider.get_expert_config = AsyncMock(return_value=mock_expert_config)
            mock_provider_class.return_value = mock_provider

            mock_service = MagicMock()
            mock_service.create_packet = AsyncMock(return_value=created_packet)
            mock_service.start_packet = AsyncMock(return_value=created_packet)
            mock_service.fail_packet = AsyncMock(return_value=created_packet)
            mock_service.cancel_active_packets_of_type = AsyncMock(return_value=0)
            mock_service_class.return_value = mock_service

            mock_gemini = MagicMock()
            mock_create_llm_client.return_value = mock_gemini

            mock_executor = MagicMock()
            mock_executor.execute_workflow = AsyncMock(
                side_effect=Exception("Tool execution failed")
            )
            mock_executor.parse_workflow = MagicMock(return_value=[])
            mock_executor_class.return_value = mock_executor

            from orchestrator.graphs.nodes.expert_handler import expert_handler

            result = await expert_handler(base_state)

        # Should have called fail_packet
        mock_service.fail_packet.assert_called_once()
        assert "expert_error" in result
        # The error response should either contain the original error or "issue"
        response_lower = result["final_response"].lower()
        assert "failed" in response_lower or "issue" in response_lower or "error" in response_lower

    @pytest.mark.asyncio
    async def test_workflow_fails_without_api_key(self, base_state, mock_expert_config):
        """Workflow fails gracefully when API key is not set."""
        base_state["settings"] = None

        created_packet = {
            "packet_id": "test_123",
            "packet_type": "grid_analysis",
            "packet_status": "in_progress",
            "packet_goal": "test",
            "packet_inputs": {},
            "packet_state": {},
            "steps_completed": [],
            "requested_by_email": "test@example.com",
            "organization_id": 1,
        }

        with (
            patch(
                "orchestrator.graphs.nodes.expert_handler.ExpertInstructionsProvider"
            ) as mock_provider_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkPacketService"
            ) as mock_service_class,
            patch(
                "orchestrator.graphs.nodes.expert_handler.create_chat_llm_client"
            ) as mock_create_llm_client,
            patch(
                "orchestrator.graphs.nodes.expert_handler.WorkflowExecutor"
            ) as mock_executor_class,
        ):
            mock_provider = MagicMock()
            mock_provider.get_expert_config = AsyncMock(return_value=mock_expert_config)
            mock_provider_class.return_value = mock_provider

            mock_service = MagicMock()
            mock_service.create_packet = AsyncMock(return_value=created_packet)
            mock_service.start_packet = AsyncMock(return_value=created_packet)
            mock_service.cancel_active_packets_of_type = AsyncMock(return_value=0)
            mock_service.fail_packet = AsyncMock(return_value=created_packet)
            mock_service_class.return_value = mock_service

            mock_gemini = MagicMock()
            mock_create_llm_client.return_value = mock_gemini

            mock_executor = MagicMock()
            mock_executor.execute_workflow = AsyncMock(
                side_effect=Exception("GOOGLE_API_KEY is not set")
            )
            mock_executor.parse_workflow = MagicMock(return_value=[])
            mock_executor_class.return_value = mock_executor

            from orchestrator.graphs.nodes.expert_handler import expert_handler

            result = await expert_handler(base_state)

        mock_service.fail_packet.assert_called_once()
        assert "expert_error" in result
        assert result["final_response"]  # Non-empty error response
