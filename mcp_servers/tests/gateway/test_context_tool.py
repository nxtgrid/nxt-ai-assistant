"""get_operating_context: the synthetic, non-registry tool.

See context_tool.py's own module docstring for why it exists at all — this
file only covers its own behaviour: the definition's shape, the name
predicate, and the call-result builder's success/failure/injection paths.
transport.py's own tests cover it actually being wired into list_tools/
call_tool; test_app.py covers the full ASGI round trip.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from gateway.context_tool import (
    DEFINITION,
    TOOL_NAME,
    is_operating_context_tool,
    operating_context_tool_result,
)


def test_tool_name_does_not_look_like_a_real_server():
    # Every other exposed tool is "{real_server_name}__{tool}" - this one
    # deliberately is not, so a client can tell it apart from domain data.
    assert TOOL_NAME == "gateway__get_operating_context"
    assert DEFINITION["name"] == TOOL_NAME


def test_definition_takes_no_arguments():
    # Zero friction is the point - a model should never need to decide what
    # to pass before calling this.
    assert DEFINITION["inputSchema"] == {"type": "object", "properties": {}}


def test_definition_is_marked_read_only_like_every_other_tool_here():
    assert DEFINITION["description"].startswith("[READ-ONLY]")


def test_is_operating_context_tool_matches_only_the_exact_name():
    assert is_operating_context_tool("gateway__get_operating_context") is True
    assert is_operating_context_tool("customer__get_operating_context") is False
    assert is_operating_context_tool("gateway__get_operating_context ") is False
    assert is_operating_context_tool("") is False


def test_result_wraps_the_built_text_as_a_successful_tool_call():
    result = operating_context_tool_result(
        session="fake-session", build=lambda session: f"CONTEXT FOR {session}"
    )

    assert result == {
        "success": True,
        "result": [{"type": "text", "text": "CONTEXT FOR fake-session"}],
    }


def test_the_session_is_the_only_thing_passed_to_build():
    # The whole reason this bypasses server_registry: build_instructions
    # needs the caller's resolved identity, not tool arguments (there are
    # none). This pins that the session is threaded through unmodified.
    seen = []

    def fake_build(session):
        seen.append(session)
        return "ok"

    operating_context_tool_result(session="the-real-session", build=fake_build)

    assert seen == ["the-real-session"]


def test_a_none_result_from_build_degrades_to_a_soft_failure_not_an_exception():
    # build_instructions returns None on a render failure or an empty
    # prompt (see its own docstring) - this must never raise, matching
    # every other tool's soft-failure shape ({"success": False, "error": ...}),
    # not an unhandled exception that would surface as a generic MCP error.
    result = operating_context_tool_result(session="s", build=lambda session: None)

    assert result["success"] is False
    assert "error" in result
    assert "result" not in result


def test_an_empty_string_result_also_degrades_to_a_soft_failure():
    result = operating_context_tool_result(session="s", build=lambda session: "")

    assert result["success"] is False


def test_defaults_to_the_real_build_instructions_when_not_injected():
    # Confirms the production wiring imports the real function rather than
    # silently requiring every caller to inject one.
    import gateway.context_tool as module
    import gateway.instructions as instructions_module

    calls = []
    original = instructions_module.build_instructions
    instructions_module.build_instructions = lambda session, **kw: calls.append(session) or "x"
    try:
        result = module.operating_context_tool_result(session="s")
    finally:
        instructions_module.build_instructions = original

    assert calls == ["s"]
    assert result == {"success": True, "result": [{"type": "text", "text": "x"}]}
