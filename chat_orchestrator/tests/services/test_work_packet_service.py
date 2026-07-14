"""Tests for WorkPacketService.mark_step_incomplete (Phase C Task 4).

Generalizes reset_failed_packet's single-step-pop logic to an arbitrary
named step and an arbitrary list of packet_state keys to clear, so
run_single_step(force=True) can re-execute a step from scratch.

Uses a small fake standing in for the real Supabase client's fluent API
(rather than a MagicMock chain) so tests can assert on the final persisted
state instead of just call arguments.
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest

from orchestrator.services.work_packet_service import WorkPacketService


class _FakeResult:
    def __init__(self, data: List[Dict[str, Any]]):
        self.data = data


class _PacketTable:
    """Fakes .select()/.update()/.eq()/.execute() chaining for one table."""

    def __init__(self, client: "FakeSupabaseClient"):
        self._client = client
        self._mode: Optional[str] = None
        self._update_payload: Optional[Dict[str, Any]] = None

    def select(self, *_args, **_kwargs) -> "_PacketTable":
        self._mode = "select"
        return self

    def update(self, payload: Dict[str, Any]) -> "_PacketTable":
        self._mode = "update"
        self._update_payload = payload
        return self

    def eq(self, _field: str, _value: Any) -> "_PacketTable":
        return self

    def execute(self) -> _FakeResult:
        if self._mode == "select":
            packet = self._client.packet
            return _FakeResult([packet] if packet else [])
        if self._mode == "update":
            self._client.updates.append(self._update_payload)
            if self._client.packet is not None:
                self._client.packet = {**self._client.packet, **(self._update_payload or {})}
            packet = self._client.packet
            return _FakeResult([packet] if packet else [])
        raise AssertionError("execute() called before select()/update()")


class _LogsTable:
    def __init__(self, client: Any):
        self._client = client

    def insert(self, payload: Dict[str, Any]) -> "_LogsTable":
        self._client.logged_events.append(payload)
        return self

    def execute(self) -> _FakeResult:
        return _FakeResult([{"id": "log-1"}])


class FakeSupabaseClient:
    """Minimal fake for the Supabase client, tracking packet state as
    mutable so update() calls actually change what a later select() sees."""

    def __init__(self, packet: Optional[Dict[str, Any]]):
        self.packet = packet
        self.updates: List[Dict[str, Any]] = []
        self.logged_events: List[Dict[str, Any]] = []

    def table(self, name: str):
        if name == "agent_work_packets":
            return _PacketTable(self)
        if name == "agent_work_packet_logs":
            return _LogsTable(self)
        raise AssertionError(f"Unexpected table: {name}")


def _make_service(
    packet: Optional[Dict[str, Any]],
) -> Tuple[WorkPacketService, FakeSupabaseClient]:
    service = WorkPacketService()
    fake_client = FakeSupabaseClient(packet)
    service._client = fake_client
    return service, fake_client


def _base_packet(**overrides: Any) -> Dict[str, Any]:
    packet: Dict[str, Any] = {
        "id": "uuid-1",
        "packet_id": "packet-1",
        "packet_type": "light_preliminary_package",
        "steps_completed": ["step_a", "step_b"],
        "packet_state": {"foo": "bar", "guard_flag": True},
        "sessions_involved": [],
    }
    packet.update(overrides)
    return packet


class TestMarkStepIncomplete:
    @pytest.mark.asyncio
    async def test_removes_completed_step(self):
        service, _fake_client = _make_service(_base_packet())

        result = await service.mark_step_incomplete("packet-1", "step_b")

        assert result["steps_completed"] == ["step_a"]

    @pytest.mark.asyncio
    async def test_absent_step_is_noop(self):
        service, _fake_client = _make_service(_base_packet())

        result = await service.mark_step_incomplete("packet-1", "not_a_completed_step")

        assert result["steps_completed"] == ["step_a", "step_b"]

    @pytest.mark.asyncio
    async def test_clear_state_keys_pops_named_keys_only(self):
        service, _fake_client = _make_service(_base_packet())

        result = await service.mark_step_incomplete(
            "packet-1", "step_b", clear_state_keys=["guard_flag"]
        )

        assert "guard_flag" not in result["packet_state"]
        assert result["packet_state"]["foo"] == "bar"

    @pytest.mark.asyncio
    async def test_no_clear_state_keys_leaves_state_untouched(self):
        service, _fake_client = _make_service(_base_packet())

        result = await service.mark_step_incomplete("packet-1", "step_b")

        assert result["packet_state"] == {"foo": "bar", "guard_flag": True}

    @pytest.mark.asyncio
    async def test_packet_not_found_raises_value_error(self):
        service, _fake_client = _make_service(None)

        with pytest.raises(ValueError):
            await service.mark_step_incomplete("missing-packet", "step_b")

    @pytest.mark.asyncio
    async def test_session_tracked_when_new(self):
        service, _fake_client = _make_service(_base_packet(sessions_involved=["session_1"]))

        result = await service.mark_step_incomplete("packet-1", "step_b", session_id="session_2")

        assert "session_1" in result["sessions_involved"]
        assert "session_2" in result["sessions_involved"]

    @pytest.mark.asyncio
    async def test_session_not_duplicated_when_already_tracked(self):
        service, _fake_client = _make_service(_base_packet(sessions_involved=["session_1"]))

        result = await service.mark_step_incomplete("packet-1", "step_b", session_id="session_1")

        assert result["sessions_involved"].count("session_1") == 1


class _ConditionCheckingPacketTable:
    """Like _PacketTable, but a conditional `.update()` only "succeeds" (i.e.
    actually mutates the fake server-side packet and returns non-empty data)
    if every `.eq(field, value)` condition applied to it still matches the
    CURRENT server-side packet at execute() time. This lets tests simulate a
    real optimistic-concurrency conflict: mutate the client's packet (as a
    concurrent writer would) between an update()'s construction and its
    execute(), and the conditional update will correctly report zero rows
    matched, exactly like a real conditional Postgres UPDATE would.
    """

    def __init__(self, client: "ConditionCheckingSupabaseClient"):
        self._client = client
        self._mode: Optional[str] = None
        self._update_payload: Optional[Dict[str, Any]] = None
        self._conditions: List[Tuple[str, Any]] = []

    def select(self, *_args, **_kwargs) -> "_ConditionCheckingPacketTable":
        self._mode = "select"
        return self

    def update(self, payload: Dict[str, Any]) -> "_ConditionCheckingPacketTable":
        self._mode = "update"
        self._update_payload = payload
        return self

    def eq(self, field: str, value: Any) -> "_ConditionCheckingPacketTable":
        self._conditions.append((field, value))
        return self

    def execute(self) -> _FakeResult:
        if self._mode == "select":
            self._client.select_count += 1
            packet = self._client.packet
            # Simulate a concurrent writer committing a change right after
            # our FIRST read of this packet (i.e. between our initial
            # get_packet() and our first conditional update attempt) -- so
            # the first conditional update's state_version condition is
            # already stale by the time it runs.
            if self._client.select_count == 1 and self._client.simulate_concurrent_write:
                self._client.packet = {
                    **self._client.packet,
                    "packet_state": {
                        **(self._client.packet.get("packet_state") or {}),
                        "concurrent_writer_key": "concurrent_value",
                    },
                    "state_version": (self._client.packet.get("state_version", 0) or 0) + 1,
                }
            return _FakeResult([packet] if packet else [])
        if self._mode == "update":
            self._client.update_attempts += 1
            packet = self._client.packet
            if packet is None:
                return _FakeResult([])
            if self._client.force_update_failure:
                return _FakeResult([])
            for field, value in self._conditions:
                if packet.get(field) != value:
                    return _FakeResult([])
            self._client.packet = {**packet, **(self._update_payload or {})}
            return _FakeResult([self._client.packet])
        raise AssertionError("execute() called before select()/update()")


class ConditionCheckingSupabaseClient:
    """FakeSupabaseClient variant that routes agent_work_packets through
    _ConditionCheckingPacketTable so conditional updates can genuinely fail.
    """

    def __init__(
        self,
        packet: Optional[Dict[str, Any]],
        simulate_concurrent_write: bool = False,
        force_update_failure: bool = False,
    ):
        self.packet = packet
        self.updates: List[Dict[str, Any]] = []
        self.logged_events: List[Dict[str, Any]] = []
        self.select_count = 0
        self.update_attempts = 0
        self.simulate_concurrent_write = simulate_concurrent_write
        self.force_update_failure = force_update_failure

    def table(self, name: str):
        if name == "agent_work_packets":
            return _ConditionCheckingPacketTable(self)
        if name == "agent_work_packet_logs":
            return _LogsTable(self)
        raise AssertionError(f"Unexpected table: {name}")


def _make_condition_checking_service(
    packet: Optional[Dict[str, Any]],
    simulate_concurrent_write: bool = False,
    force_update_failure: bool = False,
) -> Tuple[WorkPacketService, ConditionCheckingSupabaseClient]:
    service = WorkPacketService()
    fake_client = ConditionCheckingSupabaseClient(
        packet,
        simulate_concurrent_write=simulate_concurrent_write,
        force_update_failure=force_update_failure,
    )
    service._client = fake_client
    return service, fake_client


class TestUpdateState:
    """Phase C Task 5: WorkPacketService.update_state optimistic concurrency.

    update_state() used to do a plain read-then-merge-then-write with no
    concurrency guard -- a lost-update race if two callers update the same
    packet concurrently. It now conditions its UPDATE on both `id` and the
    `state_version` it read, retrying (re-fetch + re-merge on fresh state)
    when a concurrent writer wins the race in between.
    """

    @pytest.mark.asyncio
    async def test_happy_path_no_retries_needed(self):
        packet = _base_packet(packet_state={"foo": "bar"}, state_version=0)
        service, fake_client = _make_condition_checking_service(packet)

        result = await service.update_state("packet-1", {"new_key": "new_value"})

        assert result["packet_state"] == {"foo": "bar", "new_key": "new_value"}
        assert result["state_version"] == 1
        assert fake_client.update_attempts == 1
        assert len(fake_client.logged_events) == 1
        assert fake_client.logged_events[0]["message"] == "State updated: ['new_key']"

    @pytest.mark.asyncio
    async def test_conflict_then_success_retries_with_fresh_state(self):
        packet = _base_packet(packet_state={"foo": "bar"}, state_version=0)
        service, fake_client = _make_condition_checking_service(
            packet, simulate_concurrent_write=True
        )

        result = await service.update_state("packet-1", {"new_key": "new_value"})

        # The concurrent writer's key must survive the merge -- proving the
        # retry re-fetched and re-merged on FRESH state, not just retried the
        # same stale merge from attempt 1.
        assert result["packet_state"] == {
            "foo": "bar",
            "concurrent_writer_key": "concurrent_value",
            "new_key": "new_value",
        }
        # state_version reflects one successful conditional update on top of
        # the concurrent writer's version (which was itself bumped once).
        assert result["state_version"] == 2
        assert fake_client.update_attempts == 2
        assert fake_client.select_count == 2  # initial get_packet + one retry re-fetch
        # Exactly one state_update log, describing the caller's original keys.
        assert len(fake_client.logged_events) == 1
        assert fake_client.logged_events[0]["message"] == "State updated: ['new_key']"

    @pytest.mark.asyncio
    async def test_exceeds_max_retries_raises_clear_exception(self):
        packet = _base_packet(packet_state={"foo": "bar"}, state_version=0)
        service, fake_client = _make_condition_checking_service(packet, force_update_failure=True)

        with pytest.raises(RuntimeError, match="exceeded 3 retries"):
            await service.update_state("packet-1", {"new_key": "new_value"}, max_retries=3)

        assert fake_client.update_attempts == 3
        assert fake_client.logged_events == []

    @pytest.mark.asyncio
    async def test_state_version_increments_on_success(self):
        packet = _base_packet(packet_state={}, state_version=5)
        service, fake_client = _make_condition_checking_service(packet)

        result = await service.update_state("packet-1", {"k": "v"})

        assert result["state_version"] == 6
