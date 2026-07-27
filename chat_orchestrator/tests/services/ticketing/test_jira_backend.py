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
from orchestrator.services.ticketing.jira_issue_types import JiraIssueTypeSelector, normalize_issue_types


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


@pytest.fixture(autouse=True)
def _reset_alert_grid_options_cache():
    """jira_alert_profile.resolve_or_create_grid_option caches the grid
    option list in-process (module-level, keyed by base_url/field/context
    ids) -- every test here reuses the same fake base_url and field/context
    ids, so without a reset one test's cached options would leak into the
    next."""
    import orchestrator.services.ticketing.jira_alert_profile as alert_profile_module

    alert_profile_module._OPTIONS_CACHE.clear()
    yield
    alert_profile_module._OPTIONS_CACHE.clear()


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


def _set_full_alert_profile_env(monkeypatch) -> None:
    values = {
        "JIRA_ALERT_PROJECT_ID": "10000",
        "JIRA_ALERT_PROJECT_KEY": "ALM",
        "JIRA_ALERT_ISSUE_TYPE_ID": "10001",
        "JIRA_ALERT_REPORTER_ACCOUNT_ID": "acc-1",
        "JIRA_ALERT_LABEL": "alarm",
        "JIRA_ALERT_GRID_FIELD_ID": "customfield_10057",
        "JIRA_ALERT_GRID_CONTEXT_ID": "ctx-1",
        "JIRA_ALERT_GRID_IGNORED_OPTION_ID": "10315",
        "JIRA_ALERT_TYPE_FIELD_ID": "customfield_10060",
        "JIRA_ALERT_TYPE_OPTION_ID": "opt-alarm-type",
        "JIRA_ALERT_CATEGORY_FIELD_ID": "customfield_10061",
        "JIRA_ALERT_CATEGORY_PARENT_OPTION_ID": "opt-parent",
        "JIRA_ALERT_CATEGORY_CHILD_OPTION_ID": "opt-child",
        "JIRA_ALERT_DATE_FIELD_ID": "customfield_10062",
        "JIRA_ALERT_NO_GRID_PRIORITY_ID": "prio-no-grid",
        "JIRA_ALERT_ESCALATED_PRIORITY_ID": "prio-escalated",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


class TestCreateTicketAlertProfile:
    """source='notify' + a fully-configured alert profile takes over
    create_ticket() with the n8n-parity field shape (see
    jira_alert_profile.py); anything else keeps today's OPS/Task path
    completely unchanged."""

    @pytest.mark.asyncio
    async def test_notify_with_configured_profile_uses_alert_shape(self, fake_session, monkeypatch):
        _set_full_alert_profile_env(monkeypatch)
        fake_session.queue(
            "GET",
            "/field/customfield_10057/context/ctx-1/option",
            _FakeResponse(200, {"values": [{"id": "500", "value": "Kudi"}]}),
        )
        fake_session.queue(
            "POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "ALM-7", "id": "70007"})
        )
        backend = _make_backend()
        req = TicketCreateRequest(
            summary="! Warning: MPPT A3 in Kudi seems to perform lower !",
            description="Please check VRM.",
            grid_name="Kudi",
            source="notify",
        )

        result = await backend.create_ticket(req)

        assert result.ref == "ALM-7"
        assert result.backend == "jira"
        assert result.url == "https://example.atlassian.net/browse/ALM-7"

        method, url, kwargs = fake_session.calls[-1]
        assert method == "POST"
        assert url == "https://example.atlassian.net/rest/api/3/issue"
        payload = kwargs["json"]
        assert payload["fields"]["project"] == {"id": "10000"}
        assert payload["fields"]["customfield_10057"] == {"id": "500"}
        assert "grid-kudi" in payload["fields"]["labels"]

    @pytest.mark.asyncio
    async def test_notify_without_configured_profile_falls_back_to_ops_task_shape(
        self, fake_session
    ):
        """No JIRA_ALERT_* flags set -- source='notify' must still use the
        existing OPS/Task path unchanged (no grid-option lookup at all)."""
        fake_session.queue(
            "POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-1", "id": "1"})
        )
        backend = _make_backend()
        req = TicketCreateRequest(summary="x", grid_name="Kudi", source="notify")

        result = await backend.create_ticket(req)

        assert result.ref == "OPS-1"
        # Only the createmeta grid-option resolution (OPS path) + issue POST --
        # never the alert profile's field/context/option endpoint.
        assert not any("field/customfield_10057/context" in url for _m, url, _k in fake_session.calls)

    @pytest.mark.asyncio
    async def test_escalation_source_ignores_configured_alert_profile(
        self, fake_session, monkeypatch
    ):
        """Even with a fully-configured alert profile, customer escalations
        (source='escalation', the default) must keep using the OPS/Task
        path -- the alert profile only ever applies to source='notify'."""
        _set_full_alert_profile_env(monkeypatch)
        fake_session.queue(
            "POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-2", "id": "2"})
        )
        backend = _make_backend()
        req = TicketCreateRequest(summary="x", grid_name="Kudi")  # source defaults to "escalation"

        result = await backend.create_ticket(req)

        assert result.ref == "OPS-2"
        assert not any(
            "field/customfield_10057/context" in url for _m, url, _k in fake_session.calls
        )

    @pytest.mark.asyncio
    async def test_alert_shape_raises_on_creation_failure(self, fake_session, monkeypatch):
        _set_full_alert_profile_env(monkeypatch)
        fake_session.queue(
            "GET",
            "/field/customfield_10057/context/ctx-1/option",
            _FakeResponse(200, {"values": [{"id": "500", "value": "Kudi"}]}),
        )
        fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(500, text_data="boom"))
        backend = _make_backend()
        req = TicketCreateRequest(summary="x", grid_name="Kudi", source="notify")

        with pytest.raises(TicketBackendError):
            await backend.create_ticket(req)

    @pytest.mark.asyncio
    async def test_alert_shape_files_without_grid_field_when_resolution_fails(
        self, fake_session, monkeypatch
    ):
        """Grid-option lookup failing must never hard-fail ticket creation --
        file with the no-grid priority instead, exactly like the OPS path
        does when a grid can't be resolved."""
        _set_full_alert_profile_env(monkeypatch)
        fake_session.queue(
            "GET", "/field/customfield_10057/context/ctx-1/option", _FakeResponse(500, {})
        )
        fake_session.queue(
            "POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "ALM-8", "id": "8"})
        )
        backend = _make_backend()
        req = TicketCreateRequest(summary="x", grid_name="Kudi", source="notify")

        result = await backend.create_ticket(req)

        assert result.ref == "ALM-8"
        _method, _url, kwargs = fake_session.calls[-1]
        assert "customfield_10057" not in kwargs["json"]["fields"]
        assert kwargs["json"]["fields"]["priority"] == {"id": "prio-no-grid"}


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
            "/rest/api/3/issue/search",
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
        assert url == "https://example.atlassian.net/rest/api/3/issue/search"
        jql = kwargs["params"]["jql"]
        assert "grid-kudi" in jql
        assert "statusCategory != Done" in jql
        assert 'project = "OPS"' in jql

    @pytest.mark.asyncio
    async def test_grid_name_slugified_for_label_match(self, fake_session):
        fake_session.queue("GET", "/rest/api/3/issue/search", _FakeResponse(200, {"issues": []}))
        backend = _make_backend()

        await backend.find_open_by_grid("Kudi Grid #2 (West)")

        _method, _url, kwargs = fake_session.calls[-1]
        assert "grid-kudi-grid-2-west" in kwargs["params"]["jql"]

    @pytest.mark.asyncio
    async def test_empty_on_error_status(self, fake_session):
        fake_session.queue(
            "GET", "/rest/api/3/issue/search", _FakeResponse(500, text_data="boom")
        )
        backend = _make_backend()

        assert await backend.find_open_by_grid("Kudi") == []

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
