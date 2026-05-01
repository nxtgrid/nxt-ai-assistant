"""Tests for headless expert workflow execution."""

import ast
import pathlib

import pytest

from orchestrator.services.expert_tool_runner import (
    HEADLESS_ALLOWED_EXPERTS,
    MAX_EXPERT_CALL_DEPTH,
    make_input_resolver,
)


class TestInputResolver:
    """Input resolver callback for headless needs_input handling."""

    def test_resolves_known_step(self):
        resolver = make_input_resolver({"detect_duplicates": "keep_new"})
        assert resolver("detect_duplicates", "Which version?") == "keep_new"

    def test_returns_none_for_unknown_step(self):
        resolver = make_input_resolver({"detect_duplicates": "keep_new"})
        assert resolver("unknown_step", "What?") is None

    def test_empty_prefilled_returns_none(self):
        resolver = make_input_resolver({})
        assert resolver("any_step", "prompt") is None

    def test_converts_non_string_to_string(self):
        resolver = make_input_resolver({"step": 42})
        assert resolver("step", "prompt") == "42"


class TestAllowList:
    """Only approved experts can run headlessly."""

    def test_lpp_is_allowed(self):
        assert "lpp_expert" in HEADLESS_ALLOWED_EXPERTS

    def test_gtr_is_allowed(self):
        assert "gtr_expert" in HEADLESS_ALLOWED_EXPERTS

    def test_max_depth_is_one(self):
        assert MAX_EXPERT_CALL_DEPTH == 1


class TestCallDepth:
    """call_depth field on StepContext."""

    def test_default_call_depth_is_zero(self):
        from orchestrator.experts.step_context import StepContext

        ctx = StepContext(
            packet_id="test",
            packet_type="test",
            packet_goal="test",
            packet_inputs={},
            packet_state={},
            current_step="",
            steps_completed=[],
        )
        assert ctx.call_depth == 0


class TestArchitecturalBoundary:
    """Expert handlers must not import agent graph code."""

    def test_no_agent_imports_in_handlers(self):
        handlers_dir = pathlib.Path("orchestrator/experts/handlers")
        if not handlers_dir.exists():
            pytest.skip("handlers directory not found (wrong cwd)")

        forbidden = {"orchestrator.graphs", "agent_worker", "agent_events"}
        violations = []

        for py_file in handlers_dir.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for f in forbidden:
                        if f in node.module:
                            violations.append(f"{py_file}:{node.lineno} imports {node.module}")

        assert not violations, "Forbidden imports found:\n" + "\n".join(violations)
