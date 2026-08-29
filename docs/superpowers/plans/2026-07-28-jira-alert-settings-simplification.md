# Jira Alert Settings Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** File Jira-backed alerts using only the deployment's configured Jira project and its live create metadata, while deleting the dormant n8n profile and non-operational alert settings.

**Architecture:** `JiraIssueTypeSelector` will retain each issue type's complete field contract. A single metadata-aware payload builder will derive compatible issue types and create payloads for both ordinary and `/notify` tickets. The notify path asks the LLM to choose only among compatible types; all other decisions fall back deterministically. Alert-correlation policy moves to versioned application code and keeps only the real kill switch in configuration.

**Tech Stack:** Python 3.11, aiohttp, Pydantic models, pytest/pytest-asyncio, Jira Cloud REST API v3, shared feature-flag registry.

## Global Constraints

- Retain `JIRA_PROJECT_KEY`, `NOTIFY_TICKETS_BACKEND`, and `ALERT_CORRELATION_ENABLED` as deployment choices.
- Keep `JIRA_ISSUE_TYPE` backward-compatible for non-alert Jira callers; for alerts, use it only as a compatible fallback.
- Do not retain or introduce `JIRA_ALERT_*` field, option, reporter, label, priority, project, type-selection, or cache settings.
- Do not infer arbitrary category, alarm-type, or other business-specific required field values. An incompatible Jira project must use the existing internal-ticket fail-open path instead of guessing.
- Preserve `TKT-*` handling independently of the configured Jira project and of runtime backend-toggle changes.
- The application-versioned correlation instructions are the only correlation rules source; do not fetch a correlation Google Doc or RAG context.
- Duplicate alerts remain Telegram-silent. A ticket amendment is material only when the correlation decision identifies new affected equipment or a severity increase.

---

## File Structure

- Modify: `chat_orchestrator/orchestrator/services/ticketing/jira_issue_types.py` — preserve Jira create-metadata field definitions, identify compatible types, and restrict LLM selection to an explicit candidate catalogue.
- Create: `chat_orchestrator/orchestrator/services/ticketing/jira_issue_payload.py` — build a create payload from a `TicketCreateRequest` and one Jira issue-type contract; this is the only location that maps a grid to an allowed Jira field option.
- Modify: `chat_orchestrator/orchestrator/services/ticketing/jira_backend.py` — route both ticket sources through the metadata-aware builder, select alert types from the configured project, and remove the n8n profile path and hard-coded Grid field fallback.
- Delete: `chat_orchestrator/orchestrator/services/ticketing/jira_alert_profile.py` — obsolete n8n-specific profile, custom-field option management, and its cache.
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py` — load only the bundled, versioned instruction file and define named correlation policy values.
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlator.py`, `correlation_render.py`, and `jira_backend.py` — consume the versioned policy, application primary model, severity-based material-update logic, and dynamically resolved Jira Highest priority for urgent alerts rather than removed runtime flags.
- Modify: `shared/config/flag_registry.py`, `shared/config/flags.env.example`, `.do/app.example.yaml`, and `README.md` — remove the deleted keys and document the minimal contract.
- Modify/delete tests: `chat_orchestrator/tests/services/ticketing/test_jira_backend.py`, `test_jira_alert_profile.py`, `test_jira_issue_types.py` (new), `test_correlator.py`, `test_correlation_rules.py`, `test_correlation_render.py`, and `chat_orchestrator/tests/test_flag_registry.py`.

## Task 1: Model Jira create metadata and derive compatible payloads

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/jira_issue_types.py`
- Create: `chat_orchestrator/orchestrator/services/ticketing/jira_issue_payload.py`
- Create: `chat_orchestrator/tests/services/ticketing/test_jira_issue_types.py`

**Interfaces:**
- Consumes: Jira's `GET /rest/api/3/issue/createmeta/{projectKey}/issuetypes/{issueTypeId}` response shape.
- Produces: `JiraFieldOption`, `JiraFieldDefinition`, `JiraIssueType`, `JiraCreateContext`, `build_issue_payload(context, issue_type) -> dict[str, Any] | None`, and `compatible_issue_types(context, issue_types) -> list[JiraIssueType]`.
- Produces: `JiraIssueTypeSelector.select(..., candidate_types: Sequence[JiraIssueType] | None = None) -> IssueTypeSelection | None`; a supplied catalogue is the complete set the model may choose from.

- [ ] **Step 1: Write failing metadata-normalisation tests**

```python
def test_normalize_issue_types_preserves_field_names_requirements_and_options():
    types = normalize_issue_types({"values": [{
        "id": "101", "name": "Electricity Service Disruption",
        "fields": {
            "summary": {"name": "Summary", "required": True},
            "customfield_44": {
                "name": "Grid", "required": True,
                "allowedValues": [{"id": "7", "value": "GridW"}],
            },
        },
    }]})

    grid = types[0].field("customfield_44")
    assert grid is not None
    assert grid.name == "Grid"
    assert grid.allowed_values[0] == JiraFieldOption(id="7", value="GridW")
```

- [ ] **Step 2: Run the metadata test to verify it fails**

Run: `cd chat_orchestrator && pytest tests/services/ticketing/test_jira_issue_types.py::test_normalize_issue_types_preserves_field_names_requirements_and_options -v`

Expected: FAIL because `JiraFieldOption`/`JiraIssueType.field()` do not yet exist and the current normaliser discards `name` and `allowedValues`.

- [ ] **Step 3: Write failing payload-compatibility tests**

```python
def test_build_issue_payload_adds_a_required_grid_option_from_metadata():
    context = JiraCreateContext(
        project_key="OPS", summary="MPPT Q7II low output", description="details",
        grid_name="GridW", labels=["grid-gridw"],
    )
    issue_type = _type_with_required_grid_option()

    assert build_issue_payload(context, issue_type)["fields"] == {
        "project": {"key": "OPS"},
        "summary": "MPPT Q7II low output",
        "description": _expected_adf("details"),
        "issuetype": {"id": "101"},
        "labels": ["grid-gridw"],
        "customfield_44": {"id": "7"},
    }

def test_incompatible_type_with_an_unknown_required_field_is_excluded():
    assert compatible_issue_types(_context(), [_type_with_required_category()]) == []
```

- [ ] **Step 4: Run the payload tests to verify they fail**

Run: `cd chat_orchestrator && pytest tests/services/ticketing/test_jira_issue_types.py -v`

Expected: FAIL because no payload builder or compatibility filter exists.

- [ ] **Step 5: Implement lossless create-metadata types**

In `jira_issue_types.py`, replace the tuple-only field representation with immutable field-definition dataclasses. `normalize_issue_types()` must retain each field ID, display name, `required` flag, and each allowed option's ID/value. Keep the existing list endpoint plus per-type detail fetch, but make every detailed type replace its shallow entry. Add an `IssueTypeSelection` helper that accepts a supplied `candidate_types` list so `select()` builds its catalogue only from that list.

- [ ] **Step 6: Implement the isolated payload builder**

In `jira_issue_payload.py`, create `JiraCreateContext` and `build_issue_payload()`. Always populate project key, summary, ADF description, issue-type ID, labels, and optional assignee/organisation values supplied by the caller. Find a grid field only from the selected type's metadata: prefer a field named `Grid` and accept an exact or existing fuzzy matcher match from its `allowedValues`. Add the matched option only when it is safe. Return `None` when any required field cannot be populated. Do not use `customfield_10057`, `Software`, or any project-specific option ID.

- [ ] **Step 7: Run the focused unit suite**

Run: `cd chat_orchestrator && pytest tests/services/ticketing/test_jira_issue_types.py -v`

Expected: PASS, including exact grid option mapping, optional missing grids, unknown required fields, constrained LLM catalogue, and invalid model IDs.

- [ ] **Step 8: Commit the metadata layer**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/jira_issue_types.py \
  chat_orchestrator/orchestrator/services/ticketing/jira_issue_payload.py \
  chat_orchestrator/tests/services/ticketing/test_jira_issue_types.py
git commit -m "feat: derive jira ticket fields from metadata"
```

## Task 2: Route Jira tickets through the one metadata-aware creation path

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/jira_backend.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_jira_backend.py`

**Interfaces:**
- Consumes: `JiraCreateContext`, `compatible_issue_types()`, `build_issue_payload()`, and `JiraIssueTypeSelector` from Task 1.
- Produces: `JiraTicketBackend.create_ticket(req) -> TicketResult` with a result type equal to the selected Jira type name.
- Produces: `_choose_issue_type(req, compatible_types) -> IssueTypeSelection | None`; only `req.source == "notify"` invokes the LLM.

- [ ] **Step 1: Replace the profile-path tests with a generic-project alert test**

```python
@pytest.mark.asyncio
async def test_notify_selects_a_creatable_type_from_the_generic_project(fake_session, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "fake-model")
    _queue_createmeta_with_task_and_disruption(fake_session)
    _stub_type_selector_to_choose("disruption-id")
    fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-7"}))

    result = await _make_backend().create_ticket(
        TicketCreateRequest(summary="Grid down", description="0 kW", grid_name="GridW", source="notify")
    )

    assert result.ticket_type == "Electricity Service Disruption"
    assert _posted_fields(fake_session)["issuetype"] == {"id": "disruption-id"}
    assert "customfield_10057" not in _posted_fields(fake_session)
```

- [ ] **Step 2: Run the new backend test to verify it fails**

Run: `cd chat_orchestrator && pytest tests/services/ticketing/test_jira_backend.py::TestCreateTicket::test_notify_selects_a_creatable_type_from_the_generic_project -v`

Expected: FAIL because the current notify path only invokes type selection through `JIRA_ALERT_*` profile configuration.

- [ ] **Step 3: Add fallback and shared-path failing tests**

```python
@pytest.mark.asyncio
async def test_notify_without_task_uses_first_compatible_creatable_type(fake_session):
    _queue_createmeta_with_only("Comms Failure", fake_session)
    fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "NET-3"}))

    result = await _make_backend(project_key="NET", issue_type="Task").create_ticket(_notify_request())

    assert result.ticket_type == "Comms Failure"

@pytest.mark.asyncio
async def test_escalation_uses_the_same_metadata_payload_builder(fake_session):
    _queue_createmeta_with_required_grid(fake_session)
    fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-8"}))

    await _make_backend().create_ticket(TicketCreateRequest(summary="Help", grid_name="GridW"))
    assert _posted_fields(fake_session)["customfield_grid"] == {"id": "gridw-option"}
```

- [ ] **Step 4: Run the fallback/shared-path tests to verify they fail**

Run: `cd chat_orchestrator && pytest tests/services/ticketing/test_jira_backend.py -k 'generic_project or without_task or same_metadata_payload_builder' -v`

Expected: FAIL because the legacy `_create_jira_ticket()` carries its own static Grid resolver and hard-coded `Task` retry.

- [ ] **Step 5: Implement backend type choice and unified creation**

In `jira_backend.py`:

```python
async def _choose_issue_type(
    self, req: TicketCreateRequest, compatible: Sequence[JiraIssueType]
) -> IssueTypeSelection | None:
    if req.source == "notify":
        return await self._type_selector().select(
            summary=req.summary, description=req.description,
            requested_type=req.ticket_type, operational_context=req.llm_context,
            candidate_types=compatible,
        ) or self._fallback_compatible_type(compatible)
    return self._fallback_compatible_type(compatible)
```

Build the compatibility context once, append the derived `grid-<slug>` label when a grid is known, retrieve metadata, choose the type, then post exactly the payload returned by `build_issue_payload()`. Use `get_settings().gemini.model` for the selector. `_fallback_compatible_type()` first validates the existing `JIRA_ISSUE_TYPE` name against `compatible`, then uses the first compatible type. Delete `_resolve_jira_grid_option()`, `JIRA_GRID_FALLBACK_OPTION_ID`, the hard-coded custom field, and the string-based `Task` retry.

If Jira cannot supply a compatible type, return a `TicketBackendError` that the notify service's existing Jira-to-internal fallback handles; do not post a malformed Jira request.

- [ ] **Step 6: Run Jira backend tests**

Run: `cd chat_orchestrator && pytest tests/services/ticketing/test_jira_backend.py -v`

Expected: PASS, including a non-OPS project, an alert type selected from live metadata, a project without Task, ordinary ticket creation through the same builder, and no static Grid field request.

- [ ] **Step 7: Commit the unified backend**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/jira_backend.py \
  chat_orchestrator/tests/services/ticketing/test_jira_backend.py
git commit -m "refactor: unify jira ticket creation"
```

## Task 3: Delete the dormant Jira alert profile and its configuration surface

**Files:**
- Delete: `chat_orchestrator/orchestrator/services/ticketing/jira_alert_profile.py`
- Delete: `chat_orchestrator/tests/services/ticketing/test_jira_alert_profile.py`
- Modify: `shared/config/flag_registry.py`
- Modify: `shared/config/flags.env.example`
- Modify: `.do/app.example.yaml`
- Modify: `chat_orchestrator/tests/test_flag_registry.py`

**Interfaces:**
- Consumes: the metadata-only `JiraTicketBackend` from Task 2.
- Produces: a flag registry that no longer contains any name beginning `JIRA_ALERT_`.

- [ ] **Step 1: Write failing registry/UI-surface tests**

```python
def test_registry_has_no_jira_alert_profile_flags():
    assert not [name for name in registry.FLAGS if name.startswith("JIRA_ALERT_")]

def test_generated_env_example_has_no_jira_alert_profile_block():
    rendered = registry.render_env_example()
    assert "JIRA_ALERT_" not in rendered
```

- [ ] **Step 2: Run the registry tests to verify they fail**

Run: `pytest chat_orchestrator/tests/test_flag_registry.py -k 'jira_alert_profile or jira_alert' -v`

Expected: FAIL because the registry and generated example still declare the profile block.

- [ ] **Step 3: Remove profile code, tests, and flag declarations**

Delete the profile module and its dedicated tests. Remove every `JIRA_ALERT_*` registration, comment, environment-example entry, App Platform example entry, README reference, and import. Regenerate `shared/config/flags.env.example` with `python -m shared.config.flag_registry` instead of manually editing its generated body.

- [ ] **Step 4: Run profile-removal tests and import checks**

Run: `pytest chat_orchestrator/tests/test_flag_registry.py -v && cd chat_orchestrator && pytest tests/services/ticketing/test_jira_backend.py -v`

Expected: PASS; `rg 'JIRA_ALERT_|jira_alert_profile'` returns no production or test references.

- [ ] **Step 5: Commit profile removal**

```bash
git add -A chat_orchestrator/orchestrator/services/ticketing/jira_alert_profile.py \
  chat_orchestrator/tests/services/ticketing/test_jira_alert_profile.py \
  shared/config/flag_registry.py shared/config/flags.env.example .do/app.example.yaml \
  chat_orchestrator/tests/test_flag_registry.py README.md
git commit -m "refactor: remove jira alert profile settings"
```

## Task 4: Version alert-correlation policy and apply urgent highest priority

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlator.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_render.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/jira_backend.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_correlation_rules.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_correlator.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_correlation_render.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_jira_backend.py`

**Interfaces:**
- Consumes: the bundled `alert_correlation_instructions.md` and `get_settings().gemini.model`.
- Produces: `CorrelationPolicy` with named, tested safety bounds and `get_correlation_instructions() -> dict[str, str]` with no deployment override.
- Produces: `apply_amendment(..., decision: CorrelationDecision) -> AmendmentResult`, where an escalation is caused by a severity increase, not an affected-component threshold.
- Produces: `JiraTicketBackend.resolve_priority_id("highest") -> str | None`, which discovers the standard Jira Highest priority at runtime and returns `None` without raising when it is unavailable. `update_ticket(..., priority_id="highest")` resolves that sentinel before issuing the Jira update.

- [ ] **Step 1: Replace external-rules/RAG tests with bundled-rules tests**

```python
def test_correlation_instructions_are_loaded_from_the_bundled_file(monkeypatch):
    monkeypatch.setenv("ALERT_CORRELATION_DOC_ID", "ignored-doc")
    assert get_correlation_instructions()["system_instructions"]

@pytest.mark.asyncio
async def test_correlation_never_requests_rag_context_without_a_versioned_policy_hook():
    assert await get_rag_context("MPPT Q7II") == []
```

- [ ] **Step 2: Run the rules tests to verify they fail**

Run: `cd chat_orchestrator && pytest tests/services/ticketing/test_correlation_rules.py -v`

Expected: FAIL because the current code fetches `ALERT_CORRELATION_DOC_ID` and conditionally uses `ALERT_CORRELATION_RAG_IDENTITY`.

- [ ] **Step 3: Define named application policy and remove external sources**

In `correlation_rules.py`, define a frozen `CorrelationPolicy` containing the existing safe bounds with explanatory names (confidence floor, LLM timeout, open-candidate window, and maximum candidate count). Load only the bundled instructions, retaining the minimal fallback for a packaging/read failure. Delete Google-Doc loading and RAG retrieval. In `correlator.py`, default injectable test parameters to `DEFAULT_CORRELATION_POLICY` and `get_settings().gemini.model`; remove all `ALERT_CORRELATION_*` reads except the enabled kill switch. In `jira_backend.py`, resolve the Jira priority catalogue only for the `"highest"` sentinel, select the entry named `Highest` case-insensitively, and omit the priority field if the lookup fails or that name is absent.

- [ ] **Step 4: Write and run material-update tests**

```python
@pytest.mark.asyncio
async def test_new_affected_equipment_updates_the_ticket_without_occurrence_rollup(...):
    result = await apply_amendment(..., decision=_amend_decision(severity="warning"))
    assert result.escalated is False
    assert result.affected_keys_count == 2

@pytest.mark.asyncio
async def test_first_urgent_amendment_escalates_once_without_a_priority_setting(...):
    result = await apply_amendment(..., decision=_amend_decision(severity="urgent"))
    assert result.escalated is True
    assert "🔴" in updated_summary
    assert update_call["priority_id"] == "highest"

@pytest.mark.asyncio
async def test_urgent_jira_ticket_uses_discovered_highest_priority(fake_session):
    _queue_compatible_createmeta(fake_session)
    fake_session.queue("GET", "/rest/api/3/priority", _FakeResponse(200, [
        {"id": "1", "name": "High"}, {"id": "2", "name": "Highest"},
    ]))
    fake_session.queue("POST", "/rest/api/3/issue", _FakeResponse(201, {"key": "OPS-9"}))
    await _make_backend().create_ticket(_urgent_notify_request())
    assert _posted_fields(fake_session)["priority"] == {"id": "2"}
```

Run: `cd chat_orchestrator && pytest tests/services/ticketing/test_correlator.py tests/services/ticketing/test_correlation_rules.py tests/services/ticketing/test_correlation_render.py -v`

Expected: PASS; repeated duplicates are silent, a new affected component remains a material amendment, and an urgent severity increase is escalated once with Jira's dynamically discovered Highest priority. A failed priority lookup leaves the ticket operation successful.

- [ ] **Step 5: Commit versioned correlation policy**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py \
  chat_orchestrator/orchestrator/services/ticketing/correlator.py \
  chat_orchestrator/orchestrator/services/ticketing/correlation_render.py \
  chat_orchestrator/orchestrator/services/ticketing/jira_backend.py \
  chat_orchestrator/tests/services/ticketing/test_correlation_rules.py \
  chat_orchestrator/tests/services/ticketing/test_correlator.py \
  chat_orchestrator/tests/services/ticketing/test_correlation_render.py \
  chat_orchestrator/tests/services/ticketing/test_jira_backend.py
git commit -m "refactor: version alert correlation policy"
```

## Task 5: Remove the remaining alert knobs and verify the deployment contract

**Files:**
- Modify: `shared/config/flag_registry.py`
- Modify: `shared/config/flags.env.example`
- Modify: `.do/app.example.yaml`
- Modify: `README.md`
- Modify: `chat_orchestrator/tests/test_flag_registry.py`
- Modify: `chat_orchestrator/tests/api/test_notify_ticketing.py`

**Interfaces:**
- Consumes: `DEFAULT_CORRELATION_POLICY` and the retained flag registry entries.
- Produces: the settings UI's minimal alert-related fields: `JIRA_PROJECT_KEY`, `NOTIFY_TICKETS_BACKEND`, and `ALERT_CORRELATION_ENABLED`.

- [ ] **Step 1: Add a failing retained-settings test**

```python
def test_alert_settings_expose_only_operational_deployment_choices():
    visible = {flag.name for flag in registry.FLAGS.values() if flag.show_in_settings}
    assert "ALERT_CORRELATION_ENABLED" in visible
    assert "NOTIFY_TICKETS_BACKEND" in visible
    assert {
        name for name in visible
        if name.startswith("ALERT_CORRELATION_") and name != "ALERT_CORRELATION_ENABLED"
    } == set()
    assert not {name for name in visible if name.startswith("JIRA_ALERT_")}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest chat_orchestrator/tests/test_flag_registry.py::test_alert_settings_expose_only_operational_deployment_choices -v`

Expected: FAIL because the technical correlation knobs are still registered and displayed.

- [ ] **Step 3: Remove the obsolete registry keys and regenerate documentation**

Remove `ALERT_CORRELATION_MODEL`, `ALERT_CORRELATION_DOC_ID`, `ALERT_CORRELATION_MIN_CONFIDENCE`, `ALERT_CORRELATION_TIMEOUT_SECONDS`, `ALERT_CORRELATION_LOOKBACK_HOURS`, `ALERT_CORRELATION_MAX_CANDIDATES`, `ALERT_CORRELATION_ESCALATE_AFTER`, `ALERT_CORRELATION_ROLLUP_EVERY`, and `ALERT_CORRELATION_RAG_IDENTITY`. Regenerate the env example. Remove the corresponding App Platform example entries and revise README alert setup to say: set a Jira project key, choose the backend, and leave correlation enabled unless intentionally bypassing it.

- [ ] **Step 4: Add fail-open integration coverage**

```python
@pytest.mark.asyncio
async def test_notify_creates_internal_ticket_when_project_has_no_compatible_jira_type(...):
    response = await notify_client.post("/chat/notify", json=_auto_ticket_alert())
    assert response.status_code == 202
    assert response.json()["data"]["ticket_ref"].startswith("TKT-")
```

- [ ] **Step 5: Run focused configuration and notify tests**

Run: `pytest chat_orchestrator/tests/test_flag_registry.py -v && cd chat_orchestrator && pytest tests/api/test_notify_ticketing.py tests/services/ticketing/test_service_resolve_backend.py -v`

Expected: PASS; no removed key is visible/documented, a Jira-incompatible alert becomes `TKT-*`, and explicit internal mode remains internal even with `JIRA_PROJECT_KEY` configured.

- [ ] **Step 6: Run final static and full test verification**

Run: `rg 'JIRA_ALERT_|ALERT_CORRELATION_(MODEL|DOC_ID|MIN_CONFIDENCE|TIMEOUT_SECONDS|LOOKBACK_HOURS|MAX_CANDIDATES|ESCALATE_AFTER|ROLLUP_EVERY|RAG_IDENTITY)' . -g '!docs/superpowers/specs/*' -g '!docs/superpowers/plans/*'`

Expected: no production, test, generated-example, or README references. Then run: `rg 'customfield_10057|JIRA_GRID_FALLBACK_OPTION_ID' chat_orchestrator/orchestrator/services/ticketing`.

Expected: no active ticketing-backend reference. Legacy customer-escalation and
MCP Jira integrations are outside this alert-ticketing settings cleanup; they
must not be changed merely to satisfy this scan.

Run: `cd chat_orchestrator && ruff check orchestrator tests && pytest -q`

Expected: ruff exits 0 and the complete test suite passes.

- [ ] **Step 7: Commit the configuration cleanup**

```bash
git add shared/config/flag_registry.py shared/config/flags.env.example .do/app.example.yaml \
  README.md chat_orchestrator/tests/test_flag_registry.py \
  chat_orchestrator/tests/api/test_notify_ticketing.py
git commit -m "refactor: simplify alert ticket configuration"
```
