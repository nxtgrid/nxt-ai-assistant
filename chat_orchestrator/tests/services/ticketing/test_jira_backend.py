"""Tests for JiraTicketBackend, mocking aiohttp so no real network calls are made.

No existing aiohttp-mocking convention was found elsewhere in this repo's
test suite (escalation_service.py's Jira REST helpers were previously
untested), so this introduces a small fake aiohttp ClientSession: responses
are queued per (method, url-substring) and each fake response supports the
`async with session.get(...) as resp:` pattern the real helpers use.
"""

from __future__ import annotations

import json as json_module
from typing import Any, Dict, List, Optional, Tuple

import pytest

from orchestrator.services.ticketing import jira_backend as jira_backend_module
from orchestrator.services.ticketing.backend import TicketBackendError, TicketCreateRequest
from orchestrator.services.ticketing.jira_backend import JiraTicketBackend


class _FakeResponse:
    def __init__(self, status: int, json_data: Any = None, text_data: Optional[str] = None):
        self.status = status
        self._json_data = json_data
        self._text_data = (
            text_data if text_data is not None else (json_module.dumps(json_data) if json_data is not None else "")
        )

    async def json(self) -> Any:
        return self._json_data

    async def text(self) -> str:
        return self._text_data

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeRequestCM:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeJiraSession:
    """Queue responses by (method, url-substring); each call consumes one match."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self._queue: List[Tuple[str, str, _FakeResponse]] = []

    def queue(self, method: str, url_contains: str, response: _FakeResponse) -> None:
        self._queue.append((method.upper(), url_contains, response))

    def _resolve(self, method: str, url: str) -> _FakeResponse:
        for i, (m, pred, resp) in enumerate(self._queue):
            if m == method.upper() and pred in url:
                del self._queue[i]
                return resp
        raise AssertionError(f"No queued fake response for {method} {url}; calls so far: {self.calls}")

    def get(self, url: str, **kwargs: Any) -> _FakeRequestCM:
        self.calls.append(("GET", url, kwargs))
        return _FakeRequestCM(self._resolve("GET", url))

    def post(self, url: str, **kwargs: Any) -> _FakeRequestCM:
        self.calls.append(("POST", url, kwargs))
        return _FakeRequestCM(self._resolve("POST", url))


@pytest.fixture
def fake_session(monkeypatch) -> FakeJiraSession:
    session = FakeJiraSession()
    monkeypatch.setattr(jira_backend_module, "_get_jira_session", lambda: session)
    return session


def _make_backend() -> JiraTicketBackend:
    return JiraTicketBackend(
        base_url="https://example.atlassian.net",
        email="bot@example.com",
        api_token="tok",
        project_key="OPS",
        issue_type="Task",
    )


class TestHasCredentials:
    def test_true_when_all_present(self):
        backend = _make_backend()
        assert backend.has_credentials() is True

    def test_false_when_missing(self):
        backend = JiraTicketBackend(base_url="", email="", api_token="")
        assert backend.has_credentials() is False


class TestIsAvailable:
    @pytest.mark.asyncio
    async def test_false_without_credentials(self, fake_session):
        backend = JiraTicketBackend(base_url="", email="", api_token="")
        assert await backend.is_available() is False
        assert fake_session.calls == []  # no probe attempted without creds

    @pytest.mark.asyncio
    async def test_true_when_probe_succeeds(self, fake_session, monkeypatch):
        monkeypatch.setenv("JIRA_HEALTHCHECK_TTL_SECONDS", "60")
        fake_session.queue("GET", "/rest/api/3/myself", _FakeResponse(200, {"accountId": "me"}))
        backend = _make_backend()

        assert await backend.is_available() is True

    @pytest.mark.asyncio
    async def test_false_when_probe_fails(self, fake_session, monkeypatch):
        monkeypatch.setenv("JIRA_HEALTHCHECK_TTL_SECONDS", "60")
        fake_session.queue("GET", "/rest/api/3/myself", _FakeResponse(401, text_data="unauthorized"))
        backend = _make_backend()

        assert await backend.is_available() is False

    @pytest.mark.asyncio
    async def test_probe_result_is_cached_within_ttl(self, fake_session, monkeypatch):
        monkeypatch.setenv("JIRA_HEALTHCHECK_TTL_SECONDS", "60")
        fake_session.queue("GET", "/rest/api/3/myself", _FakeResponse(200, {"accountId": "me"}))
        backend = _make_backend()

        first = await backend.is_available()
        second = await backend.is_available()

        assert first is True
        assert second is True
        # Only one GET actually hit the fake session -- the second call was
        # served from the TTL cache (the queue would raise if consulted again).
        assert len(fake_session.calls) == 1


class TestCreateTicket:
    @pytest.mark.asyncio
    async def test_delegates_to_create_issue_endpoint_with_expected_payload(self, fake_session):
        fake_session.queue(
            "POST",
            "/rest/api/3/issue",
            _FakeResponse(201, {"key": "OPS-42", "id": "10001"}),
        )
        backend = _make_backend()
        req = TicketCreateRequest(
            summary="Customer needs help",
            description="Full description",
            labels=["escalation-abcd1234"],
        )

        result = await backend.create_ticket(req)

        assert result.ref == "OPS-42"
        assert result.backend == "jira"
        assert result.url == "https://example.atlassian.net/browse/OPS-42"

        method, url, kwargs = fake_session.calls[-1]
        assert method == "POST"
        assert url == "https://example.atlassian.net/rest/api/3/issue"
        payload = kwargs["json"]
        assert payload["fields"]["project"] == {"key": "OPS"}
        assert payload["fields"]["summary"] == "Customer needs help"
        assert payload["fields"]["issuetype"] == {"name": "Task"}
        assert payload["fields"]["labels"] == ["escalation-abcd1234"]
        assert payload["fields"]["description"]["content"][0]["content"][0]["text"] == (
            "Full description"
        )

    @pytest.mark.asyncio
    async def test_raises_ticket_backend_error_on_failure(self, fake_session):
        fake_session.queue(
            "POST",
            "/rest/api/3/issue",
            _FakeResponse(500, text_data="boom"),
        )
        backend = _make_backend()

        with pytest.raises(TicketBackendError):
            await backend.create_ticket(TicketCreateRequest(summary="x"))


class TestAddComment:
    @pytest.mark.asyncio
    async def test_posts_comment_body(self, fake_session):
        fake_session.queue(
            "POST",
            "/rest/api/3/issue/OPS-42/comment",
            _FakeResponse(201, {"id": "1"}),
        )
        backend = _make_backend()

        ok = await backend.add_comment("OPS-42", "hello from support")

        assert ok is True
        method, url, kwargs = fake_session.calls[-1]
        assert url == "https://example.atlassian.net/rest/api/3/issue/OPS-42/comment"
        body_text = kwargs["json"]["body"]["content"][0]["content"][0]["text"]
        assert body_text == "hello from support"

    @pytest.mark.asyncio
    async def test_false_on_error_status(self, fake_session):
        fake_session.queue(
            "POST",
            "/rest/api/3/issue/OPS-42/comment",
            _FakeResponse(404, text_data="not found"),
        )
        backend = _make_backend()

        assert await backend.add_comment("OPS-42", "x") is False


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_maps_fields_to_ticket_status(self, fake_session):
        fake_session.queue(
            "GET",
            "/rest/api/3/issue/OPS-42",
            _FakeResponse(
                200,
                {
                    "fields": {
                        "summary": "Customer issue",
                        "status": {
                            "name": "In Progress",
                            "statusCategory": {"key": "indeterminate"},
                        },
                    }
                },
            ),
        )
        backend = _make_backend()

        status = await backend.get_status("OPS-42")

        assert status is not None
        assert status.summary == "Customer issue"
        assert status.is_done is False
        assert status.raw_status == "In Progress"

    @pytest.mark.asyncio
    async def test_is_done_true_for_done_category(self, fake_session):
        fake_session.queue(
            "GET",
            "/rest/api/3/issue/OPS-42",
            _FakeResponse(
                200,
                {
                    "fields": {
                        "summary": "Customer issue",
                        "status": {"name": "Done", "statusCategory": {"key": "done"}},
                    }
                },
            ),
        )
        backend = _make_backend()

        status = await backend.get_status("OPS-42")

        assert status is not None
        assert status.is_done is True

    @pytest.mark.asyncio
    async def test_none_on_not_found(self, fake_session):
        fake_session.queue("GET", "/rest/api/3/issue/OPS-999", _FakeResponse(404, text_data="nf"))
        backend = _make_backend()

        assert await backend.get_status("OPS-999") is None


class TestTransitionToDone:
    @pytest.mark.asyncio
    async def test_finds_and_executes_done_transition(self, fake_session):
        fake_session.queue(
            "GET",
            "/rest/api/3/issue/OPS-42/transitions",
            _FakeResponse(
                200,
                {
                    "transitions": [
                        {"id": "11", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
                        {"id": "31", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
                    ]
                },
            ),
        )
        fake_session.queue(
            "POST", "/rest/api/3/issue/OPS-42/transitions", _FakeResponse(204, text_data="")
        )
        backend = _make_backend()

        await backend.transition_to_done("OPS-42")

        method, url, kwargs = fake_session.calls[-1]
        assert method == "POST"
        assert kwargs["json"] == {"transition": {"id": "31"}}

    @pytest.mark.asyncio
    async def test_noop_when_no_done_transition_available(self, fake_session):
        fake_session.queue(
            "GET",
            "/rest/api/3/issue/OPS-42/transitions",
            _FakeResponse(
                200,
                {
                    "transitions": [
                        {"id": "11", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
                    ]
                },
            ),
        )
        backend = _make_backend()

        # Should not raise, and should not attempt a POST (queue would raise
        # AssertionError on an unqueued POST if one were attempted).
        await backend.transition_to_done("OPS-42")


class TestFindByEscalation:
    @pytest.mark.asyncio
    async def test_finds_ticket_by_label(self, fake_session):
        fake_session.queue(
            "GET",
            "/rest/api/3/issue/search",
            _FakeResponse(200, {"issues": [{"key": "OPS-42"}]}),
        )
        backend = _make_backend()

        found = await backend.find_by_escalation("abcd1234-5678-90ab-cdef-1234567890ab")

        assert found == "OPS-42"
        method, url, kwargs = fake_session.calls[-1]
        assert "escalation-abcd1234" in kwargs["params"]["jql"]

    @pytest.mark.asyncio
    async def test_none_when_no_issues_found(self, fake_session):
        fake_session.queue("GET", "/rest/api/3/issue/search", _FakeResponse(200, {"issues": []}))
        backend = _make_backend()

        assert await backend.find_by_escalation("abcd1234-0000-0000-0000-000000000000") is None
