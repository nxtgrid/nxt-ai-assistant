"""The doc editor's tool seam: whitelist, adapters, image capture."""

import json

import pytest

from shared.utils import doc_edit_tools


def test_the_whitelist_is_read_only_and_small():
    """Every tool the doc editor may call must be a read. It writes a
    document; it has no business restarting an inverter on the way."""
    assert doc_edit_tools.DOC_EDIT_TOOLS
    for name in doc_edit_tools.DOC_EDIT_TOOLS:
        assert not any(
            verb in name
            for verb in ("restart", "set_", "turn_", "unassign", "resend", "delete", "create")
        ), f"{name} is not read-only"


def test_tool_specs_carry_a_schema():
    specs = doc_edit_tools.build_tool_specs({
        "grid_design_find_grid": {
            "description": "Find a grid",
            "inputSchema": {"type": "object", "properties": {}},
        }
    })
    assert len(specs) == 1
    assert specs[0].name == "grid_design_find_grid"
    assert specs[0].parameters_json_schema == {"type": "object", "properties": {}}


@pytest.mark.asyncio
async def test_registry_runner_extracts_text_and_images():
    """generate_power_chart returns ImageContent; the base64 must be captured
    out of band, never folded into the text the model reads."""

    async def _fake_call(server, tool, arguments):
        assert server == "equipment_diagnostics"
        assert tool == "generate_power_chart"
        return {
            "success": True,
            "result": [
                {"type": "image", "data": "QUJD", "mimeType": "image/png"},
                {"type": "text", "text": json.dumps({"chart_type": "power_timeline"})},
            ],
        }

    runner = doc_edit_tools.registry_tool_runner(_fake_call)
    outcome = await runner("equipment_diagnostics_generate_power_chart", {"grid_name": "X"})

    assert outcome.images == ("QUJD",)
    assert "power_timeline" in outcome.text
    assert "QUJD" not in outcome.text, "base64 must not reach the model's context"


@pytest.mark.asyncio
async def test_a_failing_tool_becomes_an_error_result_not_an_exception():
    async def _boom(*_a, **_k):
        raise RuntimeError("VRM is down")

    runner = doc_edit_tools.registry_tool_runner(_boom)
    outcome = await runner("grid_design_find_grid", {"name": "X"})
    assert outcome.is_error
    assert outcome.images == ()


def test_split_server_and_tool_handles_the_doubled_customer_prefix():
    """customer_customer_get_grid_status is a real name: server 'customer',
    tool 'customer_get_grid_status'."""
    assert doc_edit_tools.split_tool_name("customer_customer_get_grid_status") == (
        "customer",
        "customer_get_grid_status",
    )
    assert doc_edit_tools.split_tool_name("equipment_diagnostics_generate_power_chart") == (
        "equipment_diagnostics",
        "generate_power_chart",
    )
