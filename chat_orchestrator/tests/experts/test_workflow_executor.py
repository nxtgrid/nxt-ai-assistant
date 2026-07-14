"""Tests for WorkflowExecutor.

Tests workflow parsing, step execution, and LLM/function hybrid workflows.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Triggers package_generator's @register_step decorators as an import-time side
# effect (mirrors the pattern in test_contract_lint.py / test_package_generator_
# contracts.py) so the real, production StepContracts for generate_distribution_map
# et al. are available below -- not synthetic/mock contracts.
import orchestrator.experts.handlers.package_generator  # noqa: F401  (registration side effect)
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import ParamSpec, StepContract
from orchestrator.experts.step_registry import get_step_registry
from orchestrator.experts.workflow_executor import (
    ExecutionSummary,
    ParsedStep,
    PrereqReport,
    StepLoopSignal,
    WorkflowExecutor,
)

# Snapshot the REAL (handler, contract) pairs for the steps the Phase D
# regression test below needs, right after the registration-triggering import
# above -- at module-import (collection) time, before any test method body
# (including `test_parameter_confirmation.py::TestRegisterStepWithoutSchema
# .setup_method`'s `get_step_registry().clear()` with no teardown) can run.
# The regression test re-registers these exact real values immediately before
# it runs (see `_REAL_PACKAGE_GENERATOR_STEPS` usage in
# `TestValidateStepPrerequisites`), so its correctness never depends on
# cross-file test execution order.
_REAL_PACKAGE_GENERATOR_STEP_NAMES = (
    "resolve_community_site",
    "create_site_folder",
    "generate_distribution_layout",
    "generate_distribution_map",
)
_REAL_PACKAGE_GENERATOR_STEPS = {
    _name: (get_step_registry().get_handler(_name), get_step_registry().get_contract(_name))
    for _name in _REAL_PACKAGE_GENERATOR_STEP_NAMES
}


@dataclass
class MockExpertConfig:
    """Mock expert configuration for testing."""

    expert_id: str = "test_expert"
    display_name: str = "Test Expert"
    system_instructions: str = "You are a test expert."
    tools: List[str] = None
    packet_types: List[str] = None
    workflows: Dict[str, List[str]] = None
    capabilities: List[str] = None
    raw_sections: Dict[str, str] = None

    def __post_init__(self):
        self.tools = self.tools or []
        self.packet_types = self.packet_types or ["test_packet"]
        self.workflows = self.workflows or {}
        self.capabilities = self.capabilities or []
        self.raw_sections = self.raw_sections or {}

    def get_workflow(self, packet_type: str) -> Optional[List[str]]:
        return self.workflows.get(packet_type)


class TestParseWorkflow:
    """Test workflow parsing from Google Doc format."""

    def test_parse_empty_workflow(self):
        """Empty workflow returns empty list."""
        executor = WorkflowExecutor(None, None, None)
        steps = executor.parse_workflow([])
        assert steps == []

    def test_parse_llm_step(self):
        """Parse [llm] step format."""
        executor = WorkflowExecutor(None, None, None)
        steps = executor.parse_workflow(["1. [llm] understand_request - Parse user intent"])

        assert len(steps) == 1
        assert steps[0].step_type == "llm"
        assert steps[0].name == "understand_request"
        assert "Parse user intent" in steps[0].description

    def test_parse_function_step(self):
        """Parse [function:name] step format."""
        executor = WorkflowExecutor(None, None, None)
        steps = executor.parse_workflow(["1. [function:fetch_metrics] - Get metrics from Grafana"])

        assert len(steps) == 1
        assert steps[0].step_type == "function"
        assert steps[0].name == "fetch_metrics"
        assert "Get metrics" in steps[0].description

    def test_parse_mixed_workflow(self):
        """Parse workflow with both LLM and function steps."""
        executor = WorkflowExecutor(None, None, None)
        workflow = [
            "1. [llm] understand_request - Parse user intent",
            "2. [function:fetch_metrics] - Get metrics from Grafana",
            "3. [llm] synthesize - Combine results",
        ]
        steps = executor.parse_workflow(workflow)

        assert len(steps) == 3
        assert steps[0].step_type == "llm"
        assert steps[1].step_type == "function"
        assert steps[2].step_type == "llm"

    def test_parse_skips_empty_lines(self):
        """Parser skips empty lines."""
        executor = WorkflowExecutor(None, None, None)
        workflow = [
            "1. [llm] step1 - First step",
            "",
            "2. [llm] step2 - Second step",
        ]
        steps = executor.parse_workflow(workflow)

        assert len(steps) == 2

    def test_parse_default_to_llm(self):
        """Lines without [llm] or [function:] default to LLM."""
        executor = WorkflowExecutor(None, None, None)
        steps = executor.parse_workflow(["1. understand_request - Parse intent"])

        assert len(steps) == 1
        assert steps[0].step_type == "llm"


class TestWorkflowExecution:
    """Test workflow execution logic."""

    @pytest.fixture
    def mock_gemini(self):
        """Create mock Gemini client."""
        mock = MagicMock()
        # Return proper Gemini API response format
        mock.generate_content = AsyncMock(
            return_value={"candidates": [{"content": {"parts": [{"text": "LLM response text"}]}}]}
        )
        return mock

    @pytest.fixture
    def mock_packet_service(self):
        """Create mock packet service."""
        mock = MagicMock()
        mock.complete_step = AsyncMock(return_value={"packet_id": "test_123"})
        mock.update_state = AsyncMock(return_value={})
        mock.fail_packet = AsyncMock(return_value={"packet_id": "test_123", "status": "failed"})
        mock.set_awaiting_input = AsyncMock(
            return_value={"packet_id": "test_123", "status": "awaiting_input"}
        )
        return mock

    @pytest.fixture
    def base_context(self):
        """Create base StepContext for testing."""
        return StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test analysis",
            packet_inputs={"grid": {"grid_name": "ExampleGrid"}},
            packet_state={},
            current_step="execute",
            steps_completed=[],
            session_id="session_abc",
            user_email="test@example.com",
        )

    @pytest.fixture
    def base_packet(self):
        """Create base packet dict for testing."""
        return {
            "packet_id": "test_123",
            "packet_type": "grid_analysis",
            "packet_goal": "Test analysis",
            "packet_inputs": {"grid": {"grid_name": "ExampleGrid"}},
            "packet_state": {},
            "steps_completed": [],
            "current_step": "execute",
        }

    @pytest.mark.asyncio
    async def test_execute_empty_workflow(
        self, mock_gemini, mock_packet_service, base_context, base_packet
    ):
        """Empty workflow executes without error (falls back to default LLM step)."""
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        config = MockExpertConfig(workflows={"grid_analysis": []})

        response, state = await executor.execute_workflow(
            expert_config=config,
            packet=base_packet,
            context=base_context,
        )

        # Empty workflow falls back to default LLM step, should still work
        # Response will be from the default execute step
        assert response is not None
        assert "accumulated_results" in state

    @pytest.mark.asyncio
    async def test_execute_llm_only_workflow(
        self, mock_gemini, mock_packet_service, base_context, base_packet
    ):
        """Workflow with only LLM steps executes correctly."""
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        config = MockExpertConfig(
            workflows={
                "grid_analysis": [
                    "1. [llm] understand - Parse the request",
                    "2. [llm] respond - Generate response",
                ]
            }
        )

        response, state = await executor.execute_workflow(
            expert_config=config,
            packet=base_packet,
            context=base_context,
        )

        # Should have called generate_content twice
        assert mock_gemini.generate_content.call_count == 2
        # Should have called complete_step twice
        assert mock_packet_service.complete_step.call_count == 2

    @pytest.mark.asyncio
    async def test_execute_function_step(
        self, mock_gemini, mock_packet_service, base_context, base_packet
    ):
        """Function steps call registered handlers."""
        # Register a test handler to the global registry
        global_registry = get_step_registry()

        async def test_fetch_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(
                data={"metrics": [1, 2, 3]},
                message="Fetched metrics",
                metrics_fetched=True,
            )

        global_registry.register("test_fetch_step", test_fetch_handler)

        try:
            executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

            config = MockExpertConfig(
                workflows={"grid_analysis": ["1. [function:test_fetch_step] - Fetch data"]}
            )

            response, state = await executor.execute_workflow(
                expert_config=config,
                packet=base_packet,
                context=base_context,
            )

            # Should have accumulated results from function step
            assert "accumulated_results" in state
            assert "test_fetch_step" in state["accumulated_results"]
            assert state["accumulated_results"]["test_fetch_step"]["metrics"] == [1, 2, 3]
        finally:
            # Clean up: unregister the test handler
            global_registry.unregister("test_fetch_step")

    @pytest.mark.asyncio
    async def test_execute_function_step_failure(
        self, mock_gemini, mock_packet_service, base_context, base_packet
    ):
        """Function step failure stops workflow."""
        global_registry = get_step_registry()

        async def failing_handler(ctx: StepContext) -> StepResult:
            return StepResult.failure("Database connection failed")

        global_registry.register("failing_step", failing_handler)

        try:
            executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

            config = MockExpertConfig(
                workflows={"grid_analysis": ["1. [function:failing_step] - This will fail"]}
            )

            response, state = await executor.execute_workflow(
                expert_config=config,
                packet=base_packet,
                context=base_context,
            )

            # Should return error message (sanitized, so "failing_step" might not appear)
            # The error message from the handler should still be included
            assert "Database connection failed" in response or "issue" in response.lower()
        finally:
            global_registry.unregister("failing_step")

    @pytest.mark.asyncio
    async def test_execute_needs_user_input(
        self, mock_gemini, mock_packet_service, base_context, base_packet
    ):
        """Workflow pauses when step needs user input."""
        global_registry = get_step_registry()

        async def input_handler(ctx: StepContext) -> StepResult:
            return StepResult.needs_input("Which grid should I analyze?")

        global_registry.register("input_step", input_handler)

        try:
            executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

            config = MockExpertConfig(
                workflows={"grid_analysis": ["1. [function:input_step] - Ask user"]}
            )

            response, state = await executor.execute_workflow(
                expert_config=config,
                packet=base_packet,
                context=base_context,
            )

            # Should return user prompt
            assert "Which grid" in response
            assert state.get("needs_user_input") is True
        finally:
            global_registry.unregister("input_step")

    @pytest.mark.asyncio
    async def test_resume_from_completed_steps(
        self, mock_gemini, mock_packet_service, base_context, base_packet
    ):
        """Workflow resumes from where it left off."""
        # Set up packet with step1 already completed
        base_packet["steps_completed"] = ["step1"]
        base_packet["current_step"] = "step2"

        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        config = MockExpertConfig(
            workflows={
                "grid_analysis": [
                    "1. [llm] step1 - First step",
                    "2. [llm] step2 - Second step",
                ]
            }
        )

        response, state = await executor.execute_workflow(
            expert_config=config,
            packet=base_packet,
            context=base_context,
        )

        # Should only call generate_content once (for step2)
        assert mock_gemini.generate_content.call_count == 1

    @pytest.mark.asyncio
    async def test_accumulated_results_passed_to_llm(
        self, mock_gemini, mock_packet_service, base_context, base_packet
    ):
        """LLM steps receive accumulated results from previous steps."""
        global_registry = get_step_registry()

        async def fetch_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"important_data": "test value"})

        global_registry.register("fetch_data", fetch_handler)

        try:
            executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

            config = MockExpertConfig(
                workflows={
                    "grid_analysis": [
                        "1. [function:fetch_data] - Get data",
                        "2. [llm] analyze - Analyze the data",
                    ]
                }
            )

            await executor.execute_workflow(
                expert_config=config,
                packet=base_packet,
                context=base_context,
            )

            # LLM should have been called with accumulated results in prompt
            call_args = mock_gemini.generate_content.call_args
            # Handle both positional and keyword args
            if call_args[0]:
                payload = call_args[0][0]
            else:
                payload = call_args[1]
            prompt = payload["contents"][0]["parts"][0]["text"]
            assert "fetch_data" in prompt or "important_data" in prompt
        finally:
            global_registry.unregister("fetch_data")


class TestExecuteOneStepSignal:
    """Test _execute_one_step in isolation, verifying the StepLoopSignal contract
    used by _execute_workflow_inner's outer loop after the Phase C Task 3a
    extraction (see workflow_executor.py for the full extraction notes).
    """

    @pytest.fixture
    def mock_packet_service(self):
        mock = MagicMock()
        mock.complete_step = AsyncMock(return_value={"packet_id": "test_123"})
        mock.update_state = AsyncMock(return_value={})
        mock.fail_packet = AsyncMock(return_value={"packet_id": "test_123", "status": "failed"})
        mock.set_awaiting_input = AsyncMock(
            return_value={"packet_id": "test_123", "status": "awaiting_input"}
        )
        return mock

    @pytest.fixture
    def base_context(self):
        return StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test analysis",
            packet_inputs={"grid": {"grid_name": "ExampleGrid"}},
            packet_state={},
            current_step="fetch_data",
            steps_completed=[],
            session_id="session_abc",
            user_email="test@example.com",
        )

    @pytest.fixture
    def base_packet(self):
        return {
            "packet_id": "test_123",
            "packet_type": "grid_analysis",
            "packet_goal": "Test analysis",
            "packet_inputs": {"grid": {"grid_name": "ExampleGrid"}},
            "packet_state": {},
            "steps_completed": [],
            "current_step": "fetch_data",
        }

    @pytest.mark.asyncio
    async def test_advance_signal_for_successful_function_step(
        self, mock_packet_service, base_context, base_packet
    ):
        """A successful function step (no skip_remaining/multi-site) returns
        action="advance" with final_response left unset (None), matching the
        original inline loop's fall-through-to-"advance the index" behavior.
        """
        global_registry = get_step_registry()

        async def ok_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"ok": True})

        global_registry.register("fetch_data", ok_handler)

        try:
            executor = WorkflowExecutor(None, mock_packet_service, None)
            step = ParsedStep(index=0, step_type="function", name="fetch_data", description="d")
            summary = ExecutionSummary(packet_id="test_123", packet_type="grid_analysis")
            accumulated_results: Dict = {}

            signal = await executor._execute_one_step(
                step,
                [step],
                MockExpertConfig(),
                base_packet,
                base_context,
                accumulated_results,
                summary,
                None,
                None,
            )

            assert isinstance(signal, StepLoopSignal)
            assert signal.action == "advance"
            assert signal.final_response is None
            assert accumulated_results["fetch_data"] == {"ok": True}
        finally:
            global_registry.unregister("fetch_data")

    @pytest.mark.asyncio
    async def test_advance_signal_carries_final_response_for_llm_step(
        self, mock_packet_service, base_context, base_packet
    ):
        """A successful LLM step returns action="advance" with final_response
        set to the generated text, since the original inline loop set
        `final_response = result` right before falling through to "advance".
        """
        mock_gemini = MagicMock()
        mock_gemini.generate_content = AsyncMock(
            return_value={"candidates": [{"content": {"parts": [{"text": "hello there"}]}}]}
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)
        step = ParsedStep(index=0, step_type="llm", name="respond", description="d")
        summary = ExecutionSummary(packet_id="test_123", packet_type="grid_analysis")
        accumulated_results: Dict = {}
        config = MockExpertConfig(
            workflows={"grid_analysis": ["1. [llm] respond - Generate response"]}
        )

        signal = await executor._execute_one_step(
            step,
            [step],
            config,
            base_packet,
            base_context,
            accumulated_results,
            summary,
            None,
            None,
        )

        assert signal.action == "advance"
        assert signal.final_response == "hello there"


class TestParsedStep:
    """Test ParsedStep dataclass."""

    def test_parsed_step_fields(self):
        """ParsedStep has expected fields."""
        step = ParsedStep(
            index=0,
            step_type="llm",
            name="understand_request",
            description="Parse user intent",
        )
        assert step.index == 0
        assert step.step_type == "llm"
        assert step.name == "understand_request"
        assert step.description == "Parse user intent"

    def test_parsed_step_serial_default_false(self):
        """ParsedStep.serial defaults to False."""
        step = ParsedStep(index=0, step_type="function", name="test", description="test")
        assert step.serial is False

    def test_parsed_step_serial_flag(self):
        """ParsedStep.serial can be set to True."""
        step = ParsedStep(
            index=0, step_type="function", name="test", description="test", serial=True
        )
        assert step.serial is True


class TestSerialTagParsing:
    """Test [serial] tag detection in workflow parsing."""

    def test_serial_tag_on_function_step(self):
        """[serial] tag is detected and stripped from function steps."""
        executor = WorkflowExecutor(None, None, None)
        steps = executor.parse_workflow(
            ["1. [function:generate_distribution_map] [serial] - Generate site map"]
        )
        assert len(steps) == 1
        assert steps[0].step_type == "function"
        assert steps[0].name == "generate_distribution_map"
        assert steps[0].serial is True
        assert "serial" not in steps[0].description.lower()

    def test_serial_tag_case_insensitive(self):
        """[Serial] tag is detected case-insensitively."""
        executor = WorkflowExecutor(None, None, None)
        steps = executor.parse_workflow(["1. [function:heavy_step] [Serial] - Heavy processing"])
        assert steps[0].serial is True

    def test_no_serial_tag_defaults_false(self):
        """Steps without [serial] have serial=False."""
        executor = WorkflowExecutor(None, None, None)
        steps = executor.parse_workflow(["1. [function:light_step] - Light processing"])
        assert steps[0].serial is False

    def test_serial_tag_on_llm_step(self):
        """[serial] tag works on LLM steps too."""
        executor = WorkflowExecutor(None, None, None)
        steps = executor.parse_workflow(["1. [llm] [serial] synthesize - Synthesize results"])
        assert steps[0].serial is True
        assert steps[0].step_type == "llm"

    def test_mixed_serial_and_parallel_steps(self):
        """Workflow with mix of serial and parallel steps."""
        executor = WorkflowExecutor(None, None, None)
        steps = executor.parse_workflow(
            [
                "1. [function:copy_template] - Copy template",
                "2. [function:generate_map] [serial] - Generate map",
                "3. [function:populate_cells] - Populate cells",
            ]
        )
        assert len(steps) == 3
        assert steps[0].serial is False
        assert steps[1].serial is True
        assert steps[2].serial is False


class TestCloneForSite:
    """Test StepContext.clone_for_site() isolation."""

    def test_clone_has_correct_site_info(self):
        """Cloned context has the site's name and id in state and inputs."""
        ctx = StepContext(
            packet_id="pkt_1",
            packet_type="lpp",
            packet_goal="Generate LPP",
            packet_inputs={"site_name": "original", "grid_name": "original"},
            packet_state={"key": "value"},
            current_step="step1",
            steps_completed=[],
        )

        clone = ctx.clone_for_site(
            site_name="ExampleGrid",
            site_id=42,
            state_snapshot={"key": "value"},
            preserved_results={"resolve_sites": {"data": [1, 2]}},
        )

        assert clone.packet_inputs["site_name"] == "ExampleGrid"
        assert clone.packet_inputs["grid_name"] == "ExampleGrid"
        assert clone.packet_state["site_name"] == "ExampleGrid"
        assert clone.packet_state["site_id"] == 42

    def test_clone_state_isolation(self):
        """Mutating cloned state does not affect original."""
        original_state = {"nested": {"a": 1}, "top": "val"}
        ctx = StepContext(
            packet_id="pkt_1",
            packet_type="lpp",
            packet_goal="Generate LPP",
            packet_inputs={"site_name": "orig"},
            packet_state=original_state,
            current_step="step1",
            steps_completed=[],
        )

        clone = ctx.clone_for_site(
            site_name="SiteA",
            site_id=1,
            state_snapshot=original_state,
            preserved_results={},
        )

        # Mutate clone
        clone.packet_state["nested"]["a"] = 999
        clone.packet_state["new_key"] = "new_val"

        # Original must be unchanged
        assert ctx.packet_state["nested"]["a"] == 1
        assert "new_key" not in ctx.packet_state

    def test_clone_inputs_isolation(self):
        """Mutating cloned inputs does not affect original."""
        ctx = StepContext(
            packet_id="pkt_1",
            packet_type="lpp",
            packet_goal="Generate LPP",
            packet_inputs={"site_name": "orig", "extra": {"nested": True}},
            packet_state={},
            current_step="step1",
            steps_completed=[],
        )

        clone = ctx.clone_for_site(
            site_name="SiteA",
            site_id=1,
            state_snapshot={},
            preserved_results={},
        )

        clone.packet_inputs["extra"]["nested"] = False

        # Original must be unchanged
        assert ctx.packet_inputs["extra"]["nested"] is True

    def test_clone_shares_readonly_resources(self):
        """Clone shares mcp_executor and user_context (read-only)."""
        mock_executor = MagicMock()
        mock_user_ctx = MagicMock()

        ctx = StepContext(
            packet_id="pkt_1",
            packet_type="lpp",
            packet_goal="Generate LPP",
            packet_inputs={},
            packet_state={},
            current_step="step1",
            steps_completed=[],
            mcp_executor=mock_executor,
            user_context=mock_user_ctx,
        )

        clone = ctx.clone_for_site(
            site_name="SiteA",
            site_id=1,
            state_snapshot={},
            preserved_results={},
        )

        # Same object references (shared, not copied)
        assert clone.mcp_executor is mock_executor
        assert clone.user_context is mock_user_ctx

    def test_two_clones_independent(self):
        """Two clones from the same context are independent of each other."""
        ctx = StepContext(
            packet_id="pkt_1",
            packet_type="lpp",
            packet_goal="Generate LPP",
            packet_inputs={"site_name": "orig"},
            packet_state={"data": {"count": 0}},
            current_step="step1",
            steps_completed=[],
        )

        state_snap = {"data": {"count": 0}}
        clone_a = ctx.clone_for_site("SiteA", 1, state_snap, {})
        clone_b = ctx.clone_for_site("SiteB", 2, state_snap, {})

        clone_a.packet_state["data"]["count"] = 100
        clone_a.packet_state["document_url"] = "url_a"

        clone_b.packet_state["data"]["count"] = 200
        clone_b.packet_state["document_url"] = "url_b"

        # Each clone has its own state
        assert clone_a.packet_state["data"]["count"] == 100
        assert clone_b.packet_state["data"]["count"] == 200
        assert clone_a.get_state("document_url") == "url_a"
        assert clone_b.get_state("document_url") == "url_b"


class TestParallelMultiSiteExecution:
    """Test parallel multi-site step execution."""

    @pytest.fixture
    def mock_packet_service(self):
        mock = MagicMock()
        mock.complete_step = AsyncMock(return_value={"packet_id": "test_123"})
        mock.update_state = AsyncMock(return_value={})
        mock.complete_packet = AsyncMock(return_value={})
        mock._log_event = AsyncMock(return_value=None)
        return mock

    @pytest.fixture
    def base_context(self):
        return StepContext(
            packet_id="test_123",
            packet_type="light_preliminary_package",
            packet_goal="Generate LPPs",
            packet_inputs={"site_name": "original"},
            packet_state={"parsed_inputs": {"sites": ["A", "B"]}},
            current_step="copy_template",
            steps_completed=["resolve_sites"],
            session_id="session_abc",
        )

    @pytest.fixture
    def base_packet(self):
        return {
            "id": "uuid-123",
            "packet_id": "test_123",
            "packet_type": "light_preliminary_package",
            "packet_goal": "Generate LPPs",
            "packet_inputs": {},
            "packet_state": {},
            "steps_completed": ["resolve_sites"],
        }

    @pytest.mark.asyncio
    async def test_parallel_execution_runs_all_sites(
        self, mock_packet_service, base_context, base_packet
    ):
        """All sites are executed for each step."""
        call_log = []

        async def mock_handler(ctx):
            call_log.append((ctx.get_state("site_name"), ctx.current_step))
            return StepResult(
                data={"done": True},
                state_updates={"document_url": f"url_{ctx.get_state('site_name')}"},
            )

        with patch(
            "orchestrator.experts.workflow_executor.get_step_handler", return_value=mock_handler
        ):
            executor = WorkflowExecutor(None, mock_packet_service, None)
            sites = [{"name": "SiteA", "id": 1}, {"name": "SiteB", "id": 2}]
            steps = [
                ParsedStep(0, "function", "copy_template", "Copy template"),
                ParsedStep(1, "function", "populate", "Populate cells"),
            ]

            response, state = await executor._execute_multi_site_steps(
                sites_to_process=sites,
                per_site_steps=steps,
                expert_config=None,
                packet=base_packet,
                context=base_context,
                accumulated_results={"resolve_sites": {"sites": ["A", "B"]}},
                execution_summary=MagicMock(
                    add_record=MagicMock(),
                    to_dict=MagicMock(return_value={}),
                    final_status=None,
                ),
            )

        assert "2/2" in response
        assert state["multi_site_results"]["SiteA"]["status"] == "success"
        assert state["multi_site_results"]["SiteB"]["status"] == "success"
        assert state["multi_site_results"]["SiteA"]["document_url"] == "url_SiteA"
        assert state["multi_site_results"]["SiteB"]["document_url"] == "url_SiteB"

    @pytest.mark.asyncio
    async def test_error_isolation_between_sites(
        self, mock_packet_service, base_context, base_packet
    ):
        """One site failing does not prevent other sites from completing."""

        async def mock_handler(ctx):
            site = ctx.get_state("site_name")
            if site == "BadSite":
                return StepResult.failure("Something went wrong")
            return StepResult(
                data={"done": True},
                state_updates={"document_url": f"url_{site}"},
            )

        with patch(
            "orchestrator.experts.workflow_executor.get_step_handler", return_value=mock_handler
        ):
            executor = WorkflowExecutor(None, mock_packet_service, None)
            sites = [
                {"name": "GoodSite", "id": 1},
                {"name": "BadSite", "id": 2},
                {"name": "OtherGood", "id": 3},
            ]
            steps = [ParsedStep(0, "function", "do_work", "Do work")]

            response, state = await executor._execute_multi_site_steps(
                sites_to_process=sites,
                per_site_steps=steps,
                expert_config=None,
                packet=base_packet,
                context=base_context,
                accumulated_results={},
                execution_summary=MagicMock(
                    add_record=MagicMock(),
                    to_dict=MagicMock(return_value={}),
                    final_status=None,
                ),
            )

        results = state["multi_site_results"]
        assert results["GoodSite"]["status"] == "success"
        assert results["BadSite"]["status"] == "failed"
        assert results["OtherGood"]["status"] == "success"
        assert "2/3" in response

    @pytest.mark.asyncio
    async def test_serial_step_runs_sequentially(
        self, mock_packet_service, base_context, base_packet
    ):
        """Steps marked serial run one site at a time (no overlap)."""
        import asyncio

        running = []
        max_concurrent = [0]

        async def mock_handler(ctx):
            running.append(1)
            current = len(running)
            if current > max_concurrent[0]:
                max_concurrent[0] = current
            await asyncio.sleep(0.01)
            running.pop()
            return StepResult(data={"done": True})

        with patch(
            "orchestrator.experts.workflow_executor.get_step_handler", return_value=mock_handler
        ):
            executor = WorkflowExecutor(None, mock_packet_service, None)
            sites = [{"name": f"Site{i}", "id": i} for i in range(3)]
            steps = [ParsedStep(0, "function", "heavy_step", "Heavy", serial=True)]

            await executor._execute_multi_site_steps(
                sites_to_process=sites,
                per_site_steps=steps,
                expert_config=None,
                packet=base_packet,
                context=base_context,
                accumulated_results={},
                execution_summary=MagicMock(
                    add_record=MagicMock(),
                    to_dict=MagicMock(return_value={}),
                    final_status=None,
                ),
            )

        # Serial steps should never have more than 1 running at a time
        assert max_concurrent[0] == 1

    @pytest.mark.asyncio
    async def test_failed_site_skips_subsequent_steps(
        self, mock_packet_service, base_context, base_packet
    ):
        """A site that fails on step 1 is skipped for step 2."""
        call_log = []

        async def mock_handler(ctx):
            site = ctx.get_state("site_name")
            step = ctx.current_step
            call_log.append((site, step))
            if site == "FailSite" and step == "step1":
                return StepResult.failure("Failed early")
            return StepResult(data={"ok": True})

        with patch(
            "orchestrator.experts.workflow_executor.get_step_handler", return_value=mock_handler
        ):
            executor = WorkflowExecutor(None, mock_packet_service, None)
            sites = [{"name": "GoodSite", "id": 1}, {"name": "FailSite", "id": 2}]
            steps = [
                ParsedStep(0, "function", "step1", "Step 1"),
                ParsedStep(1, "function", "step2", "Step 2"),
            ]

            await executor._execute_multi_site_steps(
                sites_to_process=sites,
                per_site_steps=steps,
                expert_config=None,
                packet=base_packet,
                context=base_context,
                accumulated_results={},
                execution_summary=MagicMock(
                    add_record=MagicMock(),
                    to_dict=MagicMock(return_value={}),
                    final_status=None,
                ),
            )

        # FailSite should NOT appear in step2 calls
        step2_sites = [site for site, step in call_log if step == "step2"]
        assert "FailSite" not in step2_sites
        assert "GoodSite" in step2_sites


def test_lpp_injects_community_step_when_anchor_present():
    ex = WorkflowExecutor(None, None, None)
    base = [
        ParsedStep(0, "llm", "parse_request", "x"),
        ParsedStep(1, "function", "create_site_folder", "x"),
    ]
    steps = ex._inject_lpp_entry_steps(base, geo_source="community")
    names = [s.name for s in steps]
    assert "resolve_community_site" in names
    assert names.index("resolve_community_site") < names.index("create_site_folder")
    assert "resolve_sites" not in names


def test_lpp_injects_resolve_sites_for_submission_route():
    ex = WorkflowExecutor(None, None, None)
    base = [
        ParsedStep(0, "llm", "parse_request", "x"),
        ParsedStep(1, "function", "create_site_folder", "x"),
    ]
    steps = ex._inject_lpp_entry_steps(base, geo_source=None)
    names = [s.name for s in steps]
    assert "resolve_sites" in names
    assert "resolve_community_site" not in names


def test_lpp_summary_shows_footprint_source_and_delta():
    ex = WorkflowExecutor(None, None, None)

    class _Ctx:
        def get_state(self, k, d=None):
            return {
                "site_name": "Commville",
                "geo_source": "community",
                "footprint_count": 87,
                "grid3_building_count": 100,
                "footprint_source": "microsoft",
            }.get(k, d)

    lines = ex._format_lpp_summary({}, _Ctx())
    text = "\n".join(lines)
    assert "Footprints" in text and "87" in text
    assert "microsoft" in text.lower()
    assert "100" in text


class TestArtifactSweepOnStateUpdate:
    """Phase B Task 3: after a step's state_updates are applied, any
    `*_drive_id` keys must be swept into the design's artifact history
    (shared/grid_design/artifact_log.sweep_state_for_artifacts) whenever a
    design_id is present in packet_state -- and skipped entirely otherwise.

    Covers both insertion points: the single-site path (execute_workflow)
    and the multi-site per-site path (_execute_site_step).
    """

    @pytest.fixture
    def mock_gemini(self):
        mock = MagicMock()
        mock.generate_content = AsyncMock(
            return_value={"candidates": [{"content": {"parts": [{"text": "LLM response text"}]}}]}
        )
        return mock

    @pytest.fixture
    def mock_packet_service(self):
        mock = MagicMock()
        mock.complete_step = AsyncMock(return_value={"packet_id": "test_123"})
        mock.update_state = AsyncMock(return_value={})
        mock.fail_packet = AsyncMock(return_value={"packet_id": "test_123", "status": "failed"})
        mock.set_awaiting_input = AsyncMock(
            return_value={"packet_id": "test_123", "status": "awaiting_input"}
        )
        return mock

    # ── Single-site path (execute_workflow) ─────────────────────────────

    @pytest.mark.asyncio
    async def test_single_site_sweeps_when_design_id_present(
        self, mock_gemini, mock_packet_service
    ):
        context = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test analysis",
            packet_inputs={},
            packet_state={"design_id": "design-1"},
            current_step="execute",
            steps_completed=[],
            session_id="session_abc",
            user_email="test@example.com",
        )
        packet = {
            "packet_id": "test_123",
            "packet_type": "grid_analysis",
            "packet_goal": "Test analysis",
            "packet_inputs": {},
            "packet_state": {"design_id": "design-1"},
            "steps_completed": [],
            "current_step": "execute",
        }

        global_registry = get_step_registry()

        async def upload_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={}, my_artifact_drive_id="file-xyz")

        global_registry.register("upload_step_single", upload_handler)

        try:
            executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)
            config = MockExpertConfig(
                workflows={"grid_analysis": ["1. [function:upload_step_single] - Upload a file"]}
            )

            with patch(
                "orchestrator.experts.workflow_executor.sweep_state_for_artifacts"
            ) as mock_sweep:
                await executor.execute_workflow(
                    expert_config=config,
                    packet=packet,
                    context=context,
                )

            mock_sweep.assert_called_once_with(
                "design-1", {"my_artifact_drive_id": "file-xyz"}, packet_id="test_123"
            )
        finally:
            global_registry.unregister("upload_step_single")

    @pytest.mark.asyncio
    async def test_single_site_skips_sweep_when_design_id_absent(
        self, mock_gemini, mock_packet_service
    ):
        context = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test analysis",
            packet_inputs={},
            packet_state={},
            current_step="execute",
            steps_completed=[],
            session_id="session_abc",
            user_email="test@example.com",
        )
        packet = {
            "packet_id": "test_123",
            "packet_type": "grid_analysis",
            "packet_goal": "Test analysis",
            "packet_inputs": {},
            "packet_state": {},
            "steps_completed": [],
            "current_step": "execute",
        }

        global_registry = get_step_registry()

        async def upload_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={}, my_artifact_drive_id="file-xyz")

        global_registry.register("upload_step_single_no_design", upload_handler)

        try:
            executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)
            config = MockExpertConfig(
                workflows={
                    "grid_analysis": ["1. [function:upload_step_single_no_design] - Upload a file"]
                }
            )

            with patch(
                "orchestrator.experts.workflow_executor.sweep_state_for_artifacts"
            ) as mock_sweep:
                await executor.execute_workflow(
                    expert_config=config,
                    packet=packet,
                    context=context,
                )

            mock_sweep.assert_not_called()
        finally:
            global_registry.unregister("upload_step_single_no_design")

    # ── Multi-site path (_execute_site_step) ────────────────────────────

    @pytest.mark.asyncio
    async def test_multi_site_sweeps_when_design_id_present(self, mock_gemini, mock_packet_service):
        global_registry = get_step_registry()

        async def upload_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={}, my_artifact_drive_id="file-abc")

        global_registry.register("upload_step_multi", upload_handler)

        try:
            executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

            context = StepContext(
                packet_id="packet-multi",
                packet_type="lpp_multi",
                packet_goal="Test multi-site",
                packet_inputs={},
                packet_state={},
                current_step="execute",
                steps_completed=[],
                session_id="session_multi",
            )
            site_ctx = StepContext(
                packet_id="packet-multi",
                packet_type="lpp_multi",
                packet_goal="Test multi-site",
                packet_inputs={},
                packet_state={"design_id": "design-site-a"},
                current_step="upload_step_multi",
                steps_completed=[],
                session_id="session_multi",
            )

            step = ParsedStep(
                index=0, step_type="function", name="upload_step_multi", description="x"
            )
            packet = {"packet_id": "packet-multi"}
            execution_summary = ExecutionSummary(packet_id="packet-multi", packet_type="lpp_multi")

            with patch(
                "orchestrator.experts.workflow_executor.sweep_state_for_artifacts"
            ) as mock_sweep:
                await executor._execute_site_step(
                    step=step,
                    site={"name": "SiteA"},
                    site_contexts={"SiteA": site_ctx},
                    site_accumulated={"SiteA": {}},
                    per_site_results={},
                    failed_sites=set(),
                    execution_summary=execution_summary,
                    packet=packet,
                    context=context,
                    state_lock=asyncio.Lock(),
                )

            mock_sweep.assert_called_once_with(
                "design-site-a", {"my_artifact_drive_id": "file-abc"}, packet_id="packet-multi"
            )
        finally:
            global_registry.unregister("upload_step_multi")

    @pytest.mark.asyncio
    async def test_multi_site_skips_sweep_when_design_id_absent(
        self, mock_gemini, mock_packet_service
    ):
        global_registry = get_step_registry()

        async def upload_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={}, my_artifact_drive_id="file-abc")

        global_registry.register("upload_step_multi_no_design", upload_handler)

        try:
            executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

            context = StepContext(
                packet_id="packet-multi",
                packet_type="lpp_multi",
                packet_goal="Test multi-site",
                packet_inputs={},
                packet_state={},
                current_step="execute",
                steps_completed=[],
                session_id="session_multi",
            )
            site_ctx = StepContext(
                packet_id="packet-multi",
                packet_type="lpp_multi",
                packet_goal="Test multi-site",
                packet_inputs={},
                packet_state={},  # No design_id
                current_step="upload_step_multi_no_design",
                steps_completed=[],
                session_id="session_multi",
            )

            step = ParsedStep(
                index=0, step_type="function", name="upload_step_multi_no_design", description="x"
            )
            packet = {"packet_id": "packet-multi"}
            execution_summary = ExecutionSummary(packet_id="packet-multi", packet_type="lpp_multi")

            with patch(
                "orchestrator.experts.workflow_executor.sweep_state_for_artifacts"
            ) as mock_sweep:
                await executor._execute_site_step(
                    step=step,
                    site={"name": "SiteA"},
                    site_contexts={"SiteA": site_ctx},
                    site_accumulated={"SiteA": {}},
                    per_site_results={},
                    failed_sites=set(),
                    execution_summary=execution_summary,
                    packet=packet,
                    context=context,
                    state_lock=asyncio.Lock(),
                )

            mock_sweep.assert_not_called()
        finally:
            global_registry.unregister("upload_step_multi_no_design")

    # ── Regression: sweep must go through asyncio.to_thread ─────────────
    #
    # append_design_artifact() (called by sweep_state_for_artifacts) does
    # blocking supabase-py network I/O. Patching sweep_state_for_artifacts
    # directly (as the tests above do) can't tell the difference between
    # `sweep_state_for_artifacts(...)` and `await asyncio.to_thread(
    # sweep_state_for_artifacts, ...)` -- both satisfy
    # `mock_sweep.assert_called_once_with(...)` identically. These tests
    # patch `asyncio.to_thread` itself so a future edit that calls
    # sweep_state_for_artifacts inline (blocking the event loop) fails.

    @pytest.mark.asyncio
    async def test_single_site_sweep_uses_asyncio_to_thread(self, mock_gemini, mock_packet_service):
        context = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test analysis",
            packet_inputs={},
            packet_state={"design_id": "design-1"},
            current_step="execute",
            steps_completed=[],
            session_id="session_abc",
            user_email="test@example.com",
        )
        packet = {
            "packet_id": "test_123",
            "packet_type": "grid_analysis",
            "packet_goal": "Test analysis",
            "packet_inputs": {},
            "packet_state": {"design_id": "design-1"},
            "steps_completed": [],
            "current_step": "execute",
        }

        global_registry = get_step_registry()

        async def upload_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={}, my_artifact_drive_id="file-xyz")

        global_registry.register("upload_step_single_thread", upload_handler)

        try:
            executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)
            config = MockExpertConfig(
                workflows={
                    "grid_analysis": ["1. [function:upload_step_single_thread] - Upload a file"]
                }
            )

            from orchestrator.experts.workflow_executor import sweep_state_for_artifacts

            with patch(
                "orchestrator.experts.workflow_executor.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_to_thread:
                await executor.execute_workflow(
                    expert_config=config,
                    packet=packet,
                    context=context,
                )

            mock_to_thread.assert_called_once_with(
                sweep_state_for_artifacts,
                "design-1",
                {"my_artifact_drive_id": "file-xyz"},
                packet_id="test_123",
            )
        finally:
            global_registry.unregister("upload_step_single_thread")

    @pytest.mark.asyncio
    async def test_multi_site_sweep_uses_asyncio_to_thread(self, mock_gemini, mock_packet_service):
        global_registry = get_step_registry()

        async def upload_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={}, my_artifact_drive_id="file-abc")

        global_registry.register("upload_step_multi_thread", upload_handler)

        try:
            executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

            context = StepContext(
                packet_id="packet-multi",
                packet_type="lpp_multi",
                packet_goal="Test multi-site",
                packet_inputs={},
                packet_state={},
                current_step="execute",
                steps_completed=[],
                session_id="session_multi",
            )
            site_ctx = StepContext(
                packet_id="packet-multi",
                packet_type="lpp_multi",
                packet_goal="Test multi-site",
                packet_inputs={},
                packet_state={"design_id": "design-site-a"},
                current_step="upload_step_multi_thread",
                steps_completed=[],
                session_id="session_multi",
            )

            step = ParsedStep(
                index=0, step_type="function", name="upload_step_multi_thread", description="x"
            )
            packet = {"packet_id": "packet-multi"}
            execution_summary = ExecutionSummary(packet_id="packet-multi", packet_type="lpp_multi")

            from orchestrator.experts.workflow_executor import sweep_state_for_artifacts

            with patch(
                "orchestrator.experts.workflow_executor.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_to_thread:
                await executor._execute_site_step(
                    step=step,
                    site={"name": "SiteA"},
                    site_contexts={"SiteA": site_ctx},
                    site_accumulated={"SiteA": {}},
                    per_site_results={},
                    failed_sites=set(),
                    execution_summary=execution_summary,
                    packet=packet,
                    context=context,
                    state_lock=asyncio.Lock(),
                )

            mock_to_thread.assert_called_once_with(
                sweep_state_for_artifacts,
                "design-site-a",
                {"my_artifact_drive_id": "file-abc"},
                packet_id="packet-multi",
            )
        finally:
            global_registry.unregister("upload_step_multi_thread")


class TestValidateStepPrerequisites:
    """Phase C Task 3b: WorkflowExecutor.validate_step_prerequisites.

    Pure read-only reporting -- no packet_service.update_state or Repository.update
    calls should ever happen from this method. Covers all three availability tiers
    (current packet_state, a prior similar-completed packet, and the Phase B design
    artifact jsonb for `*_drive_id` keys) plus the producer_chain lookup.
    """

    @pytest.fixture
    def mock_packet_service(self):
        mock = MagicMock()
        mock.find_similar_completed = AsyncMock(return_value=[])
        return mock

    @pytest.fixture
    def executor(self, mock_packet_service):
        return WorkflowExecutor(None, mock_packet_service, None)

    @pytest.fixture(autouse=True)
    def _cleanup_registry(self):
        """Ensure any step registered by a test is removed afterwards, even on failure."""
        registered: list[str] = []
        registry = get_step_registry()

        def _register(name, handler=None, contract=None):
            handler = handler or (lambda ctx: None)
            registry.register(name, handler, contract=contract)
            registered.append(name)

        yield _register

        for name in registered:
            registry.unregister(name)

    def _base_packet(self, **overrides) -> Dict:
        packet = {
            "packet_id": "packet-1",
            "packet_type": "light_preliminary_package",
            "packet_goal": "Test",
            "packet_inputs": {},
            "packet_state": {},
            "steps_completed": [],
            "current_step": "some_step",
        }
        packet.update(overrides)
        return packet

    @pytest.mark.asyncio
    async def test_step_with_no_contract_is_satisfied(self, executor, _cleanup_registry):
        """A registered step with no StepContract attached -- nothing to validate."""
        _cleanup_registry("plain_step")

        report = await executor.validate_step_prerequisites(self._base_packet(), "plain_step")

        assert report == PrereqReport(step_name="plain_step", satisfied=True)

    @pytest.mark.asyncio
    async def test_all_prerequisites_present_is_satisfied(self, executor, _cleanup_registry):
        contract = StepContract(
            consumes_state=("site_name", "design_id"),
            consumes_results=("earlier_step",),
            params=(ParamSpec(name="site_name", required=True),),
        )
        _cleanup_registry("full_step", contract=contract)

        packet = self._base_packet(
            packet_state={"site_name": "SiteA", "design_id": "design-1"},
            steps_completed=["earlier_step"],
        )

        report = await executor.validate_step_prerequisites(packet, "full_step")

        assert report.satisfied is True
        assert report.missing_state == ()
        assert report.missing_results == ()
        assert report.missing_params == ()
        assert report.producer_chain == {}

    @pytest.mark.asyncio
    async def test_missing_state_with_no_producer(self, executor, _cleanup_registry):
        contract = StepContract(consumes_state=("orphan_key",))
        _cleanup_registry("needs_orphan", contract=contract)

        report = await executor.validate_step_prerequisites(self._base_packet(), "needs_orphan")

        assert report.satisfied is False
        assert report.missing_state == ("orphan_key",)
        assert "orphan_key" not in report.producer_chain

    @pytest.mark.asyncio
    async def test_missing_state_with_known_producer(self, executor, _cleanup_registry):
        producer_contract = StepContract(produces_state=("shared_key",))
        consumer_contract = StepContract(consumes_state=("shared_key",))
        _cleanup_registry("produces_shared", contract=producer_contract)
        _cleanup_registry("consumes_shared", contract=consumer_contract)

        report = await executor.validate_step_prerequisites(self._base_packet(), "consumes_shared")

        assert report.missing_state == ("shared_key",)
        assert report.producer_chain["shared_key"] == ("produces_shared",)

    @pytest.mark.asyncio
    async def test_missing_results_not_in_steps_completed(self, executor, _cleanup_registry):
        contract = StepContract(consumes_results=("prior_step",))
        _cleanup_registry("needs_prior_result", contract=contract)
        # "prior_step" itself must be a registered step for the trivial producer
        # mapping to be meaningful, but validate_step_prerequisites doesn't
        # require that -- it maps unconditionally.
        _cleanup_registry("prior_step")

        packet = self._base_packet(steps_completed=[])

        report = await executor.validate_step_prerequisites(packet, "needs_prior_result")

        assert report.satisfied is False
        assert report.missing_results == ("prior_step",)
        assert report.producer_chain["prior_step"] == ("prior_step",)

    @pytest.mark.asyncio
    async def test_missing_required_param_no_default(self, executor, _cleanup_registry):
        contract = StepContract(
            params=(ParamSpec(name="site_name", required=True),),
        )
        _cleanup_registry("needs_param", contract=contract)

        report = await executor.validate_step_prerequisites(self._base_packet(), "needs_param")

        assert report.missing_params == ("site_name",)
        assert report.satisfied is False

    @pytest.mark.asyncio
    async def test_missing_non_required_param_not_reported(self, executor, _cleanup_registry):
        contract = StepContract(
            params=(ParamSpec(name="optional_thing", required=False),),
        )
        _cleanup_registry("optional_param_step", contract=contract)

        report = await executor.validate_step_prerequisites(
            self._base_packet(), "optional_param_step"
        )

        assert report.missing_params == ()
        assert report.satisfied is True

    @pytest.mark.asyncio
    async def test_missing_required_param_with_default_not_reported(
        self, executor, _cleanup_registry
    ):
        contract = StepContract(
            params=(ParamSpec(name="phases", required=True, default="1"),),
        )
        _cleanup_registry("param_with_default_step", contract=contract)

        report = await executor.validate_step_prerequisites(
            self._base_packet(), "param_with_default_step"
        )

        assert report.missing_params == ()
        assert report.satisfied is True

    @pytest.mark.asyncio
    async def test_tier2_similar_completed_packet_satisfies_missing_state(
        self, mock_packet_service, executor, _cleanup_registry
    ):
        contract = StepContract(consumes_state=("total_kwp",))
        _cleanup_registry("needs_kwp", contract=contract)

        mock_packet_service.find_similar_completed = AsyncMock(
            return_value=[{"packet_state": {"total_kwp": 12.5}}]
        )

        packet = self._base_packet(packet_inputs={"site_name": "SiteA"})

        report = await executor.validate_step_prerequisites(packet, "needs_kwp")

        assert report.satisfied is True
        assert report.missing_state == ()
        mock_packet_service.find_similar_completed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tier2_lookup_exception_falls_through_to_missing(
        self, mock_packet_service, executor, _cleanup_registry
    ):
        contract = StepContract(consumes_state=("total_kwp",))
        _cleanup_registry("needs_kwp_2", contract=contract)

        mock_packet_service.find_similar_completed = AsyncMock(
            side_effect=RuntimeError("db unavailable")
        )

        packet = self._base_packet(packet_inputs={"site_name": "SiteA"})

        # Must not raise -- Tier 2 failure is non-fatal.
        report = await executor.validate_step_prerequisites(packet, "needs_kwp_2")

        assert report.satisfied is False
        assert report.missing_state == ("total_kwp",)

    @pytest.mark.asyncio
    async def test_tier3_design_artifact_satisfies_missing_drive_id(
        self, executor, _cleanup_registry
    ):
        contract = StepContract(consumes_state=("distribution_network_drive_id",))
        _cleanup_registry("needs_drive_id", contract=contract)

        packet = self._base_packet(packet_state={"design_id": "design-1"})

        mock_design_row = {"artifacts": {"distribution_network": [{"drive_file_id": "abc"}]}}
        with patch("orchestrator.experts.workflow_executor.Repository") as mock_repo_cls:
            mock_repo_cls.return_value.get.return_value = mock_design_row
            report = await executor.validate_step_prerequisites(packet, "needs_drive_id")

        assert report.satisfied is True
        assert report.missing_state == ()

    @pytest.mark.asyncio
    async def test_tier3_empty_artifacts_still_reported_missing(self, executor, _cleanup_registry):
        contract = StepContract(consumes_state=("distribution_network_drive_id",))
        _cleanup_registry("needs_drive_id_2", contract=contract)

        packet = self._base_packet(packet_state={"design_id": "design-1"})

        mock_design_row = {"artifacts": {}}
        with patch("orchestrator.experts.workflow_executor.Repository") as mock_repo_cls:
            mock_repo_cls.return_value.get.return_value = mock_design_row
            report = await executor.validate_step_prerequisites(packet, "needs_drive_id_2")

        assert report.satisfied is False
        assert report.missing_state == ("distribution_network_drive_id",)

    @pytest.mark.asyncio
    async def test_tier3_null_artifacts_column_does_not_raise(self, executor, _cleanup_registry):
        """gd_designs.artifacts is a nullable jsonb column -- a pre-existing or
        backfilled design row can have `artifacts IS NULL` (not `{}`, not an
        absent key). `design.get("artifacts", {})` only substitutes the
        default when the KEY is missing, so a literal `None` value used to
        reach `.get(artifact_type)` and raise AttributeError, crashing this
        supposedly pure-reporting method. It must instead be reported as a
        (correctly) missing prerequisite.
        """
        contract = StepContract(consumes_state=("distribution_network_drive_id",))
        _cleanup_registry("needs_drive_id_null", contract=contract)

        packet = self._base_packet(packet_state={"design_id": "design-1"})

        mock_design_row = {"artifacts": None}
        with patch("orchestrator.experts.workflow_executor.Repository") as mock_repo_cls:
            mock_repo_cls.return_value.get.return_value = mock_design_row
            report = await executor.validate_step_prerequisites(packet, "needs_drive_id_null")

        assert report.satisfied is False
        assert report.missing_state == ("distribution_network_drive_id",)

    @pytest.mark.asyncio
    async def test_required_param_satisfied_from_packet_inputs_only(
        self, executor, _cleanup_registry
    ):
        """Required params supplied at packet-creation time live in
        `packet_inputs`, not yet copied into `packet_state`. `get_parameter_value()`
        resolves these fine at runtime (packet_inputs outranks packet_state in
        its precedence order), so validate_step_prerequisites must not falsely
        report them as missing just because packet_state doesn't have them yet.
        """
        contract = StepContract(
            params=(ParamSpec(name="site_name", required=True),),
        )
        _cleanup_registry("needs_param_from_inputs", contract=contract)

        packet = self._base_packet(
            packet_inputs={"site_name": "SiteA"},
            packet_state={},
        )

        report = await executor.validate_step_prerequisites(packet, "needs_param_from_inputs")

        assert report.missing_params == ()
        assert report.satisfied is True

    @pytest.mark.asyncio
    async def test_falsy_but_present_state_value_is_not_reported_missing(
        self, executor, _cleanup_registry
    ):
        """A legitimately-set falsy value (0, "", False) is genuinely present
        in packet_state. StepContext.get_state() returns it as-is via
        `packet_state.get(key, default)` rather than treating it as absent, so
        `_available()` must use a presence check (`key in packet_state`), not
        a truthiness check, to stay consistent with actual runtime behavior.
        """
        contract = StepContract(consumes_state=("phase_count",))
        _cleanup_registry("needs_falsy_state", contract=contract)

        packet = self._base_packet(packet_state={"phase_count": 0})

        report = await executor.validate_step_prerequisites(packet, "needs_falsy_state")

        assert report.satisfied is True
        assert report.missing_state == ()

    @pytest.mark.asyncio
    async def test_unregistered_step_name_raises_value_error(self, executor):
        with pytest.raises(ValueError):
            await executor.validate_step_prerequisites(
                self._base_packet(), "totally_unregistered_step_xyz"
            )

    # -- Phase D regression: generate_distribution_map on the community route --

    @pytest.mark.asyncio
    async def test_generate_distribution_map_satisfied_after_community_route_producers(
        self, executor
    ):
        """Reproduces the exact bug a holistic post-merge review found in the
        real, registered `generate_distribution_map` StepContract (not a
        synthetic/mock one) -- the bug `optional_consumes_state` exists to fix.

        Before the fix, `generate_distribution_map`'s `consumes_state` listed
        several keys (`selected_site_id`, `use_site_submission_layout`,
        `layout_result`, `site_options_map_b64`, `community_boundary_drive_id`,
        `community_buildings_drive_id`, among others audited in the same pass)
        that are read via `context.get_state(...)` with genuine in-body
        fallback logic and are never produced by ANY step's `produces_state`.
        Because `validate_step_prerequisites` treated every `consumes_state`
        entry as a hard requirement with no exception, those keys could NEVER
        be satisfied -- not directly (nothing produces them) and not via
        `producer_chain` (same reason) -- so this step could never be reported
        `satisfied=True` on the community route, even with every *real*
        prerequisite met. That defeated the entire point of `run_single_step`
        being able to re-run this step standalone.

        This test re-registers the REAL production (handler, contract) pairs
        for resolve_community_site / create_site_folder /
        generate_distribution_layout / generate_distribution_map (snapshotted
        at module-import time in `_REAL_PACKAGE_GENERATOR_STEPS`, immune to
        `test_parameter_confirmation.py`'s registry `.clear()` running first),
        then builds `packet_state` exactly as it would look after
        resolve_community_site, create_site_folder, and
        generate_distribution_layout have ALL genuinely completed on the
        community route -- populating only the real keys each step's own
        `produces_state` declares, not a hand-picked/synthetic set. It does
        NOT populate the optional keys that genuinely have no producer
        (selected_site_id, layout_result, site_options_map_b64,
        use_site_submission_layout, etc.) -- proving `satisfied` no longer
        depends on them, while `missing_optional_state` still surfaces them
        informationally. It DOES include community_boundary_drive_id /
        community_buildings_drive_id, since resolve_community_site's real
        produces_state genuinely writes those (the working cross-execution
        Drive-download fallback `load_site_row_data()` now uses), so those
        two must NOT show up in `missing_optional_state` here.
        """
        registry = get_step_registry()
        for name in _REAL_PACKAGE_GENERATOR_STEP_NAMES:
            handler, contract = _REAL_PACKAGE_GENERATOR_STEPS[name]
            assert contract is not None, f"{name} lost its production StepContract"
            registry.register(name, handler, contract=contract)

        map_contract = registry.get_contract("generate_distribution_map")
        layout_contract = registry.get_contract("generate_distribution_layout")
        community_contract = registry.get_contract("resolve_community_site")
        folder_contract = registry.get_contract("create_site_folder")

        # packet_state populated ONLY from each real step's own produces_state --
        # exactly what would be in packet_state after these 3 steps completed on
        # the community route (resolve_community_site instead of resolve_sites).
        packet_state: Dict = {}
        # resolve_community_site's real produces_state.
        assert set(community_contract.produces_state) >= {
            "geo_source",
            "site_name",
            "community_state",
            "footprint_count",
        }
        packet_state.update(
            {
                "geo_source": "community",
                "site_name": "Test Community",
                "community_state": "Kaduna",
                "footprint_count": 42,
                "footprint_source": "overture",
                "grid3_building_count": 40,
                "community_boundary_drive_id": "drive-boundary-id",
                "community_buildings_drive_id": "drive-buildings-id",
            }
        )
        # create_site_folder's real produces_state.
        assert folder_contract.produces_state == ("site_folder_id",)
        packet_state["site_folder_id"] = "drive-folder-id"
        # generate_distribution_layout's real produces_state.
        assert set(layout_contract.produces_state) >= {
            "layout_generated",
            "site_candidates",
        }
        packet_state.update(
            {
                "layout_generated": True,
                "layout_coverage_pct": 92.5,
                "site_options_drive_id": "drive-options-id",
                "site_candidates": [{"rank": 1, "lat": 10.0, "lon": 7.0}],
                "editable_pole_spacing_m": 45.0,
                "editable_max_drop_distance_m": 40.0,
                "editable_target_coverage_pct": 90.0,
                "editable_number_of_phases": "1",
            }
        )

        packet = self._base_packet(
            packet_state=packet_state,
            steps_completed=[
                "resolve_community_site",
                "create_site_folder",
                "generate_distribution_layout",
            ],
        )

        report = await executor.validate_step_prerequisites(packet, "generate_distribution_map")

        # The fix: satisfied is True now that generate_distribution_map's only
        # real hard requirement (site_name) is met and both consumed results
        # have completed -- this is the line that used to be impossible.
        assert report.satisfied is True
        assert report.missing_state == ()
        assert report.missing_results == ()
        assert report.missing_params == ()

        # Not a coincidence: prove the informational channel actually works by
        # asserting specific, still-genuinely-unproduced optional keys show up
        # (the same keys that used to hard-block `satisfied` pre-fix).
        assert "layout_result" in report.missing_optional_state
        assert "site_options_map_b64" in report.missing_optional_state
        assert "selected_site_id" in report.missing_optional_state
        assert "use_site_submission_layout" in report.missing_optional_state
        # Sanity: keys that WERE actually supplied above must not be reported
        # missing, optional or otherwise.
        assert "geo_source" not in report.missing_optional_state
        assert "site_candidates" not in report.missing_optional_state
        assert "site_folder_id" not in report.missing_optional_state
        assert "community_state" not in report.missing_optional_state
        # community_boundary_drive_id / community_buildings_drive_id are
        # genuinely produced by resolve_community_site (populated in
        # packet_state above) and are the working Drive-download fallback
        # load_site_row_data() now uses -- must not show up as missing.
        assert "community_boundary_drive_id" not in report.missing_optional_state
        assert "community_buildings_drive_id" not in report.missing_optional_state
        # These optional keys must never feed producer_chain (nothing to
        # auto-run for an opportunistic-fallback read).
        for key in report.missing_optional_state:
            assert key not in report.producer_chain

        # Confirm this is exercising the real production contract, not a
        # stand-in -- site_name is its one real hard dependency.
        assert map_contract.consumes_state == ("site_name",)


class TestRunSingleStep:
    """Phase C Task 4: WorkflowExecutor.run_single_step.

    Runs exactly one named step out of normal workflow order. Uses
    packet_type="light_preliminary_package" throughout (an INTERACTIVE_PACKET_TYPES
    member) so the parameter-confirmation flow never intercepts function-step
    execution -- matching TestValidateStepPrerequisites's own convention.
    """

    @pytest.fixture
    def mock_packet_service(self):
        mock = MagicMock()
        mock.complete_step = AsyncMock(return_value={"packet_id": "packet-1"})
        mock.update_state = AsyncMock(return_value={})
        mock.fail_packet = AsyncMock(return_value={"packet_id": "packet-1", "status": "failed"})
        mock.set_awaiting_input = AsyncMock(return_value={"packet_id": "packet-1"})
        mock.find_similar_completed = AsyncMock(return_value=[])
        mock.mark_step_incomplete = AsyncMock(return_value={"packet_id": "packet-1"})
        return mock

    @pytest.fixture
    def executor(self, mock_packet_service):
        return WorkflowExecutor(None, mock_packet_service, None)

    @pytest.fixture(autouse=True)
    def _cleanup_registry(self):
        """Ensure any step registered by a test is removed afterwards, even on failure."""
        registered: list[str] = []
        registry = get_step_registry()

        def _register(name, handler=None, contract=None):
            handler = handler or (lambda ctx: None)
            registry.register(name, handler, contract=contract)
            registered.append(name)

        yield _register

        for name in registered:
            registry.unregister(name)

    def _base_packet(self, **overrides) -> Dict:
        packet = {
            "packet_id": "packet-1",
            "packet_type": "light_preliminary_package",
            "packet_goal": "Test",
            "packet_inputs": {},
            "packet_state": {},
            "steps_completed": [],
            "current_step": "some_step",
        }
        packet.update(overrides)
        return packet

    def _base_context(self, **overrides) -> StepContext:
        defaults = dict(
            packet_id="packet-1",
            packet_type="light_preliminary_package",
            packet_goal="Test",
            packet_inputs={},
            packet_state={},
            current_step="some_step",
            steps_completed=[],
            session_id="session_abc",
        )
        defaults.update(overrides)
        return StepContext(**defaults)

    # -- Step 0: v1 guards ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_multi_site_packet_refused(self, executor, _cleanup_registry):
        _cleanup_registry("some_step")
        context = self._base_context(packet_state={"sites_to_process": ["A", "B"]})
        packet = self._base_packet()

        with patch.object(executor, "_execute_one_step", new=AsyncMock()) as mock_exec:
            msg, data = await executor.run_single_step(
                packet, "some_step", context, MockExpertConfig()
            )

        assert data == {"error": "unsupported_multi_site", "refused": True}
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_step_refused(self, executor):
        context = self._base_context()
        packet = self._base_packet()

        msg, data = await executor.run_single_step(
            packet, "totally_unregistered_step_xyz", context, MockExpertConfig()
        )

        assert data == {"error": "unknown_step", "refused": True}

    # -- Step 0 (cont'd): "actively running" guard (Phase C Task 5) -----------

    @pytest.mark.asyncio
    async def test_actively_running_packet_refused(self, executor, _cleanup_registry):
        """in_progress + updated_at very recent -> refused, _execute_one_step
        never called."""
        _cleanup_registry("some_step")
        context = self._base_context()
        recent = (
            (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        )
        packet = self._base_packet(packet_status="in_progress", updated_at=recent)

        with patch.object(executor, "_execute_one_step", new=AsyncMock()) as mock_exec:
            msg, data = await executor.run_single_step(
                packet, "some_step", context, MockExpertConfig()
            )

        assert data["error"] == "packet_actively_running"
        assert data["refused"] is True
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_in_progress_but_stale_updated_at_not_refused(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        """in_progress + updated_at well past the "actively running" threshold
        -> NOT refused for this reason; proceeds normally."""

        async def handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"ok": True})

        _cleanup_registry("some_step", handler=handler)
        context = self._base_context()
        stale = (
            (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        )
        packet = self._base_packet(packet_status="in_progress", updated_at=stale)

        msg, data = await executor.run_single_step(packet, "some_step", context, MockExpertConfig())

        assert data.get("error") != "packet_actively_running"
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_non_in_progress_status_not_refused_regardless_of_recency(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        """packet_status other than "in_progress" (e.g. "completed") -> NOT
        refused for this reason regardless of updated_at recency."""

        async def handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"ok": True})

        _cleanup_registry("some_step", handler=handler)
        context = self._base_context()
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        packet = self._base_packet(packet_status="completed", updated_at=recent)

        msg, data = await executor.run_single_step(packet, "some_step", context, MockExpertConfig())

        assert data.get("error") != "packet_actively_running"
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_missing_updated_at_fails_open(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        """Missing/unparseable updated_at -> fails open (does not refuse for
        this reason)."""

        async def handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"ok": True})

        _cleanup_registry("some_step", handler=handler)
        context = self._base_context()
        packet = self._base_packet(packet_status="in_progress")
        packet.pop("updated_at", None)

        msg, data = await executor.run_single_step(packet, "some_step", context, MockExpertConfig())

        assert data.get("error") != "packet_actively_running"
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_unparseable_updated_at_fails_open(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        """Unparseable updated_at string -> fails open (does not refuse)."""

        async def handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"ok": True})

        _cleanup_registry("some_step", handler=handler)
        context = self._base_context()
        packet = self._base_packet(packet_status="in_progress", updated_at="not-a-timestamp")

        msg, data = await executor.run_single_step(packet, "some_step", context, MockExpertConfig())

        assert data.get("error") != "packet_actively_running"
        assert data["success"] is True

    # -- Step 1: already-completed check ---------------------------------------

    @pytest.mark.asyncio
    async def test_already_completed_not_forced(self, executor, _cleanup_registry):
        _cleanup_registry("done_step")
        context = self._base_context()
        packet = self._base_packet(steps_completed=["done_step"])

        with patch.object(executor, "_execute_one_step", new=AsyncMock()) as mock_exec:
            msg, data = await executor.run_single_step(
                packet, "done_step", context, MockExpertConfig()
            )

        assert data == {"already_completed": True}
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_reruns_completed_step(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        contract = StepContract(guard_keys=("already_done_flag",))
        guard_key_seen_at_execution: Dict = {}

        async def handler(ctx: StepContext) -> StepResult:
            # Regression check: the guard key must actually have been cleared
            # from packet_state (via mark_step_incomplete + the packet/context
            # refresh) by the time the handler runs. If a future change to the
            # force-path's refresh silently breaks this, the guard key would
            # still be present here and this must fail, not silently pass.
            guard_key_seen_at_execution["value"] = ctx.get_state("already_done_flag")
            return StepResult.success(data={"ok": True})

        _cleanup_registry("done_step_force", handler=handler, contract=contract)

        context = self._base_context(packet_state={"already_done_flag": True})
        packet = self._base_packet(
            packet_state={"already_done_flag": True}, steps_completed=["done_step_force"]
        )

        # After mark_step_incomplete, run_single_step re-fetches the packet --
        # simulate the DB now reflecting the step removed from steps_completed
        # AND the guard key cleared from packet_state.
        refreshed_packet = self._base_packet(steps_completed=[], packet_state={})
        mock_packet_service.get_packet = AsyncMock(return_value=refreshed_packet)

        msg, data = await executor.run_single_step(
            packet, "done_step_force", context, MockExpertConfig(), force=True
        )

        mock_packet_service.mark_step_incomplete.assert_awaited_once()
        call = mock_packet_service.mark_step_incomplete.await_args
        assert call.args[0] == "packet-1"
        assert call.args[1] == "done_step_force"
        assert call.kwargs["clear_state_keys"] == ["already_done_flag"]

        assert guard_key_seen_at_execution["value"] is None
        assert data["success"] is True
        mock_packet_service.complete_step.assert_awaited_once()

    # -- Step 2: prerequisite check ---------------------------------------------

    @pytest.mark.asyncio
    async def test_satisfied_prerequisites_executes_and_completes(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        contract = StepContract(consumes_state=("site_name",))

        async def handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"result": "ok"})

        _cleanup_registry("needs_site", handler=handler, contract=contract)

        context = self._base_context(packet_state={"site_name": "SiteA"})
        packet = self._base_packet(packet_state={"site_name": "SiteA"})

        msg, data = await executor.run_single_step(
            packet, "needs_site", context, MockExpertConfig()
        )

        assert data["success"] is True
        mock_packet_service.complete_step.assert_awaited_once_with(
            "packet-1", "needs_site", next_step=None, session_id="session_abc"
        )

    @pytest.mark.asyncio
    async def test_run_single_step_proceeds_when_tier2_prior_packet_supplies_missing_state(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        """The ONLY reason validate_step_prerequisites reports satisfied=True
        here is Tier 2 (a prior similar-completed packet's packet_state, see
        TestValidateStepPrerequisites.test_tier2_similar_completed_packet_satisfies_missing_state)
        -- the current packet's own packet_state has nothing for `total_kwp`.
        This must not be refused as needs_user_input; run_single_step should
        proceed all the way to actually executing the target step, not merely
        report it as satisfiable (that in-isolation check is already covered
        by the Tier 2 test in TestValidateStepPrerequisites)."""
        contract = StepContract(consumes_state=("total_kwp",))

        async def handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"result": "ok"})

        _cleanup_registry("needs_kwp_tier2", handler=handler, contract=contract)

        mock_packet_service.find_similar_completed = AsyncMock(
            return_value=[{"packet_state": {"total_kwp": 12.5}}]
        )

        context = self._base_context(packet_inputs={"site_name": "SiteA"})
        packet = self._base_packet(packet_inputs={"site_name": "SiteA"})

        msg, data = await executor.run_single_step(
            packet, "needs_kwp_tier2", context, MockExpertConfig()
        )

        assert data.get("needs_user_input") is not True
        assert data["success"] is True
        mock_packet_service.find_similar_completed.assert_awaited_once()
        mock_packet_service.complete_step.assert_awaited_once_with(
            "packet-1", "needs_kwp_tier2", next_step=None, session_id="session_abc"
        )

    @pytest.mark.asyncio
    async def test_missing_prerequisites_not_resolved(self, executor, _cleanup_registry):
        contract = StepContract(consumes_state=("missing_key",))
        _cleanup_registry("needs_missing", contract=contract)

        context = self._base_context()
        packet = self._base_packet()

        with patch.object(executor, "_execute_one_step", new=AsyncMock()) as mock_exec:
            msg, data = await executor.run_single_step(
                packet, "needs_missing", context, MockExpertConfig()
            )

        assert data["needs_user_input"] is True
        assert data["missing_state"] == ["missing_key"]
        assert data["missing_results"] == []
        assert data["missing_params"] == []
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_missing_prerequisites_producer_resolves_gap(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        producer_contract = StepContract(produces_state=("shared_key",))
        consumer_contract = StepContract(consumes_state=("shared_key",))

        async def producer_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"produced": True}, shared_key="value")

        async def consumer_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"consumed": True})

        _cleanup_registry("produces_shared", handler=producer_handler, contract=producer_contract)
        _cleanup_registry("consumes_shared", handler=consumer_handler, contract=consumer_contract)

        context = self._base_context()
        packet = self._base_packet()

        # After the producer pass, run_single_step re-fetches the packet --
        # simulate the DB now reflecting the producer's state_updates.
        packet_after_producer = self._base_packet(packet_state={"shared_key": "value"})
        mock_packet_service.get_packet = AsyncMock(return_value=packet_after_producer)

        msg, data = await executor.run_single_step(
            packet,
            "consumes_shared",
            context,
            MockExpertConfig(),
            run_missing_prerequisites=True,
        )

        assert data["success"] is True
        # Both the auto-run producer and the target step call complete_step.
        assert mock_packet_service.complete_step.await_count == 2

    @pytest.mark.asyncio
    async def test_producer_result_visible_to_consumer_via_get_previous_result(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        """Regression test for the accumulated_results-wiping bug: Step 3 of
        run_single_step used to pass a fresh accumulated_results={} into
        _execute_one_step, which _execute_function_step then assigned onto
        context.accumulated_results *unconditionally* before the handler ran --
        wiping out whatever a just-run producer step (auto-run via
        run_missing_prerequisites) had written there moments earlier. As a
        result, the consumer step's context.get_previous_result("producer_step")
        came back None even though the producer had just succeeded.

        This must FAIL against the pre-fix code (bare `accumulated_results={}`
        in run_single_step's Step 3) and PASS once that line is changed to
        `context.accumulated_results.copy()`.
        """
        consumer_contract = StepContract(consumes_results=("producer_step",))

        async def producer_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"key": "value"})

        captured: Dict = {}

        async def consumer_handler(ctx: StepContext) -> StepResult:
            captured["previous"] = ctx.get_previous_result("producer_step")
            return StepResult.success(data={"consumed": True})

        _cleanup_registry("producer_step", handler=producer_handler)
        _cleanup_registry("consumer_step", handler=consumer_handler, contract=consumer_contract)

        context = self._base_context()
        packet = self._base_packet()

        # After the producer auto-runs, run_single_step re-fetches the packet --
        # simulate the DB now reflecting producer_step as completed.
        packet_after_producer = self._base_packet(steps_completed=["producer_step"])
        mock_packet_service.get_packet = AsyncMock(return_value=packet_after_producer)

        msg, data = await executor.run_single_step(
            packet,
            "consumer_step",
            context,
            MockExpertConfig(),
            run_missing_prerequisites=True,
        )

        assert data["success"] is True
        assert captured["previous"] == {"key": "value"}

    @pytest.mark.asyncio
    async def test_run_missing_prerequisites_no_producer_available(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        contract = StepContract(consumes_state=("orphan_key",))
        _cleanup_registry("needs_orphan_rs", contract=contract)

        context = self._base_context()
        packet = self._base_packet()

        # Unchanged -- nothing can produce orphan_key, so re-validation still fails.
        mock_packet_service.get_packet = AsyncMock(return_value=packet)

        with patch.object(executor, "_execute_one_step", new=AsyncMock()) as mock_exec:
            msg, data = await executor.run_single_step(
                packet,
                "needs_orphan_rs",
                context,
                MockExpertConfig(),
                run_missing_prerequisites=True,
            )

        assert data["needs_user_input"] is True
        assert data["missing_state"] == ["orphan_key"]
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cycle_guard_terminates(self, executor, mock_packet_service, _cleanup_registry):
        """Two steps whose contracts claim to produce each other's missing
        dependency must not cause infinite recursion. The cycle guard means
        NEITHER step ever actually executes (each bails out reporting missing
        prerequisites once it sees its producer already in the visited set).
        """
        contract_a = StepContract(consumes_state=("key_b",), produces_state=("key_a",))
        contract_b = StepContract(consumes_state=("key_a",), produces_state=("key_b",))

        _cleanup_registry("cycle_step_a", contract=contract_a)
        _cleanup_registry("cycle_step_b", contract=contract_b)

        context = self._base_context()
        packet = self._base_packet()

        # Nothing ever gets produced, so every re-fetch returns the same packet.
        mock_packet_service.get_packet = AsyncMock(return_value=packet)

        original_execute = executor._execute_one_step
        call_count = {"n": 0}

        async def counting_execute(*args, **kwargs):
            call_count["n"] += 1
            return await original_execute(*args, **kwargs)

        with patch.object(executor, "_execute_one_step", side_effect=counting_execute):
            msg, data = await executor.run_single_step(
                packet,
                "cycle_step_a",
                context,
                MockExpertConfig(),
                run_missing_prerequisites=True,
            )

        assert call_count["n"] == 0
        assert data["needs_user_input"] is True

    # -- Step 3: overrides + signal handling -------------------------------------

    @pytest.mark.asyncio
    async def test_param_overrides_applied_before_execution(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        captured: Dict = {}

        async def handler(ctx: StepContext) -> StepResult:
            captured["value"] = ctx.get_parameter_value("total_kwp")
            return StepResult.success(data={"ok": True})

        _cleanup_registry("uses_override", handler=handler)

        context = self._base_context()
        packet = self._base_packet()

        msg, data = await executor.run_single_step(
            packet,
            "uses_override",
            context,
            MockExpertConfig(),
            param_overrides={"total_kwp": 42},
        )

        assert captured["value"] == 42
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_advance_signal_completes_step(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        _cleanup_registry("plain_step_advance")
        context = self._base_context()
        packet = self._base_packet()

        fake_signal = StepLoopSignal(action="advance", final_response="done")
        with patch.object(executor, "_execute_one_step", new=AsyncMock(return_value=fake_signal)):
            msg, data = await executor.run_single_step(
                packet, "plain_step_advance", context, MockExpertConfig()
            )

        mock_packet_service.complete_step.assert_awaited_once_with(
            "packet-1", "plain_step_advance", next_step=None, session_id="session_abc"
        )
        assert data["success"] is True
        assert data["final_response"] == "done"

    @pytest.mark.asyncio
    async def test_break_signal_completes_step(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        _cleanup_registry("plain_step_break")
        context = self._base_context()
        packet = self._base_packet()

        fake_signal = StepLoopSignal(action="break")
        with patch.object(executor, "_execute_one_step", new=AsyncMock(return_value=fake_signal)):
            msg, data = await executor.run_single_step(
                packet, "plain_step_break", context, MockExpertConfig()
            )

        mock_packet_service.complete_step.assert_awaited_once()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_return_signal_passed_through(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        _cleanup_registry("plain_step_return")
        context = self._base_context()
        packet = self._base_packet()

        fake_signal = StepLoopSignal(
            action="return",
            return_value=("Some error message", {"error": "boom"}),
        )
        with patch.object(executor, "_execute_one_step", new=AsyncMock(return_value=fake_signal)):
            msg, data = await executor.run_single_step(
                packet, "plain_step_return", context, MockExpertConfig()
            )

        assert msg == "Some error message"
        assert data == {"error": "boom"}
        mock_packet_service.complete_step.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retry_signal_refused(self, executor, mock_packet_service, _cleanup_registry):
        _cleanup_registry("plain_step_retry")
        context = self._base_context()
        packet = self._base_packet()

        fake_signal = StepLoopSignal(action="retry")
        with patch.object(executor, "_execute_one_step", new=AsyncMock(return_value=fake_signal)):
            msg, data = await executor.run_single_step(
                packet, "plain_step_retry", context, MockExpertConfig()
            )

        assert data == {"error": "unsupported_retry", "refused": True}
        mock_packet_service.complete_step.assert_not_awaited()

    # -- Multi-site discovery mid-single-step (Issue 1 regression) --------------

    @pytest.mark.asyncio
    async def test_multi_site_discovery_mid_run_refused_not_completed(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        """A step named 'resolve_sites' that discovers >1 site while running
        through run_single_step must NOT be handed off to
        _execute_multi_site_steps -- with steps=[step] that hand-off would
        run zero real per-site work yet still mark the whole packet
        "completed". run_single_step must refuse cleanly instead.
        """
        mock_packet_service.complete_packet = AsyncMock(return_value={})

        sites = [{"name": "SiteA"}, {"name": "SiteB"}, {"name": "SiteC"}]

        async def handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"resolved": True}, sites_to_process=sites)

        # "resolve_sites" is a real production step (package_generator/resolve_sites.py)
        # already registered with its own StepContract (consumes_state=("geo_source",)).
        # Pass contract=None here so we override only the *handler* for this test and
        # leave that production contract (shared via the module-level registry
        # singleton) untouched for every other test in the suite -- satisfy its
        # `geo_source` prerequisite via packet_state instead of stubbing the contract.
        _cleanup_registry("resolve_sites", handler=handler)

        context = self._base_context(packet_state={"geo_source": "community"})
        packet = self._base_packet(packet_state={"geo_source": "community"})

        msg, data = await executor.run_single_step(
            packet, "resolve_sites", context, MockExpertConfig()
        )

        assert data["error"] == "unsupported_multi_site_discovered"
        assert data["refused"] is True
        assert data["sites_to_process"] == sites
        mock_packet_service.complete_packet.assert_not_called()
        mock_packet_service.complete_step.assert_not_awaited()

    # -- Duplicate producer execution (Issue 2 regression) -----------------------

    @pytest.mark.asyncio
    async def test_run_missing_prerequisites_same_producer_runs_once(
        self, executor, mock_packet_service, _cleanup_registry
    ):
        """Two missing items (one missing_state, one missing_results) that both
        map to the SAME producer step must cause that producer to be invoked
        exactly once per pass, not once per missing item it resolves.
        """
        producer_contract = StepContract(produces_state=("key_one", "key_two"))
        consumer_contract = StepContract(consumes_state=("key_one", "key_two"))

        call_count = {"n": 0}

        async def producer_handler(ctx: StepContext) -> StepResult:
            call_count["n"] += 1
            return StepResult.success(data={"produced": True}, key_one="a", key_two="b")

        async def consumer_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"consumed": True})

        _cleanup_registry("shared_producer", handler=producer_handler, contract=producer_contract)
        _cleanup_registry("needs_both_keys", handler=consumer_handler, contract=consumer_contract)

        context = self._base_context()
        packet = self._base_packet()

        packet_after_producer = self._base_packet(packet_state={"key_one": "a", "key_two": "b"})
        mock_packet_service.get_packet = AsyncMock(return_value=packet_after_producer)

        msg, data = await executor.run_single_step(
            packet,
            "needs_both_keys",
            context,
            MockExpertConfig(),
            run_missing_prerequisites=True,
        )

        assert data["success"] is True
        assert call_count["n"] == 1
        # Producer + consumer each call complete_step once == 2 total.
        assert mock_packet_service.complete_step.await_count == 2
