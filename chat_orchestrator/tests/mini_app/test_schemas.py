"""Tests for Mini App shared schemas and validation."""

from unittest.mock import patch

import pytest

from orchestrator.mini_app.schemas import (
    FORM_SCHEMAS,
    FORM_SUBMITTED_SENTINEL,
    StateDataResponse,
    StateEntry,
    WorkflowStepProgress,
    build_mini_app_url,
    build_view_state_url,
    validate_form_values,
)


class TestValidateFormValues:
    """Tests for validate_form_values function."""

    def test_valid_values(self):
        """Valid number values should be accepted and coerced."""
        result = validate_form_values(
            "design_params",
            {"editable_total_kwp": 50.5, "editable_total_kwh": 120},
        )
        assert result["editable_total_kwp"] == 50.5
        assert result["editable_total_kwh"] == 120.0

    def test_integer_coercion_for_step_1(self):
        """Fields with step=1 should be coerced to int."""
        result = validate_form_values(
            "design_params",
            {"editable_total_buildings": 200.0},
        )
        assert result["editable_total_buildings"] == 200
        assert isinstance(result["editable_total_buildings"], int)

    def test_unknown_form_type(self):
        """Unknown form_type should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown form type"):
            validate_form_values("nonexistent", {"foo": 1})

    def test_unknown_field_keys(self):
        """Unknown field keys should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown fields"):
            validate_form_values(
                "design_params",
                {"editable_total_kwp": 50, "hacker_field": 999},
            )

    def test_non_numeric_value(self):
        """Non-numeric value for number field should raise ValueError."""
        with pytest.raises(ValueError, match="expected a number"):
            validate_form_values("design_params", {"editable_total_kwp": "abc"})

    def test_below_min_value(self):
        """Value below minimum should raise ValueError."""
        with pytest.raises(ValueError, match="must be >="):
            validate_form_values("design_params", {"editable_total_kwp": -1})

    def test_empty_values_ok(self):
        """Empty values dict should return empty dict (all optional)."""
        result = validate_form_values("design_params", {})
        assert result == {}

    def test_partial_values(self):
        """Submitting only some fields should be fine."""
        result = validate_form_values(
            "design_params",
            {"editable_total_kwp": 10.0},
        )
        assert len(result) == 1
        assert result["editable_total_kwp"] == 10.0


class TestBuildMiniAppUrl:
    """Tests for build_mini_app_url helper."""

    @patch.dict("os.environ", {"MINI_APP_BASE_URL": "https://example.com/mini-app/"})
    def test_builds_url(self):
        url = build_mini_app_url("pkt-123", "design_params")
        assert url == "https://example.com/mini-app/?packet_id=pkt-123&form_type=design_params"

    @patch.dict("os.environ", {"MINI_APP_BASE_URL": ""})
    def test_returns_none_when_not_configured(self):
        assert build_mini_app_url("pkt-123", "design_params") is None


class TestBuildViewStateUrl:
    """Tests for build_view_state_url helper."""

    @patch.dict("os.environ", {"MINI_APP_BASE_URL": "https://example.com/mini-app/"})
    def test_builds_url(self):
        url = build_view_state_url("pkt-123")
        assert url.startswith("https://example.com/mini-app/?packet_id=pkt-123&view=state&sig=")
        assert len(url.split("&sig=")[1]) == 16

    @patch.dict("os.environ", {"MINI_APP_BASE_URL": ""})
    def test_returns_none_when_not_configured(self):
        assert build_view_state_url("pkt-123") is None

    @patch.dict("os.environ", {"MINI_APP_BASE_URL": "https://example.com/mini-app"})
    def test_no_trailing_slash(self):
        """URL without trailing slash should also work."""
        url = build_view_state_url("pkt-456")
        assert url.startswith("https://example.com/mini-app/?packet_id=pkt-456&view=state&sig=")
        assert len(url.split("&sig=")[1]) == 16


class TestPydanticModels:
    """Tests for Pydantic response models."""

    def test_state_entry(self):
        entry = StateEntry(key="total_kwp", label="Total Kwp", value=50.5)
        assert entry.key == "total_kwp"
        assert entry.value == 50.5

    def test_workflow_step_progress(self):
        step = WorkflowStepProgress(name="step1", description="Do thing", status="success")
        assert step.status == "success"

    def test_state_data_response(self):
        resp = StateDataResponse(
            packet_id="pkt-1",
            packet_title="Test",
            packet_type="lpp_design",
            packet_status="completed",
            state=[StateEntry(key="k", label="K", value=1)],
            workflow_steps=[],
        )
        assert resp.packet_status == "completed"
        assert len(resp.state) == 1


class TestConstants:
    """Verify constants are correct."""

    def test_sentinel_value(self):
        assert FORM_SUBMITTED_SENTINEL == "__form_submitted__"

    def test_form_schemas_has_design_params(self):
        assert "design_params" in FORM_SCHEMAS
        assert len(FORM_SCHEMAS["design_params"]) == 4
