"""Backend-resolution matrix for TicketService.resolve_backend().

Covers all 9 combinations of TICKET_BACKEND_OVERRIDE in {auto, jira, internal}
x Jira state in {creds absent, creds present + probe healthy, creds present +
probe failing}, using a fake JiraTicketBackend so no real network calls are
made and the health probe is fully controlled per-test.
"""

from __future__ import annotations

import pytest

from orchestrator.services.ticketing.internal_backend import InternalTicketBackend
from orchestrator.services.ticketing.service import TicketService


class _FakeJiraBackend:
    """Stands in for JiraTicketBackend with a controllable creds/probe state."""

    name = "jira"

    def __init__(self, has_creds: bool, probe_ok: bool) -> None:
        self._has_creds = has_creds
        self._probe_ok = probe_ok
        self.is_available_calls = 0
        self.has_credentials_calls = 0

    def has_credentials(self) -> bool:
        self.has_credentials_calls += 1
        return self._has_creds

    async def is_available(self) -> bool:
        self.is_available_calls += 1
        # Mirrors the real is_available(): creds absent always -> False,
        # regardless of what the (irrelevant) probe would say.
        if not self._has_creds:
            return False
        return self._probe_ok


def _make_service(has_creds: bool, probe_ok: bool) -> tuple[TicketService, _FakeJiraBackend]:
    jira = _FakeJiraBackend(has_creds=has_creds, probe_ok=probe_ok)
    internal = InternalTicketBackend(client=object())
    service = TicketService(jira_backend=jira, internal_backend=internal)
    return service, jira


JIRA_STATES = {
    "creds_absent": dict(has_creds=False, probe_ok=False),
    "creds_present_healthy": dict(has_creds=True, probe_ok=True),
    "creds_present_unhealthy": dict(has_creds=True, probe_ok=False),
}


class TestResolveBackendOverrideInternal:
    """override='internal' -> always internal, regardless of Jira state."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state_name", JIRA_STATES.keys())
    async def test_always_internal(self, monkeypatch, state_name):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "internal")
        service, jira = _make_service(**JIRA_STATES[state_name])

        backend = await service.resolve_backend()

        assert backend.name == "internal"


class TestResolveBackendOverrideJira:
    """override='jira' -> jira if creds present, else internal (probe irrelevant)."""

    @pytest.mark.asyncio
    async def test_creds_absent_falls_back_to_internal(self, monkeypatch):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "jira")
        service, jira = _make_service(**JIRA_STATES["creds_absent"])

        backend = await service.resolve_backend()

        assert backend.name == "internal"

    @pytest.mark.asyncio
    async def test_creds_present_healthy_uses_jira(self, monkeypatch):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "jira")
        service, jira = _make_service(**JIRA_STATES["creds_present_healthy"])

        backend = await service.resolve_backend()

        assert backend.name == "jira"

    @pytest.mark.asyncio
    async def test_creds_present_but_probe_failing_still_uses_jira(self, monkeypatch):
        """Per design: override='jira' never hard-fails to internal on a bad
        probe -- only missing creds fall back. The probe is irrelevant here."""
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "jira")
        service, jira = _make_service(**JIRA_STATES["creds_present_unhealthy"])

        backend = await service.resolve_backend()

        assert backend.name == "jira"
        # Confirms this branch doesn't even consult the health probe.
        assert jira.is_available_calls == 0


class TestResolveBackendOverrideAuto:
    """override='auto' (default) -> jira iff is_available() (creds + healthy probe)."""

    @pytest.mark.asyncio
    async def test_creds_absent_uses_internal(self, monkeypatch):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "auto")
        service, jira = _make_service(**JIRA_STATES["creds_absent"])

        backend = await service.resolve_backend()

        assert backend.name == "internal"

    @pytest.mark.asyncio
    async def test_creds_present_healthy_uses_jira(self, monkeypatch):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "auto")
        service, jira = _make_service(**JIRA_STATES["creds_present_healthy"])

        backend = await service.resolve_backend()

        assert backend.name == "jira"

    @pytest.mark.asyncio
    async def test_creds_present_but_probe_failing_uses_internal(self, monkeypatch):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "auto")
        service, jira = _make_service(**JIRA_STATES["creds_present_unhealthy"])

        backend = await service.resolve_backend()

        assert backend.name == "internal"


class TestResolveBackendDefaultsToAuto:
    @pytest.mark.asyncio
    async def test_unset_override_behaves_like_auto(self, monkeypatch):
        monkeypatch.delenv("TICKET_BACKEND_OVERRIDE", raising=False)
        service, jira = _make_service(**JIRA_STATES["creds_present_healthy"])

        backend = await service.resolve_backend()

        assert backend.name == "jira"

    @pytest.mark.asyncio
    async def test_unrecognized_override_behaves_like_auto(self, monkeypatch):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "bogus")
        service, jira = _make_service(**JIRA_STATES["creds_present_unhealthy"])

        backend = await service.resolve_backend()

        assert backend.name == "internal"
