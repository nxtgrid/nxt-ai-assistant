"""Tests for the /design and /editdoc commands' override mechanisms:
max_tool_rounds_override and the tier-based model_override.

Covers:
- The /design CommandDefinition itself (registry shape).
- CommandParser.process_command's 5-tuple return, with and without a
  max_tool_rounds override, and with and without a model_override tier
  (resolved via shared.llm.model_tiers.resolve_model()).
- The parse_command graph node threading max_rounds and command_model_override
  through for both the slash-command path and the natural-language-trigger
  path, including the fail-open behavior when a model_override tier's env
  var isn't set.
"""

from __future__ import annotations

from orchestrator.graphs.nodes.parse_command import parse_command
from orchestrator.models.schemas import UserContext
from orchestrator.services.command_parser import CommandParser
from orchestrator.services.command_registry import COMMAND_REGISTRY


def _user_context(is_staff: bool, source: str = "telegram") -> UserContext:
    return UserContext(
        user_id="12345",
        user_email="staff@example.com" if is_staff else "customer@example.com",
        source=source,
        is_staff=is_staff,
    )


# ---------------------------------------------------------------------------
# command_registry: /design shape
# ---------------------------------------------------------------------------


class TestDesignCommandRegistryEntry:
    def test_design_command_exists(self):
        assert "design" in COMMAND_REGISTRY

    def test_design_uses_prefix_exclusive_tools(self):
        cmd_def = COMMAND_REGISTRY["design"]
        assert cmd_def.exclusive_tools == ["prefix:grid_design_"]

    def test_design_is_staff_only(self):
        assert COMMAND_REGISTRY["design"].staff_only is True

    def test_design_has_positive_rounds_override(self):
        assert COMMAND_REGISTRY["design"].max_tool_rounds_override > 0

    def test_design_is_tool_command(self):
        assert COMMAND_REGISTRY["design"].command_type == "tool"


# ---------------------------------------------------------------------------
# CommandParser.process_command: 5-tuple arity and override propagation
# ---------------------------------------------------------------------------


class TestProcessCommandRoundsOverride:
    async def test_design_command_returns_override_as_fifth_element(self):
        parser = CommandParser()
        result = await parser.process_command(
            text="/design ExampleGrid",
            user_context=_user_context(is_staff=True),
        )

        assert len(result) == 5
        _, is_command, unlocked_tools, _model_override, max_tool_rounds = result

        assert is_command is True
        assert unlocked_tools == ["prefix:grid_design_"]
        assert max_tool_rounds == COMMAND_REGISTRY["design"].max_tool_rounds_override
        assert max_tool_rounds > 0

    async def test_command_without_override_returns_zero(self):
        parser = CommandParser()
        result = await parser.process_command(
            text="/grid ExampleGrid",
            user_context=_user_context(is_staff=True),
        )

        assert len(result) == 5
        max_tool_rounds = result[4]
        assert max_tool_rounds == 0

    async def test_tickets_command_without_override_returns_zero(self):
        parser = CommandParser()
        result = await parser.process_command(
            text="/tickets",
            user_context=_user_context(is_staff=True),
        )

        assert result[4] == 0

    async def test_non_command_text_returns_zero(self):
        parser = CommandParser()
        result = await parser.process_command(
            text="hello there",
            user_context=_user_context(is_staff=True),
        )

        assert result == ("hello there", False, [], "", 0)

    async def test_unrecognized_command_returns_zero(self):
        parser = CommandParser()
        result = await parser.process_command(
            text="/totally_not_a_real_command",
            user_context=_user_context(is_staff=True),
        )

        assert result[1] is True  # is_command
        assert result[4] == 0

    async def test_non_staff_user_blocked_from_design(self):
        parser = CommandParser()
        result = await parser.process_command(
            text="/design ExampleGrid",
            user_context=_user_context(is_staff=False),
        )

        # Staff-only command rejected for non-staff -> unrecognized/unauthorized path
        assert result[2] == []  # no tools unlocked
        assert result[4] == 0  # no rounds override applied


# ---------------------------------------------------------------------------
# CommandParser.process_command: model_override tier resolution
# ---------------------------------------------------------------------------


class TestProcessCommandModelOverride:
    async def test_editdoc_command_returns_resolved_model_override(self, monkeypatch):
        monkeypatch.setenv("MODEL_THINKING", "gemini-pro-latest")
        parser = CommandParser()
        result = await parser.process_command(
            text="/editdoc Site Visit Plan ExampleGrid",
            user_context=_user_context(is_staff=True),
        )

        assert len(result) == 5
        model_override = result[3]
        assert model_override == "gemini-pro-latest"

    async def test_command_without_model_override_returns_empty_string(self):
        # /design has no model_override tier configured.
        assert COMMAND_REGISTRY["design"].model_override == ""
        parser = CommandParser()
        result = await parser.process_command(
            text="/design ExampleGrid",
            user_context=_user_context(is_staff=True),
        )

        assert result[3] == ""

    async def test_editdoc_model_override_fails_open_when_tier_env_var_unset(self, monkeypatch):
        monkeypatch.delenv("MODEL_THINKING", raising=False)
        parser = CommandParser()
        result = await parser.process_command(
            text="/editdoc Site Visit Plan ExampleGrid",
            user_context=_user_context(is_staff=True),
        )

        # resolve_model("thinking") raises RuntimeError with MODEL_THINKING unset;
        # process_command must catch it and fail open, not propagate.
        assert result[3] == ""


# ---------------------------------------------------------------------------
# parse_command graph node: max_rounds threading
# ---------------------------------------------------------------------------


class TestParseCommandNodeMaxRounds:
    async def test_slash_design_command_sets_max_rounds(self):
        state = {
            "user_input": "/design ExampleGrid",
            "user_context": _user_context(is_staff=True),
        }

        result = await parse_command(state)

        assert result["max_rounds"] == COMMAND_REGISTRY["design"].max_tool_rounds_override

    async def test_slash_grid_command_omits_max_rounds(self):
        state = {
            "user_input": "/grid ExampleGrid",
            "user_context": _user_context(is_staff=True),
        }

        result = await parse_command(state)

        assert "max_rounds" not in result

    async def test_nl_trigger_for_design_sets_max_rounds(self):
        # Pick one of /design's nl_triggers verbatim; input must not start with "/".
        trigger_phrase = COMMAND_REGISTRY["design"].nl_triggers[0]
        assert trigger_phrase  # sanity: registry actually defines nl_triggers
        state = {
            "user_input": f"Can you {trigger_phrase} for design abc123?",
            "user_context": _user_context(is_staff=True),
        }

        result = await parse_command(state)

        assert result["parsed_command"] == "/design"
        assert result["max_rounds"] == COMMAND_REGISTRY["design"].max_tool_rounds_override

    async def test_non_telegram_source_skips_command_parsing_entirely(self):
        state = {
            "user_input": "/design ExampleGrid",
            "user_context": _user_context(is_staff=True, source="api"),
        }

        result = await parse_command(state)

        assert "max_rounds" not in result
        assert result["parsed_command"] is None


# ---------------------------------------------------------------------------
# parse_command graph node: command_model_override threading
# ---------------------------------------------------------------------------


class TestParseCommandNodeModelOverride:
    async def test_slash_editdoc_command_sets_command_model_override(self, monkeypatch):
        monkeypatch.setenv("MODEL_THINKING", "gemini-pro-latest")
        state = {
            "user_input": "/editdoc Site Visit Plan ExampleGrid",
            "user_context": _user_context(is_staff=True),
        }

        result = await parse_command(state)

        assert result["command_model_override"] == "gemini-pro-latest"

    async def test_slash_design_command_omits_model_override(self):
        # /design has no model_override tier configured.
        state = {
            "user_input": "/design ExampleGrid",
            "user_context": _user_context(is_staff=True),
        }

        result = await parse_command(state)

        assert "command_model_override" not in result

    async def test_nl_trigger_for_editdoc_sets_command_model_override(self, monkeypatch):
        monkeypatch.setenv("MODEL_THINKING", "gemini-pro-latest")
        # Pick one of /editdoc's nl_triggers verbatim; input must not start with "/".
        trigger_phrase = COMMAND_REGISTRY["editdoc"].nl_triggers[0]
        assert trigger_phrase  # sanity: registry actually defines nl_triggers
        state = {
            "user_input": f"Can you {trigger_phrase} for the Site Visit Plan?",
            "user_context": _user_context(is_staff=True),
        }

        result = await parse_command(state)

        assert result["parsed_command"] == "/editdoc"
        assert result["command_model_override"] == "gemini-pro-latest"

    async def test_nl_trigger_model_override_fails_open_when_tier_env_var_unset(self, monkeypatch):
        monkeypatch.delenv("MODEL_THINKING", raising=False)
        trigger_phrase = COMMAND_REGISTRY["editdoc"].nl_triggers[0]
        state = {
            "user_input": f"Can you {trigger_phrase} for the Site Visit Plan?",
            "user_context": _user_context(is_staff=True),
        }

        result = await parse_command(state)

        # resolve_model("thinking") raises RuntimeError with MODEL_THINKING unset;
        # the node must catch it and omit the key, not propagate.
        assert result["parsed_command"] == "/editdoc"
        assert "command_model_override" not in result
