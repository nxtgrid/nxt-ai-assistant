"""Tests for WorkflowExecutor.

Tests workflow parsing, step execution, and LLM/function hybrid workflows.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import get_step_registry
from orchestrator.experts.workflow_executor import ParsedStep, WorkflowExecutor


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
