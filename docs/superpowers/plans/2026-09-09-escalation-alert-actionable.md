# Escalation Sweep Alert — Readable & Actionable — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the daily "escalations older than 24h with no ticket" admin alert stop rendering entries as bare UUIDs, and let an admin close a listed escalation in one tap.

**Architecture:** Four independent changes to existing files plus one new backfill script. (1) Reorder the sweep alert's label precedence so `org_hashtag` is used before the `id:<uuid>` fallback. (2) Attach an inline keyboard to the alert with one Close button per entry, routed through a new `ex:` callback prefix to a handler that resolves the canonical escalation row. (3) Give `verification_failed` escalations the standard action keyboard (they currently ship button-less). (4) A one-time, idempotent script that backfills `org_hashtag` on existing open unfiled rows from the session's stored org short name.

**Tech Stack:** Python 3.11, pytest (`uv run pytest` from `chat_orchestrator/`), Supabase/PostgREST, python-telegram-bot-style inline keyboards.

**Spec:** `docs/superpowers/specs/2026-09-09-escalation-sweep-alert-actionable-design.md`

---

## Test environment

All `chat_orchestrator` test commands run from `chat_orchestrator/` with CI's hermetic env (never `--env-file .env` — the local `.env` has real creds):

```bash
cd chat_orchestrator && GOOGLE_API_KEY=test-key CHAT_DB_URL=https://placeholder.supabase.co \
  CHAT_DB_SERVICE_KEY=placeholder MODEL_THINKING=gemini-pro-latest MODEL_FAST=gemini-flash-latest \
  MODEL_LITE=gemini-2.5-flash-lite FALLBACK_MODEL=gemini-2.5-flash-lite \
  uv run --no-env-file pytest <args>
```

Shorthand below: `PYTEST <args>` means the full prefixed command above.

`shared/tests` run the same way but `uv run --no-env-file pytest ../shared/tests/<file>`.

---

## Task 1: Keyboard builder — optional "Close & inform customer" button

**Files:**
- Modify: `shared/utils/telegram_buttons.py` — `build_escalation_track_keyboard` (~line 505-544)
- Test: `shared/tests/test_telegram_buttons.py` (new file)

- [ ] **Step 1: Write the failing test**

Create `shared/tests/test_telegram_buttons.py`:

```python
"""Tests for shared/utils/telegram_buttons.py keyboard builders."""

from __future__ import annotations

from shared.utils.telegram_buttons import (
    ESCALATION_ALERT_DISMISS_PREFIX,
    ESCALATION_CLOSE_NOTIFY_PREFIX,
    ESCALATION_CLOSE_SILENT_PREFIX,
    ESCALATION_TRACK_CALLBACK_PREFIX,
    build_escalation_track_keyboard,
    build_stale_alert_keyboard,
    parse_callback_data,
)

MID = "00000000-0000-0000-0000-000000000001"


def _texts(kb):
    return [btn["text"] for row in kb["inline_keyboard"] for btn in row]


def _datas(kb):
    return [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]


def test_track_keyboard_default_has_all_three_actions():
    kb = build_escalation_track_keyboard(MID)
    datas = _datas(kb)
    assert f"{ESCALATION_TRACK_CALLBACK_PREFIX}:{MID}" in datas
    assert f"{ESCALATION_CLOSE_SILENT_PREFIX}:{MID}" in datas
    assert f"{ESCALATION_CLOSE_NOTIFY_PREFIX}:{MID}" in datas


def test_track_keyboard_can_omit_close_and_inform():
    kb = build_escalation_track_keyboard(MID, include_close_notify=False)
    datas = _datas(kb)
    assert f"{ESCALATION_CLOSE_SILENT_PREFIX}:{MID}" in datas
    assert f"{ESCALATION_CLOSE_NOTIFY_PREFIX}:{MID}" not in datas
    # Track button unaffected
    assert f"{ESCALATION_TRACK_CALLBACK_PREFIX}:{MID}" in datas
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-env-file pytest ../shared/tests/test_telegram_buttons.py -q` (from `chat_orchestrator/`)
Expected: FAIL — `ImportError` (`build_stale_alert_keyboard` / `ESCALATION_ALERT_DISMISS_PREFIX` don't exist yet), or once those land, `test_track_keyboard_can_omit_close_and_inform` fails on unexpected `include_close_notify` kwarg. (Tasks 1–2 share this file; expect red until both are done.)

- [ ] **Step 3: Add the `include_close_notify` parameter**

In `shared/utils/telegram_buttons.py`, replace the `build_escalation_track_keyboard` signature and its close-row block:

```python
def build_escalation_track_keyboard(
    mapping_id: str,
    include_track: bool = True,
    include_close_notify: bool = True,
) -> Dict[str, Any]:
    """Build inline keyboard with escalation action buttons.

    Actions:
    - Track as ticket & close (when include_track=True)
    - Close silently
    - Close & inform customer (when include_close_notify=True)

    include_track=False: after-hours auto-Jira, the Track row is omitted.
    include_close_notify=False: verification-failure escalations, where a
    customer-facing "resolved" note would be wrong (internal AI-quality flag).

    Args:
        mapping_id: Pre-generated UUID for the escalation mapping
        include_track: Include the "Track as ticket" button (default True)
        include_close_notify: Include "Close & inform customer" (default True)

    Returns:
        InlineKeyboardMarkup dict
    """
    rows = []
    if include_track:
        rows.append(
            [
                {
                    "text": "\U0001f4cb Track as ticket & close",
                    "callback_data": f"{ESCALATION_TRACK_CALLBACK_PREFIX}:{mapping_id}",
                }
            ]
        )
    close_row = [
        {
            "text": "\U0001f507 Close silently",
            "callback_data": f"{ESCALATION_CLOSE_SILENT_PREFIX}:{mapping_id}",
        }
    ]
    if include_close_notify:
        close_row.append(
            {
                "text": "✅ Close & inform customer",
                "callback_data": f"{ESCALATION_CLOSE_NOTIFY_PREFIX}:{mapping_id}",
            }
        )
    rows.append(close_row)
    return {"inline_keyboard": rows}
```

- [ ] **Step 4: Run the two track-keyboard tests**

Run: `uv run --no-env-file pytest ../shared/tests/test_telegram_buttons.py -q -k track_keyboard`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add -f shared/tests/test_telegram_buttons.py
git add shared/utils/telegram_buttons.py
git commit -m "feat(telegram): optional Close-&-inform button in the escalation keyboard

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: New `ex:` dismiss prefix + `build_stale_alert_keyboard`

**Files:**
- Modify: `shared/utils/telegram_buttons.py` — add prefix constant (~line 79), extend `parse_callback_data` escalation branch (~line 218-227), add `build_stale_alert_keyboard` after `build_escalation_track_keyboard`, add both names to `__all__` (~line 685)
- Test: `shared/tests/test_telegram_buttons.py` (from Task 1)

- [ ] **Step 1: Write the failing tests** — append to `shared/tests/test_telegram_buttons.py`:

```python
def test_stale_alert_keyboard_one_close_button_per_entry():
    kb = build_stale_alert_keyboard(
        [("11111111-1111-1111-1111-111111111111", "#ExampleOrg"),
         ("22222222-2222-2222-2222-222222222222", "Jane Doe")]
    )
    rows = kb["inline_keyboard"]
    assert len(rows) == 2
    assert rows[0][0]["callback_data"] == f"{ESCALATION_ALERT_DISMISS_PREFIX}:11111111-1111-1111-1111-111111111111"
    assert rows[1][0]["callback_data"] == f"{ESCALATION_ALERT_DISMISS_PREFIX}:22222222-2222-2222-2222-222222222222"
    assert "#ExampleOrg" in rows[0][0]["text"]
    assert rows[0][0]["text"].startswith("✅")


def test_stale_alert_keyboard_truncates_long_labels_within_callback_budget():
    long_label = "x" * 200
    kb = build_stale_alert_keyboard([("33333333-3333-3333-3333-333333333333", long_label)])
    btn = kb["inline_keyboard"][0][0]
    assert len(btn["callback_data"].encode("utf-8")) <= 64
    assert len(btn["text"]) <= 64


def test_stale_alert_keyboard_empty_entries_yields_no_keyboard():
    assert build_stale_alert_keyboard([]) is None


def test_parse_callback_data_recognises_the_dismiss_prefix():
    parsed = parse_callback_data(f"{ESCALATION_ALERT_DISMISS_PREFIX}:44444444-4444-4444-4444-444444444444")
    assert parsed == {"type": ESCALATION_ALERT_DISMISS_PREFIX, "mapping_id": "44444444-4444-4444-4444-444444444444"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --no-env-file pytest ../shared/tests/test_telegram_buttons.py -q`
Expected: FAIL — `ImportError` on `build_stale_alert_keyboard` / `ESCALATION_ALERT_DISMISS_PREFIX`.

- [ ] **Step 3: Add the constant**

In `shared/utils/telegram_buttons.py`, after the `ESCALATION_OFFER_PREFIX` line (~line 82):

```python
# Callback data prefix for one-tap dismiss from the stale-escalation sweep
# alert (ex:{mapping_uuid}) -- resolves the escalation, same as Close silently.
ESCALATION_ALERT_DISMISS_PREFIX = "ex"
```

- [ ] **Step 4: Extend `parse_callback_data`**

In the escalation branch of `parse_callback_data` (~line 218), add the new prefix to the tuple:

```python
    # Handle escalation callbacks (es/ec/en/ex:mapping_uuid)
    if callback_type in (
        ESCALATION_TRACK_CALLBACK_PREFIX,
        ESCALATION_CLOSE_SILENT_PREFIX,
        ESCALATION_CLOSE_NOTIFY_PREFIX,
        ESCALATION_ALERT_DISMISS_PREFIX,
    ):
        return {
            "type": callback_type,
            "mapping_id": parts[1],
        }
```

- [ ] **Step 5: Add `build_stale_alert_keyboard`** — immediately after `build_escalation_track_keyboard`:

```python
def build_stale_alert_keyboard(
    entries: List[tuple],
) -> Optional[Dict[str, Any]]:
    """Inline keyboard for the daily stale-escalation sweep alert: one
    "Close" button per listed (escalation_id, label) entry. Tapping it routes
    to ESCALATION_ALERT_DISMISS_PREFIX and fully resolves that escalation.

    Args:
        entries: list of (escalation_id, label) for the linkable alert rows

    Returns:
        InlineKeyboardMarkup dict, or None when entries is empty.
    """
    if not entries:
        return None
    rows = []
    for esc_id, label in entries:
        text = f"✅ Close — {label}"
        if len(text) > 60:
            text = text[:59] + "…"
        rows.append(
            [{"text": text, "callback_data": f"{ESCALATION_ALERT_DISMISS_PREFIX}:{esc_id}"}]
        )
    return {"inline_keyboard": rows}
```

- [ ] **Step 6: Export both names**

In the `__all__` list (~line 685), add `"ESCALATION_ALERT_DISMISS_PREFIX"` and `"build_stale_alert_keyboard"` (keep alphabetical-ish grouping with the other escalation entries).

- [ ] **Step 7: Run the full button test module**

Run: `uv run --no-env-file pytest ../shared/tests/test_telegram_buttons.py -q`
Expected: all passed (6 tests).

- [ ] **Step 8: Commit**

```bash
git add shared/utils/telegram_buttons.py shared/tests/test_telegram_buttons.py
git commit -m "feat(telegram): ex: dismiss prefix + stale-alert keyboard builder

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: Alert label precedence — org before the id: fallback

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/escalation_service.py` — the per-entry loop in `run_escalation_ticket_sweep` (~line 2393-2411)
- Test: `chat_orchestrator/tests/services/test_escalation_service_ticketing.py`

- [ ] **Step 1: Write the failing tests** — append near the other sweep-alert tests (after `test_sweep_old_escalations_alert_labels_entry_with_name_and_org_not_bare_uuid`):

```python
async def test_sweep_alert_uses_org_as_label_when_name_and_email_absent(monkeypatch):
    """Org-only row (name + email both null, org_hashtag backfilled): the
    bullet reads '#Org', not 'id:<uuid> (#Org)'."""
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    row = _canonical_escalation_row("esc-old", age_hours=30)
    row["customer_username"] = None
    row["customer_email"] = None
    row["org_hashtag"] = "#ExampleOrg"
    raw.table("escalations").rows = [row]
    raw.table("message_deliveries").rows = [
        {"escalation_id": "esc-old", "purpose": "escalation", "external_message_id": 777}
    ]
    supa = _FakeSupabase(raw)
    _wire_canonical_session(supa)
    svc = _make_service(supa)
    calls = _wire_sweep_telegram(svc)

    await svc.run_escalation_ticket_sweep()

    text = calls["messages"][-1]["text"]
    assert "• #ExampleOrg — [View](https://t.me/c/123456/777)" in text
    assert "id:esc-old" not in text
    assert "(#ExampleOrg)" not in text  # not duplicated as a suffix


async def test_sweep_alert_still_shows_bare_id_when_nothing_is_known(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    row = _canonical_escalation_row("esc-blank", age_hours=30)
    row["customer_username"] = None
    row["customer_email"] = None
    row["org_hashtag"] = None
    raw.table("escalations").rows = [row]
    raw.table("message_deliveries").rows = [
        {"escalation_id": "esc-blank", "purpose": "escalation", "external_message_id": 888}
    ]
    supa = _FakeSupabase(raw)
    _wire_canonical_session(supa)
    svc = _make_service(supa)
    calls = _wire_sweep_telegram(svc)

    await svc.run_escalation_ticket_sweep()

    assert "id:esc-blank" in calls["messages"][-1]["text"]


async def test_sweep_alert_name_and_org_row_is_unchanged(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    row = _canonical_escalation_row("esc-full", age_hours=30)
    row["customer_username"] = "Jane Doe"
    row["org_hashtag"] = "#ExampleOrg"
    raw.table("escalations").rows = [row]
    raw.table("message_deliveries").rows = [
        {"escalation_id": "esc-full", "purpose": "escalation", "external_message_id": 999}
    ]
    supa = _FakeSupabase(raw)
    _wire_canonical_session(supa)
    svc = _make_service(supa)
    calls = _wire_sweep_telegram(svc)

    await svc.run_escalation_ticket_sweep()

    assert "Jane Doe (#ExampleOrg)" in calls["messages"][-1]["text"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTEST tests/services/test_escalation_service_ticketing.py -q -k "org_as_label or bare_id_when_nothing or name_and_org_row_is_unchanged"`
Expected: `test_sweep_alert_uses_org_as_label_when_name_and_email_absent` FAILS (bullet is `id:esc-old (#ExampleOrg)`); the other two PASS already (characterization).

- [ ] **Step 3: Reorder the label construction**

In `run_escalation_ticket_sweep`, replace lines ~2393-2399 (the `for esc in old_escalations:` head through `org_part = ...`):

```python
            for esc in old_escalations:
                username = esc.get("customer_username")
                email = esc.get("customer_email") or ""
                masked_email = (email[:2] + "***@" + email.split("@", 1)[1]) if "@" in email else ""
                org_raw = esc.get("org_hashtag") or ""
                # Precedence: real name -> masked email -> org hashtag
                # (backfilled onto rows created before PR #192) -> and only
                # then the bare canonical id, when nothing human-meaningful
                # is known at all.
                primary = username or masked_email or org_raw or f"id:{esc['id']}"
                label = _escape_telegram_markdown(primary)
                # Don't repeat the org as a "(...)" suffix when it's already
                # standing in as the label.
                org_part = (
                    f" ({_escape_telegram_markdown(org_raw)})"
                    if org_raw and org_raw != primary
                    else ""
                )
```

Leave the `msg_id = esc.get("escalation_message_id")` line and everything below it in the loop unchanged.

- [ ] **Step 4: Run the three tests**

Run: `PYTEST tests/services/test_escalation_service_ticketing.py -q -k "org_as_label or bare_id_when_nothing or name_and_org_row_is_unchanged"`
Expected: 3 passed.

- [ ] **Step 5: Run the whole sweep-alert test group for regressions**

Run: `PYTEST tests/services/test_escalation_service_ticketing.py -q -k sweep`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add -f chat_orchestrator/tests/services/test_escalation_service_ticketing.py
git add chat_orchestrator/orchestrator/services/escalation_service.py
git commit -m "fix(escalation): use org hashtag as the sweep-alert label before id: fallback

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: Attach the dismiss keyboard to the sweep alert

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/escalation_service.py` — imports (~line 33 area) and `run_escalation_ticket_sweep` (~line 2383-2441)
- Test: `chat_orchestrator/tests/services/test_escalation_service_ticketing.py`

- [ ] **Step 1: Write the failing test** — append after Task 3's tests:

```python
async def test_sweep_alert_attaches_one_close_button_per_linkable_entry(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    r1 = _canonical_escalation_row("esc-a", age_hours=30)
    r1["customer_username"] = "Jane Doe"
    r1["org_hashtag"] = "#ExampleOrg"
    r2 = _canonical_escalation_row("esc-b", age_hours=40)
    r2["customer_username"] = None
    r2["customer_email"] = None
    r2["org_hashtag"] = "#OtherOrg"
    raw.table("escalations").rows = [r1, r2]
    raw.table("message_deliveries").rows = [
        {"escalation_id": "esc-a", "purpose": "escalation", "external_message_id": 111},
        {"escalation_id": "esc-b", "purpose": "escalation", "external_message_id": 222},
    ]
    supa = _FakeSupabase(raw)
    _wire_canonical_session(supa)
    svc = _make_service(supa)
    calls = _wire_sweep_telegram(svc)

    await svc.run_escalation_ticket_sweep()

    markup = calls["messages"][-1]["reply_markup"]
    datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert datas == ["ex:esc-a", "ex:esc-b"]
```

Prereq: `_wire_sweep_telegram`'s `fake_send` currently records only `{"chat_id": chat_id, "text": text}`. Extend that one line to `{"chat_id": chat_id, "text": text, "reply_markup": reply_markup}` (its signature already has the `reply_markup=None` param). Do this in Step 1.

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST tests/services/test_escalation_service_ticketing.py -q -k attaches_one_close_button`
Expected: FAIL — `KeyError: 'reply_markup'` (no markup passed) or `None`.

- [ ] **Step 3: Import the builder**

At the top of `escalation_service.py`, in the existing `from shared.utils.telegram_buttons import ...` group (the module already imports `build_escalation_track_keyboard`), add `build_stale_alert_keyboard`.

- [ ] **Step 4: Collect (id, label) for linkable rows and pass the keyboard**

In `run_escalation_ticket_sweep`, add a collector alongside `linkable_lines` (~line 2383):

```python
            linkable_lines: List[str] = []
            linkable_dismiss: List[tuple] = []  # (esc_id, label) for the keyboard
```

In the loop, in the `if msg_id and isinstance(msg_id, int) and channel_id:` branch, after appending to `linkable_lines`:

```python
                    linkable_lines.append(f"• {label}{org_part} — [View]({link})")
                    linkable_dismiss.append((esc["id"], primary))
```

Replace the final send (~line 2438-2441):

```python
            await self._send_telegram_message(
                chat_id=self._escalation_chat_id,
                text="\n".join(lines),
                reply_markup=build_stale_alert_keyboard(linkable_dismiss),
            )
```

(`build_stale_alert_keyboard([])` returns `None`, so the all-unlinkable branch sends no keyboard — correct.)

- [ ] **Step 5: Run the test + the sweep group**

Run: `PYTEST tests/services/test_escalation_service_ticketing.py -q -k "attaches_one_close_button or sweep"`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/services/escalation_service.py chat_orchestrator/tests/services/test_escalation_service_ticketing.py
git commit -m "feat(escalation): one Close button per entry on the stale-escalation alert

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: Dismiss callback handler

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/callback_handlers.py` — import (~line 28-31), dispatch (~after line 169), new handler function (near `_handle_escalation_close_callback`)
- Test: `chat_orchestrator/tests/services/test_callback_handlers_ticketing.py`

- [ ] **Step 1: Write the failing tests** — append to `test_callback_handlers_ticketing.py`:

```python
async def test_stale_alert_dismiss_resolves_the_canonical_escalation(monkeypatch):
    mapping_id = "00000000-0000-0000-0000-0000000000aa"
    supa = _FakeSupabase(claim_row=None, escalations_state={mapping_id: {"state": "open"}})
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)
    monkeypatch.setenv("ESCALATION_TELEGRAM_CHAT_ID", "-100999")
    answered = []
    monkeypatch.setattr(ch, "_answer_callback_query", AsyncMock(side_effect=lambda *a, **k: answered.append((a, k))))

    result = await ch._handle_stale_alert_dismiss_callback(
        callback_id="cb1", mapping_id=mapping_id, chat_id="-100999",
    )

    assert result["statusCode"] == 200
    resolve_calls = [
        c for c in supa.canonical_calls
        if c[0] == "escalations" and c[1].get("state") == "resolved"
    ]
    assert len(resolve_calls) == 1
    assert resolve_calls[0][2] == [("id", mapping_id)]
    assert "Closed" in answered[-1][0][1]


async def test_stale_alert_dismiss_rejects_other_chats(monkeypatch):
    monkeypatch.setenv("ESCALATION_TELEGRAM_CHAT_ID", "-100999")
    supa = _FakeSupabase(claim_row=None)
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)
    monkeypatch.setattr(ch, "_answer_callback_query", AsyncMock())

    result = await ch._handle_stale_alert_dismiss_callback(
        callback_id="cb1", mapping_id="00000000-0000-0000-0000-0000000000bb", chat_id="-100555",
    )

    assert result["statusCode"] == 403
    assert supa.canonical_calls == []


async def test_stale_alert_dismiss_rejects_bad_uuid(monkeypatch):
    monkeypatch.setenv("ESCALATION_TELEGRAM_CHAT_ID", "-100999")
    supa = _FakeSupabase(claim_row=None)
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)
    monkeypatch.setattr(ch, "_answer_callback_query", AsyncMock())

    result = await ch._handle_stale_alert_dismiss_callback(
        callback_id="cb1", mapping_id="not-a-uuid", chat_id="-100999",
    )

    assert result["statusCode"] == 400
    assert supa.canonical_calls == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTEST tests/services/test_callback_handlers_ticketing.py -q -k stale_alert_dismiss`
Expected: FAIL — `AttributeError: module ... has no attribute '_handle_stale_alert_dismiss_callback'`.

- [ ] **Step 3: Import the prefix**

In `callback_handlers.py`, add `ESCALATION_ALERT_DISMISS_PREFIX` to the `from shared.utils.telegram_buttons import (...)` block that already imports `ESCALATION_CLOSE_NOTIFY_PREFIX` etc.

- [ ] **Step 4: Add the dispatch branch**

After the close-callback `if callback_type in (ESCALATION_CLOSE_SILENT_PREFIX, ESCALATION_CLOSE_NOTIFY_PREFIX):` block (~line 169), before the offer block:

```python
        # =================================================================
        # STALE-ALERT DISMISS (ex:mapping_id) - one-tap close from the daily
        # "older than 24h with no ticket" sweep alert
        # =================================================================
        if callback_type == ESCALATION_ALERT_DISMISS_PREFIX:
            return await _handle_stale_alert_dismiss_callback(
                callback_id=callback_id,
                mapping_id=parsed["mapping_id"],
                chat_id=chat_id,
            )
```

- [ ] **Step 5: Add the handler** — next to `_handle_escalation_close_callback`:

```python
async def _handle_stale_alert_dismiss_callback(
    callback_id: str,
    mapping_id: str,
    chat_id: str,
) -> Dict[str, Any]:
    """Handle a one-tap 'Close' from the stale-escalation sweep alert
    (ex:mapping_id). Fully resolves the canonical escalation row so it drops
    out of the next daily alert; reversible with EscalationRepository.reopen().
    Toast-only feedback -- the alert is regenerated fresh each day, so there
    is nothing to edit in place.
    """
    escalation_group_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
    if chat_id != escalation_group_id:
        await _answer_callback_query(callback_id, "Unauthorized", show_alert=True)
        return {"success": True, "message": "Unauthorized", "statusCode": 403}

    try:
        uuid_mod.UUID(mapping_id)
    except ValueError:
        await _answer_callback_query(callback_id, "Invalid escalation ID", show_alert=True)
        return {"success": True, "message": "Invalid mapping_id", "statusCode": 400}

    supabase_client = get_supabase_client()
    try:
        await _canonical_escalations(supabase_client).resolve(mapping_id)
    except Exception:
        LOGGER.opt(exception=True).warning(
            "Stale-alert dismiss: could not resolve canonical escalation {}", mapping_id
        )
        await _answer_callback_query(callback_id, "Could not close — try again", show_alert=True)
        return {"success": False, "message": "resolve failed", "statusCode": 500}

    await _answer_callback_query(callback_id, "✓ Closed — gone from tomorrow's alert.")
    return {"success": True, "message": "dismissed", "statusCode": 200}
```

- [ ] **Step 6: Run the tests**

Run: `PYTEST tests/services/test_callback_handlers_ticketing.py -q -k stale_alert_dismiss`
Expected: 3 passed.

- [ ] **Step 7: Run the whole callback-handlers ticketing module**

Run: `PYTEST tests/services/test_callback_handlers_ticketing.py -q`
Expected: all passed.

- [ ] **Step 8: Commit**

```bash
git add chat_orchestrator/orchestrator/services/callback_handlers.py chat_orchestrator/tests/services/test_callback_handlers_ticketing.py
git commit -m "feat(escalation): handler for one-tap dismiss from the stale-escalation alert

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 6: Verification-failure escalations carry the action keyboard

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/escalation_service.py` — `escalate_verification_failure` (~line 3296-3336)
- Test: `chat_orchestrator/tests/services/test_escalation_service_ticketing.py`

- [ ] **Step 1: Write the failing test** — append near `test_verification_failure_escalation_persists_identity_on_the_canonical_row`:

```python
async def test_verification_failure_escalation_message_carries_action_buttons():
    """Regression: escalate_verification_failure sent a button-less message,
    leaving no admin path to close the escalation. It must now ship the
    standard Track + Close-silently keyboard (no 'Close & inform customer'),
    wired to the same id the canonical row is written under."""
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)

    async def fake_get_session(_sid):
        return SimpleNamespace(id=uuid.uuid4())

    supa.get_session = fake_get_session
    svc = _make_service(supa)

    sent: list = []

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        sent.append({"text": text, "reply_markup": reply_markup})
        return {"ok": True, "result": {"message_id": 77}}

    svc._send_telegram_message = fake_send

    await svc.escalate_verification_failure(
        original_message="why is my meter reading zero",
        failed_response="bad answer",
        verification_feedback="not grounded",
        session_id="telegram_abc",
        customer_chat_id="123",
        customer_username="Jane Doe",
        organization_short_name="Acme Energy",
    )

    markup = sent[-1]["reply_markup"]
    assert markup is not None
    datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    row_id = raw.tables["escalations"].rows[0]["id"]
    assert f"es:{row_id}" in datas        # Track
    assert f"ec:{row_id}" in datas        # Close silently
    assert all(not d.startswith("en:") for d in datas)  # no Close & inform
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST tests/services/test_escalation_service_ticketing.py -q -k message_carries_action_buttons`
Expected: FAIL — `assert markup is not None` (currently sent with no `reply_markup`).

- [ ] **Step 3: Pre-generate the id, build the keyboard, pass it, reuse the id**

In `escalate_verification_failure`, after the forum-topic resolution block (~line 3302) and before the `LOGGER.info("Sending verification failure ...")`:

```python
            # Pre-generate the escalation id so the message can carry action
            # buttons wired to it. Verification-failure escalations were
            # button-less -- the only close path was the DB. No "Close & inform
            # customer": this is an internal AI-quality flag, not something to
            # send the customer a resolution note about.
            verification_id = str(uuid.uuid4())
            track_keyboard = build_escalation_track_keyboard(
                verification_id, include_track=True, include_close_notify=False
            )
```

Change the send call to pass `reply_markup=track_keyboard`:

```python
            result = await self._send_telegram_message(
                chat_id=self._escalation_chat_id,
                text=message_text,
                topic_id=escalation_topic_id,
                reply_markup=track_keyboard,
            )
```

In the `if result.get("ok"):` block, delete the `saved_verification_id = str(uuid.uuid4())` line and its `if saved_verification_id:` guard, calling `_record_canonical_escalation` with `verification_id` directly:

```python
            if result.get("ok"):
                escalation_message_id = result.get("result", {}).get("message_id")

                if escalation_message_id and customer_chat_id:
                    supabase_client = self._get_supabase_client()
                    if supabase_client:
                        await self._record_canonical_escalation(
                            verification_id,
                            session_id,
                            message_id=escalation_message_id,
                            topic_id=escalation_topic_id,
                            reason="verification_failed",
                            customer_username=customer_username,
                            org_hashtag=_org_hashtag_from_short_name(organization_short_name),
                        )
                        LOGGER.info(
                            f"Saved verification failure escalation to database: "
                            f"msg_id={escalation_message_id} → session={session_id}, "
                            f"reason=verification_failed"
                        )
```

- [ ] **Step 4: Run the test + the verification-failure group**

Run: `PYTEST tests/services/test_escalation_service_ticketing.py -q -k verification_failure`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/escalation_service.py chat_orchestrator/tests/services/test_escalation_service_ticketing.py
git commit -m "fix(escalation): verification-failure escalations carry Track/Close buttons

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 7: Backfill script for `org_hashtag` on existing open unfiled rows

**Files:**
- Create: `scripts/backfill_escalation_org_hashtag.py`
- Test: `shared/tests/test_backfill_escalation_org_hashtag.py`

- [ ] **Step 1: Write the failing tests** — create `shared/tests/test_backfill_escalation_org_hashtag.py`:

```python
"""Tests for scripts/backfill_escalation_org_hashtag.py -- a one-time
backfill of escalations.org_hashtag on rows created before PR #192, so the
daily sweep alert can label them with an org instead of a bare UUID.

Async tests carry an explicit @pytest.mark.asyncio (see the sibling
test_resolve_stale_swept_escalations.py docstring for why).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from scripts import backfill_escalation_org_hashtag as backfill


class _Query:
    def __init__(self, client: "_Client", table: str):
        self.c = client
        self.table = table
        self.mode = "select"
        self.payload: Dict[str, Any] | None = None
        self.filters: List[tuple] = []

    def select(self, *_a):
        return self

    def update(self, payload):
        self.mode = "update"
        self.payload = payload
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self.filters.append(("is", col, val))
        return self

    def limit(self, _n):
        return self

    def execute(self):
        self.c.calls.append((self.table, self.mode, self.payload, self.filters))
        if self.mode == "update":
            return _Resp([])
        return _Resp(self.c.responses.pop(0))


class _Resp:
    def __init__(self, data):
        self.data = data


class _Client:
    def __init__(self, responses):
        self.calls: List[tuple] = []
        self.responses = responses

    def table(self, name):
        return _Query(self, name)


class _Svc:
    def __init__(self, responses):
        self._supabase_url = "http://test.supabase.co"
        self._supabase_key = "key"
        self._client = _Client(responses)

    def _get_raw_client(self):
        return self._client


@pytest.mark.asyncio
async def test_dry_run_reports_but_writes_nothing(capsys, monkeypatch):
    svc = _Svc(responses=[
        [{"id": "esc-1", "chat_session_id": "s-1", "org_hashtag": None}],  # missing-org rows
        [{"metadata": {"organization_short_name": "Acme Energy"}}],        # session s-1
    ])
    monkeypatch.setattr(backfill, "EscalationService", lambda: svc)

    await backfill.run(apply=False)

    updates = [c for c in svc._client.calls if c[1] == "update"]
    assert updates == []
    out = capsys.readouterr().out
    assert "esc-1" in out and "#AcmeEnergy" in out and "Dry run" in out


@pytest.mark.asyncio
async def test_apply_writes_org_hashtag_from_session_metadata(monkeypatch):
    svc = _Svc(responses=[
        [{"id": "esc-1", "chat_session_id": "s-1", "org_hashtag": None}],
        [{"metadata": {"organization_short_name": "Acme Energy"}}],
    ])
    monkeypatch.setattr(backfill, "EscalationService", lambda: svc)

    await backfill.run(apply=True)

    updates = [c for c in svc._client.calls if c[1] == "update"]
    assert len(updates) == 1
    table, _mode, payload, filters = updates[0]
    assert table == "escalations"
    assert payload == {"org_hashtag": "#AcmeEnergy"}
    assert ("eq", "id", "esc-1") in filters
    assert ("is", "org_hashtag", "null") in filters  # idempotent guard


@pytest.mark.asyncio
async def test_apply_skips_rows_whose_session_has_no_org(capsys, monkeypatch):
    svc = _Svc(responses=[
        [{"id": "esc-2", "chat_session_id": "s-2", "org_hashtag": None}],
        [{"metadata": {}}],
    ])
    monkeypatch.setattr(backfill, "EscalationService", lambda: svc)

    await backfill.run(apply=True)

    assert [c for c in svc._client.calls if c[1] == "update"] == []
    assert "skip" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_refuses_when_db_not_configured(monkeypatch):
    class _Bare:
        _supabase_url = ""
        _supabase_key = ""

    monkeypatch.setattr(backfill, "EscalationService", lambda: _Bare())
    with pytest.raises(SystemExit):
        await backfill.run(apply=False)


def test_parse_args_defaults_to_dry_run():
    assert backfill.parse_args([]).apply is False
    assert backfill.parse_args(["--apply"]).apply is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --no-env-file pytest ../shared/tests/test_backfill_escalation_org_hashtag.py -q` (from `chat_orchestrator/`)
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.backfill_escalation_org_hashtag'`.

- [ ] **Step 3: Write the script** — create `scripts/backfill_escalation_org_hashtag.py`:

```python
#!/usr/bin/env python3
"""Backfill escalations.org_hashtag on rows the daily sweep alert would
otherwise label with a bare id:<uuid>.

PR #192 made _record_canonical_escalation persist customer_username /
customer_email / org_hashtag for new escalations. Rows created before that
deploy have all three NULL, so run_escalation_ticket_sweep's "older than 24h
with no ticket" alert falls back to the canonical id. customer_username /
customer_email are not recoverable for those rows, but the org is: it's in
chat_sessions.metadata->>'organization_short_name'. This fills org_hashtag
from there, which (after the label-precedence change) is enough for the alert
to show '#Org' instead of the UUID.

Scope: state='open' AND ticket_id IS NULL AND org_hashtag IS NULL. Idempotent
-- the UPDATE is guarded on org_hashtag IS NULL, and a second run re-queries
and finds nothing. Already-populated rows are never touched.

Usage:
    python scripts/backfill_escalation_org_hashtag.py [--apply]

Defaults to a DRY RUN. Pass --apply to write.

Requires CHAT_DB_URL / CHAT_DB_SERVICE_KEY (same as the running bot) and
PYTHONPATH=<repo-root> (see CLAUDE.md's scripts/ conventions):

    PYTHONPATH=. CHAT_DB_URL=... CHAT_DB_SERVICE_KEY=... \
        python scripts/backfill_escalation_org_hashtag.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from orchestrator.services.escalation_service import (
    EscalationService,
    _org_hashtag_from_short_name,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)


async def find_rows_missing_org(svc: EscalationService) -> list[dict[str, Any]]:
    """open, unfiled, org_hashtag IS NULL."""
    raw = svc._get_raw_client()
    resp = (
        raw.table("escalations")
        .select("id, chat_session_id, org_hashtag")
        .eq("state", "open")
        .is_("ticket_id", "null")
        .is_("org_hashtag", "null")
        .execute()
    )
    return list(getattr(resp, "data", None) or [])


async def org_hashtag_for_session(svc: EscalationService, session_uuid: str) -> str | None:
    raw = svc._get_raw_client()
    resp = (
        raw.table("chat_sessions")
        .select("metadata")
        .eq("id", session_uuid)
        .limit(1)
        .execute()
    )
    rows = list(getattr(resp, "data", None) or [])
    if not rows:
        return None
    short_name = (rows[0].get("metadata") or {}).get("organization_short_name")
    return _org_hashtag_from_short_name(short_name)


async def run(*, apply: bool) -> None:
    svc = EscalationService()
    if not (svc._supabase_url and svc._supabase_key):
        raise SystemExit(
            "CHAT_DB_URL / CHAT_DB_SERVICE_KEY must be set -- refusing to run "
            "against no database."
        )

    rows = await find_rows_missing_org(svc)
    print(f"Found {len(rows)} open, unfiled escalation(s) with no org_hashtag.")
    updated = skipped = 0
    for r in rows:
        tag = await org_hashtag_for_session(svc, r["chat_session_id"])
        if not tag:
            print(f"  skip      id={r['id']}  session={r['chat_session_id']}  (no org short name)")
            skipped += 1
            continue
        verb = "set      " if apply else "would set"
        print(f"  {verb} id={r['id']}  org_hashtag={tag!r}")
        if apply:
            svc._get_raw_client().table("escalations").update(
                {"org_hashtag": tag}
            ).eq("id", r["id"]).is_("org_hashtag", "null").execute()
            updated += 1

    if apply:
        print(f"\nUpdated {len(rows) - skipped} row(s); skipped {skipped}.")
    else:
        print(f"\nDry run only -- nothing changed. {len(rows) - skipped} row(s) would be updated, {skipped} skipped.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill escalations.org_hashtag from chat_sessions org short name."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without this flag, only prints what would change.",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests**

Run: `uv run --no-env-file pytest ../shared/tests/test_backfill_escalation_org_hashtag.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add -f scripts/backfill_escalation_org_hashtag.py shared/tests/test_backfill_escalation_org_hashtag.py
git commit -m "feat(scripts): backfill escalation org_hashtag from session metadata

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 8: Full verification & PR

- [ ] **Step 1: Full chat_orchestrator suite**

Run: `PYTEST tests/ -q`
Expected: all passed except the pre-existing, environment-only
`test_verification_criteria_library.py::test_get_verification_criteria_returns_the_bundled_default`
failure (real-cred `.env` trap — see CLAUDE.md; green in CI). Confirm no *other* failures.

- [ ] **Step 2: shared suite**

Run: `uv run --no-env-file pytest ../shared -q`
Expected: all passed.

- [ ] **Step 3: pre-commit**

Run (from repo root): `pre-commit run --all-files`
Expected: `ruff check`, `test-wiring`, loguru-guard, operator-data guards all Pass.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin fix/escalation-alert-actionable
gh pr create --title "fix(escalation): make the stale-escalation sweep alert readable and closable" --body "$(cat <<'EOF'
## What

The daily "N escalations older than 24h with no ticket" admin alert had two problems:

1. **Every entry rendered as a bare UUID** whenever `customer_username`, `customer_email` and `org_hashtag` were all absent — which is every escalation created before PR #192.
2. **No way for an admin to close a listed escalation.** The two rows stuck in the alert are `verification_failed` escalations, and `escalate_verification_failure` sends its Telegram message with no buttons at all.

## Changes

- **Label precedence** (`run_escalation_ticket_sweep`): `org_hashtag` is now used as the bullet label before falling back to `id:<uuid>`. `#Org — View` instead of `id:… — View`.
- **One-tap Close** in the alert: one `✓ Close` button per entry, new `ex:` callback prefix → `_handle_stale_alert_dismiss_callback` resolves the canonical row (reversible via `reopen()`), toast confirmation.
- **Verification-failure escalations** now carry the standard Track + Close-silently keyboard (no "Close & inform customer" — it's an internal AI-quality flag).
- **`scripts/backfill_escalation_org_hashtag.py`**: one-time, idempotent, dry-run-by-default backfill of `org_hashtag` on open unfiled rows from `chat_sessions.metadata`. Run once after deploy.

No schema change — `escalations` already has these columns (migration `0005a`).

## Tests

TDD throughout. New coverage for the label precedence cases, the dismiss keyboard + handler (authz, bad-uuid, resolve), the verification-failure keyboard, and the backfill script (dry-run / apply / skip / idempotency).

## Rollout

Merge → GHCR → DO deploy → run `python scripts/backfill_escalation_org_hashtag.py` (dry-run, then `--apply`).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Watch CI green, squash-merge, delete branch**

```bash
gh pr checks <PR#> --watch --interval 20
gh pr merge <PR#> --squash --delete-branch
```

- [ ] **Step 6: Deploy** — wait for `Build & Push Images` on `main` to finish, then invoke the `deployDO` skill.

- [ ] **Step 7: Run the backfill** (after deploy is `ACTIVE`)

```bash
cd chat_orchestrator
set -a; . <(grep -E '^(CHAT_DB_URL|CHAT_DB_SERVICE_KEY)=' .env); set +a
PYTHONPATH=/Users/vaibha/Downloads/git/nxt-ai-assistant uv run --no-env-file \
  python ../scripts/backfill_escalation_org_hashtag.py          # dry run — eyeball
PYTHONPATH=/Users/vaibha/Downloads/git/nxt-ai-assistant uv run --no-env-file \
  python ../scripts/backfill_escalation_org_hashtag.py --apply  # write
```

Expected: the two alert rows (and any sibling) get `org_hashtag` set from their session's org short name. Next daily alert shows `#Org — View` with a `✓ Close` button per row.

---

## Self-review notes

- **Spec coverage:** Piece 1 → Task 3; Piece 2 → Tasks 2, 4, 5; Piece 3 → Tasks 1, 6; Piece 4 → Task 7. All four covered.
- **Type consistency:** `build_stale_alert_keyboard(entries: list[(id, label)]) -> dict | None`, `ESCALATION_ALERT_DISMISS_PREFIX = "ex"`, `_handle_stale_alert_dismiss_callback(callback_id, mapping_id, chat_id)` used identically across tasks. `_org_hashtag_from_short_name` imported from `escalation_service` in Task 7 matches its PR #192 definition.
- **Decisions locked from brainstorming:** toast-only dismiss (no message edit); verification-failure keyboard = Track + Close-silently only.
