# Cutting Alert-Judgment Token Cost — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the LLM token cost of the `/chat/notify` `ticket_id="auto"` correlation judgment — skip the model entirely on exact-signature re-fires, make the model write less when it decides not to send, and instrument token usage so we can tell whether the system prompt is being implicitly cached.

**Architecture:** Three changes against one function, `_resolve_notify_ticket_llm_judgment` in `chat_orchestrator/orchestrator/api/app.py`, plus its schema (`alert_judgment.py`) and prompt (`ticketing.correlation.prompt`). Change 1 adds a deterministic pre-LLM short-circuit that mirrors what the legacy `AlertCorrelator.decide()` ladder already does. Change 2 reorders the required JSON output so the model commits to `send_telegram` first, then makes two free-text fields nullable. Change 3 threads `GenerateResult.usage` through `AlertJudgmentResult` and logs it.

**Tech stack:** Python 3.12 / asyncio, Pydantic v2, the `shared.llm` provider-neutral gateway (Gemini), the `shared/prompts/` library, pytest + `pytest-asyncio`.

---

## What is already true

Verified in the tree at `52b21f69` (branch `perf/alert-judgment-token-cost`, off `origin/main` `ef99914d`).

| Fact | Evidence |
|---|---|
| The judgment path's only pre-LLM exit is a `dedup_key` replay | `app.py:2552` `replayed = await correlator.replay_decision(body.dedup_key)`; nothing else returns before `judge()`. |
| The legacy ladder DOES short-circuit exact signatures before the LLM | `correlator.py:1053-1055` — `find_deterministic_decision(candidates, alert)` returns before `_call_llm`. |
| `find_deterministic_decision` returns a `confidence=1.0` `CorrelationDecision` or `None` | `correlator.py:506-594`. Rungs: exact signature+component duplicate, keyless signature-only duplicate, signature-amend onto a new component. `decision` is `"duplicate"` or `"amend"`. |
| The judgment path already reaches into correlator internals | `app.py:2589` `correlator._assemble_candidates`, `app.py:2609` `correlator._min_confidence`, `app.py:2552` `correlator.replay_decision` — calling `correlator._finalize` is consistent with the existing style. |
| `_finalize` writes the audit row and returns the decision unchanged | `correlator.py:1139-1165` — `record_event(...)`, then `return decision`. |
| `_finalize_correlation_decision` executes both `amend` and `duplicate` | `app.py:2092-2198` — `apply_amendment` (occurrence bump, in-place edit for amend, `_duplicate_delivery` for duplicate), returns the `(ref, None, extra, delivery)` 4-tuple with `extra` already carrying `decision`/`correlated_with`/`confidence`/`decided_by`. |
| Legacy path pairs the two calls exactly this way | `app.py:2511-2514` — `# amend ... or duplicate. return await _finalize_correlation_decision(...)` after `correlator.decide()` (which calls `_finalize` internally). |
| `judge()` discards `response.usage` | `correlator.py:1133-1137` returns `parse_alert_judgment(getattr(response, "text", None), ...)` — `response.usage` never read. |
| `AlertJudgmentResult` is a non-frozen `_StrictModel` (`extra="forbid"`) | `alert_judgment.py:45-46,118-123`. `model_copy(update={...})` is the safe way to add a field value. |
| `Usage` is exported from `shared.llm` | `shared/llm/__init__.py:22,40`; it is a plain frozen dataclass in `types.py:60-65` (`input_tokens`, `output_tokens`, `thinking_tokens`, `cached_tokens`), no heavy imports. |
| The gateway populates `usage.cached_tokens` from the response | `gemini.py:680-684,714-718` read `cachedContentTokenCount`. |
| `_FakeGateway` in `test_correlator.py` returns an object with only `.text` | `test_correlator.py:560-577` — a Change-3 test needs a fake that also carries `.usage`. |
| `test_notify_alert_storm.py` drives the real `_resolve_notify_ticket_full` with an `_ExplodingGateway` | `test_notify_alert_storm.py:24,73,390-393` — raises if `.generate` is ever called. Existing storm tests pin `ALERT_LLM_JUDGMENT_ENABLED=false`; a Change-1 test leaves it unset (defaults on). |
| `_FakeStore` records every `record_event` call | `test_notify_alert_storm.py:264-275` — `store.record_event_calls` list, and `bump_occurrence` / `merge_affected_key` mutate `store.rows`. |
| Editing a bundled prompt breaks a checksum test | `chat_orchestrator/tests/prompt_checksums.json:27` holds `"ticketing.correlation"`; `test_prompt_parity.py:128-137` asserts no drift. Regenerate that one key deliberately. |
| `NotificationJudgment.reason` / `LikelyUserAction.summary` are required non-empty today | `alert_judgment.py:75,100` — `Field(min_length=1, max_length=500)`. |
| Downstream reads of the fields being made nullable are already None-safe | `app.py:2710-2715` `(impact.summary or "").strip()`, `action.summary or ""`; `correlator.py:777` reads only `ticket.reason`; `alert_delivery_policy.py:280` reads only `notification.send_telegram`. |
| `_has_text(None)` is `False` | `alert_judgment.py:167-168` — `bool(value and value.strip())`. |

## File structure

| Path | Change |
|---|---|
| `chat_orchestrator/orchestrator/services/ticketing/alert_judgment.py` | `AlertJudgmentResult` gains `usage: Usage \| None`. `NotificationJudgment.reason` and `LikelyUserAction.summary` become `str \| None`. New `missing_notification_reason` guardrail in `parse_alert_judgment`. |
| `chat_orchestrator/orchestrator/services/ticketing/correlator.py` | `judge()` attaches `response.usage` to its result via `model_copy`. |
| `chat_orchestrator/orchestrator/api/app.py` | `_resolve_notify_ticket_llm_judgment`: pre-LLM `find_deterministic_decision` short-circuit after `_assemble_candidates`; a `logger.info("alert_judgment_tokens ...")` line after `judge()`. |
| `shared/prompts/library/ticketing.correlation.prompt` | Reorder the required-output JSON skeleton (`notification` first); reword the "four independent answers" sentence; add the send-false verbosity rule; cap `ticket.reason`. |
| `chat_orchestrator/tests/prompt_checksums.json` | Regenerate the `"ticketing.correlation"` value only. |
| `chat_orchestrator/tests/services/ticketing/test_alert_judgment.py` | Nullable-field accept/reject cases; `missing_notification_reason`. |
| `chat_orchestrator/tests/services/ticketing/test_correlator.py` | `judge()` populates `.usage`; `_FakeGateway` carries `.usage`. |
| `chat_orchestrator/tests/api/test_notify_alert_storm.py` | Exact re-fire on the judgment path short-circuits without touching the gateway. |

---

## Task 1: `AlertJudgmentResult.usage`, populated by `judge()`

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/alert_judgment.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlator.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_correlator.py`

- [ ] **Step 1: Give `_FakeGateway` an optional `usage`**

In `test_correlator.py`, replace the `_FakeGateway` class (lines ~560-577) with:

```python
class _FakeGateway:
    def __init__(
        self,
        text: Optional[str] = None,
        raise_exc: Optional[Exception] = None,
        delay: float = 0.0,
        usage: Optional[Any] = None,
    ):
        self.text = text
        self.raise_exc = raise_exc
        self.delay = delay
        self.usage = usage
        self.calls: List[Any] = []

    async def generate(self, messages, options, **kwargs):
        self.calls.append((messages, options))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc:
            raise self.raise_exc

        class _Result:
            text = self.text
            usage = self.usage

        return _Result()
```

- [ ] **Step 2: Write the failing test**

Append to `test_correlator.py`'s `TestLlmFirstJudgment` class:

```python
    @pytest.mark.asyncio
    async def test_judge_attaches_gateway_usage_to_the_result(self):
        from shared.llm import Usage

        usage = Usage(input_tokens=4200, output_tokens=180, thinking_tokens=900, cached_tokens=3300)
        correlator, _, _, _ = _make_correlator(
            gateway=_FakeGateway(text=_full_judgment_json(), usage=usage)
        )

        result = await correlator.judge("GridA", _mppt_alert(), _judgment_context())

        assert result.usage == usage

    @pytest.mark.asyncio
    async def test_judge_usage_is_none_when_the_call_fails(self):
        correlator, _, _, _ = _make_correlator(
            gateway=_FakeGateway(raise_exc=RuntimeError("boom"))
        )

        result = await correlator.judge("GridA", _mppt_alert(), _judgment_context())

        assert result.valid is False
        assert result.usage is None
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_correlator.py -k "judge_attaches_gateway_usage or judge_usage_is_none" -v`
Expected: FAIL — `test_judge_attaches_gateway_usage_to_the_result` errors with `AttributeError` / `ValidationError` on `usage` (field does not exist yet).

- [ ] **Step 4: Add the `usage` field to `AlertJudgmentResult`**

In `alert_judgment.py`, add the import near the top (after the existing `from shared.grid_status import SiteStatus`):

```python
from shared.llm import Usage
```

Change `AlertJudgmentResult` (currently lines ~118-123) to:

```python
class AlertJudgmentResult(_StrictModel):
    valid: bool
    judgment: AlertJudgment | None = None
    error_code: str = ""
    error_detail: str = ""
    raw: str | None = None
    usage: Usage | None = None
```

- [ ] **Step 5: Populate it in `judge()`**

In `correlator.py`, change the final `return` of `judge()` (currently lines ~1133-1137) from:

```python
        return parse_alert_judgment(
            getattr(response, "text", None),
            {ticket.ref for ticket in context.open_tickets},
            self._min_confidence,
        )
```

to:

```python
        result = parse_alert_judgment(
            getattr(response, "text", None),
            {ticket.ref for ticket in context.open_tickets},
            self._min_confidence,
        )
        usage = getattr(response, "usage", None)
        return result.model_copy(update={"usage": usage}) if usage is not None else result
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_correlator.py -k "judge_attaches_gateway_usage or judge_usage_is_none" -v`
Expected: PASS (both).

- [ ] **Step 7: Run the full correlator + judgment suites**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_correlator.py tests/services/ticketing/test_alert_judgment.py -q`
Expected: PASS (no regressions — `usage` defaults to `None`, existing constructions are unaffected).

- [ ] **Step 8: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/alert_judgment.py chat_orchestrator/orchestrator/services/ticketing/correlator.py chat_orchestrator/tests/services/ticketing/test_correlator.py
git commit -m "feat(alerts): carry LLM token usage on the alert judgment result

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: Log per-judgment token counts

**Files:**
- Modify: `chat_orchestrator/orchestrator/api/app.py` (`_resolve_notify_ticket_llm_judgment`)
- Test: `chat_orchestrator/tests/api/test_notify_ticketing.py`

- [ ] **Step 1: Write the failing test**

Append to `test_notify_ticketing.py`. The repo logs through **loguru**, not stdlib
`logging`, and there is no `caplog` shim in the suite — capture with a temporary
loguru sink. Use the file's existing `_notify_body` / `_target` helpers:

```python
@pytest.mark.asyncio
async def test_llm_judgment_logs_token_counts(monkeypatch):
    """The judgment path emits one greppable token-usage line per model call."""
    from shared.llm import Usage
    from shared.utils.logging import logger as _loguru_logger
    from orchestrator.services.ticketing.alert_judgment import AlertJudgmentResult

    monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
    monkeypatch.setenv("ALERT_LLM_JUDGMENT_ENABLED", "true")

    async def _fake_judge(self, grid_name, alert, context):
        return AlertJudgmentResult(
            valid=False,
            error_code="llm_failed",
            error_detail="Boom",
            usage=Usage(input_tokens=5000, output_tokens=120, thinking_tokens=800, cached_tokens=4096),
        )

    monkeypatch.setattr(
        "orchestrator.services.ticketing.correlator.AlertCorrelator.judge", _fake_judge
    )

    lines: list[str] = []
    sink_id = _loguru_logger.add(lambda m: lines.append(str(m)), level="INFO", format="{message}")
    try:
        body = _notify_body(ticket_id="auto", text="! Warning: MPPT A1 in GridA looks low !",
                            alert={"subject": "! Warning: MPPT A1 in GridA looks low !"})
        await _resolve_notify_ticket_full(body, _target())
    finally:
        _loguru_logger.remove(sink_id)

    line = next((m for m in lines if "alert_judgment_tokens" in m), None)
    assert line is not None
    assert "in=5000" in line and "cached=4096" in line
```

If `_notify_body` does not accept `alert=`, check its definition (near the top of
the file) and pass whatever kwarg it uses for the alert subject; the only
requirement is `ticket_id="auto"` so the judgment path runs.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd chat_orchestrator && python -m pytest tests/api/test_notify_ticketing.py -k test_llm_judgment_logs_token_counts -v`
Expected: FAIL — no `alert_judgment_tokens` line (`line is None`).

- [ ] **Step 3: Add the log line**

In `app.py`, in `_resolve_notify_ticket_llm_judgment`, immediately after:

```python
    judgment = await correlator.judge(target.grid_name, alert, context)
```

add:

```python
    _u = judgment.usage
    logger.info(
        "alert_judgment_tokens grid={} in={} out={} thinking={} cached={} valid={}",
        target.grid_name,
        _u.input_tokens if _u else 0,
        _u.output_tokens if _u else 0,
        _u.thinking_tokens if _u else 0,
        _u.cached_tokens if _u else 0,
        judgment.valid,
    )
```

- [ ] **Step 4: Run it to verify it passes**

Run: `cd chat_orchestrator && python -m pytest tests/api/test_notify_ticketing.py -k test_llm_judgment_logs_token_counts -v`
Expected: PASS.

- [ ] **Step 5: Run the notify-ticketing suite**

Run: `cd chat_orchestrator && python -m pytest tests/api/test_notify_ticketing.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/api/app.py chat_orchestrator/tests/api/test_notify_ticketing.py
git commit -m "feat(alerts): log per-judgment LLM token counts for cache verification

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: Nullable `notification.reason` and `likely_user_action.summary`

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/alert_judgment.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_alert_judgment.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_alert_judgment.py`:

```python
def test_parse_accepts_null_reason_and_action_summary_when_not_sending() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["grid_impact"] = {
        "prior_known_status": "on",
        "current_assessed_status": "on",
        "material_status_change": False,
        "summary": "recurrence, no status change",
        "confidence": 0.9,
    }
    payload["notification"] = {"send_telegram": False, "reason": None}
    payload["likely_user_action"] = {"category": "none", "summary": None, "confidence": 0.7}
    payload["ticket"] = {
        "action": "record_occurrence",
        "target_ticket_ref": "OPS-1234",
        "change_title": False,
        "proposed_title": None,
        "change_description": False,
        "description_addition": None,
        "relationship": "same_issue",
        "root_cause_kind": "other",
        "reason": "re-fire",
        "confidence": 0.9,
    }

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is True
    assert result.judgment is not None
    assert result.judgment.notification.reason is None
    assert result.judgment.likely_user_action.summary is None
    round_tripped = result.judgment.model_dump(mode="json")
    assert round_tripped["notification"]["reason"] is None


def test_parse_rejects_missing_reason_when_sending() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["notification"] = {"send_telegram": True, "reason": None}

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is False
    assert result.error_code == "missing_notification_reason"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_alert_judgment.py -k "null_reason_and_action_summary or missing_reason_when_sending" -v`
Expected: FAIL — first test hits `ValidationError` (`reason`/`summary` not nullable); second passes for the wrong reason (validation error, not `missing_notification_reason`).

- [ ] **Step 3: Make the two fields nullable**

In `alert_judgment.py`, change `NotificationJudgment` (lines ~73-75):

```python
class NotificationJudgment(_StrictModel):
    send_telegram: bool
    reason: str | None = Field(default=None, max_length=500)
```

and `LikelyUserAction.summary` (line ~100):

```python
    summary: str | None = Field(default=None, max_length=500)
```

- [ ] **Step 4: Add the `missing_notification_reason` guardrail**

In `parse_alert_judgment`, right after the `AlertJudgment.model_validate` block that sets `judgment` (immediately before the existing `impact = judgment.grid_impact` line, ~line 190), insert:

```python
    if judgment.notification.send_telegram and not _has_text(judgment.notification.reason):
        return _invalid(
            raw,
            "missing_notification_reason",
            "a sent alert must carry a notification reason",
            judgment,
        )
```

- [ ] **Step 5: Run to verify they pass**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_alert_judgment.py -k "null_reason_and_action_summary or missing_reason_when_sending" -v`
Expected: PASS (both).

- [ ] **Step 6: Run the full judgment + delivery-policy + correlator suites**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_alert_judgment.py tests/services/ticketing/test_alert_delivery_policy.py tests/services/ticketing/test_correlator.py -q`
Expected: PASS. If any existing case constructed `NotificationJudgment` / `LikelyUserAction` positionally, it still works (field order unchanged); if one asserted `reason` was required, update it to the new contract and note it in the commit body.

- [ ] **Step 7: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/alert_judgment.py chat_orchestrator/tests/services/ticketing/test_alert_judgment.py
git commit -m "feat(alerts): allow null notification.reason and likely_user_action.summary

A suppressed alert does not show these to anyone. Making them nullable lets
the prompt tell the model to skip them when send_telegram is false. A sent
alert still must carry a reason (new missing_notification_reason guardrail).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: Reorder the prompt output + add the verbosity rule

**Files:**
- Modify: `shared/prompts/library/ticketing.correlation.prompt`
- Modify: `chat_orchestrator/tests/prompt_checksums.json`

- [ ] **Step 1: Reword the "four independent answers" sentence**

In `ticketing.correlation.prompt`, change the opening paragraph (lines ~17-20) from:

```
You assess an incoming infrastructure alert against a grid's current status,
open tickets, prior delivered alerts, and its exact O&M Telegram topic. Give
four independent answers: grid impact, whether Telegram should send, the
ticket action, and the likely user action.
```

to:

```
You assess an incoming infrastructure alert against a grid's current status,
open tickets, prior delivered alerts, and its exact O&M Telegram topic. Give
four independent answers, in this order: whether Telegram should send, the
grid impact, the ticket action, and the likely user action. Decide
`send_telegram` first, then let it govern how much you write below.
```

- [ ] **Step 2: Reorder the required-output JSON skeleton**

Change the skeleton block (lines ~63-69) from:

```
Return exactly one JSON object and no markdown:
{
  "grid_impact": {"prior_known_status": "on|isolated|off|unknown", "current_assessed_status": "on|isolated|off|unknown", "material_status_change": true, "summary": "impact", "confidence": 0.0},
  "notification": {"send_telegram": true, "reason": "reason"},
  "ticket": {"action": "create_new|update_existing|record_occurrence", "target_ticket_ref": null, "change_title": false, "proposed_title": null, "change_description": false, "description_addition": null, "relationship": "same_issue|same_root_cause|new_issue", "root_cause_kind": "grid_off|grid_isolated|power_chain|component|other", "reason": "reason", "confidence": 0.0},
  "likely_user_action": {"category": "none|remote_investigation|equipment_restart|site_visit|contact_operator|monitor|other", "summary": "likely action", "confidence": 0.0}
}
```

to:

```
Return exactly one JSON object and no markdown:
{
  "notification": {"send_telegram": true, "reason": "reason or null"},
  "grid_impact": {"prior_known_status": "on|isolated|off|unknown", "current_assessed_status": "on|isolated|off|unknown", "material_status_change": true, "summary": "impact", "confidence": 0.0},
  "ticket": {"action": "create_new|update_existing|record_occurrence", "target_ticket_ref": null, "change_title": false, "proposed_title": null, "change_description": false, "description_addition": null, "relationship": "same_issue|same_root_cause|new_issue", "root_cause_kind": "grid_off|grid_isolated|power_chain|component|other", "reason": "reason", "confidence": 0.0},
  "likely_user_action": {"category": "none|remote_investigation|equipment_restart|site_visit|contact_operator|monitor|other", "summary": "likely action or null", "confidence": 0.0}
}
```

- [ ] **Step 3: Add the verbosity rule**

Immediately after the skeleton's closing `}` and before the `## Root Cause Rules` heading, insert a new paragraph:

```
**Write less when you are not sending.** When `send_telegram` is false,
nobody reads the prose, so: set `notification.reason` to null; set
`likely_user_action` to `{"category": "none", "summary": null, "confidence":
<c>}`; and keep `grid_impact.summary` to at most 12 words -- a bare audit
tag such as "recurrence, no status change" or "grid still off, day 3". When
`send_telegram` is true, write `notification.reason`, `grid_impact.summary`
and `likely_user_action.summary` in full for the operator, exactly as
described above. In all cases keep `ticket.reason` to at most 25 words; a
`record_occurrence` may be a terse phrase.
```

- [ ] **Step 4: Update the paragraph that assumes summaries are always shown**

Change the sentence in the `grid_impact.summary`/`likely_user_action.summary` paragraph (lines ~54-56) from:

```
`grid_impact.summary` and `likely_user_action.summary` are shown to operators
verbatim on every alert that sends, so write both for that reader rather than
for a log.
```

to:

```
`grid_impact.summary` and `likely_user_action.summary` are shown to operators
verbatim on every alert that sends, so write both for that reader rather than
for a log (see "Write less when you are not sending" above for the
suppressed case).
```

- [ ] **Step 5: Confirm the prompt still renders**

Run: `cd chat_orchestrator && python -m pytest tests/test_prompt_parity.py -k "renders_without_error or every_declared_variable" -v`
Expected: PASS. (`test_prompt_text_has_not_drifted` will still FAIL — that is expected and fixed in Step 6.)

- [ ] **Step 6: Regenerate the one checksum**

Run exactly this (regenerates only `ticketing.correlation`, avoiding the whole-file delete that can pull in DB/gdoc-override drift per the repo's prompt-parity notes):

```bash
cd chat_orchestrator && python -c "
import json, pathlib
from tests.test_prompt_parity import _checksum
p = pathlib.Path('tests/prompt_checksums.json')
data = json.loads(p.read_text())
data['ticketing.correlation'] = _checksum('ticketing.correlation')
p.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
print('updated', data['ticketing.correlation'])
"
```

- [ ] **Step 7: Verify drift test now passes**

Run: `cd chat_orchestrator && python -m pytest tests/test_prompt_parity.py -q`
Expected: PASS (all).

Sanity-check the render by eye:

```bash
cd chat_orchestrator && python -c "
from shared.prompts import PROMPTS
print(PROMPTS.render('ticketing.correlation').system_text)
" | sed -n '/Return exactly one JSON object/,/^}/p'
```

Expected: `notification` is the first key; `grid_impact`, `ticket`, `likely_user_action` follow.

- [ ] **Step 8: Commit**

```bash
git add shared/prompts/library/ticketing.correlation.prompt chat_orchestrator/tests/prompt_checksums.json
git commit -m "feat(alerts): correlation prompt emits send decision first, trims suppressed prose

Reorder the required JSON so the model commits to send_telegram before
writing any summary, then instruct it to null notification.reason /
likely_user_action.summary and keep grid_impact.summary to <=12 words when
not sending. Cap ticket.reason at 25 words.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: Pre-LLM exact-signature short-circuit

**Files:**
- Modify: `chat_orchestrator/orchestrator/api/app.py` (`_resolve_notify_ticket_llm_judgment`)
- Test: `chat_orchestrator/tests/api/test_notify_alert_storm.py`

- [ ] **Step 1: Write the failing test**

Append to `test_notify_alert_storm.py`. It reuses the file's existing fakes; note the new `_RecordingGateway` (a fake that returns a canned judgment and counts calls — unlike `_ExplodingGateway`, we WANT the first no-candidates alert to reach it):

```python
class _RecordingGateway:
    """Returns a fixed 'create_new' judgment and counts .generate calls."""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def generate(self, messages, options, **kwargs):
        self.calls.append((messages, options))
        payload = {
            "notification": {"send_telegram": True, "reason": "New MPPT underperformance."},
            "grid_impact": {
                "prior_known_status": "on", "current_assessed_status": "on",
                "material_status_change": False, "summary": "One MPPT underperforming.",
                "confidence": 0.9,
            },
            "ticket": {
                "action": "create_new", "target_ticket_ref": None, "change_title": False,
                "proposed_title": None, "change_description": False, "description_addition": None,
                "relationship": "new_issue", "root_cause_kind": "component",
                "reason": "No matching open ticket.", "confidence": 0.9,
            },
            "likely_user_action": {
                "category": "remote_investigation", "summary": "Check the tracker.",
                "confidence": 0.8,
            },
        }

        class _Result:
            text = json.dumps(payload)
            usage = None

        return _Result()


@pytest.mark.asyncio
async def test_exact_refire_on_the_judgment_path_skips_the_llm(monkeypatch):
    """With LLM judgment ON: the first alert (no candidates) is judged and files
    a ticket; an identical re-fire matches by signature and is decided WITHOUT
    a second model call."""
    monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
    monkeypatch.setenv("ALERT_LLM_JUDGMENT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")

    tickets: Dict[str, Dict[str, Any]] = {}
    store = _FakeStore(tickets)
    ticket_service = _FakeTicketService(tickets)
    transport = _FakeTelegramTransport()
    gateway = _RecordingGateway()

    monkeypatch.setattr(
        "orchestrator.services.ticketing.correlation_store.CorrelationStore",
        lambda get_client=None: store,
    )
    monkeypatch.setattr(
        "orchestrator.services.ticketing.service.TicketService",
        lambda get_supabase_client=None: ticket_service,
    )
    monkeypatch.setattr(
        "orchestrator.services.ticketing.delivery_repository.DeliveryRepository",
        _FakeDeliveryRepository,
    )
    monkeypatch.setattr(
        "shared.llm.get_default_generation_gateway", lambda default_model=None: gateway
    )
    monkeypatch.setattr(
        "shared.utils.telegram_send.send_telegram_message_with_fallback", transport.send
    )
    monkeypatch.setattr("shared.utils.telegram_send.edit_telegram_message", transport.edit)

    async def _noop_chat_db_log(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("orchestrator.api.app._log_notification_to_chat_db", _noop_chat_db_log)

    import orchestrator.api.app as app_module

    target = _target(_STORM_GRID)
    subject = _STORM_SUBJECTS[0]

    body1 = _body(subject, _STORM_GRID)
    ref1, err1, extra1, delivery1 = await _resolve_notify_ticket_full(body1, target)
    assert err1 is None
    await app_module._deliver_notification(body1, target, ref1, delivery1)

    body2 = _body(subject, _STORM_GRID)          # identical subject
    body2 = body2.model_copy(update={"dedup_key": "different-key"})  # not a dedup replay
    ref2, err2, extra2, delivery2 = await _resolve_notify_ticket_full(body2, target)
    assert err2 is None
    await app_module._deliver_notification(body2, target, ref2, delivery2)

    assert ref2 == ref1
    assert extra2["decision"] in ("duplicate", "amend")
    assert extra2["decided_by"] == "signature"
    assert len(gateway.calls) == 1, "the exact re-fire must not call the LLM again"
    assert store.rows[next(iter(tickets))]["occurrence_count"] == 2
```

If `NotifyRequest` has no `model_copy` (it is a Pydantic model, so it does), or `dedup_key` is positional-only, build `body2` directly with `_body` and then set a distinct `dedup_key` via `NotifyRequest(...)` kwargs mirroring `_body`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd chat_orchestrator && python -m pytest tests/api/test_notify_alert_storm.py -k test_exact_refire_on_the_judgment_path_skips_the_llm -v`
Expected: FAIL — `len(gateway.calls) == 2` (the re-fire currently runs a full second judgment).

- [ ] **Step 3: Add the short-circuit**

In `app.py`, in `_resolve_notify_ticket_llm_judgment`, find the block (currently ~lines 2588-2593):

```python
    try:
        candidates = await correlator._assemble_candidates(target.grid_name, backend_override=backend_override)
    except Exception:
        logger.opt(exception=True).warning("Notify: judgment candidate assembly failed")
        candidates = []
```

Immediately after it, insert:

```python
    deterministic = find_deterministic_decision(candidates, alert)
    if deterministic is not None:
        # An exact signature match against an open ticket is decided the same
        # way the legacy ladder and the post-LLM backstop already decide it --
        # confidence 1.0, no model call. Doing it here also skips the context
        # assembly (20 prior alerts + 50 O&M messages + telemetry) the judgment
        # would otherwise gather.
        logger.info(
            "Notify: exact-signature short-circuit for grid {!r} -- {} onto {!r}, no LLM call",
            target.grid_name,
            deterministic.decision,
            deterministic.ticket_ref,
        )
        await correlator._finalize(target.grid_name, alert, body.dedup_key, deterministic)
        return await _finalize_correlation_decision(
            body, target, alert, alert_context, store, ticket_service, deterministic
        )
```

Confirm `find_deterministic_decision` is imported in this function — it already is, in the `from orchestrator.services.ticketing.correlator import (...)` block near the top of `_resolve_notify_ticket_llm_judgment` (it lists `find_deterministic_decision` alongside `collect_deterministic_findings`, `to_legacy_correlation_decision`). If it is not, add it there.

- [ ] **Step 4: Run to verify it passes**

Run: `cd chat_orchestrator && python -m pytest tests/api/test_notify_alert_storm.py -k test_exact_refire_on_the_judgment_path_skips_the_llm -v`
Expected: PASS.

- [ ] **Step 5: Run the whole storm + notify-ticketing + correlator suites**

Run: `cd chat_orchestrator && python -m pytest tests/api/test_notify_alert_storm.py tests/api/test_notify_ticketing.py tests/services/ticketing/ -q`
Expected: PASS. The existing `test_seven_alerts...` / `test_underperforming_mppt_storm...` cases (pinned `ALERT_LLM_JUDGMENT_ENABLED=false`) are unaffected; `test_llm_outage_during_a_storm...` still exercises the no-signature-match → `judge()` → fail-open path.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/api/app.py chat_orchestrator/tests/api/test_notify_alert_storm.py
git commit -m "feat(alerts): skip the LLM judgment on an exact-signature re-fire

The judgment path only short-circuited on a dedup_key replay, so a fresh
periodic re-fire of an already-ticketed fault ran a full context assembly and
model call whose ticket verdict the deterministic backstop then usually threw
away. Run find_deterministic_decision before the model, exactly as the legacy
decide() ladder does, and record-occurrence / amend without a call.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 6: Full verification and PR

- [ ] **Step 1: Run every touched suite together**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/ tests/api/test_notify_ticketing.py tests/api/test_notify_alert_storm.py tests/test_prompt_parity.py -q`
Expected: PASS.

- [ ] **Step 2: Run `pre-commit` across the repo**

Run: `pre-commit run --all-files`
Expected: PASS. If it reports untracked test files, none should exist here (all edits are to tracked files) — if it does, `git add -f` the file after checking it for real operator identifiers, then re-run.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin perf/alert-judgment-token-cost
gh pr create --base main --title "perf(alerts): cut alert-judgment token cost" --body "$(cat <<'EOF'
## What

Three coordinated changes to the `/chat/notify` `ticket_id="auto"` correlation judgment, spec in `docs/superpowers/specs/2026-09-01-alert-judgment-token-cost-design.md`.

1. **Pre-LLM exact-signature short-circuit.** The judgment path only exited early on a `dedup_key` replay, so a fresh periodic re-fire of an already-ticketed fault ran a full context assembly (20 prior alerts + 50 O&M messages + telemetry) and a model call whose ticket verdict the deterministic backstop usually discarded. It now runs `find_deterministic_decision` before the model — the same check the legacy `decide()` ladder already uses — and records the occurrence / amends with no LLM call.
2. **Conditional output verbosity.** The required JSON output is reordered so the model emits `send_telegram` first; when it is false, `notification.reason` and `likely_user_action.summary` are `null` and `grid_impact.summary` is capped at ~12 words. Both fields are now nullable in the schema; a sent alert still must carry a reason (`missing_notification_reason` guardrail).
3. **Token instrumentation.** `judge()` now carries `GenerateResult.usage` on its result, and the judgment path logs `alert_judgment_tokens grid=… in=… out=… thinking=… cached=…` per call, so we can confirm from a week of logs whether the ~3,400-token system prompt is being implicitly cached before deciding whether explicit caching is worth building.

## Not in scope

Capping the thinking budget, trimming the judgment context window, and explicit Gemini context caching — see the spec's Non-goals.

## Testing

New coverage for the short-circuit (exact re-fire on the judgment path makes no second model call), the nullable fields, and `usage` threading. Full `tests/services/ticketing/`, notify-ticketing, storm, and prompt-parity suites pass; `pre-commit run --all-files` clean.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Report the PR URL to the user.**

---

## Self-review

**Spec coverage:**
- Change 1 (short-circuit) → Task 5. Uses `_finalize` + `_finalize_correlation_decision` exactly as the spec's revised Change 1 section specifies. ✓
- Change 2 (conditional verbosity: schema) → Task 3; (prompt reorder + rule) → Task 4. ✓
- Change 2 guardrail `missing_notification_reason` → Task 3 Step 4. ✓
- Change 3 (`usage` on result) → Task 1; (log line) → Task 2. ✓
- Non-goals (thinking cap, context trim, explicit caching) → none implemented; PR body restates them. ✓
- Spec "Testing" list: short-circuit no-call → Task 5; nullable accept/reject → Task 3; `usage` populated + None on failure → Task 1; prompt still parses → Task 4 Steps 5, 7. The spec also lists "new-component signature → amend" and "exact match + urgent severity increase → escalate" as desirable cases — Task 5's single test covers the `("duplicate", "amend")` outcome and the no-call assertion; the amend-vs-duplicate split and the severity-increase path are already covered by the existing `test_correlator.py` `find_deterministic_decision` unit tests, so they are not re-implemented here. ✓

**Placeholder scan:** No TBD/TODO. Every code step has complete code. Test bodies are literal. The one conditional ("if `NotifyRequest` has no `model_copy`") names the exact fallback. ✓

**Type consistency:** `AlertJudgmentResult.usage: Usage | None` defined in Task 1 Step 4, read in Task 2 Step 3 (`judgment.usage`) and populated in Task 1 Step 5 (`result.model_copy(update={"usage": usage})`). `find_deterministic_decision` (Task 5) returns `CorrelationDecision` with `.decision`, `.ticket_ref` — matches `correlator.py:552-594`. `_finalize(grid_name, alert, dedup_key, decision)` signature matches `correlator.py:1139`. `_finalize_correlation_decision(body, target, alert, alert_context, store, ticket_service, decision)` matches `app.py:2092`. Prompt field order in Task 4 (`notification, grid_impact, ticket, likely_user_action`) matches the reordered skeleton and the "four answers" sentence. ✓
