from shared.llm.types import GenerateResult, ToolCall, Usage


def test_generate_result_defaults_are_empty():
    result = GenerateResult(text="")

    assert result.text == ""
    assert result.tool_calls == []
    assert result.usage == Usage()
    assert result.finish_reason is None


def test_tool_call_preserves_opaque_provider_state():
    call = ToolCall(
        id="call-1",
        name="lookup_customer",
        args={"customer_id": "123"},
        provider_state={"thought_signature": "opaque"},
    )

    assert call.provider_state["thought_signature"] == "opaque"
