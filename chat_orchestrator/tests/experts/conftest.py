"""Pytest fixtures for expert system tests."""

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_user_context():
    """Create mock UserContext."""
    ctx = MagicMock()
    ctx.user_email = "test@example.com"
    ctx.organization_ids = ["1"]
    ctx.is_staff = False
    return ctx


@pytest.fixture
def mock_settings():
    """Create mock AppSettings."""
    settings = MagicMock()
    settings.google_api_key = "test_api_key"  # pragma: allowlist secret
    settings.gemini = MagicMock()
    return settings


@pytest.fixture
def base_packet() -> Dict[str, Any]:
    """Create base packet for testing."""
    return {
        "id": "uuid-123",
        "packet_id": "grid_analysis_20260120_abc123",
        "packet_type": "grid_analysis",
        "packet_title": "Grid Analysis: ExampleGrid",
        "packet_goal": "Analyze ExampleGrid grid performance",
        "assigned_expert": "grid_analyst",
        "packet_status": "pending",
        "current_step": None,
        "steps_completed": [],
        "packet_inputs": {
            "grid": {"grid_name": "ExampleGrid", "grid_id": 123},
            "time_range": {
                "start_date": "2026-01-01T00:00:00Z",
                "end_date": "2026-01-20T00:00:00Z",
            },
        },
        "packet_state": {},
        "packet_outputs": {},
        "organization_id": 1,
        "requested_by_email": "test@example.com",
        "requested_in_session": "session_abc",
        "sessions_involved": ["session_abc"],
        "created_at": "2026-01-20T10:00:00Z",
        "updated_at": "2026-01-20T10:00:00Z",
    }


@pytest.fixture
def mock_expert_config():
    """Create mock ExpertConfig."""
    config = MagicMock()
    config.expert_id = "grid_analyst"
    config.display_name = "Grid Analyst"
    config.system_instructions = "You are the Grid Analyst expert."
    config.tools = ["grafana_query", "vrm_status", "jira_search"]
    config.packet_types = ["grid_analysis", "kpi_report"]
    config.workflows = {
        "grid_analysis": [
            "1. [llm] understand_request - Parse user intent",
            "2. [function:fetch_month_metrics] - Get metrics from Grafana",
            "3. [llm] synthesize_findings - Combine results",
        ],
        "kpi_report": [
            "1. [llm] parse_request - Identify grids and time range",
            "2. [function:fetch_multi_grid_metrics] - Get all grid data",
            "3. [function:calculate_kpi_values] - Compute KPIs",
            "4. [llm] generate_report - Write report",
        ],
    }
    config.capabilities = ["tool_access", "external_docs"]
    config.raw_sections = {}

    def get_workflow(packet_type):
        return config.workflows.get(packet_type)

    config.get_workflow = get_workflow
    return config
