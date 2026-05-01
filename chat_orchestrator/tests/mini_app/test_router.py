"""Tests for Mini App API router endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.mini_app.router import (
    SignSubmission,
    _extract_visible_state,
    _extract_workflow_progress,
    _format_label,
    _stamp_pdf_sync,
    router,
)
from orchestrator.mini_app.schemas import FORM_SCHEMAS, FORM_SUBMITTED_SENTINEL

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MOCK_USER = {
    "user": {"id": 12345, "first_name": "Test"},
    "organization_id": 2,
    "account_id": "uuid-abc",
    "email": "test@example.com",
}

MOCK_PACKET = {
    "id": "uuid-1",
    "packet_id": "lpp_design_20260301_abc",
    "packet_title": "LPP Design: TestGrid",
    "organization_id": 2,
    "packet_status": "awaiting_input",
    "packet_inputs": {
        "total_kwp": 50.0,
        "total_kwh": 120.0,
        "total_buildings": 200,
        "served_building_count": 180,
    },
    "packet_state": {
        "awaiting_user_input": True,
        "editable_total_kwp": 50.0,
        "editable_total_kwh": 120.0,
        "editable_total_buildings": 200,
        "editable_served_building_count": 180,
    },
    "requested_in_session": "telegram_session_1",
}


def _make_app(user_override=None, packet_override=None):
    """Create a test FastAPI app with mocked dependencies."""
    app = FastAPI()

    mock_user = user_override or MOCK_USER
    mock_packet = packet_override

    async def fake_get_validated_user():
        return mock_user

    mock_service = AsyncMock()
    mock_service.get_packet = AsyncMock(return_value=mock_packet)
    mock_service.update_state = AsyncMock()
    mock_service.resume_from_input = AsyncMock()

    def fake_get_service():
        return mock_service

    async def fake_get_optional_user():
        return mock_user

    app.include_router(router)
    app.dependency_overrides[
        __import__("orchestrator.mini_app.auth", fromlist=["get_validated_user"]).get_validated_user
    ] = fake_get_validated_user
    app.dependency_overrides[
        __import__("orchestrator.mini_app.router", fromlist=["get_optional_user"]).get_optional_user
    ] = fake_get_optional_user
    app.dependency_overrides[
        __import__(
            "orchestrator.mini_app.router", fromlist=["_get_packet_service"]
        )._get_packet_service
    ] = fake_get_service

    return app, mock_service


class TestGetFormData:
    """Tests for GET /api/mini-app/form-data."""

    def test_returns_form_schema_and_values(self):
        """Valid request returns schema fields and pre-populated values."""
        app, _ = _make_app(packet_override=MOCK_PACKET)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/form-data",
            params={"packet_id": "lpp_design_20260301_abc", "form_type": "design_params"},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["form_type"] == "design_params"
        assert data["packet_id"] == "lpp_design_20260301_abc"
        assert data["packet_title"] == "LPP Design: TestGrid"
        assert len(data["fields"]) == len(FORM_SCHEMAS["design_params"])

        # Values should be populated from packet_state editable_ keys
        assert data["values"]["editable_total_kwp"] == 50.0
        assert data["values"]["editable_total_buildings"] == 200

    def test_unknown_form_type_returns_400(self):
        """Unknown form_type should return 400."""
        app, _ = _make_app(packet_override=MOCK_PACKET)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/form-data",
            params={"packet_id": "test", "form_type": "nonexistent"},
        )
        assert resp.status_code == 400

    def test_packet_not_found_returns_404(self):
        """Missing packet should return 404."""
        app, _ = _make_app(packet_override=None)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/form-data",
            params={"packet_id": "missing", "form_type": "design_params"},
        )
        assert resp.status_code == 404

    def test_org_mismatch_returns_403(self):
        """Packet from different org should return 403."""
        packet = {**MOCK_PACKET, "organization_id": 999}
        app, _ = _make_app(packet_override=packet)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/form-data",
            params={"packet_id": "test", "form_type": "design_params"},
        )
        assert resp.status_code == 403

    def test_packet_not_awaiting_input_returns_410(self):
        """Packet that is not awaiting input should return 410 Gone."""
        packet = {
            **MOCK_PACKET,
            "packet_state": {"awaiting_user_input": False},
        }
        app, _ = _make_app(packet_override=packet)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/form-data",
            params={"packet_id": "test", "form_type": "design_params"},
        )
        assert resp.status_code == 410

    def test_values_from_overrides_take_priority(self):
        """pending_param_overrides values should take priority over state."""
        packet = {
            **MOCK_PACKET,
            "packet_state": {
                **MOCK_PACKET["packet_state"],
                "pending_param_overrides": {"editable_total_kwp": 99.9},
            },
        }
        app, _ = _make_app(packet_override=packet)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/form-data",
            params={"packet_id": "test", "form_type": "design_params"},
        )
        assert resp.status_code == 200
        assert resp.json()["values"]["editable_total_kwp"] == 99.9

    def test_values_fallback_to_inputs(self):
        """When state has no editable_ keys, fall back to packet_inputs."""
        packet = {
            **MOCK_PACKET,
            "packet_state": {"awaiting_user_input": True},
        }
        app, _ = _make_app(packet_override=packet)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/form-data",
            params={"packet_id": "test", "form_type": "design_params"},
        )
        assert resp.status_code == 200
        # Should fall back to inputs (bare key lookup)
        values = resp.json()["values"]
        assert values["editable_total_kwp"] == 50.0
        assert values["editable_total_kwh"] == 120.0


class TestSubmitForm:
    """Tests for POST /api/mini-app/submit."""

    def test_successful_submit(self):
        """Valid submission should update state and resume workflow."""
        app, mock_service = _make_app(packet_override=MOCK_PACKET)
        client = TestClient(app)

        resp = client.post(
            "/api/mini-app/submit",
            json={
                "packet_id": "lpp_design_20260301_abc",
                "form_type": "design_params",
                "values": {"editable_total_kwp": 75.0, "editable_total_kwh": 150.0},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify update_state was called with merged overrides
        mock_service.update_state.assert_called_once()
        call_args = mock_service.update_state.call_args
        assert call_args[0][0] == "lpp_design_20260301_abc"
        overrides = call_args[0][1]["pending_param_overrides"]
        assert overrides["editable_total_kwp"] == 75.0

        # Verify resume_from_input was called with sentinel constant
        mock_service.resume_from_input.assert_called_once_with(
            "lpp_design_20260301_abc",
            FORM_SUBMITTED_SENTINEL,
            session_id="telegram_session_1",
        )

    def test_submit_packet_not_found(self):
        """Submit for missing packet returns 404."""
        app, _ = _make_app(packet_override=None)
        client = TestClient(app)

        resp = client.post(
            "/api/mini-app/submit",
            json={"packet_id": "missing", "form_type": "design_params", "values": {}},
        )
        assert resp.status_code == 404

    def test_submit_org_mismatch(self):
        """Submit to packet from different org returns 403."""
        packet = {**MOCK_PACKET, "organization_id": 999}
        app, _ = _make_app(packet_override=packet)
        client = TestClient(app)

        resp = client.post(
            "/api/mini-app/submit",
            json={
                "packet_id": "test",
                "form_type": "design_params",
                "values": {"editable_total_kwp": 10},
            },
        )
        assert resp.status_code == 403

    def test_submit_not_awaiting_input(self):
        """Submit to packet not awaiting input returns 410."""
        packet = {
            **MOCK_PACKET,
            "packet_state": {"awaiting_user_input": False},
        }
        app, _ = _make_app(packet_override=packet)
        client = TestClient(app)

        resp = client.post(
            "/api/mini-app/submit",
            json={
                "packet_id": "test",
                "form_type": "design_params",
                "values": {"editable_total_kwp": 10},
            },
        )
        assert resp.status_code == 410

    def test_submit_merges_with_existing_overrides(self):
        """Submit should merge new values with existing pending_param_overrides."""
        packet = {
            **MOCK_PACKET,
            "packet_state": {
                **MOCK_PACKET["packet_state"],
                "pending_param_overrides": {"editable_total_buildings": 250},
            },
        }
        app, mock_service = _make_app(packet_override=packet)
        client = TestClient(app)

        resp = client.post(
            "/api/mini-app/submit",
            json={
                "packet_id": "test",
                "form_type": "design_params",
                "values": {"editable_total_kwp": 60.0},
            },
        )
        assert resp.status_code == 200

        call_args = mock_service.update_state.call_args
        overrides = call_args[0][1]["pending_param_overrides"]
        # Both old and new values should be present
        assert overrides["editable_total_buildings"] == 250
        assert overrides["editable_total_kwp"] == 60.0

    def test_submit_rejects_unknown_fields(self):
        """Submit with unknown field keys should return 422."""
        app, _ = _make_app(packet_override=MOCK_PACKET)
        client = TestClient(app)

        resp = client.post(
            "/api/mini-app/submit",
            json={
                "packet_id": "test",
                "form_type": "design_params",
                "values": {"editable_total_kwp": 50.0, "hacker_field": 999},
            },
        )
        assert resp.status_code == 422
        assert "Unknown fields" in resp.json()["detail"]

    def test_submit_rejects_invalid_number_type(self):
        """Submit with string where number expected should return 422."""
        app, _ = _make_app(packet_override=MOCK_PACKET)
        client = TestClient(app)

        resp = client.post(
            "/api/mini-app/submit",
            json={
                "packet_id": "test",
                "form_type": "design_params",
                "values": {"editable_total_kwp": "not_a_number"},
            },
        )
        assert resp.status_code == 422
        assert "expected a number" in resp.json()["detail"]

    def test_submit_rejects_below_min(self):
        """Submit with value below minimum should return 422."""
        app, _ = _make_app(packet_override=MOCK_PACKET)
        client = TestClient(app)

        resp = client.post(
            "/api/mini-app/submit",
            json={
                "packet_id": "test",
                "form_type": "design_params",
                "values": {"editable_total_kwp": -5},
            },
        )
        assert resp.status_code == 422
        assert "must be >=" in resp.json()["detail"]

    def test_submit_unknown_form_type(self):
        """Submit with unknown form_type should return 422."""
        app, _ = _make_app(packet_override=MOCK_PACKET)
        client = TestClient(app)

        resp = client.post(
            "/api/mini-app/submit",
            json={
                "packet_id": "test",
                "form_type": "nonexistent",
                "values": {"foo": "bar"},
            },
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestFormatLabel:
    """Tests for _format_label helper."""

    def test_snake_case(self):
        assert _format_label("total_kwp") == "Total Kwp"

    def test_editable_prefix_not_stripped(self):
        """_format_label does NOT strip editable_ — that's done in _extract_visible_state."""
        assert _format_label("editable_total_kwp") == "Editable Total Kwp"


class TestExtractVisibleState:
    """Tests for _extract_visible_state helper."""

    def test_empty_state_and_inputs(self):
        result = _extract_visible_state({}, {})
        assert result == []

    def test_filters_internal_keys(self):
        state = {
            "awaiting_user_input": True,
            "pending_param_overrides": {"a": 1},
            "execution_summary": {},
            "visible_key": 42,
        }
        result = _extract_visible_state(state, {})
        keys = [e.key for e in result]
        assert "visible_key" in keys
        assert "awaiting_user_input" not in keys
        assert "pending_param_overrides" not in keys
        assert "execution_summary" not in keys

    def test_editable_prefix_stripped_in_label(self):
        state = {"editable_total_kwp": 50.0}
        result = _extract_visible_state(state, {})
        assert len(result) == 1
        assert result[0].label == "Total Kwp"
        assert result[0].key == "editable_total_kwp"

    def test_inputs_appear_first(self):
        state = {"some_state_key": "val"}
        inputs = {"site_name": "TestGrid"}
        result = _extract_visible_state(state, inputs)
        assert result[0].key == "site_name"
        assert result[1].key == "some_state_key"

    def test_inputs_take_precedence_over_state(self):
        """If a key exists in both inputs and state, only inputs version shown."""
        state = {"site_name": "FromState"}
        inputs = {"site_name": "FromInputs"}
        result = _extract_visible_state(state, inputs)
        keys = [e.key for e in result]
        assert keys.count("site_name") == 1
        assert result[0].value == "FromInputs"

    def test_none_and_empty_filtered(self):
        state = {"null_val": None, "empty_val": "", "zero_val": 0}
        inputs = {"none_input": None, "empty_input": "", "valid": 10}
        result = _extract_visible_state(state, inputs)
        keys = [e.key for e in result]
        assert "valid" in keys
        assert "zero_val" in keys  # 0 is valid, not filtered
        assert "null_val" not in keys
        assert "empty_val" not in keys
        assert "none_input" not in keys
        assert "empty_input" not in keys


class TestExtractWorkflowProgress:
    """Tests for _extract_workflow_progress helper."""

    def test_no_execution_summary(self):
        result = _extract_workflow_progress({})
        assert result == []

    def test_empty_step_records(self):
        result = _extract_workflow_progress({"execution_summary": {"steps": []}})
        assert result == []

    def test_extracts_steps(self):
        state = {
            "execution_summary": {
                "steps": [
                    {"step_name": "fetch_data", "description": "Fetch data", "status": "success"},
                    {"step_name": "process", "description": "Process", "status": "failed"},
                    {"step_name": "finalize", "status": "pending"},
                ]
            }
        }
        result = _extract_workflow_progress(state)
        assert len(result) == 3
        assert result[0].name == "fetch_data"
        assert result[0].status == "success"
        assert result[1].status == "failed"
        assert result[2].description == ""  # missing description defaults to ""


# ---------------------------------------------------------------------------
# State data endpoint tests
# ---------------------------------------------------------------------------

MOCK_COMPLETED_PACKET = {
    "id": "uuid-2",
    "packet_id": "lpp_design_20260301_xyz",
    "packet_title": "LPP Design: ExampleSite",
    "organization_id": 2,
    "packet_type": "lpp_design",
    "packet_status": "completed",
    "packet_inputs": {"site_name": "ExampleSite", "total_kwp": 80.0},
    "packet_state": {
        "editable_total_kwp": 85.0,
        "some_result": "computed_value",
        "execution_summary": {
            "steps": [
                {"step_name": "step1", "description": "First step", "status": "success"},
                {"step_name": "step2", "description": "Second step", "status": "success"},
            ]
        },
    },
}


class TestGetStateData:
    """Tests for GET /api/mini-app/state-data."""

    def test_returns_state_for_completed_packet(self):
        app, _ = _make_app(packet_override=MOCK_COMPLETED_PACKET)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/state-data",
            params={"packet_id": "lpp_design_20260301_xyz"},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["packet_id"] == "lpp_design_20260301_xyz"
        assert data["packet_title"] == "LPP Design: ExampleSite"
        assert data["packet_type"] == "lpp_design"
        assert data["packet_status"] == "completed"
        assert len(data["workflow_steps"]) == 2
        assert data["workflow_steps"][0]["name"] == "step1"
        assert data["workflow_steps"][0]["status"] == "success"

        # State should include inputs and visible state keys
        state_keys = [e["key"] for e in data["state"]]
        assert "site_name" in state_keys
        assert "editable_total_kwp" in state_keys or "some_result" in state_keys

    def test_does_not_require_awaiting_input(self):
        """Unlike form-data, state-data works for non-awaiting packets."""
        packet = {**MOCK_COMPLETED_PACKET, "packet_state": {"some_key": "some_val"}}
        app, _ = _make_app(packet_override=packet)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/state-data",
            params={"packet_id": "test"},
        )
        assert resp.status_code == 200

    def test_packet_not_found_returns_404(self):
        app, _ = _make_app(packet_override=None)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/state-data",
            params={"packet_id": "missing"},
        )
        assert resp.status_code == 404

    def test_org_mismatch_returns_403(self):
        packet = {**MOCK_COMPLETED_PACKET, "organization_id": 999}
        app, _ = _make_app(packet_override=packet)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/state-data",
            params={"packet_id": "test"},
        )
        assert resp.status_code == 403

    def test_empty_state_returns_empty_lists(self):
        packet = {
            **MOCK_COMPLETED_PACKET,
            "packet_state": {},
            "packet_inputs": {},
        }
        app, _ = _make_app(packet_override=packet)
        client = TestClient(app)

        resp = client.get(
            "/api/mini-app/state-data",
            params={"packet_id": "test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == []
        assert data["workflow_steps"] == []


# ---------------------------------------------------------------------------
# Sign data endpoint header tests
# ---------------------------------------------------------------------------

MOCK_SIGN_PACKET = {
    "id": "uuid-sign-1",
    "packet_id": "sign_packet_abc",
    "packet_title": "Sign: Contract",
    "organization_id": 2,
    "packet_status": "pending",
    "packet_inputs": {},
    "packet_state": {
        "signing_signer_telegram_id": "12345",
        "signing_document_drive_id": "drive_file_id_123",
        "signing_document_name": "Service Contract",
        "signing_requester_name": "Alice Smith",
        "signing_requester_email": "alice@example.com",
        "signing_status": "pending",
    },
    "requested_in_session": "telegram_session_1",
}


class TestSignDataHeaders:
    """Tests that GET /sign-data returns requester metadata in response headers."""

    def test_headers_contain_requester_info(self):
        """Response headers include X-Requester-Name and X-Document-Name (not email)."""
        app, mock_service = _make_app(packet_override=MOCK_SIGN_PACKET)
        mock_service.get_packet = AsyncMock(return_value=MOCK_SIGN_PACKET)
        client = TestClient(app)

        with (
            patch("orchestrator.mini_app.router._get_validated_user_sign") as mock_auth,
            patch(
                "shared.utils.drive_upload.download_drive_file", new_callable=AsyncMock
            ) as mock_dl,
        ):
            mock_auth.return_value = {"user": {"id": 12345}}
            mock_dl.return_value = b"%PDF-1.4 fake"

            resp = client.get(
                "/api/mini-app/sign-data",
                params={"packet_id": "sign_packet_abc"},
            )

        assert resp.status_code == 200
        assert resp.headers["x-requester-name"] == "Alice Smith"
        assert "x-requester-email" not in resp.headers
        assert resp.headers["x-document-name"] == "Service Contract"

    def test_headers_empty_when_state_missing_fields(self):
        """Missing state fields result in empty (not absent) header values."""
        packet = {
            **MOCK_SIGN_PACKET,
            "packet_state": {
                "signing_signer_telegram_id": "12345",
                "signing_document_drive_id": "drive_file_id_123",
                "signing_status": "pending",
            },
        }
        app, mock_service = _make_app(packet_override=packet)
        mock_service.get_packet = AsyncMock(return_value=packet)
        client = TestClient(app)

        with (
            patch("orchestrator.mini_app.router._get_validated_user_sign") as mock_auth,
            patch(
                "shared.utils.drive_upload.download_drive_file", new_callable=AsyncMock
            ) as mock_dl,
        ):
            mock_auth.return_value = {"user": {"id": 12345}}
            mock_dl.return_value = b"%PDF-1.4 fake"

            resp = client.get(
                "/api/mini-app/sign-data",
                params={"packet_id": "sign_packet_abc"},
            )

        assert resp.status_code == 200
        assert resp.headers["x-requester-name"] == ""
        assert "x-requester-email" not in resp.headers

    def test_returns_403_when_already_signed(self):
        packet = {
            **MOCK_SIGN_PACKET,
            "packet_state": {**MOCK_SIGN_PACKET["packet_state"], "signing_status": "signed"},
        }
        app, mock_service = _make_app(packet_override=packet)
        mock_service.get_packet = AsyncMock(return_value=packet)
        client = TestClient(app)

        with patch("orchestrator.mini_app.router._get_validated_user_sign") as mock_auth:
            mock_auth.return_value = {"user": {"id": 12345}}
            resp = client.get(
                "/api/mini-app/sign-data",
                params={"packet_id": "sign_packet_abc"},
            )

        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# SignSubmission model validation tests
# ---------------------------------------------------------------------------


class TestSignSubmissionModel:
    """Tests for SignSubmission field validation."""

    def test_default_w_frac_h_frac(self):
        s = SignSubmission(packet_id="p", page=0, x=0.1, y=0.1, sig_png_b64="abc")
        assert s.w_frac == 0.25
        assert s.h_frac == 0.08

    def test_custom_w_frac_h_frac(self):
        s = SignSubmission(
            packet_id="p", page=0, x=0.1, y=0.1, sig_png_b64="abc", w_frac=0.3, h_frac=0.1
        )
        assert s.w_frac == 0.3
        assert s.h_frac == 0.1

    def test_w_frac_below_min_raises(self):
        with pytest.raises(Exception):
            SignSubmission(packet_id="p", page=0, x=0.1, y=0.1, sig_png_b64="abc", w_frac=0.01)

    def test_w_frac_above_max_raises(self):
        with pytest.raises(Exception):
            SignSubmission(packet_id="p", page=0, x=0.1, y=0.1, sig_png_b64="abc", w_frac=0.99)

    def test_h_frac_below_min_raises(self):
        with pytest.raises(Exception):
            SignSubmission(packet_id="p", page=0, x=0.1, y=0.1, sig_png_b64="abc", h_frac=0.01)

    def test_h_frac_above_max_raises(self):
        with pytest.raises(Exception):
            SignSubmission(packet_id="p", page=0, x=0.1, y=0.1, sig_png_b64="abc", h_frac=0.99)

    def test_negative_page_raises(self):
        with pytest.raises(Exception):
            SignSubmission(packet_id="p", page=-1, x=0.1, y=0.1, sig_png_b64="abc")

    def test_page_upper_bound_raises(self):
        with pytest.raises(Exception):
            SignSubmission(packet_id="p", page=9999, x=0.1, y=0.1, sig_png_b64="abc")


# ---------------------------------------------------------------------------
# _stamp_pdf_sync page validation tests
# ---------------------------------------------------------------------------


class TestStampPdfSync:
    """Tests for _stamp_pdf_sync page bounds validation."""

    def _make_minimal_pdf(self) -> bytes:
        """Create a single-page in-memory PDF using pymupdf."""
        try:
            import fitz
        except ImportError:
            pytest.skip("pymupdf not installed")

        doc = fitz.open()
        doc.new_page(width=595, height=842)
        result: bytes = doc.tobytes()
        return result

    def _make_sig_bytes(self) -> bytes:
        """Create a valid 1×1 white RGB PNG using struct + zlib."""
        import struct
        import zlib

        def chunk(tag: bytes, data: bytes) -> bytes:
            c = struct.pack(">I", len(data)) + tag + data
            return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        header = b"\x89PNG\r\n\x1a\n"
        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        # filter byte (0) + R G B for white
        idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
        iend = chunk(b"IEND", b"")
        return header + ihdr + idat + iend

    def test_valid_page_returns_bytes(self):
        """Stamping page 0 on a single-page PDF returns signed bytes."""
        pdf = self._make_minimal_pdf()
        sig = self._make_sig_bytes()
        result = _stamp_pdf_sync(pdf, 0, 0.1, 0.1, sig, w_frac=0.25, h_frac=0.08)
        assert isinstance(result, tuple)
        assert len(result) == 5
        signed_bytes = result[0]
        assert signed_bytes[:4] == b"%PDF"

    def test_out_of_range_page_raises_value_error(self):
        """Requesting page 5 on a 1-page PDF raises ValueError."""
        pdf = self._make_minimal_pdf()
        sig = self._make_sig_bytes()
        with pytest.raises(ValueError, match="Page 5 does not exist"):
            _stamp_pdf_sync(pdf, 5, 0.1, 0.1, sig)
