"""Tests for StepContext and StepResult.

Tests context building, result factory methods, and helper methods.
"""

from orchestrator.experts.step_context import StepContext, StepResult


class TestStepContext:
    """Test StepContext dataclass and methods."""

    def test_minimal_context(self):
        """StepContext can be created with minimal required fields."""
        ctx = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Analyze ExampleGrid grid",
            packet_inputs={},
            packet_state={},
            current_step="execute",
            steps_completed=[],
        )
        assert ctx.packet_id == "test_123"
        assert ctx.packet_type == "grid_analysis"
        assert ctx.accumulated_results == {}

    def test_effective_email_prefers_packet_requester(self):
        """effective_email returns packet_requester_email if available."""
        ctx = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test",
            packet_inputs={},
            packet_state={},
            current_step="execute",
            steps_completed=[],
            user_email="current@example.com",
            packet_requester_email="original@example.com",
        )
        assert ctx.effective_email == "original@example.com"

    def test_effective_email_falls_back_to_user_email(self):
        """effective_email falls back to user_email when no requester."""
        ctx = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test",
            packet_inputs={},
            packet_state={},
            current_step="execute",
            steps_completed=[],
            user_email="current@example.com",
        )
        assert ctx.effective_email == "current@example.com"

    def test_effective_org_id_prefers_packet_org(self):
        """effective_org_id returns packet_organization_id if available."""
        ctx = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test",
            packet_inputs={},
            packet_state={},
            current_step="execute",
            steps_completed=[],
            organization_id=1,
            packet_organization_id=2,
        )
        assert ctx.effective_org_id == 2

    def test_get_previous_result(self):
        """get_previous_result retrieves from accumulated_results."""
        ctx = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test",
            packet_inputs={},
            packet_state={},
            current_step="step2",
            steps_completed=["step1"],
            accumulated_results={"step1": {"data": "from step 1"}},
        )
        result = ctx.get_previous_result("step1")
        assert result == {"data": "from step 1"}

    def test_get_previous_result_returns_none_for_missing(self):
        """get_previous_result returns None for missing steps."""
        ctx = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test",
            packet_inputs={},
            packet_state={},
            current_step="step2",
            steps_completed=[],
        )
        result = ctx.get_previous_result("nonexistent")
        assert result is None

    def test_get_input(self):
        """get_input retrieves from packet_inputs."""
        ctx = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test",
            packet_inputs={"grid": {"grid_name": "ExampleGrid"}},
            packet_state={},
            current_step="execute",
            steps_completed=[],
        )
        grid = ctx.get_input("grid")
        assert grid["grid_name"] == "ExampleGrid"

    def test_get_input_with_default(self):
        """get_input returns default for missing keys."""
        ctx = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test",
            packet_inputs={},
            packet_state={},
            current_step="execute",
            steps_completed=[],
        )
        value = ctx.get_input("missing", default="fallback")
        assert value == "fallback"

    def test_get_state(self):
        """get_state retrieves from packet_state."""
        ctx = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test",
            packet_inputs={},
            packet_state={"metrics_fetched": True},
            current_step="execute",
            steps_completed=[],
        )
        assert ctx.get_state("metrics_fetched") is True

    def test_get_rag_context_empty(self):
        """get_rag_context returns empty string when no context."""
        ctx = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test",
            packet_inputs={},
            packet_state={},
            current_step="execute",
            steps_completed=[],
        )
        assert ctx.get_rag_context() == ""

    def test_get_rag_context_joins_chunks(self):
        """get_rag_context joins multiple chunks with double newlines."""
        ctx = StepContext(
            packet_id="test_123",
            packet_type="grid_analysis",
            packet_goal="Test",
            packet_inputs={},
            packet_state={},
            current_step="execute",
            steps_completed=[],
            rag_context=["Chunk 1", "Chunk 2", "Chunk 3"],
        )
        result = ctx.get_rag_context()
        assert result == "Chunk 1\n\nChunk 2\n\nChunk 3"


class TestStepResult:
    """Test StepResult dataclass and factory methods."""

    def test_default_result_is_success(self):
        """Default StepResult indicates success."""
        result = StepResult()
        assert result.is_success is True
        assert result.error is None

    def test_result_with_error_is_failure(self):
        """StepResult with error is not success."""
        result = StepResult(error="Something went wrong")
        assert result.is_success is False
        assert result.error == "Something went wrong"

    def test_success_factory(self):
        """StepResult.success() creates successful result."""
        result = StepResult.success(
            data={"metrics": [1, 2, 3]},
            message="Fetched metrics",
            metrics_fetched=True,
        )
        assert result.is_success is True
        assert result.data == {"metrics": [1, 2, 3]}
        assert result.progress_message == "Fetched metrics"
        assert result.state_updates == {"metrics_fetched": True}

    def test_failure_factory(self):
        """StepResult.failure() creates failed result."""
        result = StepResult.failure("Database connection failed")
        assert result.is_success is False
        assert result.error == "Database connection failed"

    def test_needs_input_factory(self):
        """StepResult.needs_input() creates paused result."""
        result = StepResult.needs_input("Which grid should I analyze?")
        assert result.needs_user_input is True
        assert result.user_prompt == "Which grid should I analyze?"
        assert result.is_success is True  # Not an error, just paused

    def test_skip_remaining(self):
        """StepResult can signal to skip remaining steps."""
        result = StepResult(
            data={"summary": "Task complete"},
            skip_remaining=True,
        )
        assert result.skip_remaining is True

    def test_state_updates_separate_from_data(self):
        """data and state_updates are separate concerns."""
        result = StepResult(
            data={"response": "Analysis complete"},
            state_updates={"analysis_complete": True},
        )
        # data goes to accumulated_results
        assert result.data == {"response": "Analysis complete"}
        # state_updates goes to packet_state
        assert result.state_updates == {"analysis_complete": True}
