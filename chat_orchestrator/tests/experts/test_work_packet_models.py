"""Tests for work packet Pydantic models.

Tests validation, composition patterns, and schema registry.
"""

from datetime import datetime, timezone

import pytest

from orchestrator.models.work_packets import (
    PACKET_TYPE_SCHEMAS,
    ExternalDocRef,
    GridAnalysisInputs,
    GridAnalysisOutputs,
    GridAnalysisState,
    GridReference,
    KPIReportInputs,
    KPIReportState,
    PacketStatus,
    ProgressInfo,
    TimeRange,
    ToolCallRecord,
    validate_packet_data,
)


class TestComposableShapes:
    """Test the composable shape building blocks."""

    def test_grid_reference_minimal(self):
        """GridReference works with just grid_name."""
        ref = GridReference(grid_name="ExampleGrid")
        assert ref.grid_name == "ExampleGrid"
        assert ref.grid_id is None
        assert ref.site_id is None

    def test_grid_reference_full(self):
        """GridReference accepts all optional fields."""
        ref = GridReference(grid_name="ExampleGrid", grid_id=123, site_id="SITE-456")
        assert ref.grid_name == "ExampleGrid"
        assert ref.grid_id == 123
        assert ref.site_id == "SITE-456"

    def test_time_range_defaults(self):
        """TimeRange has default timezone."""
        now = datetime.now(timezone.utc)
        tr = TimeRange(start_date=now, end_date=now)
        import os

        assert tr.timezone == os.getenv("DEFAULT_TIMEZONE", "UTC")

    def test_time_range_custom_timezone(self):
        """TimeRange accepts custom timezone."""
        now = datetime.now(timezone.utc)
        tr = TimeRange(start_date=now, end_date=now, timezone="UTC")
        assert tr.timezone == "UTC"

    def test_progress_info_defaults(self):
        """ProgressInfo has sensible defaults."""
        progress = ProgressInfo()
        assert progress.percent_complete == 0
        assert progress.current_action is None
        assert progress.steps_total == 0
        assert progress.steps_done == 0

    def test_external_doc_ref(self):
        """ExternalDocRef captures document reference."""
        ref = ExternalDocRef(
            system="google_docs",
            doc_id="abc123",
            url="https://docs.google.com/abc123",
            version="1",
        )
        assert ref.system == "google_docs"
        assert ref.doc_id == "abc123"

    def test_tool_call_record_auto_timestamp(self):
        """ToolCallRecord auto-generates timestamp."""
        record = ToolCallRecord(
            tool_name="grafana_query",
            arguments={"grid": "ExampleGrid"},
            result_summary="Success",
        )
        assert record.tool_name == "grafana_query"
        assert record.called_at is not None


class TestPacketStatus:
    """Test PacketStatus enum."""

    def test_all_statuses_exist(self):
        """All expected statuses are defined."""
        statuses = [s.value for s in PacketStatus]
        assert "pending" in statuses
        assert "in_progress" in statuses
        assert "blocked" in statuses
        assert "awaiting_input" in statuses
        assert "completed" in statuses
        assert "failed" in statuses
        assert "cancelled" in statuses

    def test_status_is_string_enum(self):
        """PacketStatus values are strings."""
        assert PacketStatus.PENDING == "pending"
        assert PacketStatus.COMPLETED == "completed"


class TestGridAnalysisModels:
    """Test grid_analysis packet type models."""

    def test_grid_analysis_inputs_minimal(self):
        """GridAnalysisInputs works with required fields only."""
        now = datetime.now(timezone.utc)
        inputs = GridAnalysisInputs(
            grid=GridReference(grid_name="ExampleGrid"),
            time_range=TimeRange(start_date=now, end_date=now),
        )
        assert inputs.grid.grid_name == "ExampleGrid"
        # analysis_focus defaults to "all" per model definition
        assert inputs.analysis_focus == "all"
        assert inputs.include_comparisons is False

    def test_grid_analysis_inputs_full(self):
        """GridAnalysisInputs accepts all optional fields."""
        now = datetime.now(timezone.utc)
        inputs = GridAnalysisInputs(
            grid=GridReference(grid_name="GridB", grid_id=42),
            time_range=TimeRange(start_date=now, end_date=now),
            analysis_focus="battery",
            include_comparisons=True,
        )
        assert inputs.analysis_focus == "battery"
        assert inputs.include_comparisons is True

    def test_grid_analysis_state_defaults(self):
        """GridAnalysisState has proper defaults."""
        state = GridAnalysisState()
        assert state.progress.percent_complete == 0
        assert state.metrics_fetched is False
        assert state.alerts_fetched is False
        assert state.faults_analyzed is False
        assert state.key_findings == []
        assert state.tool_calls == []

    def test_grid_analysis_outputs(self):
        """GridAnalysisOutputs validates required fields."""
        outputs = GridAnalysisOutputs(
            summary="Grid performing well",
            findings=["Battery SOC stable", "Solar output normal"],
            recommendations=["Continue monitoring"],
        )
        assert outputs.summary == "Grid performing well"
        assert len(outputs.findings) == 2
        assert len(outputs.recommendations) == 1


class TestKPIReportModels:
    """Test kpi_report packet type models."""

    def test_kpi_report_inputs_multiple_grids(self):
        """KPIReportInputs supports multiple grids."""
        now = datetime.now(timezone.utc)
        inputs = KPIReportInputs(
            grids=[
                GridReference(grid_name="ExampleGrid"),
                GridReference(grid_name="GridB"),
            ],
            time_range=TimeRange(start_date=now, end_date=now),
        )
        assert len(inputs.grids) == 2
        assert inputs.report_type == "weekly"

    def test_kpi_report_inputs_custom_sections(self):
        """KPIReportInputs allows custom sections."""
        now = datetime.now(timezone.utc)
        inputs = KPIReportInputs(
            grids=[GridReference(grid_name="ExampleGrid")],
            time_range=TimeRange(start_date=now, end_date=now),
            report_type="monthly",
            sections_requested=["overview", "efficiency"],
        )
        assert inputs.report_type == "monthly"
        assert "efficiency" in inputs.sections_requested

    def test_kpi_report_state_tracks_processed_grids(self):
        """KPIReportState tracks which grids have been processed."""
        state = KPIReportState()
        assert state.grids_processed == []
        assert state.sections_completed == []


class TestPacketTypeRegistry:
    """Test the packet type schema registry."""

    def test_registry_has_grid_analysis(self):
        """Registry contains grid_analysis schemas."""
        assert "grid_analysis" in PACKET_TYPE_SCHEMAS
        schemas = PACKET_TYPE_SCHEMAS["grid_analysis"]
        assert "inputs" in schemas
        assert "state" in schemas
        assert "outputs" in schemas

    def test_registry_has_kpi_report(self):
        """Registry contains kpi_report schemas."""
        assert "kpi_report" in PACKET_TYPE_SCHEMAS


class TestValidatePacketData:
    """Test the validate_packet_data helper function."""

    def test_validate_valid_inputs(self):
        """validate_packet_data accepts valid data."""
        now = datetime.now(timezone.utc)
        data = {
            "grid": {"grid_name": "ExampleGrid"},
            "time_range": {"start_date": now.isoformat(), "end_date": now.isoformat()},
        }
        result = validate_packet_data("grid_analysis", "inputs", data)
        assert result.grid.grid_name == "ExampleGrid"

    def test_validate_unknown_packet_type(self):
        """validate_packet_data raises for unknown packet type."""
        with pytest.raises(ValueError, match="Unknown packet type"):
            validate_packet_data("unknown_type", "inputs", {})

    def test_validate_unknown_field(self):
        """validate_packet_data raises for unknown field."""
        with pytest.raises(ValueError, match="No schema for"):
            validate_packet_data("grid_analysis", "unknown_field", {})

    def test_validate_invalid_data(self):
        """validate_packet_data raises for invalid data."""
        with pytest.raises(Exception):  # Pydantic ValidationError
            validate_packet_data("grid_analysis", "inputs", {"invalid": "data"})
