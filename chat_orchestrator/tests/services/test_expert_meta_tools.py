"""Tests for orchestrator.services.expert_meta_tools (Phase D meta-tools).

Covers the three read-only module functions (list_steps/find_packet/
get_packet_state), the fourth execution-capable one (run_steps -- the
confirmation-gated step runner), and the conversation_graph.py dispatch
wiring that routes EXPERT_META_TOOL_NAMES calls to
_handle_expert_meta_tool_call instead of the regular MCP tool executor.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Triggers package_generator's @register_step decorators as an import-time side
# effect (mirrors tests/experts/test_workflow_executor.py's own pattern) so
# TestRunSteps's real-contract test below exercises the ACTUAL, production
# StepContracts for generate_distribution_map / generate_distribution_layout /
# resolve_community_site -- not synthetic/mock contracts.
import orchestrator.experts.handlers.package_generator  # noqa: F401  (registration side effect)
from orchestrator.experts.step_contracts import ParamSpec, StepContract
from orchestrator.experts.step_registry import get_step_registry
from orchestrator.experts.workflow_executor import WorkflowExecutor
from orchestrator.services import expert_meta_tools

# ---------------------------------------------------------------------------
# list_steps
# ---------------------------------------------------------------------------


def _make_expert_config():
    """A MagicMock ExpertConfig, mirroring tests/experts/conftest.py's
    mock_expert_config fixture shape (not reused directly since that fixture
    lives in a different test package and models a different expert)."""
    config = MagicMock()
    config.expert_id = "lpp_expert"
    config.packet_types = ["light_preliminary_package"]
    config.workflows = {
        "light_preliminary_package": [
            "1. [llm] understand_request - Parse user intent",
            "2. [function:fetch_month_metrics] - Get metrics from Grafana",
            "3. [llm] synthesize_findings - Combine results",
        ],
    }

    def get_workflow(packet_type):
        return config.workflows.get(packet_type, ["[llm] execute - Execute the task"])

    config.get_workflow = get_workflow
    return config


class TestListSteps:
    @pytest.mark.asyncio
    async def test_merges_contract_into_function_step(self):
        config = _make_expert_config()
        contract = StepContract(
            description="Fetch monthly metrics",
            consumes_state=("site_name",),
            optional_consumes_state=("design_id",),
            produces_state=("metrics_fetched",),
            consumes_results=("understand_request",),
            params=(
                ParamSpec(
                    name="pole_spacing_m",
                    param_type="number",
                    description="Distance between poles in meters",
                    synonyms=("pole spacing", "pole gap"),
                    required=False,
                    default=45,
                ),
            ),
            guard_keys=("metrics_fetched",),
            side_effects="calls Grafana MCP server",
        )

        def fake_get_contract(name):
            return contract if name == "fetch_month_metrics" else None

        with (
            patch.object(expert_meta_tools, "ExpertInstructionsProvider") as MockProvider,
            patch.object(expert_meta_tools, "get_step_contract", side_effect=fake_get_contract),
        ):
            MockProvider.return_value.get_expert_config = AsyncMock(return_value=config)

            result = await expert_meta_tools.list_steps("lpp_expert", "light_preliminary_package")

        assert result["expert_id"] == "lpp_expert"
        assert result["packet_type"] == "light_preliminary_package"
        steps = result["steps"]
        assert [s["name"] for s in steps] == [
            "understand_request",
            "fetch_month_metrics",
            "synthesize_findings",
        ]

        llm_step = steps[0]
        assert llm_step["step_type"] == "llm"
        assert "consumes_state" not in llm_step  # no contract attached -> handled gracefully

        fn_step = steps[1]
        assert fn_step["step_type"] == "function"
        assert fn_step["consumes_state"] == ["site_name"]
        assert fn_step["optional_consumes_state"] == ["design_id"]
        assert fn_step["produces_state"] == ["metrics_fetched"]
        assert fn_step["consumes_results"] == ["understand_request"]
        assert fn_step["guard_keys"] == ["metrics_fetched"]
        assert fn_step["side_effects"] == "calls Grafana MCP server"
        assert fn_step["params"] == [
            {
                "name": "pole_spacing_m",
                "param_type": "number",
                "description": "Distance between poles in meters",
                "synonyms": ["pole spacing", "pole gap"],
                "required": False,
                "default": 45,
            }
        ]

    @pytest.mark.asyncio
    async def test_unknown_expert_id_returns_error_not_raise(self):
        with patch.object(expert_meta_tools, "ExpertInstructionsProvider") as MockProvider:
            MockProvider.return_value.get_expert_config = AsyncMock(return_value=None)

            result = await expert_meta_tools.list_steps("no_such_expert", "some_packet_type")

        assert "error" in result
        assert "no_such_expert" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_packet_type_returns_error_not_raise(self):
        config = _make_expert_config()
        with patch.object(expert_meta_tools, "ExpertInstructionsProvider") as MockProvider:
            MockProvider.return_value.get_expert_config = AsyncMock(return_value=config)

            result = await expert_meta_tools.list_steps("lpp_expert", "not_a_real_packet_type")

        assert "error" in result
        assert "not_a_real_packet_type" in result["error"]

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_caught(self):
        with patch.object(expert_meta_tools, "ExpertInstructionsProvider") as MockProvider:
            MockProvider.return_value.get_expert_config = AsyncMock(
                side_effect=RuntimeError("boom")
            )

            result = await expert_meta_tools.list_steps("lpp_expert", "light_preliminary_package")

        assert "error" in result  # never raises


# ---------------------------------------------------------------------------
# find_packet
# ---------------------------------------------------------------------------


class TestFindPacket:
    @pytest.mark.asyncio
    async def test_found_returns_summary(self):
        packet = {
            "packet_id": "lpp_20260101_abc",
            "packet_status": "in_progress",
            "steps_completed": ["resolve_sites"],
            "packet_state": {
                "design_id": "design-123",
                "map_image_drive_id": "drive-abc",
                "empty_drive_id": "",
            },
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }
        mock_service = MagicMock()
        mock_service.find_packets_by_entity = AsyncMock(return_value=[packet])

        with patch.object(expert_meta_tools, "WorkPacketService", return_value=mock_service):
            result = await expert_meta_tools.find_packet(
                "light_preliminary_package", "ExampleSite", 1
            )

        assert result["found"] is True
        assert result["packet_id"] == "lpp_20260101_abc"
        assert result["packet_status"] == "in_progress"
        assert result["steps_completed"] == ["resolve_sites"]
        assert result["design_id"] == "design-123"
        # Only non-empty *_drive_id keys surfaced
        assert result["artifact_drive_ids"] == {"map_image_drive_id": "drive-abc"}
        assert result["created_at"] == "2026-01-01T00:00:00Z"
        assert result["updated_at"] == "2026-01-02T00:00:00Z"

    @pytest.mark.asyncio
    async def test_not_found_is_plain_outcome_not_error(self):
        mock_service = MagicMock()
        mock_service.find_packets_by_entity = AsyncMock(return_value=[])

        with patch.object(expert_meta_tools, "WorkPacketService", return_value=mock_service):
            result = await expert_meta_tools.find_packet(
                "light_preliminary_package", "NoSuchSite", 1
            )

        assert result == {
            "found": False,
            "packet_type": "light_preliminary_package",
            "key_entity": "NoSuchSite",
        }
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_caught(self):
        mock_service = MagicMock()
        mock_service.find_packets_by_entity = AsyncMock(side_effect=RuntimeError("db down"))

        with patch.object(expert_meta_tools, "WorkPacketService", return_value=mock_service):
            result = await expert_meta_tools.find_packet(
                "light_preliminary_package", "ExampleSite", 1
            )

        assert "error" in result


# ---------------------------------------------------------------------------
# get_packet_state
# ---------------------------------------------------------------------------


class TestGetPacketState:
    @pytest.mark.asyncio
    async def test_full_state_in_progress_packet(self):
        packet = {
            "packet_id": "lpp_20260101_abc",
            "packet_status": "in_progress",
            "packet_state": {"design_id": "design-123", "site_name": "ExampleSite"},
            "packet_outputs": {},
        }
        mock_service = MagicMock()
        mock_service.get_packet = AsyncMock(return_value=packet)

        with patch.object(expert_meta_tools, "WorkPacketService", return_value=mock_service):
            result = await expert_meta_tools.get_packet_state("lpp_20260101_abc")

        assert result["packet_id"] == "lpp_20260101_abc"
        assert result["packet_status"] == "in_progress"
        assert result["state"] == {"design_id": "design-123", "site_name": "ExampleSite"}

    @pytest.mark.asyncio
    async def test_completed_packet_merges_outputs_over_state(self):
        packet = {
            "packet_id": "lpp_20260101_abc",
            "packet_status": "completed",
            "packet_state": {"design_id": "design-123", "stale_key": "old"},
            "packet_outputs": {"stale_key": "new", "document_url": "https://example.com/doc"},
        }
        mock_service = MagicMock()
        mock_service.get_packet = AsyncMock(return_value=packet)

        with patch.object(expert_meta_tools, "WorkPacketService", return_value=mock_service):
            result = await expert_meta_tools.get_packet_state("lpp_20260101_abc")

        assert result["state"]["stale_key"] == "new"  # outputs win over state
        assert result["state"]["document_url"] == "https://example.com/doc"
        assert result["state"]["design_id"] == "design-123"

    @pytest.mark.asyncio
    async def test_filtered_by_keys_missing_keys_absent_not_error(self):
        packet = {
            "packet_id": "lpp_20260101_abc",
            "packet_status": "in_progress",
            "packet_state": {"design_id": "design-123", "site_name": "ExampleSite"},
            "packet_outputs": {},
        }
        mock_service = MagicMock()
        mock_service.get_packet = AsyncMock(return_value=packet)

        with patch.object(expert_meta_tools, "WorkPacketService", return_value=mock_service):
            result = await expert_meta_tools.get_packet_state(
                "lpp_20260101_abc", keys=["design_id", "does_not_exist"]
            )

        assert result["state"] == {"design_id": "design-123"}

    @pytest.mark.asyncio
    async def test_packet_not_found(self):
        mock_service = MagicMock()
        mock_service.get_packet = AsyncMock(return_value=None)

        with patch.object(expert_meta_tools, "WorkPacketService", return_value=mock_service):
            result = await expert_meta_tools.get_packet_state("no_such_packet")

        assert result == {"error": "Packet not found"}

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_caught(self):
        mock_service = MagicMock()
        mock_service.get_packet = AsyncMock(side_effect=RuntimeError("db down"))

        with patch.object(expert_meta_tools, "WorkPacketService", return_value=mock_service):
            result = await expert_meta_tools.get_packet_state("lpp_20260101_abc")

        assert "error" in result


# ---------------------------------------------------------------------------
# run_steps
# ---------------------------------------------------------------------------


def _base_packet(**overrides) -> Dict[str, Any]:
    packet = {
        "packet_id": "pkt-1",
        "packet_type": "test_packet_type",
        "assigned_expert": "test_expert",
        "packet_goal": "Test goal",
        "packet_inputs": {},
        "packet_state": {},
        "steps_completed": [],
        "packet_status": "pending",
        "updated_at": None,
        "requested_in_session": "session-abc",
        "state_version": 0,
    }
    packet.update(overrides)
    return packet


def _mock_packet_service(packet: Dict[str, Any]) -> MagicMock:
    service = MagicMock()
    service.get_packet = AsyncMock(return_value=packet)
    service.find_similar_completed = AsyncMock(return_value=[])
    return service


def _mock_provider() -> MagicMock:
    provider = MagicMock()
    expert_config = MagicMock()
    expert_config.get_workflow = MagicMock(return_value=["[function:whatever] - whatever"])
    expert_config.workflows = {"test_packet_type": []}
    provider.get_expert_config = AsyncMock(return_value=expert_config)
    return provider


class TestRunSteps:
    """Phase D Task: expert_run_steps' confirmation-gated step runner.

    `run_single_step` itself is always mocked here (its own behavior is
    covered exhaustively by tests/experts/test_workflow_executor.py) --
    these tests are about run_steps' OWN logic: packet resolution, the
    Phase 1 dry-run preview (built from the real, read-only
    `validate_step_prerequisites`), the confirmation gate, and the Phase 2
    execution loop's stop conditions.
    """

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

    # -- No producer needed: executes immediately -----------------------------

    @pytest.mark.asyncio
    async def test_no_producer_needed_executes_immediately(self, _cleanup_registry):
        _cleanup_registry("solo_step", contract=StepContract())
        packet = _base_packet()

        with (
            patch.object(
                expert_meta_tools, "WorkPacketService", return_value=_mock_packet_service(packet)
            ),
            patch.object(
                expert_meta_tools, "ExpertInstructionsProvider", return_value=_mock_provider()
            ),
            patch.object(
                WorkflowExecutor,
                "run_single_step",
                new=AsyncMock(return_value=("done", {"success": True, "step_name": "solo_step"})),
            ) as mock_run_single_step,
        ):
            result = await expert_meta_tools.run_steps(steps=["solo_step"], packet_id="pkt-1")

        assert result["success"] is True
        assert result["executed_steps"] == [
            {"step_name": "solo_step", "result": {"success": True, "step_name": "solo_step"}}
        ]
        mock_run_single_step.assert_awaited_once()
        _, kwargs = mock_run_single_step.call_args
        assert kwargs["run_missing_prerequisites"] is True
        assert kwargs["force"] is False

    # -- Producer auto-insertion: THE confirmation-gate test -------------------

    @pytest.mark.asyncio
    async def test_producer_auto_insertion_without_confirmed_never_executes(
        self, _cleanup_registry
    ):
        """The single most important test in this module: when a producer step
        would need to auto-run, run_steps must return needs_confirmation and
        must NEVER call run_single_step -- not "call it and ignore the
        result", not "call it in a dry-run mode" -- literally never invoked.
        """
        producer_contract = StepContract(
            description="Produces shared_key",
            produces_state=("shared_key",),
            side_effects="does something side-effecty",
        )
        consumer_contract = StepContract(
            description="Needs shared_key",
            consumes_state=("shared_key",),
        )
        _cleanup_registry("produces_shared", contract=producer_contract)
        _cleanup_registry("consumes_shared", contract=consumer_contract)
        packet = _base_packet()

        with (
            patch.object(
                expert_meta_tools, "WorkPacketService", return_value=_mock_packet_service(packet)
            ),
            patch.object(
                expert_meta_tools, "ExpertInstructionsProvider", return_value=_mock_provider()
            ),
            patch.object(
                WorkflowExecutor, "run_single_step", new=AsyncMock()
            ) as mock_run_single_step,
        ):
            result = await expert_meta_tools.run_steps(steps=["consumes_shared"], packet_id="pkt-1")

        assert result.get("needs_confirmation") is True
        assert result["requested_steps"] == ["consumes_shared"]
        assert result["auto_inserted_steps"] == [
            {
                "name": "produces_shared",
                "description": "Produces shared_key",
                "side_effects": "does something side-effecty",
            }
        ]
        assert "produces_shared" in result["message"]
        assert "does something side-effecty" in result["message"]
        # THE assertion that matters: nothing was executed.
        mock_run_single_step.assert_not_awaited()
        # A confirmation_token is issued for the follow-up call to echo back.
        assert result["confirmation_token"]

    @pytest.mark.asyncio
    async def test_two_call_flow_matching_token_now_executes(self, _cleanup_registry):
        """The intended two-call flow: first call (no token) gets
        needs_confirmation with a confirmation_token; a second call echoing
        that EXACT token back executes. This is the replacement for the old
        bare `confirmed=True` shape."""
        producer_contract = StepContract(produces_state=("shared_key",))
        consumer_contract = StepContract(consumes_state=("shared_key",))
        _cleanup_registry("produces_shared", contract=producer_contract)
        _cleanup_registry("consumes_shared", contract=consumer_contract)
        packet = _base_packet()

        with (
            patch.object(
                expert_meta_tools, "WorkPacketService", return_value=_mock_packet_service(packet)
            ),
            patch.object(
                expert_meta_tools, "ExpertInstructionsProvider", return_value=_mock_provider()
            ),
            patch.object(
                WorkflowExecutor, "run_single_step", new=AsyncMock()
            ) as mock_run_single_step,
        ):
            first = await expert_meta_tools.run_steps(steps=["consumes_shared"], packet_id="pkt-1")

            assert first.get("needs_confirmation") is True
            token = first["confirmation_token"]
            assert isinstance(token, str) and token
            mock_run_single_step.assert_not_awaited()

            mock_run_single_step.return_value = (
                "done",
                {"success": True, "step_name": "consumes_shared"},
            )
            second = await expert_meta_tools.run_steps(
                steps=["consumes_shared"], packet_id="pkt-1", confirmation_token=token
            )

        assert second["success"] is True
        # Only the explicitly-requested step is looped over here -- the
        # producer itself is run BY run_single_step's own
        # run_missing_prerequisites=True machinery, not by an extra call
        # from run_steps' own loop.
        mock_run_single_step.assert_awaited_once()
        _, kwargs = mock_run_single_step.call_args
        assert kwargs["run_missing_prerequisites"] is True

    @pytest.mark.asyncio
    async def test_fabricated_confirmation_token_never_executes(self, _cleanup_registry):
        """THE direct regression test for the vulnerability this change fixes:
        a caller (an LLM, possibly hallucinating, or influenced by injected
        content) supplies a `confirmation_token` on its VERY FIRST call for a
        packet that never went through a real Phase-1 round-trip. This must
        NOT execute -- it must be indistinguishable from an unconfirmed call,
        returning a fresh needs_confirmation with a new token instead."""
        producer_contract = StepContract(produces_state=("shared_key",))
        consumer_contract = StepContract(consumes_state=("shared_key",))
        _cleanup_registry("produces_shared", contract=producer_contract)
        _cleanup_registry("consumes_shared", contract=consumer_contract)
        packet = _base_packet()

        with (
            patch.object(
                expert_meta_tools, "WorkPacketService", return_value=_mock_packet_service(packet)
            ),
            patch.object(
                expert_meta_tools, "ExpertInstructionsProvider", return_value=_mock_provider()
            ),
            patch.object(
                WorkflowExecutor, "run_single_step", new=AsyncMock()
            ) as mock_run_single_step,
        ):
            result = await expert_meta_tools.run_steps(
                steps=["consumes_shared"],
                packet_id="pkt-1",
                confirmation_token="totally-fabricated-not-a-real-token",
            )

        assert result.get("needs_confirmation") is True
        assert result["confirmation_token"] != "totally-fabricated-not-a-real-token"
        # THE assertion that matters: run_single_step was NEVER awaited, not
        # just "the right dict came back" -- a fabricated/mismatched token
        # must never reach Phase 2 execution.
        mock_run_single_step.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_state_version_change_invalidates_confirmation_token(self, _cleanup_registry):
        """A token that was valid for an earlier plan must stop matching once
        the packet's state_version has changed underneath it (e.g. a
        concurrent update_state landed in the real-world gap between a human
        seeing the confirmation prompt and replying to it)."""
        producer_contract = StepContract(produces_state=("shared_key",))
        consumer_contract = StepContract(consumes_state=("shared_key",))
        _cleanup_registry("produces_shared", contract=producer_contract)
        _cleanup_registry("consumes_shared", contract=consumer_contract)
        packet_v0 = _base_packet(state_version=0)
        packet_v1 = _base_packet(state_version=1)

        service = MagicMock()
        service.get_packet = AsyncMock(side_effect=[packet_v0, packet_v1])
        service.find_similar_completed = AsyncMock(return_value=[])

        with (
            patch.object(expert_meta_tools, "WorkPacketService", return_value=service),
            patch.object(
                expert_meta_tools, "ExpertInstructionsProvider", return_value=_mock_provider()
            ),
            patch.object(
                WorkflowExecutor, "run_single_step", new=AsyncMock()
            ) as mock_run_single_step,
        ):
            first = await expert_meta_tools.run_steps(steps=["consumes_shared"], packet_id="pkt-1")
            token = first["confirmation_token"]

            second = await expert_meta_tools.run_steps(
                steps=["consumes_shared"], packet_id="pkt-1", confirmation_token=token
            )

        assert second.get("needs_confirmation") is True
        assert second["confirmation_token"] != token
        mock_run_single_step.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_completed_producer_not_auto_inserted(self, _cleanup_registry):
        """Mirrors run_single_step's own Step-1 already-completed short-circuit
        (workflow_executor.py ~2953-2956): a producer already in
        steps_completed is treated as satisfied and never walked into for its
        OWN prerequisites, even if those prerequisites would otherwise look
        unsatisfied. Without this, the preview could report a producer (and
        demand confirmation for it) that real execution would never actually
        run."""
        producer_contract = StepContract(
            description="Produces shared_key",
            consumes_state=("other_input",),  # would be unsatisfied if actually checked
            produces_state=("shared_key",),
        )
        consumer_contract = StepContract(consumes_state=("shared_key",))
        _cleanup_registry("produces_shared", contract=producer_contract)
        _cleanup_registry("consumes_shared", contract=consumer_contract)
        # produces_shared is already completed -- run_single_step would
        # short-circuit on it (Step 1) before ever validating its own
        # prerequisites; the preview must match that exactly.
        packet = _base_packet(steps_completed=["produces_shared"])

        with (
            patch.object(
                expert_meta_tools, "WorkPacketService", return_value=_mock_packet_service(packet)
            ),
            patch.object(
                expert_meta_tools, "ExpertInstructionsProvider", return_value=_mock_provider()
            ),
            patch.object(
                WorkflowExecutor,
                "run_single_step",
                new=AsyncMock(
                    return_value=("done", {"success": True, "step_name": "consumes_shared"})
                ),
            ) as mock_run_single_step,
        ):
            result = await expert_meta_tools.run_steps(steps=["consumes_shared"], packet_id="pkt-1")

        # produces_shared is satisfied (already completed) so consumes_shared's
        # own missing "shared_key" prerequisite resolves via a producer that's
        # already done -- nothing left to auto-run, nothing to block on.
        assert "blocked" not in result
        assert "needs_confirmation" not in result
        assert result["success"] is True
        mock_run_single_step.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_real_package_generator_contracts_dedupe_and_order_producers(self):
        """Uses the REAL, production generate_distribution_map /
        generate_distribution_layout / resolve_community_site contracts
        (registered at module-import time above) to exercise the
        recursive-preview-walk's cycle-guard/dedup logic against real
        contract shapes, not synthetic ones. generate_distribution_map needs
        (via consumes_results) both generate_distribution_layout AND
        resolve_community_site; generate_distribution_layout ALSO needs
        resolve_community_site -- so resolve_community_site must appear
        exactly once in auto_inserted_steps (dedup), and it must appear
        BEFORE generate_distribution_layout (it's that step's own
        prerequisite).
        """
        packet = _base_packet(packet_type="light_preliminary_package")

        with (
            patch.object(
                expert_meta_tools, "WorkPacketService", return_value=_mock_packet_service(packet)
            ),
            patch.object(
                expert_meta_tools, "ExpertInstructionsProvider", return_value=_mock_provider()
            ),
            patch.object(
                WorkflowExecutor, "run_single_step", new=AsyncMock()
            ) as mock_run_single_step,
        ):
            result = await expert_meta_tools.run_steps(
                steps=["generate_distribution_map"], packet_id="pkt-1"
            )

        assert "blocked" not in result
        assert result.get("needs_confirmation") is True
        names = [s["name"] for s in result["auto_inserted_steps"]]

        # No duplicates, and the requested step itself is never auto-inserted.
        assert len(names) == len(set(names))
        assert "generate_distribution_map" not in names

        assert "resolve_community_site" in names
        assert "generate_distribution_layout" in names
        assert names.index("resolve_community_site") < names.index("generate_distribution_layout")

        mock_run_single_step.assert_not_awaited()

    # -- Blocked: missing prerequisite with no producer at all -----------------

    @pytest.mark.asyncio
    async def test_missing_prerequisite_with_no_producer_is_blocked(self, _cleanup_registry):
        _cleanup_registry("needs_orphan", contract=StepContract(consumes_state=("orphan_key",)))
        packet = _base_packet()

        with (
            patch.object(
                expert_meta_tools, "WorkPacketService", return_value=_mock_packet_service(packet)
            ),
            patch.object(
                expert_meta_tools, "ExpertInstructionsProvider", return_value=_mock_provider()
            ),
            patch.object(
                WorkflowExecutor, "run_single_step", new=AsyncMock()
            ) as mock_run_single_step,
        ):
            result = await expert_meta_tools.run_steps(steps=["needs_orphan"], packet_id="pkt-1")

        assert result["success"] is False
        assert result["blocked"] is True
        assert "needs_confirmation" not in result
        assert any(d["missing_item"] == "orphan_key" for d in result["details"])
        mock_run_single_step.assert_not_awaited()

    # -- Mid-loop needs_user_input stops the loop ------------------------------

    @pytest.mark.asyncio
    async def test_needs_user_input_mid_loop_stops_before_next_step(self, _cleanup_registry):
        _cleanup_registry("step_a", contract=StepContract())
        _cleanup_registry("step_b", contract=StepContract())
        packet = _base_packet()

        with (
            patch.object(
                expert_meta_tools, "WorkPacketService", return_value=_mock_packet_service(packet)
            ),
            patch.object(
                expert_meta_tools, "ExpertInstructionsProvider", return_value=_mock_provider()
            ),
            patch.object(
                WorkflowExecutor,
                "run_single_step",
                new=AsyncMock(
                    return_value=(
                        "paused",
                        {"needs_user_input": True, "missing_state": [], "missing_results": []},
                    )
                ),
            ) as mock_run_single_step,
        ):
            result = await expert_meta_tools.run_steps(
                steps=["step_a", "step_b"], packet_id="pkt-1"
            )

        assert result["success"] is False
        assert result["needs_user_input"] is True
        assert result["stopped_at_step"] == "step_a"
        assert result["executed_steps"] == []
        mock_run_single_step.assert_awaited_once()  # step_b never attempted

    # -- force=True on an already-completed step ------------------------------

    @pytest.mark.asyncio
    async def test_force_true_passed_through_even_if_already_completed(self, _cleanup_registry):
        _cleanup_registry("step_a", contract=StepContract())
        packet = _base_packet(steps_completed=["step_a"])

        with (
            patch.object(
                expert_meta_tools, "WorkPacketService", return_value=_mock_packet_service(packet)
            ),
            patch.object(
                expert_meta_tools, "ExpertInstructionsProvider", return_value=_mock_provider()
            ),
            patch.object(
                WorkflowExecutor,
                "run_single_step",
                new=AsyncMock(return_value=("done", {"success": True, "step_name": "step_a"})),
            ) as mock_run_single_step,
        ):
            result = await expert_meta_tools.run_steps(
                steps=["step_a"], packet_id="pkt-1", force=True
            )

        assert result["success"] is True
        mock_run_single_step.assert_awaited_once()
        _, kwargs = mock_run_single_step.call_args
        assert kwargs["force"] is True

    # -- Bad param_overrides_json -----------------------------------------------

    @pytest.mark.asyncio
    async def test_bad_param_overrides_json_returns_clean_error_not_raise(self):
        result = await expert_meta_tools.run_steps(
            steps=["whatever"], packet_id="pkt-1", param_overrides_json="{not valid json"
        )

        assert "error" in result
        assert "param_overrides_json" in result["error"]

    @pytest.mark.asyncio
    async def test_bad_packet_inputs_json_returns_clean_error_not_raise(self):
        result = await expert_meta_tools.run_steps(
            steps=["whatever"],
            expert_id="lpp_expert",
            packet_type="light_preliminary_package",
            key_entity="ExampleSite",
            packet_inputs_json="{not valid json",
        )

        assert "error" in result
        assert "packet_inputs_json" in result["error"]

    # -- Packet resolution errors ------------------------------------------------

    @pytest.mark.asyncio
    async def test_empty_steps_list_returns_error_not_raise(self):
        result = await expert_meta_tools.run_steps(steps=[], packet_id="pkt-1")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_unknown_packet_id_returns_clean_error(self):
        service = MagicMock()
        service.get_packet = AsyncMock(return_value=None)

        with patch.object(expert_meta_tools, "WorkPacketService", return_value=service):
            result = await expert_meta_tools.run_steps(
                steps=["whatever"], packet_id="no_such_packet"
            )

        assert "error" in result
        assert "no_such_packet" in result["error"]

    @pytest.mark.asyncio
    async def test_new_packet_missing_required_fields_returns_clean_error(self):
        # No packet_id AND missing expert_id/packet_type/key_entity.
        result = await expert_meta_tools.run_steps(steps=["whatever"], expert_id="lpp_expert")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_caught(self):
        with patch.object(
            expert_meta_tools, "WorkPacketService", side_effect=RuntimeError("db down")
        ):
            result = await expert_meta_tools.run_steps(steps=["whatever"], packet_id="pkt-1")

        assert "error" in result  # never raises


# ---------------------------------------------------------------------------
# Dispatch wiring: EXPERT_META_TOOL_NAMES routes to _handle_expert_meta_tool_call
# ---------------------------------------------------------------------------


def _make_graph_builder():
    """Build a ConversationGraphBuilder with mocked collaborators.

    gemini_client/registry/executor are never exercised by the dispatch path
    under test (expert meta-tool calls bypass self._executor entirely), so
    plain MagicMocks are sufficient.
    """
    from orchestrator.graphs.conversation_graph import ConversationGraphBuilder

    gemini_client = MagicMock()
    registry = MagicMock()
    executor = MagicMock()
    executor.execute = AsyncMock(
        side_effect=AssertionError(
            "regular tool executor should NOT be invoked for expert meta-tool calls"
        )
    )
    return ConversationGraphBuilder(
        gemini_client=gemini_client,
        registry=registry,
        executor=executor,
    )


class TestDispatchWiring:
    @pytest.mark.asyncio
    async def test_expert_list_steps_routes_to_meta_handler_not_executor(self):
        from orchestrator.models.schemas import FunctionCall

        builder = _make_graph_builder()
        call = FunctionCall(
            name="expert_list_steps",
            arguments={"expert_id": "lpp_expert", "packet_type": "light_preliminary_package"},
        )

        fake_output = {
            "expert_id": "lpp_expert",
            "packet_type": "light_preliminary_package",
            "steps": [],
        }
        with patch.object(
            expert_meta_tools, "list_steps", AsyncMock(return_value=fake_output)
        ) as mock_list_steps:
            results = await builder._execute_tool_calls([call], metadata={"organization_id": 1})

        mock_list_steps.assert_awaited_once_with(
            expert_id="lpp_expert", packet_type="light_preliminary_package"
        )
        assert len(results) == 1
        assert results[0].name == "expert_list_steps"
        assert results[0].success is True
        assert results[0].output == fake_output

    @pytest.mark.asyncio
    async def test_expert_find_packet_defaults_org_id_when_missing_from_metadata(self):
        from orchestrator.models.schemas import FunctionCall

        builder = _make_graph_builder()
        call = FunctionCall(
            name="expert_find_packet",
            arguments={"packet_type": "light_preliminary_package", "key_entity": "ExampleSite"},
        )

        with patch.object(
            expert_meta_tools,
            "find_packet",
            AsyncMock(return_value={"found": False}),
        ) as mock_find_packet:
            # No organization_id in metadata at all.
            await builder._execute_tool_calls([call], metadata={})

        _, kwargs = mock_find_packet.call_args
        assert kwargs["organization_id"] is not None  # falls back to STAFF_ORG_ID, not None

    @pytest.mark.asyncio
    async def test_expert_get_packet_state_error_output_marks_failure(self):
        from orchestrator.models.schemas import FunctionCall

        builder = _make_graph_builder()
        call = FunctionCall(
            name="expert_get_packet_state",
            arguments={"packet_id": "no_such_packet"},
        )

        with patch.object(
            expert_meta_tools,
            "get_packet_state",
            AsyncMock(return_value={"error": "Packet not found"}),
        ):
            results = await builder._execute_tool_calls([call], metadata={})

        assert results[0].success is False
        assert results[0].output == {"error": "Packet not found"}

    @pytest.mark.asyncio
    async def test_expert_run_steps_routes_to_meta_handler_with_args(self):
        from orchestrator.models.schemas import FunctionCall

        builder = _make_graph_builder()
        call = FunctionCall(
            name="expert_run_steps",
            arguments={
                "steps": ["generate_distribution_map"],
                "packet_id": "pkt-1",
                "confirmation_token": "abc123",
                "force": True,
                "packet_inputs_json": '{"technology_family":"deye"}',
            },
        )

        fake_output = {"success": True, "packet_id": "pkt-1", "executed_steps": []}
        with patch.object(
            expert_meta_tools, "run_steps", AsyncMock(return_value=fake_output)
        ) as mock_run_steps:
            results = await builder._execute_tool_calls(
                [call], metadata={"organization_id": 1, "user_email": "staff@example.com"}
            )

        mock_run_steps.assert_awaited_once_with(
            steps=["generate_distribution_map"],
            packet_id="pkt-1",
            expert_id=None,
            packet_type=None,
            key_entity=None,
            param_overrides_json=None,
            packet_inputs_json='{"technology_family":"deye"}',
            force=True,
            confirmation_token="abc123",
            organization_id=1,
            user_email="staff@example.com",
            session_id=None,
        )
        assert results[0].success is True
        assert results[0].output == fake_output

    @pytest.mark.asyncio
    async def test_expert_run_steps_blocked_output_marks_failure_without_error_key(self):
        """blocked=True results have no "error" key at all -- the dispatcher must
        respect the explicit success=False rather than defaulting to True just
        because "error" isn't present (a real bug this test guards against)."""
        from orchestrator.models.schemas import FunctionCall

        builder = _make_graph_builder()
        call = FunctionCall(name="expert_run_steps", arguments={"steps": ["needs_orphan"]})

        blocked_output = {"success": False, "blocked": True, "details": []}
        with patch.object(expert_meta_tools, "run_steps", AsyncMock(return_value=blocked_output)):
            results = await builder._execute_tool_calls([call], metadata={})

        assert results[0].success is False
        assert results[0].output == blocked_output

    @pytest.mark.asyncio
    async def test_unknown_expert_meta_tool_name_handled_gracefully(self):
        """Defensive: if a name were ever added to EXPERT_META_TOOL_NAMES without a
        matching branch in _handle_expert_meta_tool_call, it should degrade to a
        clean error output rather than raising."""
        from orchestrator.models.schemas import FunctionCall

        builder = _make_graph_builder()
        call = FunctionCall(name="expert_get_packet_state", arguments={})

        # Simulate the branch being hit with an unrecognized name by calling the
        # handler directly with a call whose name won't match any branch.
        call.name = "expert_totally_made_up"
        result = await builder._handle_expert_meta_tool_call(call, metadata={})

        assert result.success is False
        assert "Unknown expert meta-tool" in result.output["error"]
