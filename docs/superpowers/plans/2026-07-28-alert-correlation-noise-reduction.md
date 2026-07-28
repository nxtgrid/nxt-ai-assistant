# Alert Correlation Noise Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep an alert-correlated ticket eligible during temporary Jira read failures and deliver Telegram updates only for meaningful ticket changes.

**Architecture:** `ticket_correlations` is the durable cross-backend correlation index. Candidate assembly will preserve its last-known-open rows when Jira cannot confirm a status, while an explicit done state remains authoritative. Jira's manually-filed-ticket discovery moves to the supported JQL endpoint, and duplicate delivery becomes unconditionally silent; only new or amended equipment state reaches Telegram.

**Tech Stack:** Python 3.11, FastAPI, asyncio, aiohttp, Jira Cloud REST API v3, pytest, DigitalOcean App Platform configuration.

## Global Constraints

- Keep `/chat/notify` ticket creation and Telegram base-alert delivery fail-open when ticket creation or correlation cannot complete.
- Do not add a database migration, rate limiter, digest job, or an additional LLM decision.
- A cached candidate is marked done only after `TicketStatus.is_done is True`; an unavailable status preserves cached open state.
- Jira Cloud's deprecated `GET /rest/api/3/search` is replaced with `GET /rest/api/3/search/jql` using `jql`, `fields`, and `maxResults` query parameters.
- `ALERT_CORRELATION_ROLLUP_EVERY=0` means every duplicate is silent; amendments report distinct affected components, never occurrence totals.

---

## File structure

- `chat_orchestrator/orchestrator/services/ticketing/correlator.py` — preserve stored candidates when backend status is temporarily unavailable.
- `chat_orchestrator/tests/services/ticketing/test_correlator.py` — regression coverage for status-unavailable and explicit-done candidates.
- `chat_orchestrator/orchestrator/services/ticketing/jira_backend.py` — use Jira's supported JQL search endpoint and observable bounded failure logs.
- `chat_orchestrator/tests/services/ticketing/test_jira_backend.py` — assert the current endpoint and non-success contract for both Jira searches.
- `chat_orchestrator/orchestrator/api/app.py` — keep duplicate delivery silent and make amendment text identify the new component and total distinct affected components.
- `chat_orchestrator/tests/api/test_notify_ticketing.py` — assert no duplicate roll-up and concise amend wording.
- `shared/config/flag_registry.py`, `shared/config/flags.env.example`, `README.md`, `.do/app.example.yaml` — set and document the default production-safe roll-up policy.

### Task 1: Preserve durable candidates during Jira status outages

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlator.py:537-548`
- Modify: `chat_orchestrator/tests/services/ticketing/test_correlator.py:489-529`

**Interfaces:**
- Consumes: `TicketService.get_status(ref) -> Optional[TicketStatus]` and stored candidate references from `CorrelationStore.open_candidates_for_grid`.
- Produces: `AlertCorrelator._assemble_candidates(...) -> List[CandidateSummary]` in which a stored candidate survives `get_status(...) is None`.

- [ ] **Step 1: Replace the obsolete missing-status test with a failing duplicate-preservation test**

```python
@pytest.mark.asyncio
async def test_unavailable_status_preserves_stored_exact_duplicate(self, monkeypatch):
    monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
    alert = _mppt_alert()
    correlator, store, _ts, gateway = _make_correlator()
    store.correlations.append(
        {
            "ticket_ref": "OPS-42",
            "grid_name": "Kudi",
            "status": "open",
            "signatures": [alert.signature],
            "affected_keys": [{"kind": "mppt", "key": "A3", "label": "MPPT A3"}],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    decision = await correlator.decide("Kudi", alert)

    assert decision.decision == "duplicate"
    assert decision.ticket_ref == "OPS-42"
    assert store.mark_closed_calls == []
    assert gateway.calls == []
```

- [ ] **Step 2: Run the focused test and verify it fails under current behaviour**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_correlator.py::TestLiveStatusConfirmation::test_unavailable_status_preserves_stored_exact_duplicate -q`

Expected: FAIL because `status is None` drops and marks the stored candidate closed.

- [ ] **Step 3: Change candidate confirmation to distinguish unavailable from done**

```python
for candidate in by_ref.values():
    try:
        status = await self._ticket_service.get_status(candidate.ref)
    except Exception:
        LOGGER.warning("Candidate status lookup raised for {!r}", candidate.ref, exc_info=True)
        status = None

    if status is not None and status.is_done:
        if candidate.ref in store_refs:
            await self._store.mark_closed(candidate.ref)
        continue
    if status is None and candidate.ref not in store_refs:
        continue
    if status is None:
        LOGGER.warning("Preserving cached candidate {!r}: status unavailable", candidate.ref)
    confirmed.append(candidate)
```

- [ ] **Step 4: Run the candidate tests**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_correlator.py::TestSignatureDuplicate chat_orchestrator/tests/services/ticketing/test_correlator.py::TestLiveStatusConfirmation -q`

Expected: PASS; the explicit done test still records `mark_closed("TKT-1")`, while the unavailable-status test returns a deterministic duplicate.

- [ ] **Step 5: Commit the isolated behaviour change**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/correlator.py chat_orchestrator/tests/services/ticketing/test_correlator.py
git commit -m "fix: preserve alert candidates during jira outages"
```

### Task 2: Migrate Jira JQL reads and expose failures

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/jira_backend.py:466-511, 991-1017`
- Modify: `chat_orchestrator/tests/services/ticketing/test_jira_backend.py:566-579, 652-727`

**Interfaces:**
- Consumes: Jira Cloud `GET /rest/api/3/search/jql` response `{ "issues": [...] }`.
- Produces: `JiraTicketBackend.find_open_by_grid(...) -> List[TicketSummary]` and `find_by_escalation(...) -> Optional[str]` using the supported endpoint.

- [ ] **Step 1: Change the endpoint assertions before implementation**

```python
fake_session.queue(
    "GET", "/rest/api/3/search/jql", _FakeResponse(200, {"issues": []})
)
...
assert url == "https://example.atlassian.net/rest/api/3/search/jql"
assert kwargs["params"]["maxResults"] == "20"
```

Update both `TestFindOpenByGrid` and `TestFindByEscalation` queues from `/rest/api/3/issue/search` to `/rest/api/3/search/jql`; retain their JQL assertions.

- [ ] **Step 2: Run the Jira search tests and verify they fail**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_jira_backend.py::TestFindOpenByGrid chat_orchestrator/tests/services/ticketing/test_jira_backend.py::TestFindByEscalation -q`

Expected: FAIL because production code still requests `/rest/api/3/issue/search`.

- [ ] **Step 3: Replace both legacy URLs and correct the failure log**

```python
url = f"{self._jira_base_url}/rest/api/3/search/jql"
...
body = (await resp.text())[:1000]
LOGGER.warning(
    "Jira search failed for grid {!r}: HTTP {} -- {}",
    grid_name,
    resp.status,
    body,
)
```

Use the same URL for `_search_jira_for_escalation`; it only needs the first
issue, so preserve `maxResults="1"`. Keep the existing short-circuit and
safe empty-result behaviour for HTTP errors.

- [ ] **Step 4: Add a non-success logging regression test**

```python
fake_session.queue("GET", "/rest/api/3/search/jql", _FakeResponse(410, text_data="removed"))
assert await backend.find_open_by_grid("Kudi") == []
```

Capture the logger and assert that the rendered record includes `HTTP 410`
and `removed`, not literal `%s` placeholders.

- [ ] **Step 5: Run the Jira backend suite**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_jira_backend.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the Jira compatibility change**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/jira_backend.py chat_orchestrator/tests/services/ticketing/test_jira_backend.py
git commit -m "fix: use supported jira alert search"
```

### Task 3: Silence duplicate roll-ups and improve amendment copy

**Files:**
- Modify: `chat_orchestrator/orchestrator/api/app.py:1389-1419`
- Modify: `chat_orchestrator/tests/api/test_notify_ticketing.py:1027-1082`
- Modify: `shared/config/flag_registry.py:428-433`
- Modify: `shared/config/flags.env.example:180`
- Modify: `README.md:665-668`
- Modify: `.do/app.example.yaml:414-419`

**Interfaces:**
- Consumes: `AmendmentResult.affected_keys_count`, `CorrelationDecision.affected_key`, and `ALERT_CORRELATION_ROLLUP_EVERY`.
- Produces: `NotificationDelivery(suppress=True)` for every duplicate; `NotificationDelivery.text_override` describing the newly added component for amendments.

- [ ] **Step 1: Change the delivery tests before implementation**

```python
def test_duplicate_delivery_is_silent_even_at_rollup_boundary(monkeypatch):
    monkeypatch.setenv("ALERT_CORRELATION_ROLLUP_EVERY", "10")
    delivery = _duplicate_delivery(
        AmendmentResult(..., decision="duplicate", occurrence_count=10, ...),
        NotificationTicket(ref="OPS-42", backend="jira"),
    )
    assert delivery.suppress is True

def test_amend_delivery_names_new_component_and_distinct_total():
    delivery = _amend_delivery(
        _amend_decision(affected_key={"label": "MPPT Q7II"}),
        AmendmentResult(..., affected_keys_count=2, ...),
        NotificationTicket(ref="OPS-42", backend="jira"),
    )
    assert delivery.text_override == "Added MPPT Q7II (2 affected components)"
```

- [ ] **Step 2: Run the focused delivery tests and verify the duplicate-boundary test fails**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/api/test_notify_ticketing.py -k 'duplicate_delivery or amend_delivery' -q`

Expected: FAIL because every tenth duplicate currently produces `still firing — 10 occurrences`.

- [ ] **Step 3: Make duplicates unconditionally silent and render amendments from component state**

```python
def _duplicate_delivery(amendment: Any, ticket: NotificationTicket) -> NotificationDelivery:
    return NotificationDelivery(suppress=True)

def _amend_delivery(decision: Any, amendment: Any, ticket: NotificationTicket) -> NotificationDelivery:
    label = (decision.affected_key or {}).get("label") or "another component"
    count = amendment.affected_keys_count if amendment is not None else 1
    message = f"Added {label} ({count} affected component{'s' if count != 1 else ''})"
    ...
```

Preserve the existing escalation branch so an escalation is still a fresh,
top-level Telegram post.

- [ ] **Step 4: Change the deployment-safe default and documentation**

```python
_i(
    "ALERT_CORRELATION_ROLLUP_EVERY",
    0,
    "Deprecated duplicate roll-up interval; 0 keeps every duplicate silent.",
    scope=SERVICE_BOT,
)
```

Set `ALERT_CORRELATION_ROLLUP_EVERY=0` in `flags.env.example`, document that
duplicates never send Telegram, and add the explicit `0` runtime environment
entry under `anansi-bot` in `.do/app.example.yaml`.

- [ ] **Step 5: Run focused notification/config tests**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/api/test_notify_ticketing.py chat_orchestrator/tests/test_flag_registry.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the delivery policy change**

```bash
git add chat_orchestrator/orchestrator/api/app.py chat_orchestrator/tests/api/test_notify_ticketing.py shared/config/flag_registry.py shared/config/flags.env.example README.md .do/app.example.yaml
git commit -m "fix: suppress duplicate alert notifications"
```

### Task 4: Verify the integrated fail-safe behaviour

**Files:**
- Verify: `chat_orchestrator/tests/services/ticketing/test_correlator.py`
- Verify: `chat_orchestrator/tests/services/ticketing/test_jira_backend.py`
- Verify: `chat_orchestrator/tests/api/test_notify_ticketing.py`

**Interfaces:**
- Consumes: the completed candidate, Jira, and delivery behaviours.
- Produces: verified regression coverage without altering the fail-open ticket-creation path.

- [ ] **Step 1: Run lint over changed Python files**

Run: `chat_orchestrator/.venv/bin/python -m pre_commit run ruff-check --files chat_orchestrator/orchestrator/services/ticketing/correlator.py chat_orchestrator/orchestrator/services/ticketing/jira_backend.py chat_orchestrator/orchestrator/api/app.py chat_orchestrator/tests/services/ticketing/test_correlator.py chat_orchestrator/tests/services/ticketing/test_jira_backend.py chat_orchestrator/tests/api/test_notify_ticketing.py`

Expected: PASS.

- [ ] **Step 2: Run the targeted regression suite**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_correlator.py chat_orchestrator/tests/services/ticketing/test_jira_backend.py chat_orchestrator/tests/api/test_notify_ticketing.py chat_orchestrator/tests/test_flag_registry.py -q`

Expected: PASS.

- [ ] **Step 3: Run the complete orchestrator suite**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests -q`

Expected: PASS. If the test environment blocks loopback sockets, rerun this
unchanged command with the required loopback permission and report that
environment limitation separately from test failures.

- [ ] **Step 4: Inspect the final change set and commit the plan artifact**

```bash
git diff --check
git status --short
git add docs/superpowers/plans/2026-07-28-alert-correlation-noise-reduction.md
git commit -m "docs: plan alert correlation noise reduction"
```

Expected: no whitespace errors and a clean worktree after the final commit.
