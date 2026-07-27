"""Tests for the Jira alert-ticket profile (n8n field-parity path for /notify tickets).

``AlertTicketProfile``/``build_alert_issue_fields`` are pure (no I/O) and are
tested directly. ``resolve_or_create_grid_option`` talks to Jira over
aiohttp; it's tested against a small fake session in the same style as
``test_jira_backend.py``'s ``FakeJiraSession`` (duplicated locally rather
than imported, to keep this test file independent of that one's internals).
"""

from __future__ import annotations

import json as json_module
from typing import Any, Dict, List, Tuple

import pytest

from orchestrator.services.ticketing.jira_alert_profile import (
    AlertTicketProfile,
    build_alert_issue_fields,
    resolve_or_create_grid_option,
)


def _full_profile(**overrides: Any) -> AlertTicketProfile:
    defaults = dict(
        project_id="10000",
        project_key="ALM",
        issue_type_id="10001",
        reporter_account_id="acc-1",
        alarm_label="alarm",
        grid_field_id="customfield_10057",
        grid_context_id="ctx-1",
        grid_ignored_option_id="10315",
        alarm_type_field_id="customfield_10060",
        alarm_type_option_id="opt-alarm-type",
        category_field_id="customfield_10061",
        category_parent_option_id="opt-parent",
        category_child_option_id="opt-child",
        date_field_id="customfield_10062",
        no_grid_priority_id="prio-no-grid",
        escalated_priority_id="prio-escalated",
    )
    defaults.update(overrides)
    return AlertTicketProfile(**defaults)


class TestIsConfigured:
    def test_true_when_all_required_fields_present(self):
        assert _full_profile().is_configured() is True

    def test_true_even_when_optional_fields_blank(self):
        profile = _full_profile(grid_ignored_option_id="", escalated_priority_id="")
        assert profile.is_configured() is True

    @pytest.mark.parametrize(
        "field",
        [
            "project_id",
            "project_key",
            "issue_type_id",
            "reporter_account_id",
            "alarm_label",
            "grid_field_id",
            "grid_context_id",
            "alarm_type_field_id",
            "alarm_type_option_id",
            "category_field_id",
            "category_parent_option_id",
            "category_child_option_id",
            "date_field_id",
            "no_grid_priority_id",
        ],
    )
    def test_false_when_any_required_field_missing(self, field):
        profile = _full_profile(**{field: ""})
        assert profile.is_configured() is False

    def test_default_profile_not_configured(self):
        assert AlertTicketProfile().is_configured() is False


class TestBuildAlertIssueFields:
    def test_full_shape_with_grid(self):
        profile = _full_profile()

        body = build_alert_issue_fields(
            profile,
            summary="! Warning: MPPT A3 in Kudi seems to perform lower !",
            description="Please check VRM.",
            grid_option_id="10500",
            grid_name="Kudi",
        )

        fields = body["fields"]
        assert fields["project"] == {"id": "10000"}
        assert fields["summary"] == "! Warning: MPPT A3 in Kudi seems to perform lower !"
        assert fields["issuetype"] == {"id": "10001"}
        assert fields["reporter"] == {"id": "acc-1"}
        assert fields["description"]["content"][0]["content"][0]["text"] == "Please check VRM."
        assert fields["customfield_10060"] == {"id": "opt-alarm-type"}
        assert fields["customfield_10061"] == {
            "id": "opt-parent",
            "child": {"id": "opt-child"},
        }
        assert "customfield_10062" in fields  # date field, value asserted separately below
        assert fields["customfield_10057"] == {"id": "10500"}
        assert "priority" not in fields  # grid resolved -> no no-grid priority
        assert "alarm" in fields["labels"]
        assert "grid-kudi" in fields["labels"]

    def test_date_field_is_iso_date_today(self):
        from datetime import datetime, timezone

        profile = _full_profile()
        body = build_alert_issue_fields(
            profile, summary="s", description="d", grid_option_id="1", grid_name="Kudi"
        )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert body["fields"]["customfield_10062"] == today

    def test_no_grid_sets_no_grid_priority_and_omits_grid_field(self):
        profile = _full_profile()

        body = build_alert_issue_fields(
            profile, summary="s", description="d", grid_option_id=None, grid_name=None
        )

        fields = body["fields"]
        assert "customfield_10057" not in fields
        assert fields["priority"] == {"id": "prio-no-grid"}
        assert "grid-" not in " ".join(fields["labels"])

    def test_grid_name_without_resolved_option_still_gets_grid_label(self):
        """The grid-<slug> label is backend-independent correlation glue --
        it must be present even when the custom grid *field* couldn't be
        resolved/created (e.g. Jira API hiccup), so long as we know the name."""
        profile = _full_profile()

        body = build_alert_issue_fields(
            profile, summary="s", description="d", grid_option_id=None, grid_name="Kudi"
        )

        fields = body["fields"]
        assert "customfield_10057" not in fields
        assert "grid-kudi" in fields["labels"]

    def test_extra_labels_merged_without_duplicates(self):
        profile = _full_profile()

        body = build_alert_issue_fields(
            profile,
            summary="s",
            description="d",
            grid_option_id="1",
            grid_name="Kudi",
            extra_labels=["alarm", "custom-label"],
        )

        labels = body["fields"]["labels"]
        assert labels.count("alarm") == 1
        assert "custom-label" in labels
        assert "grid-kudi" in labels


# ---------------------------------------------------------------------------
# resolve_or_create_grid_option
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, json_data: Any = None):
        self.status = status
        self._json_data = json_data

    async def json(self) -> Any:
        return self._json_data

    async def text(self) -> str:
        return json_module.dumps(self._json_data) if self._json_data is not None else ""

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


class FakeSession:
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
        raise AssertionError(f"No queued response for {method} {url}")

    def get(self, url: str, **kwargs: Any) -> _FakeRequestCM:
        self.calls.append(("GET", url, kwargs))
        return _FakeRequestCM(self._resolve("GET", url))

    def post(self, url: str, **kwargs: Any) -> _FakeRequestCM:
        self.calls.append(("POST", url, kwargs))
        return _FakeRequestCM(self._resolve("POST", url))


def _headers() -> Dict[str, str]:
    return {"Authorization": "Basic x"}


@pytest.fixture(autouse=True)
def _reset_grid_options_cache(monkeypatch):
    """Caching is opt-in per-test (see test_caches_option_list_within_ttl) --
    every other test disables the TTL and clears the cache so results from
    one test can never leak into the next."""
    import orchestrator.services.ticketing.jira_alert_profile as profile_module

    profile_module._OPTIONS_CACHE.clear()
    monkeypatch.setattr(profile_module, "_GRID_OPTIONS_TTL", 0.0)
    yield
    profile_module._OPTIONS_CACHE.clear()


class TestResolveOrCreateGridOption:
    @pytest.mark.asyncio
    async def test_exact_match(self):
        session = FakeSession()
        session.queue(
            "GET",
            "/field/customfield_10057/context/ctx-1/option",
            _FakeResponse(200, {"values": [{"id": "500", "value": "Kudi"}]}),
        )
        profile = _full_profile()

        option_id = await resolve_or_create_grid_option(
            base_url="https://example.atlassian.net",
            headers=_headers(),
            profile=profile,
            grid_name="Kudi",
            get_session=lambda: session,
        )

        assert option_id == "500"
        assert len(session.calls) == 1  # no POST needed

    @pytest.mark.asyncio
    async def test_excludes_ignored_option_id(self):
        session = FakeSession()
        session.queue(
            "GET",
            "/field/customfield_10057/context/ctx-1/option",
            _FakeResponse(
                200,
                {
                    "values": [
                        {"id": "10315", "value": "Kudi"},  # matches value but is the ignored id
                        {"id": "600", "value": "Kudi"},
                    ]
                },
            ),
        )
        profile = _full_profile()

        option_id = await resolve_or_create_grid_option(
            base_url="https://example.atlassian.net",
            headers=_headers(),
            profile=profile,
            grid_name="Kudi",
            get_session=lambda: session,
        )

        assert option_id == "600"

    @pytest.mark.asyncio
    async def test_fuzzy_match_fallback(self):
        session = FakeSession()
        # "Kudi Gird" (transposed) vs the real "Kudi Grid" -- 88.9%
        # token_sort_ratio, above the 80% threshold but not an exact match.
        session.queue(
            "GET",
            "/field/customfield_10057/context/ctx-1/option",
            _FakeResponse(200, {"values": [{"id": "700", "value": "Kudi Gird"}]}),
        )
        profile = _full_profile()

        option_id = await resolve_or_create_grid_option(
            base_url="https://example.atlassian.net",
            headers=_headers(),
            profile=profile,
            grid_name="Kudi Grid",
            get_session=lambda: session,
        )

        assert option_id == "700"

    @pytest.mark.asyncio
    async def test_creates_missing_option(self):
        session = FakeSession()
        session.queue(
            "GET",
            "/field/customfield_10057/context/ctx-1/option",
            _FakeResponse(200, {"values": []}),
        )
        session.queue(
            "POST",
            "/field/customfield_10057/context/ctx-1/option",
            _FakeResponse(200, {"options": [{"id": "800", "value": "NewGrid"}]}),
        )
        profile = _full_profile()

        option_id = await resolve_or_create_grid_option(
            base_url="https://example.atlassian.net",
            headers=_headers(),
            profile=profile,
            grid_name="NewGrid",
            get_session=lambda: session,
        )

        assert option_id == "800"
        method, url, kwargs = session.calls[-1]
        assert method == "POST"
        assert kwargs["json"] == {"options": [{"value": "NewGrid", "disabled": False}]}

    @pytest.mark.asyncio
    async def test_none_when_context_fetch_fails(self):
        session = FakeSession()
        session.queue(
            "GET", "/field/customfield_10057/context/ctx-1/option", _FakeResponse(500, {})
        )
        profile = _full_profile()

        option_id = await resolve_or_create_grid_option(
            base_url="https://example.atlassian.net",
            headers=_headers(),
            profile=profile,
            grid_name="Kudi",
            get_session=lambda: session,
        )

        assert option_id is None

    @pytest.mark.asyncio
    async def test_none_when_creation_fails(self):
        session = FakeSession()
        session.queue(
            "GET",
            "/field/customfield_10057/context/ctx-1/option",
            _FakeResponse(200, {"values": []}),
        )
        session.queue(
            "POST", "/field/customfield_10057/context/ctx-1/option", _FakeResponse(400, {})
        )
        profile = _full_profile()

        option_id = await resolve_or_create_grid_option(
            base_url="https://example.atlassian.net",
            headers=_headers(),
            profile=profile,
            grid_name="NewGrid",
            get_session=lambda: session,
        )

        assert option_id is None

    @pytest.mark.asyncio
    async def test_none_when_grid_name_blank(self):
        session = FakeSession()
        profile = _full_profile()

        option_id = await resolve_or_create_grid_option(
            base_url="https://example.atlassian.net",
            headers=_headers(),
            profile=profile,
            grid_name="",
            get_session=lambda: session,
        )

        assert option_id is None
        assert session.calls == []

    @pytest.mark.asyncio
    async def test_caches_option_list_within_ttl(self, monkeypatch):
        import orchestrator.services.ticketing.jira_alert_profile as profile_module

        monkeypatch.setattr(profile_module, "_GRID_OPTIONS_TTL", 300.0)
        session = FakeSession()
        session.queue(
            "GET",
            "/field/customfield_10057/context/ctx-1/option",
            _FakeResponse(200, {"values": [{"id": "500", "value": "Kudi"}]}),
        )
        profile = _full_profile()

        first = await resolve_or_create_grid_option(
            base_url="https://example.atlassian.net",
            headers=_headers(),
            profile=profile,
            grid_name="Kudi",
            get_session=lambda: session,
        )
        second = await resolve_or_create_grid_option(
            base_url="https://example.atlassian.net",
            headers=_headers(),
            profile=profile,
            grid_name="Kudi",
            get_session=lambda: session,
        )

        assert first == second == "500"
        # Second call served from cache -- the queue would raise on a second GET.
        assert len(session.calls) == 1
