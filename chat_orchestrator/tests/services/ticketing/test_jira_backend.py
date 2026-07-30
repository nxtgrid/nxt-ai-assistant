"""Tests for JiraTicketBackend, mocking aiohttp so no real network calls are made.

No existing aiohttp-mocking convention was found elsewhere in this repo's
test suite (escalation_service.py's Jira REST helpers were previously
untested), so this introduces a small fake aiohttp ClientSession: responses
are queued per (method, url-substring) and each fake response supports the
`async with session.get(...) as resp:` pattern the real helpers use.
"""

from __future__ import annotations

import asyncio
import json as json_module
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import pytest
from loguru import logger

from orchestrator.services.ticketing import jira_backend as jira_backend_module
from orchestrator.services.ticketing.backend import TicketBackendError, TicketCreateRequest
from orchestrator.services.ticketing.jira_backend import JiraTicketBackend
from orchestrator.services.ticketing.jira_issue_types import (
    JiraIssueType,
    JiraIssueTypeSelector,
    normalize_issue_types,
)


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

    def put(self, url: str, **kwargs: Any) -> _FakeRequestCM:
        self.calls.append(("PUT", url, kwargs))
        return _FakeRequestCM(self._resolve("PUT", url))


@pytest.fixture
def fake_session(monkeypatch) -> FakeJiraSession:
    session = FakeJiraSession()
    monkeypatch.setattr(jira_backend_module, "_get_jira_session", lambda: session)
    return session


def _make_backend(project_key: str = "OPS", issue_type: str = "Task") -> JiraTicketBackend:
    return JiraTicketBackend(
        base_url="https://example.atlassian.net",
        email="bot@example.com",
        api_token="tok",
        project_key=project_key,
        issue_type=issue_type,
    )


def _queue_createmeta(
    fake_session: FakeJiraSession, project_key: str, issue_types: List[Dict[str, Any]]
) -> None:
    fake_session.queue(
        "GET",
        f"/rest/api/3/issue/createmeta/{project_key}/issuetypes",
        _FakeResponse(
            200,
            {
                "values": [
                    {"id": issue_type["id"], "name": issue_type["name"]}
                    for issue_type in issue_types
                ]
            },
        ),
    )
    for issue_type in issue_types:
        field_items = [
            {"fieldId": field_id, **field}
            for field_id, field in issue_type.get("fields", {}).items()
        ]
        fake_session.queue(
            "GET",
            f"/rest/api/3/issue/createmeta/{project_key}/issuetypes/{issue_type['id']}",
            _FakeResponse(
                200,
                {
                    "startAt": 0,
                    "maxResults": 50,
                    "total": len(field_items),
                    "fields": field_items,
                },
            ),
        )


def _queue_createmeta_with_task_and_disruption(fake_session: FakeJiraSession) -> None:
    _queue_createmeta(
        fake_session,
        "OPS",
        [
            {"id": "task-id", "name": "Task", "fields": {}},
            {
                "id": "disruption-id",
                "name": "Electricity Service Disruption",
                "fields": {},
            },
        ],
    )


def _queue_createmeta_with_only(
    issue_type_name: str, fake_session: FakeJiraSession, project_key: str = "NET"
) -> None:
    _queue_createmeta(
        fake_session,
        project_key,
        [{"id": "only-type", "name": issue_type_name, "fields": {}}],
    )


def _queue_createmeta_with_required_grid(fake_session: FakeJiraSession) -> None:
    _queue_createmeta(
        fake_session,
        "OPS",
        [
            {
                "id": "grid-type",
                "name": "Task",
                "fields": {
                    "customfield_grid": {
                        "name": "Grid",
                        "required": True,
                        "allowedValues": [{"id": "ogheye-option", "value": "Ogheye"}],
                    }
                },
            }
        ],
    )


def _queue_createmeta_with_optional_priority(fake_session: FakeJiraSession) -> None:
    _queue_createmeta(
        fake_session,
        "OPS",
        [
            {
                "id": "task-id",
                "name": "Task",
                "fields": {
                    "priority": {
                        "name": "Priority",
                        "required": False,
                    }
                },
            }
        ],
    )


def _queue_createmeta_with_required_reporter(fake_session: FakeJiraSession) -> None:
    _queue_createmeta(
        fake_session,
        "OPS",
        [
            {
                "id": "email-request-id",
                "name": "Email request",
                "fields": {"reporter": {"name": "Reporter", "required": True}},
            }
        ],
    )


def _queue_user_search(fake_session: FakeJiraSession, email: str, account_id: str) -> None:
    fake_session.queue(
        "GET",
        "/rest/api/3/user/search",
        _FakeResponse(200, [{"emailAddress": email, "accountId": account_id}]),
    )


def _queue_createmeta_with_required_priority(fake_session: FakeJiraSession) -> None:
    _queue_createmeta(
        fake_session,
        "OPS",
        [
            {
                "id": "task-id",
                "name": "Task",
                "fields": {
                    "priority": {
                        "name": "Priority",
                        "required": True,
                    }
                },
            }
        ],
    )


def _stub_type_selector_to_choose(monkeypatch: pytest.MonkeyPatch, issue_type_id: str) -> None:
    async def select(_self, *, candidate_types, **_kwargs):
        issue_type = next(item for item in candidate_types if item.id == issue_type_id)
        return jira_backend_module.IssueTypeSelection(issue_type, "test", "test selection")

    monkeypatch.setattr(JiraIssueTypeSelector, "select", select)


def _posted_fields(fake_session: FakeJiraSession) -> Dict[str, Any]:
    _method, _url, kwargs = next(call for call in reversed(fake_session.calls) if call[0] == "POST")
    return kwargs["json"]["fields"]


def _notify_request() -> TicketCreateRequest:
    return TicketCreateRequest(summary="Grid down", description="0 kW", source="notify")


def _urgent_notify_request() -> TicketCreateRequest:
    return TicketCreateRequest(
        summary="Grid down",
        description="0 kW",
        severity="urgent",
        source="notify",
    )


class TestHasCredentials:
    def test_true_when_all_present(self):
        backend = _make_backend()
        assert backend.has_credentials() is True

    def test_false_when_missing(self):
        backend = JiraTicketBackend(base_url="", email="", api_token="")
        assert backend.has_credentials() is False


class TestAlertIssueTypeMetadata:
    def test_normalizes_creatable_types_and_required_fields(self):
        types = normalize_issue_types(
            {
                "values": [
                    {
                        "id": "10001",
                        "name": "Electricity Service Disruption",
                        "fields": {
                            "summary": {"required": True},
                            "customfield_123": {"required": True},
                            "labels": {"required": False},
                        },
                    }
                ]
            }
        )

        assert len(types) == 1
        assert types[0].id == "10001"
        assert types[0].required_fields == ("summary", "customfield_123")

    def test_fallback_type_is_limited_to_project_catalogue(self):
        types = normalize_issue_types({"values": [{"id": "1", "name": "Task"}]})

        assert JiraIssueTypeSelector.fallback(types, "task").issue_type.id == "1"
        assert JiraIssueTypeSelector.fallback(types, "Bug") is None

    @pytest.mark.asyncio
    async def test_selector_includes_live_context_without_changing_description(self):
        class RecordingGateway:
            def __init__(self):
                self.messages = []

            async def generate(self, messages, _options):
                self.messages = messages

                class Result:
                    text = '{"issue_type_id":"1","reason":"output is zero"}'

                return Result()

        gateway = RecordingGateway()
        selector = JiraIssueTypeSelector(
            base_url="https://example.atlassian.net",
            headers={},
            project_key="OPS",
            model="fake-model",
            get_session=lambda: None,
            gateway=gateway,
        )
        selector._cached_types = [JiraIssueType(id="1", name="Task")]
        selector._cached_at = time.monotonic()

        result = await selector.select(
            summary="! Urgent: Grid down",
            description="Original alert text",
            operational_context={"live_inverter_output_kw": 0.0},
        )

        assert result is not None
        assert result.issue_type.id == "1"
        prompt = gateway.messages[0].text or ""
        assert "Original alert text" in prompt
        assert '"live_inverter_output_kw": 0.0' in prompt


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
    async def test_probe_failure_status_is_logged(self, fake_session, monkeypatch):
        monkeypatch.setenv("JIRA_HEALTHCHECK_TTL_SECONDS", "60")
        fake_session.queue("GET", "/rest/api/3/myself", _FakeResponse(401, text_data="unauthorized"))
        backend = _make_backend()
        records: List[str] = []
        handler_id = logger.add(records.append, format="{message}", level="WARNING")

        try:
            assert await backend.is_available() is False
        finally:
            logger.remove(handler_id)

        assert any("401" in record for record in records)

    @pytest.mark.asyncio
    async def test_probe_exception_is_logged_as_a_warning(self, fake_session, monkeypatch):
        monkeypatch.setenv("JIRA_HEALTHCHECK_TTL_SECONDS", "60")

        def raise_connection_error(*_args: Any, **_kwargs: Any) -> Any:
            raise aiohttp.ClientConnectionError("connection refused")

        monkeypatch.setattr(fake_session, "get", raise_connection_error)
        backend = _make_backend()
        records: List[str] = []
        handler_id = logger.add(records.append, format="{message}", level="WARNING")

        try:
            assert await backend.is_available() is False
        finally:
            logger.remove(handler_id)

        assert any("connection refused" in record for record in records)

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
        _queue_createmeta_with_only("Task", fake_session, project_key="OPS")
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
        assert payload["fields"]["issuetype"] == {"id": "only-type"}
        assert payload["fields"]["labels"] == ["escalation-abcd1234"]
        assert payload["fields"]["description"]["content"][0]["content"][0]["text"] == (
            "Full description"
        )

    @pytest.mark.asyncio
    async def test_raises_ticket_backend_error_on_failure(self, fake_session):
        _queue_createmeta_with_only("Task", fake_session, project_key="OPS")
        fake_session.queue(
            "POST",
            "/rest/api/3/issue",
            _FakeResponse(500, text_data="boom"),
        )
        backend = _make_backend()

        with pytest.raises(TicketBackendError):
            await backend.create_ticket(TicketCreateRequest(summary="x"))

    @pytest.mark.asyncio
    async def test_notify_selects_a_creatable_type_from_the_generic_project(
        self, fake_session, monkeypatch
    ):
        monkeypatch.setenv("GEMINI_MODEL", "fake-model")
        _queue_createmeta_with_task_and_disruption(fake_session)
        _stub_type_selector_to_choose(monkeypatch, "disruption-id")
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-7"}))

        result = await _make_backend().create_ticket(
            TicketCreateRequest(
                summary="Grid down",
                description="0 kW",
                grid_name="Ogheye",
                source="notify",
            )
        )

        assert result.ticket_type == "Electricity Service Disruption"
        assert _posted_fields(fake_session)["issuetype"] == {"id": "disruption-id"}
        assert "customfield_10057" not in _posted_fields(fake_session)

    @pytest.mark.asyncio
    async def test_notify_without_task_uses_first_compatible_creatable_type(self, fake_session):
        _queue_createmeta_with_only("Comms Failure", fake_session)
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "NET-3"}))

        result = await _make_backend(project_key="NET", issue_type="Task").create_ticket(
            _notify_request()
        )

        assert result.ticket_type == "Comms Failure"

    @pytest.mark.asyncio
    async def test_notify_falls_back_to_a_compatible_type_when_llm_selection_stalls(
        self, fake_session
    ):
        class StalledGateway:
            async def generate(self, _messages, _options):
                await asyncio.Event().wait()

        fake_session.queue(
            "GET",
            "/rest/api/3/issue/createmeta/OPS/issuetypes",
            _FakeResponse(200, {"values": [{"id": "task-id", "name": "Task"}]}),
        )
        fake_session.queue(
            "GET",
            "/rest/api/3/issue/createmeta/OPS/issuetypes/task-id",
            _FakeResponse(
                200,
                {
                    "startAt": 0,
                    "maxResults": 50,
                    "total": 1,
                    "fields": [
                        {"fieldId": "summary", "name": "Summary", "required": True}
                    ],
                },
            ),
        )
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-14"}))
        backend = _make_backend()
        backend._issue_type_selector = JiraIssueTypeSelector(
            base_url="https://example.atlassian.net",
            headers={},
            project_key="OPS",
            model="fake-model",
            get_session=lambda: fake_session,
            gateway=StalledGateway(),
            llm_timeout_seconds=0.05,
        )

        started = time.monotonic()
        result = await backend.create_ticket(_notify_request())

        assert time.monotonic() - started < 0.5
        assert result.ref == "OPS-14"
        assert result.ticket_type == "Task"

    @pytest.mark.asyncio
    async def test_escalation_uses_the_same_metadata_payload_builder(self, fake_session):
        _queue_createmeta_with_required_grid(fake_session)
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-8"}))

        await _make_backend().create_ticket(TicketCreateRequest(summary="Help", grid_name="Ogheye"))

        assert _posted_fields(fake_session)["customfield_grid"] == {"id": "ogheye-option"}

    @pytest.mark.asyncio
    async def test_urgent_jira_ticket_uses_discovered_highest_priority(self, fake_session):
        _queue_createmeta_with_optional_priority(fake_session)
        fake_session.queue(
            "GET",
            "/rest/api/3/priority",
            _FakeResponse(
                200,
                [{"id": "1", "name": "High"}, {"id": "2", "name": "Highest"}],
            ),
        )
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-9"}))

        await _make_backend().create_ticket(_urgent_notify_request())

        assert _posted_fields(fake_session)["priority"] == {"id": "2"}

    @pytest.mark.asyncio
    async def test_urgent_jira_ticket_can_satisfy_required_priority(self, fake_session):
        _queue_createmeta_with_required_priority(fake_session)
        fake_session.queue(
            "GET",
            "/rest/api/3/priority",
            _FakeResponse(
                200,
                [{"id": "1", "name": "High"}, {"id": "2", "name": "Highest"}],
            ),
        )
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-13"}))

        result = await _make_backend().create_ticket(_urgent_notify_request())

        assert result.ref == "OPS-13"
        assert _posted_fields(fake_session)["priority"] == {"id": "2"}

    @pytest.mark.asyncio
    async def test_priority_lookup_failure_does_not_block_urgent_ticket(self, fake_session):
        _queue_createmeta_with_optional_priority(fake_session)
        fake_session.queue(
            "GET",
            "/rest/api/3/priority",
            _FakeResponse(503, text_data="temporarily unavailable"),
        )
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-10"}))

        result = await _make_backend().create_ticket(_urgent_notify_request())

        assert result.ref == "OPS-10"
        assert "priority" not in _posted_fields(fake_session)

    @pytest.mark.asyncio
    async def test_urgent_ticket_omits_priority_when_issue_type_cannot_create_it(
        self, fake_session
    ):
        _queue_createmeta_with_only("Task", fake_session, project_key="OPS")
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-12"}))

        result = await _make_backend().create_ticket(_urgent_notify_request())

        assert result.ref == "OPS-12"
        assert "priority" not in _posted_fields(fake_session)
        assert not any("/rest/api/3/priority" in url for _method, url, _kwargs in fake_session.calls)

    @pytest.mark.asyncio
    async def test_non_urgent_jira_ticket_does_not_request_or_set_priority(self, fake_session):
        _queue_createmeta_with_only("Task", fake_session, project_key="OPS")
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-11"}))

        await _make_backend().create_ticket(_notify_request())

        assert "priority" not in _posted_fields(fake_session)
        assert not any("/rest/api/3/priority" in url for _method, url, _kwargs in fake_session.calls)

    @pytest.mark.asyncio
    async def test_raises_without_posting_when_no_compatible_type_exists(self, fake_session):
        _queue_createmeta(
            fake_session,
            "OPS",
            [
                {
                    "id": "unsupported-type",
                    "name": "Unsupported",
                    "fields": {"customfield_unknown": {"required": True}},
                }
            ],
        )

        with pytest.raises(TicketBackendError, match="compatible issue type"):
            await _make_backend().create_ticket(TicketCreateRequest(summary="Help"))

        assert not any(method == "POST" for method, _url, _kwargs in fake_session.calls)

    async def test_no_compatible_type_error_names_the_blocking_type_and_field(self, fake_session):
        _queue_createmeta(
            fake_session,
            "OPS",
            [
                {
                    "id": "unsupported-type",
                    "name": "Unsupported",
                    "fields": {"customfield_unknown": {"required": True}},
                }
            ],
        )

        with pytest.raises(TicketBackendError, match="Unsupported.*customfield_unknown"):
            await _make_backend().create_ticket(TicketCreateRequest(summary="Help"))

    async def test_no_compatible_type_error_distinguishes_no_types_at_all(self, fake_session):
        _queue_createmeta(fake_session, "OPS", [])

        with pytest.raises(TicketBackendError, match="no issue types"):
            await _make_backend().create_ticket(TicketCreateRequest(summary="Help"))

    @pytest.mark.asyncio
    async def test_resolves_own_account_as_reporter_when_required(self, fake_session):
        """Jira Service Management projects commonly require ``reporter`` on
        every issue type; customers aren't Jira users, so the integration's
        own (bot) account is the only reporter identity it can always supply.
        Without this, every issue type is permanently incompatible and every
        ticket silently falls back to the internal backend."""
        _queue_createmeta_with_required_reporter(fake_session)
        _queue_user_search(fake_session, "bot@example.com", "bot-account-1")
        fake_session.queue(
            "POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-50"})
        )

        result = await _make_backend().create_ticket(TicketCreateRequest(summary="Help"))

        assert result.ref == "OPS-50"
        assert _posted_fields(fake_session)["reporter"] == {"accountId": "bot-account-1"}

    @pytest.mark.asyncio
    async def test_own_account_lookup_is_cached_across_creates(self, fake_session):
        _queue_createmeta_with_required_reporter(fake_session)
        _queue_user_search(fake_session, "bot@example.com", "bot-account-1")
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-51"}))
        backend = _make_backend()
        await backend.create_ticket(TicketCreateRequest(summary="Help"))

        # Second create: no user/search response queued -- must reuse the cached id.
        _queue_createmeta_with_required_reporter(fake_session)
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-52"}))

        await backend.create_ticket(TicketCreateRequest(summary="Help again"))

        assert _posted_fields(fake_session)["reporter"] == {"accountId": "bot-account-1"}

    @pytest.mark.asyncio
    async def test_does_not_look_up_own_account_when_reporter_not_required(self, fake_session):
        """No user/search response is queued -- resolving unconditionally would
        raise the fake session's unmatched-request assertion, so this also
        guards against a regression that makes every ticket pay for a lookup
        it doesn't need."""
        _queue_createmeta_with_only("Task", fake_session, project_key="OPS")
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-53"}))

        result = await _make_backend().create_ticket(TicketCreateRequest(summary="Help"))

        assert result.ref == "OPS-53"

    @pytest.mark.asyncio
    async def test_names_reporter_when_own_account_cannot_be_resolved(self, fake_session):
        _queue_createmeta_with_required_reporter(fake_session)
        fake_session.queue("GET", "/rest/api/3/user/search", _FakeResponse(200, []))

        with pytest.raises(TicketBackendError, match="Email request.*[Rr]eporter"):
            await _make_backend().create_ticket(TicketCreateRequest(summary="Help"))


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
            "/rest/api/3/search/jql",
            _FakeResponse(200, {"issues": [{"key": "OPS-42"}]}),
        )
        backend = _make_backend()

        found = await backend.find_by_escalation("abcd1234-5678-90ab-cdef-1234567890ab")

        assert found == "OPS-42"
        method, url, kwargs = fake_session.calls[-1]
        assert method == "GET"
        assert url == "https://example.atlassian.net/rest/api/3/search/jql"
        assert "escalation-abcd1234" in kwargs["params"]["jql"]
        assert kwargs["params"]["maxResults"] == "1"

    @pytest.mark.asyncio
    async def test_none_when_no_issues_found(self, fake_session):
        fake_session.queue("GET", "/rest/api/3/search/jql", _FakeResponse(200, {"issues": []}))
        backend = _make_backend()

        assert await backend.find_by_escalation("abcd1234-0000-0000-0000-000000000000") is None


class TestUpdateTicket:
    @pytest.mark.asyncio
    async def test_puts_summary_and_description(self, fake_session):
        fake_session.queue("PUT", "/rest/api/3/issue/OPS-42", _FakeResponse(204, text_data=""))
        backend = _make_backend()

        ok = await backend.update_ticket("OPS-42", summary="new summary", description="new desc")

        assert ok is True
        method, url, kwargs = fake_session.calls[-1]
        assert method == "PUT"
        assert url == "https://example.atlassian.net/rest/api/3/issue/OPS-42"
        payload = kwargs["json"]
        assert payload["fields"]["summary"] == "new summary"
        assert payload["fields"]["description"]["content"][0]["content"][0]["text"] == "new desc"

    @pytest.mark.asyncio
    async def test_includes_priority_when_given(self, fake_session):
        fake_session.queue("PUT", "/rest/api/3/issue/OPS-42", _FakeResponse(204, text_data=""))
        backend = _make_backend()

        await backend.update_ticket("OPS-42", priority_id="10001")

        _method, _url, kwargs = fake_session.calls[-1]
        assert kwargs["json"]["fields"]["priority"] == {"id": "10001"}

    @pytest.mark.asyncio
    async def test_highest_priority_sentinel_is_resolved_before_update(self, fake_session):
        fake_session.queue(
            "GET",
            "/rest/api/3/priority",
            _FakeResponse(200, [{"id": "2", "name": "hIGHeSt"}]),
        )
        fake_session.queue("PUT", "/rest/api/3/issue/OPS-42", _FakeResponse(204, text_data=""))
        backend = _make_backend()

        assert await backend.update_ticket(
            "OPS-42", summary="urgent update", priority_id="highest"
        )

        _method, _url, kwargs = fake_session.calls[-1]
        assert kwargs["json"]["fields"]["priority"] == {"id": "2"}

    @pytest.mark.asyncio
    async def test_failed_highest_lookup_still_updates_without_priority(self, fake_session):
        fake_session.queue(
            "GET",
            "/rest/api/3/priority",
            _FakeResponse(200, [{"id": "1", "name": "High"}]),
        )
        fake_session.queue("PUT", "/rest/api/3/issue/OPS-42", _FakeResponse(204, text_data=""))
        backend = _make_backend()

        assert await backend.update_ticket(
            "OPS-42", summary="urgent update", priority_id="highest"
        )

        _method, _url, kwargs = fake_session.calls[-1]
        assert set(kwargs["json"]["fields"]) == {"summary"}

    @pytest.mark.asyncio
    async def test_omits_unset_fields(self, fake_session):
        fake_session.queue("PUT", "/rest/api/3/issue/OPS-42", _FakeResponse(204, text_data=""))
        backend = _make_backend()

        await backend.update_ticket("OPS-42", summary="only summary")

        _method, _url, kwargs = fake_session.calls[-1]
        assert set(kwargs["json"]["fields"].keys()) == {"summary"}

    @pytest.mark.asyncio
    async def test_noop_true_when_nothing_to_update(self, fake_session):
        backend = _make_backend()

        ok = await backend.update_ticket("OPS-42")

        assert ok is True
        assert fake_session.calls == []

    @pytest.mark.asyncio
    async def test_false_on_error_status(self, fake_session):
        fake_session.queue(
            "PUT", "/rest/api/3/issue/OPS-42", _FakeResponse(400, text_data="bad field")
        )
        backend = _make_backend()

        assert await backend.update_ticket("OPS-42", summary="x") is False

    @pytest.mark.asyncio
    async def test_false_on_exception(self, fake_session):
        backend = _make_backend()
        # No response queued -- _resolve raises AssertionError, which the
        # implementation must catch and turn into a plain False, not a crash.
        assert await backend.update_ticket("OPS-42", summary="x") is False


class TestFindOpenByGrid:
    @pytest.mark.asyncio
    async def test_returns_candidates_from_search(self, fake_session):
        fake_session.queue(
            "GET",
            "/rest/api/3/search/jql",
            _FakeResponse(
                200,
                {
                    "issues": [
                        {
                            "key": "OPS-42",
                            "fields": {
                                "summary": "MPPT issue",
                                "description": {
                                    "type": "doc",
                                    "version": 1,
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "details"}],
                                        }
                                    ],
                                },
                                "status": {
                                    "name": "In Progress",
                                    "statusCategory": {"key": "indeterminate"},
                                },
                                "created": "2026-07-20T10:00:00.000+0000",
                                "labels": ["alert-ticket", "grid-kudi"],
                            },
                        }
                    ]
                },
            ),
        )
        backend = _make_backend()

        results = await backend.find_open_by_grid("Kudi")

        assert len(results) == 1
        candidate = results[0]
        assert candidate.ref == "OPS-42"
        assert candidate.backend == "jira"
        assert candidate.summary == "MPPT issue"
        assert candidate.description == "details"
        assert candidate.is_done is False
        assert candidate.labels == ["alert-ticket", "grid-kudi"]

        method, url, kwargs = fake_session.calls[-1]
        assert method == "GET"
        assert url == "https://example.atlassian.net/rest/api/3/search/jql"
        jql = kwargs["params"]["jql"]
        assert "grid-kudi" in jql
        assert "statusCategory != Done" in jql
        assert 'project = "OPS"' in jql
        assert kwargs["params"]["maxResults"] == "20"

    @pytest.mark.asyncio
    async def test_grid_name_slugified_for_label_match(self, fake_session):
        fake_session.queue("GET", "/rest/api/3/search/jql", _FakeResponse(200, {"issues": []}))
        backend = _make_backend()

        await backend.find_open_by_grid("Kudi Grid #2 (West)")

        _method, _url, kwargs = fake_session.calls[-1]
        assert "grid-kudi-grid-2-west" in kwargs["params"]["jql"]

    @pytest.mark.asyncio
    async def test_empty_on_error_status(self, fake_session):
        fake_session.queue(
            "GET", "/rest/api/3/search/jql", _FakeResponse(410, text_data="removed")
        )
        backend = _make_backend()
        records = []
        handler_id = logger.add(records.append, format="{message}", level="WARNING")

        try:
            assert await backend.find_open_by_grid("Kudi") == []
        finally:
            logger.remove(handler_id)

        assert any("HTTP 410" in record and "removed" in record for record in records)

    @pytest.mark.asyncio
    async def test_empty_when_base_url_missing(self, fake_session):
        backend = JiraTicketBackend(base_url="", email="e", api_token="t")

        assert await backend.find_open_by_grid("Kudi") == []
        assert fake_session.calls == []


class TestAdfRoundTrip:
    """_text_to_adf / _adf_to_text must round-trip correctly for both the
    original single-paragraph shape (escalation descriptions, comments) and
    the multi-block shape correlation_render.py produces (paragraph ->
    bulletList -> paragraph, for an amended ticket's description)."""

    def test_single_line_round_trips_unchanged(self):
        text = "Please check VRM."
        adf = jira_backend_module._text_to_adf(text)

        assert adf == {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
        }
        assert jira_backend_module._adf_to_text(adf) == text

    def test_multi_block_paragraph_bulletlist_paragraph_round_trips_exactly(self):
        text = (
            "desc\n\n"
            "[anansi:affected-start]\n"
            "Affected components (2):\n"
            "- MPPT A3 — first seen t1, last t2 (2x)\n"
            "- MPPT A7 — first seen t3, last t3 (1x)\n"
            "Occurrences: 3 · Grouped by Anansi alert correlation\n"
            "[anansi:affected-end]"
        )

        adf = jira_backend_module._text_to_adf(text)

        assert [block["type"] for block in adf["content"]] == [
            "paragraph",
            "bulletList",
            "paragraph",
        ]
        assert jira_backend_module._adf_to_text(adf) == text

    def test_empty_text_produces_valid_empty_doc(self):
        adf = jira_backend_module._text_to_adf("")
        assert jira_backend_module._adf_to_text(adf) == ""
