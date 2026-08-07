# Ticket Update Notifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Anansi the single author of Telegram ticket updates — for Jira and internal tickets alike — posting an intelligent, scroll-aware update card on every meaningful status transition and on operationally significant comments.

**Architecture:** One notifier (`TicketUpdateNotifier`) sits behind `TicketService.transition_to_done()` and the Jira webhook handlers. It renders a *full current-state card* (never an incremental append), so the same text is valid whether it edits the original ticket message in place or posts as a fresh reply. Anchor message and target chat come from the existing `message_deliveries` table; the edit-vs-repost decision is derived read-only from `chat_messages` and `message_deliveries` — no new table. Jira comments are mirrored into `ticket_comments` (the `source` enum already reserves `'jira'`), so closing summaries read from one table regardless of backend.

**Tech Stack:** Python 3.11, FastAPI, Supabase/PostgREST, Pydantic, loguru-style logging (`shared.utils.logging.get_logger`), `shared.llm` gateway (Gemini), pytest.

---

## Background: what exists today

Read these before starting — the plan builds on them rather than replacing them.

| Thing | Where | Why it matters |
|---|---|---|
| `TicketService.transition_to_done()` | `chat_orchestrator/orchestrator/services/ticketing/service.py:310` | The one orchestrator-side chokepoint through which 3 of 4 close paths already flow |
| `TicketRepository` — "sole writer" | `chat_orchestrator/orchestrator/services/ticketing/repository.py:35` | All ticket/comment writes belong here |
| `DeliveryRepository.record()` | `chat_orchestrator/orchestrator/services/ticketing/delivery_repository.py:27` | Already records chat/topic/message-id per ticket, and its `purpose` enum already includes `"update"` |
| `edit_telegram_message()` | `shared/utils/telegram_send.py:253` | Handles the "message is not modified" case for you |
| `send_telegram_message_with_fallback()` | `shared/utils/telegram_send.py:201` | Returns the new `message_id`; falls back when Markdown fails to parse |
| `/notify` amend path | `chat_orchestrator/orchestrator/api/app.py:1018-1061` | The existing edit-then-fall-back-to-send pattern this notifier mirrors |
| Hardcoded Jira closure post | `chat_orchestrator/orchestrator/services/escalation_service.py:3222` | The "direct Jira update" being replaced |
| Escalation-group blind spot | `chat_orchestrator/handler.py:2056-2067` | Non-reply messages there are deliberately dropped — Task 1 closes this by passive-saving the group like every other staff group |
| `_save_passive_group_message()` | `chat_orchestrator/handler.py:880` | Already writes `telegram_message_id`; Task 1 reuses it rather than adding new persistence |

**Two known bugs this plan fixes as a side effect:**
1. `handle_jira_issue_updated` matches the literal strings `"Done"`/`"Closed"` (`escalation_service.py:3188`), so a workflow status named "Resolved" never fires. The correct, workflow-agnostic check via `statusCategory` already exists at `escalation_service.py:1234`.
2. `transition_to_done_by_ref()` is an unconditional UPDATE, so a retried Jira webhook closes the same ticket twice. Harmless today; a double-post once a notifier is attached.

**Commands**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/ticket-update-notifier
```

Run tests from the `chat_orchestrator/` directory (that is where `pyproject.toml`'s `testpaths = ["tests"]` resolves):

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing -v
```

**CRITICAL — from CLAUDE.md:** every new file under any `tests/` directory needs `git add -f`. A plain `git add` is a silent no-op, the commit succeeds, and CI never runs the test. Task 10 verifies this; do not skip it.

---

## File Structure

**Create:**
- `chat_orchestrator/orchestrator/services/ticketing/chat_watermark.py` — `ChatWatermarkRepository` (read-only, derived from `chat_messages` + `message_deliveries`)
- `chat_orchestrator/orchestrator/services/ticketing/update_render.py` — pure rendering + LLM summarisation
- `chat_orchestrator/orchestrator/services/ticketing/update_notifier.py` — `TicketEvent`, `TicketUpdateNotifier`
- `chat_orchestrator/tests/services/ticketing/test_chat_watermark.py`
- `chat_orchestrator/tests/services/ticketing/test_update_render.py`
- `chat_orchestrator/tests/services/ticketing/test_update_notifier.py`

**Modify:**
- `chat_orchestrator/handler.py` — passive-save the escalation group (position signal + bot context), same as every other staff group already gets
- `anansi_app/nicegui_app/pages/chat.py` — filter the escalation group out of the admin chat list
- `chat_orchestrator/orchestrator/services/ticketing/repository.py` — add `list_comments_by_ref()`, make `transition_to_done_by_ref()` idempotent and boolean-returning
- `chat_orchestrator/orchestrator/services/ticketing/delivery_repository.py` — add `latest_for_ticket()`
- `chat_orchestrator/orchestrator/services/ticketing/service.py` — fire the notifier from `transition_to_done()`
- `chat_orchestrator/orchestrator/services/escalation_service.py` — `statusCategory` transition detection, delegate the closure post to the notifier, mirror Jira comments
- `README.md` — Jira webhook setup section

---

### Task 1: Derive chat position from what Anansi already stores

**Files:**
- Modify: `chat_orchestrator/handler.py:2050-2067`
- Create: `chat_orchestrator/orchestrator/services/ticketing/chat_watermark.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_chat_watermark.py`

Telegram message ids increment by one per message within a chat, so the gap between the newest id on record and a ticket's anchor id approximates how many messages have scrolled past it. **No new table is needed** — the two tables that can answer this already exist:

- `chat_messages.telegram_message_id` — every group message Anansi observes
- `message_deliveries.external_message_id` — Anansi's own ticket posts

The one blind spot is the escalation group: its messages are dropped without being saved (`handler.py:2056`), while every other staff group is passively saved (`handler.py:2100`). Closing that inconsistency with one call is the whole write-side change.

**Accepted accuracy limit:** `_save_passive_group_message` returns early on messages with no text and no caption, so a bare photo does not advance the head. Field photos usually carry a caption, but not always, so the count can run slightly low — which biases toward editing in place. Widening that guard would change behavior for every other group that already uses this function, so it is deliberately left alone.

- [ ] **Step 1: Persist escalation-group messages**

In `handler.py`, inside `if current_chat_id == escalation_chat_id:` and **above** the `if "reply_to_message" in telegram_msg:` branch, so both replies and non-replies are covered:

```python
                # Persist escalation-group messages the same way every other
                # staff group already is (see the staff_group branch below).
                # Two reasons: the bot gets conversation context here, and the
                # ticket update notifier can tell from telegram_message_id
                # whether a ticket's message has scrolled out of view. Without
                # this, the one chat where ticket updates matter most is the
                # only group with no position signal at all.
                try:
                    await _save_passive_group_message(telegram_msg, chat)
                except Exception as e:
                    LOGGER.warning(f"Failed to save escalation group message: {e}")
```

The existing routing below is unchanged — replies still forward to the customer, non-replies are still not answered. This only adds persistence.

- [ ] **Step 2: Write the failing test**

```python
"""ChatWatermarkRepository: how far a chat has scrolled past a ticket message.

Derived from data Anansi already stores rather than a dedicated table:
chat_messages for observed group traffic, message_deliveries for the bot's
own ticket posts, whichever is higher.

The notifier calls messages_since() inside ticket-close paths, so an absent
or unreadable signal must degrade to 0 ("nothing has scrolled", i.e. edit in
place) rather than raise.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing.chat_watermark import ChatWatermarkRepository


class _FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]], raises: bool = False) -> None:
        self._rows = rows
        self._raises = raises

    def select(self, *_a, **_k) -> "_FakeQuery":
        return self

    def eq(self, *_a, **_k) -> "_FakeQuery":
        return self

    def gte(self, *_a, **_k) -> "_FakeQuery":
        return self

    def order(self, *_a, **_k) -> "_FakeQuery":
        return self

    def limit(self, *_a, **_k) -> "_FakeQuery":
        return self

    def execute(self) -> _FakeResponse:
        if self._raises:
            raise RuntimeError("postgrest down")
        return _FakeResponse(self._rows)


class _FakeClient:
    """Serves a different row set per table, so each source can be tested alone."""

    def __init__(
        self,
        chat_messages: Optional[List[Dict[str, Any]]] = None,
        deliveries: Optional[List[Dict[str, Any]]] = None,
        raising_tables: Optional[set] = None,
    ) -> None:
        self._by_table = {
            "chat_messages": chat_messages or [],
            "message_deliveries": deliveries or [],
        }
        self._raising = raising_tables or set()

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self._by_table.get(name, []), raises=name in self._raising)


@pytest.mark.asyncio
async def test_head_reads_observed_group_traffic():
    client = _FakeClient(chat_messages=[{"telegram_message_id": 120}])
    assert await ChatWatermarkRepository(client=client).head("-100123") == 120


@pytest.mark.asyncio
async def test_head_reads_the_bots_own_ticket_posts():
    client = _FakeClient(deliveries=[{"external_message_id": 140}])
    assert await ChatWatermarkRepository(client=client).head("-100123") == 140


@pytest.mark.asyncio
async def test_head_takes_the_higher_of_both_sources():
    client = _FakeClient(
        chat_messages=[{"telegram_message_id": 120}],
        deliveries=[{"external_message_id": 140}],
    )
    assert await ChatWatermarkRepository(client=client).head("-100123") == 140


@pytest.mark.asyncio
async def test_head_survives_one_source_failing():
    """A broken source must not blind the other one."""
    client = _FakeClient(
        deliveries=[{"external_message_id": 140}],
        raising_tables={"chat_messages"},
    )
    assert await ChatWatermarkRepository(client=client).head("-100123") == 140


@pytest.mark.asyncio
async def test_head_is_none_when_nothing_is_on_record():
    assert await ChatWatermarkRepository(client=_FakeClient()).head("-100123") is None


@pytest.mark.asyncio
async def test_messages_since_returns_the_gap():
    client = _FakeClient(chat_messages=[{"telegram_message_id": 120}])
    assert await ChatWatermarkRepository(client=client).messages_since("-100123", 100) == 20


@pytest.mark.asyncio
async def test_messages_since_is_zero_when_nothing_is_on_record():
    """Unknown position must read as "still on screen" -- the same behavior
    as before any of this existed."""
    assert await ChatWatermarkRepository(client=_FakeClient()).messages_since("-100123", 100) == 0


@pytest.mark.asyncio
async def test_messages_since_is_zero_when_head_is_behind_the_anchor():
    client = _FakeClient(chat_messages=[{"telegram_message_id": 50}])
    assert await ChatWatermarkRepository(client=client).messages_since("-100123", 100) == 0
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_chat_watermark.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.ticketing.chat_watermark'`

- [ ] **Step 4: Write the implementation**

```python
"""How far a chat has scrolled past a ticket's message.

Telegram message ids increment by one per message within a chat, so the gap
between the newest id on record and a given anchor id approximates how many
messages have been posted since. TicketUpdateNotifier uses that to choose
between editing a ticket's message in place and posting a fresh reply.

Read-only, and derived from tables Anansi already writes:

* ``chat_messages.telegram_message_id`` -- group traffic the bot observes,
  including the escalation group (see the passive-save call in ``handler.py``)
* ``message_deliveries.external_message_id`` -- the bot's own ticket posts

Both reads are bounded by a recency window. ``chat_messages`` has no index on
``group_id``, but ``chat_messages_created_at_idx`` makes a short window
selective, and an anchor older than the window is stale by definition -- for
which "post a fresh message" is the right answer regardless.

The approximation runs slightly low: ``_save_passive_group_message`` skips
messages with no text or caption, so bare photos do not advance the head.
Under-counting biases toward editing in place, which is the pre-existing
behavior, so it degrades rather than misbehaves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

#: How far back to look for chat position. Long enough to cover any ticket
#: still being actively discussed, short enough to keep the group_id scan
#: bounded by the created_at index.
LOOKBACK_DAYS = 7


class ChatWatermarkRepository:
    """Reads the newest known Telegram message id for a chat.

    Every method is best-effort: this is positioning telemetry sitting in the
    path of ticket closes, so a database hiccup must degrade the notifier's
    placement decision rather than fail the close.
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        get_client: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        if client is None and get_client is None:
            raise ValueError("ChatWatermarkRepository requires either `client` or `get_client`")
        self._client_instance = client
        self._get_client = get_client

    def _raw_client(self) -> Optional[Any]:
        if self._client_instance is not None:
            return self._client_instance
        if self._get_client is not None:
            try:
                return self._get_client()
            except Exception:
                LOGGER.warning("chat watermark: get_client() raised", exc_info=True)
                return None
        return None

    @staticmethod
    def _since() -> str:
        return (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()

    async def head(self, chat_id: str) -> Optional[int]:
        """Newest message id on record for ``chat_id``, or None if unknown."""
        client = self._raw_client()
        if client is None or not chat_id:
            return None

        since = self._since()
        candidates: List[int] = []

        try:
            response = (
                client.table("chat_messages")
                .select("telegram_message_id")
                .eq("group_id", str(chat_id))
                .gte("created_at", since)
                .order("telegram_message_id", desc=True)
                .limit(1)
                .execute()
            )
            rows = getattr(response, "data", None) or []
            if rows and rows[0].get("telegram_message_id"):
                candidates.append(int(rows[0]["telegram_message_id"]))
        except Exception:
            LOGGER.debug("chat watermark: chat_messages read failed for {}", chat_id, exc_info=True)

        try:
            response = (
                client.table("message_deliveries")
                .select("external_message_id")
                .eq("external_chat_id", str(chat_id))
                .gte("sent_at", since)
                .order("external_message_id", desc=True)
                .limit(1)
                .execute()
            )
            rows = getattr(response, "data", None) or []
            if rows and rows[0].get("external_message_id"):
                candidates.append(int(rows[0]["external_message_id"]))
        except Exception:
            LOGGER.debug(
                "chat watermark: message_deliveries read failed for {}", chat_id, exc_info=True
            )

        return max(candidates) if candidates else None

    async def messages_since(self, chat_id: str, anchor_message_id: int) -> int:
        """Approximate message count posted in ``chat_id`` after ``anchor_message_id``.

        Returns 0 when unknown, which the notifier reads as "still on screen"
        -- matching how ticket messages behaved before this existed.
        """
        head = await self.head(chat_id)
        if head is None or head <= anchor_message_id:
            return 0
        return head - anchor_message_id
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_chat_watermark.py -v`
Expected: PASS — 8 passed

- [ ] **Step 6: Commit (note the `-f` on the test file)**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/chat_watermark.py \
        chat_orchestrator/handler.py
git add -f chat_orchestrator/tests/services/ticketing/test_chat_watermark.py
git commit -m "feat(ticketing): derive telegram chat position for update placement"
```

---

### Task 2: Hide the escalation group from the admin chat list

**Files:**
- Modify: `anansi_app/nicegui_app/pages/chat.py:99-105`

Task 1 makes escalation-group messages appear as a conversation in the admin Chat page — one passive session per forum topic. That page is for reviewing customer conversations; internal escalation chatter does not belong in it.

Filtering after the search merge (rather than inside `get_chat_contexts`) is deliberate: it is the one point every path flows through, so a content search cannot surface the group either, and the derived counts in `stats` stay consistent with the list.

- [ ] **Step 1: Add the filter**

In `chat.py`, immediately after the `if search:` block ends and **before** `groups = [c for c in contexts if c["is_group"]]`:

```python
        # The escalation group is persisted for bot context and for ticket
        # update placement (see handler.py's passive-save call), but it is
        # internal staff traffic -- this page is for customer conversations.
        escalation_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
        if escalation_chat_id:
            contexts = [
                c
                for c in contexts
                if str(c.get("chat_id") or "") != escalation_chat_id
                and str(c.get("group_id") or "") != escalation_chat_id
            ]
```

Both keys are checked because `get_chat_contexts` returns group rows keyed on `(chat_id, group_id)` and the escalation chat can appear under either depending on how the session was created.

Add `import os` to the top of `chat.py` if it is not already imported.

- [ ] **Step 2: Verify the page still renders**

Run: `cd anansi_app && python -c "import ast,sys; ast.parse(open('nicegui_app/pages/chat.py').read()); print('ok')"`
Expected: `ok`

Then load the admin Chat page and confirm the escalation group is absent from the Groups list and that the group count dropped by one. If the app is not running locally, this is a post-deploy check — note it and move on.

- [ ] **Step 3: Commit**

```bash
git add anansi_app/nicegui_app/pages/chat.py
git commit -m "feat(admin): hide the escalation group from the chat conversation list"
```

Note: `_render_stats`' message and token totals come from `get_period_stats`, which aggregates all messages — those numbers will now include escalation-group traffic. That is a truthful "messages handled" figure and is left alone; say so if anyone asks why the count stepped up after deploy.


### Task 3: Repository additions

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/repository.py:192-202` (`transition_to_done_by_ref`)
- Modify: `chat_orchestrator/orchestrator/services/ticketing/repository.py` (add `list_comments_by_ref`)
- Modify: `chat_orchestrator/orchestrator/services/ticketing/delivery_repository.py` (add `latest_for_ticket`)
- Modify: `chat_orchestrator/tests/services/ticketing/test_service.py:129-130` (fake must return `True`)
- Test: `chat_orchestrator/tests/services/ticketing/test_repository.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `chat_orchestrator/tests/services/ticketing/test_repository.py`:

```python
@pytest.mark.asyncio
async def test_transition_to_done_by_ref_returns_true_when_it_flips_the_row():
    client = _FakeClient(
        tickets=[{"id": "t-1", "ticket_ref": "TKT-1", "backend": "internal",
                  "created_via": "escalation", "provisioning_state": "active",
                  "status": "open", "summary": "s"}]
    )
    flipped = await TicketRepository(client=client).transition_to_done_by_ref("TKT-1")
    assert flipped is True


@pytest.mark.asyncio
async def test_transition_to_done_by_ref_returns_false_when_already_done():
    """A retried Jira webhook must not be reported as a fresh closure —
    otherwise the update notifier posts the same card twice."""
    client = _FakeClient(
        tickets=[{"id": "t-1", "ticket_ref": "TKT-1", "backend": "internal",
                  "created_via": "escalation", "provisioning_state": "active",
                  "status": "done", "summary": "s"}]
    )
    flipped = await TicketRepository(client=client).transition_to_done_by_ref("TKT-1")
    assert flipped is False


@pytest.mark.asyncio
async def test_list_comments_by_ref_returns_oldest_first():
    client = _FakeClient(
        tickets=[{"id": "t-1", "ticket_ref": "TKT-1", "backend": "internal",
                  "created_via": "escalation", "provisioning_state": "active",
                  "status": "open", "summary": "s"}],
        comments=[
            {"author": "b", "body": "second", "is_public": False, "source": "staff",
             "created_at": "2026-08-07T10:00:00Z"},
            {"author": "a", "body": "first", "is_public": False, "source": "staff",
             "created_at": "2026-08-07T09:00:00Z"},
        ],
    )
    rows = await TicketRepository(client=client).list_comments_by_ref("TKT-1", limit=5)
    # The query pulls newest-first (to honour `limit`), the method reverses so
    # the summariser reads them in chronological order.
    assert [r["body"] for r in rows] == ["first", "second"]


@pytest.mark.asyncio
async def test_list_comments_by_ref_returns_empty_for_unknown_ref():
    client = _FakeClient(tickets=[])
    assert await TicketRepository(client=client).list_comments_by_ref("NOPE-1") == []
```

The existing `_FakeClient` in that file must gain a `comments` table and honour `.neq()` / `.order(desc=)`. Read the file's existing fake first and extend it in place rather than replacing it. Its `_FakeQuery` needs three additions:

```python
    def neq(self, column: str, value: Any) -> "_FakeQuery":
        self._excludes.append((column, value))
        return self

    def order(self, column: str, desc: bool = False) -> "_FakeQuery":
        self._order = (column, desc)
        return self
```

with `self._excludes: list = []` and `self._order = None` initialised in `_FakeQuery.__init__`, and `execute()` extended so that — after the existing `eq` filtering and before the existing `limit` slice — it runs:

```python
        for column, value in self._excludes:
            rows = [r for r in rows if r.get(column) != value]
        if self._order is not None:
            column, desc = self._order
            rows = sorted(rows, key=lambda r: r.get(column) or "", reverse=desc)
```

For an `update` operation, `neq` must filter which rows are updated and `execute()` must return only the rows it actually changed — that is what makes the idempotency test meaningful.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_repository.py -v`
Expected: FAIL — `AttributeError: 'TicketRepository' object has no attribute 'list_comments_by_ref'`, and the boolean tests fail because the method returns `None`.

- [ ] **Step 3: Rewrite `transition_to_done_by_ref` in `repository.py`**

Replace lines 192-202 with:

```python
    async def transition_to_done_by_ref(self, ref: str) -> bool:
        """Close a ticket. Returns True only if this call is what closed it.

        The ``status != 'done'`` guard makes this idempotent: Jira retries
        webhook deliveries, and several close paths can race for the same
        ticket. Callers use the return value to decide whether to announce
        the closure, so a redundant close must report False rather than
        raising (every caller already treats failures as non-fatal).
        """
        ticket = await self.get_by_ref(ref)
        if ticket is None:
            raise TicketRepositoryError(f"cannot close: unknown ticket ref {ref}")
        payload = {"status": "done", "resolved_at": datetime.now(timezone.utc).isoformat()}
        try:
            response = (
                self._raw_client()
                .table("tickets")
                .update(payload)
                .eq("id", ticket.id)
                .neq("status", "done")
                .execute()
            )
        except Exception as exc:
            raise TicketRepositoryError(f"failed to close canonical ticket: {exc}") from exc
        return bool(getattr(response, "data", None))
```

- [ ] **Step 4: Add `list_comments_by_ref` to `repository.py`**

Insert directly after `add_comment_by_ref`:

```python
    async def list_comments_by_ref(self, ref: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Return the most recent comments for a ticket, oldest-first.

        Ordered newest-first in the query so ``limit`` keeps the *latest*
        comments, then reversed so the summariser reads them chronologically.
        Returns [] for an unknown ref rather than raising -- this feeds a
        best-effort notification, not a correctness-critical path.
        """
        ticket = await self.get_by_ref(ref)
        if ticket is None:
            return []
        try:
            response = (
                self._raw_client()
                .table("ticket_comments")
                .select("author,body,is_public,source,created_at")
                .eq("ticket_id", ticket.id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
        except Exception as exc:
            raise TicketRepositoryError(f"failed to read canonical ticket comments: {exc}") from exc
        rows = list(getattr(response, "data", None) or [])
        return list(reversed(rows))
```

- [ ] **Step 5: Add `latest_for_ticket` to `delivery_repository.py`**

Append to the `DeliveryRepository` class:

```python
    async def latest_for_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """Most recent Telegram delivery for a ticket -- its update anchor.

        The notifier edits or replies to whatever it last posted about this
        ticket (or the original notification, if there have been no updates
        yet), so "latest row wins" is exactly the anchor semantics wanted.
        """
        try:
            response = (
                self._raw_client()
                .table("message_deliveries")
                .select("*")
                .eq("ticket_id", ticket_id)
                .eq("channel", "telegram")
                .order("sent_at", desc=True)
                .limit(1)
                .execute()
            )
        except Exception:
            return None
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None
```

- [ ] **Step 6: Update the fakes in `test_service.py`**

Two fakes now carry a "did this close it" signal.

In `_FakeTicketRepository` (line ~85), add `self.transition_returns = True` to `__init__` and change the method at line 129-130:

```python
    async def transition_to_done_by_ref(self, ref: str) -> bool:
        self.transition_to_done_by_ref_calls.append(ref)
        return self.transition_returns
```

In `_FakeBackend` (line ~33), add `self.transition_returns = True` to `__init__` and make its `transition_to_done` return it:

```python
    async def transition_to_done(self, ref: str) -> bool:
        self.transition_to_done_calls.append(ref)
        return self.transition_returns
```

- [ ] **Step 7: Run the full ticketing suite**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing -v`
Expected: PASS — all existing tests plus the 4 new ones

- [ ] **Step 8: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/repository.py \
        chat_orchestrator/orchestrator/services/ticketing/delivery_repository.py
git add -f chat_orchestrator/tests/services/ticketing/test_repository.py \
           chat_orchestrator/tests/services/ticketing/test_service.py
git commit -m "feat(ticketing): idempotent close, comment listing, delivery anchor lookup"
```

---

### Task 4: Update card rendering and comment summarisation

**Files:**
- Create: `chat_orchestrator/orchestrator/services/ticketing/update_render.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_update_render.py`

The card is a **full current-state render**, never an append. That invariant is what makes edit-in-place and fresh-reply produce identical, correct text.

- [ ] **Step 1: Write the failing test**

```python
"""Rendering and summarisation for ticket update cards.

The card must be a complete statement of current state: the same text is
used both to edit the original message in place and to post a fresh reply,
so it can never depend on what a previous message said.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from orchestrator.services.ticketing.update_render import (
    NOISE_FLOOR_CHARS,
    fallback_summary,
    is_probably_noise,
    render_update_card,
)


def test_render_card_states_ticket_status_and_summary():
    text = render_update_card(
        ticket_ref="ANS-42",
        summary="Inverter 3 offline at Kasoa",
        status="done",
        activity="Field team replaced the DC isolator; output confirmed at 4.1 kW.",
        url=None,
    )
    assert "ANS-42" in text
    assert "Inverter 3 offline at Kasoa" in text
    assert "Field team replaced the DC isolator" in text
    assert "closed" in text.lower()


def test_render_card_links_jira_tickets_when_a_url_is_known():
    text = render_update_card(
        ticket_ref="OPS-7",
        summary="Meter comms lost",
        status="in_progress",
        activity="Awaiting SIM replacement.",
        url="https://example.atlassian.net/browse/OPS-7",
    )
    assert "[OPS-7](https://example.atlassian.net/browse/OPS-7)" in text


def test_render_card_omits_activity_line_when_there_is_none():
    text = render_update_card(
        ticket_ref="ANS-1", summary="Test", status="open",
        activity="", url=None,
    )
    assert "ANS-1" in text
    assert text.count("\n\n") <= 1


def test_fallback_summary_uses_the_latest_comment_truncated():
    comments: List[dict[str, Any]] = [
        {"body": "first", "author": "a"},
        {"body": "x" * 400, "author": "b"},
    ]
    out = fallback_summary(comments)
    assert out.startswith("x")
    assert len(out) <= 303  # 300 + the ellipsis


def test_fallback_summary_is_empty_without_comments():
    assert fallback_summary([]) == ""


def test_short_comments_are_noise():
    assert is_probably_noise("ok") is True
    assert is_probably_noise("x" * (NOISE_FLOOR_CHARS + 1)) is False


def test_whitespace_only_comment_is_noise():
    assert is_probably_noise("   \n  ") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_update_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.ticketing.update_render'`

- [ ] **Step 3: Write the implementation**

```python
"""Rendering and LLM summarisation for ticket update cards.

Split out from ``update_notifier`` so the text logic is unit-testable without
a database or a Telegram token, mirroring how ``correlation_render`` is split
from ``correlator``.

The card is always a *full statement of current state*. The notifier may
either edit the original ticket message in place or post a fresh reply, and
both must read correctly standing alone -- so nothing here may depend on the
content of a previous message.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from shared.llm import GenerationOptions, LLMMessage
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

#: Comments shorter than this are treated as chatter ("ok", "thanks", "+1")
#: and never reach the LLM classifier.
NOISE_FLOOR_CHARS = 20

#: How much of a comment survives into the deterministic fallback summary.
FALLBACK_CHARS = 300

_STATUS_LABELS = {
    "open": "open",
    "in_progress": "in progress",
    "done": "closed",
}

_STATUS_ICONS = {
    "open": "\U0001f7e2",        # green circle
    "in_progress": "\U0001f7e0",  # orange circle
    "done": "✅",             # white heavy check mark
}


def is_probably_noise(body: str) -> bool:
    """True for comments not worth spending an LLM call on."""
    return len(body.strip()) <= NOISE_FLOOR_CHARS


def fallback_summary(comments: List[Dict[str, Any]]) -> str:
    """Deterministic stand-in when the LLM is unavailable: latest comment, truncated.

    ``comments`` is oldest-first (as returned by
    ``TicketRepository.list_comments_by_ref``), so the last element is newest.
    """
    if not comments:
        return ""
    body = (comments[-1].get("body") or "").strip()
    if not body:
        return ""
    if len(body) <= FALLBACK_CHARS:
        return body
    return body[:FALLBACK_CHARS] + "…"


def render_update_card(
    *,
    ticket_ref: str,
    summary: str,
    status: str,
    activity: str,
    url: Optional[str],
) -> str:
    """Render the complete current state of a ticket as Telegram Markdown."""
    icon = _STATUS_ICONS.get(status, "\U0001f4cb")
    label = _STATUS_LABELS.get(status, status or "unknown")
    ref_text = f"[{ticket_ref}]({url})" if url else f"*{ticket_ref}*"

    lines = [f"{icon} {ref_text} — {label}"]
    if summary:
        lines.append(summary)
    text = "\n".join(lines)
    if activity.strip():
        text = f"{text}\n\n{activity.strip()}"
    return text


_SUMMARY_SYSTEM = (
    "You summarise support-ticket activity for a solar mini-grid operations "
    "team on Telegram. Reply with one or two plain sentences, no preamble, no "
    "bullet points, no markdown. State what was actually done or found. If the "
    "comments do not say what happened, say so briefly rather than inventing "
    "detail."
)

_SIGNIFICANCE_SYSTEM = (
    "You triage support-ticket comments for a solar mini-grid operations team. "
    "Decide whether a comment is operationally significant -- a diagnosis, a "
    "root cause, an escalation, a customer impact, a blocker, a schedule "
    "change, or a resolution. Routine acknowledgements, status pings, and "
    "administrative chatter are not significant. Respond with JSON only: "
    '{"significant": true|false, "summary": "<one sentence>"}'
)


def _format_comments(comments: List[Dict[str, Any]]) -> str:
    parts = []
    for comment in comments:
        author = (comment.get("author") or "unknown").strip()
        body = (comment.get("body") or "").strip()
        if body:
            parts.append(f"{author}: {body}")
    return "\n\n".join(parts)


async def summarize_activity(
    gateway: Any,
    model: str,
    comments: List[Dict[str, Any]],
) -> str:
    """One-or-two-sentence summary of recent ticket comments.

    Fails open to ``fallback_summary`` on any error -- a closure notification
    that says a little less is strictly better than one that never arrives.
    """
    if not comments:
        return ""
    formatted = _format_comments(comments)
    if not formatted:
        return ""
    try:
        result = await gateway.generate(
            [
                LLMMessage(role="system", text=_SUMMARY_SYSTEM),
                LLMMessage(role="user", text=formatted),
            ],
            GenerationOptions(model=model, temperature=0.0),
        )
    except Exception:
        LOGGER.warning("ticket update: activity summarisation failed", exc_info=True)
        return fallback_summary(comments)
    text = (getattr(result, "text", "") or "").strip()
    return text or fallback_summary(comments)


async def classify_significance(
    gateway: Any,
    model: str,
    comment_body: str,
) -> bool:
    """Whether a single comment warrants interrupting a Telegram group.

    Fails *closed* (returns False): the cost of a missed notification is one
    less message, while the cost of a false positive is training the team to
    ignore ticket updates.
    """
    import json

    if is_probably_noise(comment_body):
        return False
    try:
        result = await gateway.generate(
            [
                LLMMessage(role="system", text=_SIGNIFICANCE_SYSTEM),
                LLMMessage(role="user", text=comment_body.strip()),
            ],
            GenerationOptions(model=model, temperature=0.0, response_format="json"),
        )
        parsed = json.loads((getattr(result, "text", "") or "").strip())
    except Exception:
        LOGGER.warning("ticket update: significance classification failed", exc_info=True)
        return False
    return bool(parsed.get("significant")) if isinstance(parsed, dict) else False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_update_render.py -v`
Expected: PASS — 7 passed

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/update_render.py
git add -f chat_orchestrator/tests/services/ticketing/test_update_render.py
git commit -m "feat(ticketing): render ticket update cards and summarise recent activity"
```

---

### Task 5: TicketUpdateNotifier

**Files:**
- Create: `chat_orchestrator/orchestrator/services/ticketing/update_notifier.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_update_notifier.py`

This is the whole placement policy in one place: **≤ 5 messages since the anchor → edit it in place; otherwise post a fresh reply to the anchor and make that the new anchor.**

- [ ] **Step 1: Write the failing test**

```python
"""TicketUpdateNotifier: scroll-aware ticket update delivery.

Placement policy under test:
  * anchor still on screen (<= SCROLL_THRESHOLD messages since) -> edit it
  * anchor scrolled away                                        -> fresh reply
  * edit rejected by Telegram                                   -> fresh reply
  * no anchor at all                                            -> stay silent
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing.update_notifier import (
    SCROLL_THRESHOLD,
    TicketEvent,
    TicketUpdateNotifier,
)


class _FakeTickets:
    def __init__(self, record: Optional[Any] = None, comments: Optional[List[Dict]] = None) -> None:
        self._record = record
        self._comments = comments or []

    async def get_by_ref(self, ref: str) -> Any:
        return self._record

    async def list_comments_by_ref(self, ref: str, *, limit: int = 5) -> List[Dict]:
        return self._comments


class _FakeDeliveries:
    def __init__(self, anchor: Optional[Dict] = None) -> None:
        self._anchor = anchor
        self.recorded: List[Dict] = []

    async def latest_for_ticket(self, ticket_id: str) -> Optional[Dict]:
        return self._anchor

    async def record(self, **kwargs: Any) -> Dict:
        self.recorded.append(kwargs)
        return kwargs


class _FakeWatermark:
    def __init__(self, gap: int = 0) -> None:
        self._gap = gap

    async def messages_since(self, chat_id: str, anchor_message_id: int) -> int:
        return self._gap


class _Record:
    id = "t-1"
    ticket_ref = "ANS-42"
    backend = "internal"
    status = "done"
    summary = "Inverter 3 offline"


def _notifier(*, gap: int, anchor: Optional[Dict], edit_ok: bool = True, tickets=None):
    edits: List[Dict] = []
    sends: List[Dict] = []

    async def _edit(bot_token, chat_id, message_id, text, parse_mode=None):
        edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return edit_ok

    async def _send(bot_token, chat_id, text, parse_mode=None, topic_id=None,
                    reply_to_message_id=None):
        sends.append({"chat_id": chat_id, "text": text,
                      "reply_to_message_id": reply_to_message_id})
        return 9999

    deliveries = _FakeDeliveries(anchor=anchor)
    notifier = TicketUpdateNotifier(
        tickets=tickets or _FakeTickets(record=_Record()),
        deliveries=deliveries,
        watermark=_FakeWatermark(gap=gap),
        bot_token="tok",
        gateway=None,
        model="fake-model",
        edit_fn=_edit,
        send_fn=_send,
    )
    return notifier, edits, sends, deliveries


_ANCHOR = {
    "external_chat_id": "-100123",
    "external_topic_id": "77",
    "external_message_id": 500,
}


@pytest.mark.asyncio
async def test_edits_in_place_when_anchor_is_still_on_screen():
    notifier, edits, sends, _ = _notifier(gap=SCROLL_THRESHOLD, anchor=_ANCHOR)
    posted = await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition",
                                               to_status="done"))
    assert posted is True
    assert len(edits) == 1
    assert edits[0]["message_id"] == 500
    assert sends == []


@pytest.mark.asyncio
async def test_posts_fresh_reply_once_the_anchor_has_scrolled():
    notifier, edits, sends, deliveries = _notifier(gap=SCROLL_THRESHOLD + 1, anchor=_ANCHOR)
    posted = await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition",
                                               to_status="done"))
    assert posted is True
    assert edits == []
    assert len(sends) == 1
    assert sends[0]["reply_to_message_id"] == 500
    # The new message becomes the anchor for the next update.
    assert deliveries.recorded[0]["external_message_id"] == 9999
    assert deliveries.recorded[0]["purpose"] == "update"


@pytest.mark.asyncio
async def test_falls_back_to_a_fresh_reply_when_the_edit_is_rejected():
    notifier, edits, sends, _ = _notifier(gap=0, anchor=_ANCHOR, edit_ok=False)
    posted = await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition",
                                               to_status="done"))
    assert posted is True
    assert len(edits) == 1
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_stays_silent_when_the_ticket_was_never_announced():
    notifier, edits, sends, _ = _notifier(gap=0, anchor=None)
    posted = await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition",
                                               to_status="done"))
    assert posted is False
    assert edits == []
    assert sends == []


@pytest.mark.asyncio
async def test_stays_silent_for_an_unknown_ticket_ref():
    notifier, _, _, _ = _notifier(gap=0, anchor=_ANCHOR, tickets=_FakeTickets(record=None))
    posted = await notifier.notify(TicketEvent(ticket_ref="NOPE-1", kind="transition",
                                               to_status="done"))
    assert posted is False


@pytest.mark.asyncio
async def test_notify_never_raises_into_the_caller():
    """A ticket close must succeed even when the notifier is entirely broken."""

    class _Exploding:
        async def get_by_ref(self, ref):
            raise RuntimeError("db gone")

    notifier, _, _, _ = _notifier(gap=0, anchor=_ANCHOR, tickets=_Exploding())
    assert await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition",
                                             to_status="done")) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_update_notifier.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.ticketing.update_notifier'`

- [ ] **Step 3: Write the implementation**

```python
"""TicketUpdateNotifier -- the single author of Telegram ticket updates.

Anansi, not Jira, posts ticket updates. Both backends flow through here, so a
Jira transition and an internal transition produce an identical card from
identical code.

Placement policy
----------------
Every ticket already has an anchor: the most recent Telegram message Anansi
posted about it (``message_deliveries``). When that anchor is still on screen
the card is edited in place, which keeps one message per ticket instead of a
growing trail. Once roughly ``SCROLL_THRESHOLD`` messages have gone by, an
edit would go unnoticed, so a fresh reply to the anchor is posted instead --
and that reply becomes the next anchor.

Because the card is a full current-state render (see ``update_render``),
both branches emit the same text and neither depends on message history.

Everything here is best-effort. ``notify()`` returns a bool and never raises:
it runs inside ticket-close paths, and a Telegram outage must not roll back a
closure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional

from shared.utils.logging import get_logger
from shared.utils.markdown import convert_github_to_telegram_markdown

from .update_render import (
    classify_significance,
    fallback_summary,
    render_update_card,
    summarize_activity,
)

LOGGER = get_logger(__name__)

#: How many messages may pass before the anchor counts as scrolled away.
#: Matches the operator's rule of thumb: an update to a ticket message more
#: than five messages back will not be seen, so post a fresh reply.
SCROLL_THRESHOLD = 5

#: How many recent comments feed the activity summary.
COMMENT_WINDOW = 5


@dataclass(frozen=True)
class TicketEvent:
    """Something that happened to a ticket and may deserve a Telegram update."""

    ticket_ref: str
    kind: Literal["transition", "comment"]
    from_status: str = ""
    to_status: str = ""
    comment_body: str = ""
    comment_author: str = ""
    ticket_url: Optional[str] = None


class TicketUpdateNotifier:
    """Renders and places ticket update cards on Telegram.

    Collaborators are injected rather than constructed so the placement policy
    can be tested without a database, an LLM, or a bot token -- the same
    posture ``AlertCorrelator`` takes.
    """

    def __init__(
        self,
        *,
        tickets: Any,
        deliveries: Any,
        watermark: Any,
        bot_token: str,
        gateway: Any = None,
        model: str = "",
        edit_fn: Optional[Callable[..., Awaitable[bool]]] = None,
        send_fn: Optional[Callable[..., Awaitable[Optional[int]]]] = None,
    ) -> None:
        self._tickets = tickets
        self._deliveries = deliveries
        self._watermark = watermark
        self._bot_token = bot_token
        self._gateway = gateway
        self._model = model
        self._edit_fn = edit_fn
        self._send_fn = send_fn

    # -- injected transport ------------------------------------------------

    async def _edit(self, chat_id: str, message_id: int, text: str) -> bool:
        if self._edit_fn is not None:
            return await self._edit_fn(
                self._bot_token, chat_id, message_id, text, parse_mode="Markdown"
            )
        from shared.utils.telegram_send import edit_telegram_message

        return await edit_telegram_message(
            self._bot_token, chat_id, message_id, text, parse_mode="Markdown"
        )

    async def _send(
        self, chat_id: str, text: str, topic_id: Optional[str], reply_to: Optional[int]
    ) -> Optional[int]:
        if self._send_fn is not None:
            return await self._send_fn(
                self._bot_token,
                chat_id,
                text,
                parse_mode="Markdown",
                topic_id=topic_id,
                reply_to_message_id=reply_to,
            )
        from shared.utils.telegram_send import send_telegram_message_with_fallback

        return await send_telegram_message_with_fallback(
            self._bot_token,
            chat_id,
            text,
            parse_mode="Markdown",
            topic_id=topic_id,
            reply_to_message_id=reply_to,
        )

    # -- policy ------------------------------------------------------------

    async def _is_worth_posting(self, event: TicketEvent) -> bool:
        """Transitions always post; comments must clear the significance bar."""
        if event.kind == "transition":
            return True
        if self._gateway is None:
            # No classifier configured -- stay quiet rather than relay every
            # comment into the group.
            return False
        return await classify_significance(self._gateway, self._model, event.comment_body)

    async def _activity_line(self, event: TicketEvent) -> str:
        try:
            comments = await self._tickets.list_comments_by_ref(
                event.ticket_ref, limit=COMMENT_WINDOW
            )
        except Exception:
            LOGGER.warning(
                "ticket update: comment lookup failed for {}", event.ticket_ref, exc_info=True
            )
            comments = []
        if self._gateway is None:
            return fallback_summary(comments)
        return await summarize_activity(self._gateway, self._model, comments)

    # -- entry point -------------------------------------------------------

    async def notify(self, event: TicketEvent) -> bool:
        """Post or update this ticket's card. Returns True if Telegram was touched."""
        try:
            return await self._notify_inner(event)
        except Exception:
            LOGGER.warning(
                "ticket update: notification failed for {} (non-fatal)",
                event.ticket_ref,
                exc_info=True,
            )
            return False

    async def _notify_inner(self, event: TicketEvent) -> bool:
        if not self._bot_token:
            LOGGER.debug("ticket update: no bot token configured -- skipping")
            return False

        ticket = await self._tickets.get_by_ref(event.ticket_ref)
        if ticket is None:
            LOGGER.debug("ticket update: unknown ref {}", event.ticket_ref)
            return False

        if not await self._is_worth_posting(event):
            LOGGER.debug("ticket update: {} judged not significant", event.ticket_ref)
            return False

        anchor = await self._deliveries.latest_for_ticket(ticket.id)
        if not anchor:
            # Never announced on Telegram (e.g. a ticket filed by a sweep).
            # There is no thread to update and no defensible place to start one.
            LOGGER.debug("ticket update: no delivery anchor for {}", event.ticket_ref)
            return False

        chat_id = str(anchor.get("external_chat_id") or "")
        topic_id = anchor.get("external_topic_id")
        anchor_message_id = int(anchor.get("external_message_id") or 0)
        if not chat_id or not anchor_message_id:
            return False

        activity = await self._activity_line(event)
        card = render_update_card(
            ticket_ref=ticket.ticket_ref or event.ticket_ref,
            summary=ticket.summary or "",
            status=event.to_status or ticket.status,
            activity=activity,
            url=event.ticket_url,
        )
        text = convert_github_to_telegram_markdown(card)

        gap = await self._watermark.messages_since(chat_id, anchor_message_id)
        if gap <= SCROLL_THRESHOLD:
            if await self._edit(chat_id, anchor_message_id, text):
                LOGGER.info(
                    "ticket update: edited {} in place (chat={} msg={} gap={})",
                    event.ticket_ref,
                    chat_id,
                    anchor_message_id,
                    gap,
                )
                # The anchor is unchanged, so there is no new receipt to write.
                return True
            LOGGER.warning(
                "ticket update: edit of msg={} rejected -- posting a reply instead",
                anchor_message_id,
            )

        message_id = await self._send(chat_id, text, topic_id, anchor_message_id)
        if message_id is None:
            LOGGER.warning("ticket update: send failed for {}", event.ticket_ref)
            return False

        try:
            await self._deliveries.record(
                ticket_id=ticket.id,
                escalation_id=None,
                purpose="update",
                external_chat_id=chat_id,
                external_topic_id=str(topic_id) if topic_id is not None else None,
                external_message_id=int(message_id),
            )
        except Exception:
            LOGGER.warning(
                "ticket update: failed to record receipt for {} -- the next update "
                "will anchor on the older message",
                event.ticket_ref,
                exc_info=True,
            )
        LOGGER.info(
            "ticket update: posted reply for {} (chat={} msg={} gap={})",
            event.ticket_ref,
            chat_id,
            message_id,
            gap,
        )
        return True
```

- [ ] **Step 4: Confirm the markdown helper's import path**

Run: `cd chat_orchestrator && python -c "from shared.utils.markdown import convert_github_to_telegram_markdown; print('ok')"`
Expected: `ok`. If it fails, find the real module with `grep -rn "def convert_github_to_telegram_markdown" --include="*.py" ..` from the repo root and fix the import in `update_notifier.py`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_update_notifier.py -v`
Expected: PASS — 6 passed

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/update_notifier.py
git add -f chat_orchestrator/tests/services/ticketing/test_update_notifier.py
git commit -m "feat(ticketing): add scroll-aware ticket update notifier"
```

---

### Task 6: Fire the notifier from TicketService.transition_to_done

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/service.py:310-325`
- Test: `chat_orchestrator/tests/services/ticketing/test_service.py` (append)

This single hook covers the escalation close button, `/notify` with `close: true`, and the Jira webhook closure.

**Read the existing `TestTransitionToDone` class (line 493) before writing anything.** It documents an invariant this task must preserve: for the **internal** backend, `TicketService` must *not* call `transition_to_done_by_ref` itself, because `InternalTicketBackend.transition_to_done` already went through the shared repository and a second write would bump `resolved_at` twice.

That invariant dictates where the "did this call close it" signal comes from:

| Backend | Who writes the canonical row | Flip signal |
|---|---|---|
| internal | `InternalTicketBackend.transition_to_done` (via the shared repository) | the backend's own return value |
| jira | `TicketService`, because `JiraTicketBackend` only calls the Jira API | `transition_to_done_by_ref`'s return value |

So `TicketBackend.transition_to_done` gains a `bool` return. Getting this backwards is the one way to silently break the feature: if `TicketService` re-ran the repository write for internal tickets, the guarded UPDATE would find the row already `done`, report `False`, and internal closures would never be announced.

- [ ] **Step 1: Write the failing tests**

Add these to the existing `TestTransitionToDone` class in `chat_orchestrator/tests/services/ticketing/test_service.py`, keeping its two current tests untouched:

```python
    @pytest.mark.asyncio
    async def test_announces_an_internal_closure(self, monkeypatch):
        """Internal tickets take their flip signal from the backend, which is
        the only thing that wrote the canonical row."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        repository.records_by_ref["TKT-1"] = TicketRecord(
            id="ticket-1", ticket_ref="TKT-1", backend="internal",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, internal=internal, ticket_repository=repository)
        service._update_notifier = _Notifier()

        await service.transition_to_done("TKT-1")

        assert len(events) == 1
        assert events[0].ticket_ref == "TKT-1"
        assert events[0].kind == "transition"
        assert events[0].to_status == "done"
        # The invariant from this class's existing tests still holds.
        assert repository.transition_to_done_by_ref_calls == []

    @pytest.mark.asyncio
    async def test_announces_a_jira_closure(self, monkeypatch):
        """Jira tickets take their flip signal from the canonical write, since
        the Jira backend only talks to the Jira API."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        jira = _FakeBackend("jira")
        repository = _FakeTicketRepository()
        repository.records_by_ref["OPS-99"] = TicketRecord(
            id="ticket-1", ticket_ref="OPS-99", backend="jira",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, jira=jira, ticket_repository=repository)
        service._update_notifier = _Notifier()

        await service.transition_to_done("OPS-99")

        assert [e.ticket_ref for e in events] == ["OPS-99"]

    @pytest.mark.asyncio
    async def test_stays_silent_on_a_redundant_jira_close(self, monkeypatch):
        """A retried Jira webhook must not re-announce an already-closed ticket."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        jira = _FakeBackend("jira")
        repository = _FakeTicketRepository()
        repository.records_by_ref["OPS-99"] = TicketRecord(
            id="ticket-1", ticket_ref="OPS-99", backend="jira",
            summary="x", created_via="notification", provisioning_state="active",
        )
        repository.transition_returns = False  # row was already done
        service = _make_service(None, jira=jira, ticket_repository=repository)
        service._update_notifier = _Notifier()

        await service.transition_to_done("OPS-99")

        assert events == []

    @pytest.mark.asyncio
    async def test_stays_silent_on_a_redundant_internal_close(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        internal = _FakeBackend("internal")
        internal.transition_returns = False  # row was already done
        repository = _FakeTicketRepository()
        repository.records_by_ref["TKT-1"] = TicketRecord(
            id="ticket-1", ticket_ref="TKT-1", backend="internal",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, internal=internal, ticket_repository=repository)
        service._update_notifier = _Notifier()

        await service.transition_to_done("TKT-1")

        assert events == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing/test_service.py -k transition_to_done -v`
Expected: FAIL — the notifier is never called, so `events` is empty.

- [ ] **Step 3: Make `transition_to_done` report whether it closed the ticket**

Three files, one signature change.

In `backend.py`, update the Protocol (line 165):

```python
    async def transition_to_done(self, ref: str) -> bool:
        """Mark a ticket as done/resolved.

        Returns True only when *this* call is what closed it, so callers can
        announce a closure exactly once across retries and racing paths.
        Non-blocking -- failures are logged and reported as False, not raised.
        """
        ...
```

In `internal_backend.py`, replace `transition_to_done` (line 124-132):

```python
    async def transition_to_done(self, ref: str) -> bool:
        """Mark a ticket as done.

        The canonical repository owns the state transition and resolved time,
        and its guarded UPDATE is what makes this idempotent -- so its return
        value is the authoritative "this call closed it" signal.
        """
        try:
            return await self._tickets.transition_to_done_by_ref(ref)
        except Exception as e:
            LOGGER.warning("Failed to transition internal ticket {} to done: {}", ref, e)
            return False
```

In `jira_backend.py`, make `transition_to_done` (line 422) return `True` when the Jira transition call succeeded and `False` on any handled failure. Read the existing body and add returns to its existing branches — do not restructure it. Its return value is advisory only; `TicketService` overrides it with the canonical write below.

- [ ] **Step 4: Add the notifier to `TicketService`**

In `service.py`'s `__init__` (line 44-73), after the existing `self._attachments = ...` line, add:

```python
        self._deliveries = DeliveryRepository(get_client=self._raw_client)
        self._update_notifier: Optional[Any] = None
```

and add the import at the top of the file:

```python
from .delivery_repository import DeliveryRepository
```

Then add the lazily-built notifier and a public entry point:

```python
    @property
    def _notifier(self) -> Any:
        """Built on first use so constructing a TicketService stays cheap --
        several call sites build one per request, and most never notify."""
        if self._update_notifier is None:
            from shared.llm import get_default_generation_gateway

            from .chat_watermark import ChatWatermarkRepository
            from .update_notifier import TicketUpdateNotifier

            try:
                gateway = get_default_generation_gateway()
            except Exception:
                LOGGER.warning("ticket update: no LLM gateway -- summaries degrade", exc_info=True)
                gateway = None

            self._update_notifier = TicketUpdateNotifier(
                tickets=self._tickets,
                deliveries=self._deliveries,
                watermark=ChatWatermarkRepository(get_client=self._raw_client),
                bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
                gateway=gateway,
                model=get_settings().gemini.model,
            )
        return self._update_notifier

    async def notify_ticket_event(self, event: Any) -> bool:
        """Post a ticket update card. The public entry point for callers
        outside this service (e.g. the Jira comment webhook).

        Short-circuits before building the notifier when there is no bot
        token, which keeps unrelated unit tests from constructing an LLM
        gateway they never use.
        """
        if not os.getenv("TELEGRAM_BOT_TOKEN", ""):
            return False
        return await self._notifier.notify(event)
```

Ensure `service.py` imports `os` and `get_settings` — check the existing imports and add only what is missing. `get_settings` is imported in `correlator.py:385` as a reference for the right module path.

- [ ] **Step 5: Rewrite `transition_to_done`**

Replace lines 310-325:

```python
    async def transition_to_done(self, ref: str) -> None:
        backend = await self._backend_for_ref(ref)
        flipped = bool(await backend.transition_to_done(ref))

        if backend is self._jira:
            # Unlike the internal backend (which persists via the repository it
            # shares with this service), jira_backend.transition_to_done() only
            # calls the Jira transitions API -- it has no repository reference,
            # so the canonical row would otherwise stay "open" forever. That
            # guarded write is also the authoritative flip signal here.
            try:
                flipped = await self._tickets.transition_to_done_by_ref(ref)
            except Exception:
                flipped = False
                LOGGER.warning(
                    "transition_to_done: failed to persist canonical status for jira ticket {}",
                    ref,
                    exc_info=True,
                )

        if flipped:
            from .update_notifier import TicketEvent

            await self.notify_ticket_event(
                TicketEvent(ticket_ref=ref, kind="transition", to_status="done")
            )
```

The internal branch deliberately does **not** call `transition_to_done_by_ref` — see the table above and the existing `TestTransitionToDone` docstring.

`notify_ticket_event`'s early return on a missing bot token is why the two pre-existing tests in `TestTransitionToDone` keep passing untouched: with no `TELEGRAM_BOT_TOKEN` in the environment they never construct a notifier or an LLM gateway. The four new tests set the token via `monkeypatch` and inject a fake notifier, so they never reach the real one either.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd chat_orchestrator && python -m pytest tests/services/ticketing -v`
Expected: PASS — all green, including the four new tests and the two pre-existing `TestTransitionToDone` tests, which must still pass unmodified.

- [ ] **Step 7: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/service.py \
        chat_orchestrator/orchestrator/services/ticketing/backend.py \
        chat_orchestrator/orchestrator/services/ticketing/internal_backend.py \
        chat_orchestrator/orchestrator/services/ticketing/jira_backend.py
git add -f chat_orchestrator/tests/services/ticketing/test_service.py
git commit -m "feat(ticketing): announce ticket closures from TicketService"
```

---

### Task 7: Jira webhook — status categories, comment mirroring, delegated posting

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/escalation_service.py:3116-3230`
- Test: `chat_orchestrator/tests/services/test_escalation_jira_webhook.py` (append)

Three changes: detect transitions by `statusCategory` instead of literal strings, mirror Jira comments into `ticket_comments` so closing summaries work for Jira tickets too, and hand the Telegram post to the notifier.

- [ ] **Step 1: Write the failing tests**

Append to `chat_orchestrator/tests/services/test_escalation_jira_webhook.py`:

```python
@pytest.mark.asyncio
async def test_closure_detected_for_a_custom_done_category_status():
    """A workflow whose done status is named "Resolved" must still close.

    The old literal match on "Done"/"Closed" silently ignored these.
    """
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": "OPS-5", "fields": {"status": {"statusCategory": {"key": "done"}}}},
        "changelog": {"items": [{"field": "status", "toString": "Resolved"}]},
    }
    assert _is_closure_event(payload) is True


@pytest.mark.asyncio
async def test_non_status_changes_are_not_closures():
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": "OPS-5", "fields": {"status": {"statusCategory": {"key": "new"}}}},
        "changelog": {"items": [{"field": "assignee", "toString": "Ada"}]},
    }
    assert _is_closure_event(payload) is False


@pytest.mark.asyncio
async def test_status_change_to_a_non_done_category_is_not_a_closure():
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": "OPS-5", "fields": {"status": {"statusCategory": {"key": "indeterminate"}}}},
        "changelog": {"items": [{"field": "status", "toString": "In Progress"}]},
    }
    assert _is_closure_event(payload) is False
```

Import the helper at the top of the test file:

```python
from orchestrator.services.escalation_service import _is_closure_event
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd chat_orchestrator && python -m pytest tests/services/test_escalation_jira_webhook.py -v`
Expected: FAIL — `ImportError: cannot import name '_is_closure_event'`

- [ ] **Step 3: Add the closure detector to `escalation_service.py`**

Add at module level, near the other module-level helpers:

```python
def _is_closure_event(payload: Dict[str, Any]) -> bool:
    """Whether a jira:issue_updated payload represents a move into Done.

    Uses Jira's ``statusCategory`` rather than status *names*: category keys
    are fixed ("new", "indeterminate", "done") while names are per-workflow,
    so a project whose done status is called "Resolved" or "Completed" is
    handled without configuration. This mirrors the check already used when
    reading issue status directly (see ``_parse_issue_status``).
    """
    changed_status = any(
        item.get("field") == "status" for item in payload.get("changelog", {}).get("items", [])
    )
    if not changed_status:
        return False
    category = (
        payload.get("issue", {})
        .get("fields", {})
        .get("status", {})
        .get("statusCategory", {})
        .get("key", "")
    )
    return category == "done"
```

- [ ] **Step 4: Replace the closure branch of `handle_jira_issue_updated`**

Replace lines 3187-3227 (the `closed = any(...)` block through the hardcoded `_send_telegram_message` call) with:

```python
        if not _is_closure_event(payload):
            return

        issue_key = payload.get("issue", {}).get("key", "")
        issue_fields = payload.get("issue", {}).get("fields", {})
        ctx = await self._resolve_escalation_context_for_jira_key(issue_key, issue_fields)
        if ctx is None:
            return

        mapping = ctx["mapping"]
        jira_orgs = ctx["jira_orgs"]
        escalation_org_name = ctx["escalation_org_name"]

        # Close the canonical ticket record. transition_to_done() reports
        # whether this call is what closed it and posts the update card
        # itself, so a retried webhook delivery cannot double-announce.
        ticket_ref = mapping.get("ticket_ref")
        if ticket_ref:
            try:
                await self._tickets.transition_to_done(ticket_ref)
            except Exception:
                LOGGER.warning(
                    "Jira webhook closure: failed to close canonical ticket {}",
                    ticket_ref,
                    exc_info=True,
                )

        notify_customer = self._single_matching_org(jira_orgs, escalation_org_name)
        await self.close_escalation_by_mapping(mapping=mapping, notify_customer=notify_customer)
```

The hardcoded `✅ Jira ticket *{issue_key}* has been closed.` send is deleted — that is the "direct Jira update" being retired. `escalation_topic_id` is no longer read here; remove the now-unused local.

- [ ] **Step 5: Mirror Jira comments into `ticket_comments` and notify**

In `handle_jira_comment`, after `ctx` is resolved and before the existing escalation-group post, insert:

```python
        # Mirror into the canonical comment log so closing summaries read from
        # one table regardless of backend -- ticket_comments.source already
        # reserves 'jira' for exactly this.
        ticket_ref = mapping.get("ticket_ref")
        if ticket_ref:
            try:
                await self._tickets.add_comment(ticket_ref, comment_text, public=is_public)
            except Exception:
                LOGGER.warning(
                    "Jira webhook: failed to mirror comment for {}", ticket_ref, exc_info=True
                )
            try:
                from orchestrator.services.ticketing.update_notifier import TicketEvent

                await self._tickets.notify_ticket_event(
                    TicketEvent(
                        ticket_ref=ticket_ref,
                        kind="comment",
                        comment_body=comment_text,
                        comment_author=author_name,
                        ticket_url=self._jira_browse_url(issue_key),
                    )
                )
            except Exception:
                LOGGER.warning(
                    "Jira webhook: comment notification failed for {}", ticket_ref, exc_info=True
                )
```

Move the `author_name = comment.get(...)` assignment above this block so it is defined. Add a small helper to `EscalationService` if `_jira_browse_url` does not already exist:

```python
    def _jira_browse_url(self, issue_key: str) -> Optional[str]:
        base = os.getenv("JIRA_BASE_URL", "").rstrip("/")
        return f"{base}/browse/{issue_key}" if base and issue_key else None
```

Check the real env var name first: `grep -n "JIRA_BASE_URL\|JIRA_URL" chat_orchestrator/orchestrator/services/ticketing/jira_backend.py` and use whatever that file uses.

- [ ] **Step 6: Run tests**

Run: `cd chat_orchestrator && python -m pytest tests/services/test_escalation_jira_webhook.py tests/services/ticketing -v`
Expected: PASS. Existing tests asserting the old literal-string closure still pass (`"Done"` has category `done`). If a test asserts the exact text `"✅ Jira ticket ... has been closed."`, update it to assert the notifier was invoked instead.

- [ ] **Step 7: Commit**

```bash
git add chat_orchestrator/orchestrator/services/escalation_service.py
git add -f chat_orchestrator/tests/services/test_escalation_jira_webhook.py
git commit -m "feat(jira): detect closures by status category and route updates through the notifier"
```

---

### Task 8: Close the MCP `change_status` gap

**Files:**
- Modify: `chat_orchestrator/orchestrator/api/app.py` (new endpoint)
- Modify: `mcp_servers/servers/jira_server/jira_mcp_server.py:188-202, 2200-2213`
- Modify: `.do/app.example.yaml` (tools-service env)

**Read this before starting.** The MCP servers run in the separate `tools-service` component (`.do/app.example.yaml:461`), which has no `CHAT_ORCHESTRATOR_URL` today — only `anansi-app` does (line 510). So this task needs a deploy-config change to take effect in production, unlike Tasks 1-8. It also fixes an existing architectural violation: `close_internal_ticket` writes `tickets` directly, despite `TicketRepository` being documented as "the sole writer".

**If the operator would rather not touch the tools-service env right now, skip this task.** Tasks 1-8 are complete and shippable on their own; the only uncovered path is "user asks the bot in chat to close an internal ticket", where the user already gets an inline confirmation.

- [ ] **Step 1: Add the endpoint to `app.py`**

Place it next to the other authenticated internal endpoints (near `list_scheduled_jobs`, ~line 551):

```python
@app.post("/internal/tickets/{ticket_ref}/close")
async def close_ticket_internal(ticket_ref: str, request: Request) -> JSONResponse:
    """Close a ticket through TicketService, from the tools-service process.

    The Jira MCP server used to UPDATE ``tickets`` directly, which bypassed
    TicketRepository (documented as the sole writer) and, once the update
    notifier existed, meant a bot-initiated close was the one transition that
    never reached Telegram. Routing it here restores both.

    Authentication: X-Api-Key, same as the other /internal and /api/v1 routes.
    """
    get_auth_method(request)

    from orchestrator.services.supabase_client import get_supabase_client
    from orchestrator.services.ticketing.service import TicketService

    try:
        await TicketService(get_supabase_client=get_supabase_client).transition_to_done(ticket_ref)
    except Exception as exc:
        logger.exception("Internal close failed for {!r}", ticket_ref)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
    return JSONResponse(status_code=200, content={"ok": True, "ticket_ref": ticket_ref})
```

- [ ] **Step 2: Replace `close_internal_ticket` in the MCP server**

Replace the body of `close_internal_ticket` (`jira_mcp_server.py:188-202`) with an HTTP call:

```python
    async def close_internal_ticket(self, ticket_ref: str) -> bool:
        """Close an internal ticket via the orchestrator.

        Deliberately not a direct DB write: TicketRepository is the sole
        writer for `tickets`, and closing through the orchestrator is what
        triggers the Telegram update card. Falls back to reporting failure
        rather than writing behind the orchestrator's back.
        """
        base = os.getenv("CHAT_ORCHESTRATOR_URL", "").rstrip("/")
        api_key = os.getenv("API_KEY", "")
        if not base:
            logger.warning("CHAT_ORCHESTRATOR_URL not set — cannot close internal ticket %s", ticket_ref)
            return False
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base}/internal/tickets/{ticket_ref}/close",
                    headers={"X-Api-Key": api_key},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            "Internal ticket close for %s returned HTTP %s", ticket_ref, response.status
                        )
                        return False
                    return True
        except Exception as exc:
            logger.warning("Internal ticket close failed for %s: %s", ticket_ref, exc)
            return False
```

Confirm the API-key env var name the tools-service already uses — `grep -rn "API_KEY" mcp_servers/ --include="*.py" | grep -v __pycache__ | head` — and match it rather than introducing a new one.

- [ ] **Step 3: Add the env var to the tools-service component**

In `.do/app.example.yaml`, under the `tools-service` component's `envs:`:

```yaml
      - key: CHAT_ORCHESTRATOR_URL
        value: "${chat-orchestrator.PRIVATE_URL}"
```

- [ ] **Step 4: Verify the module still imports**

Run: `cd chat_orchestrator && python -c "import mcp_servers.servers.jira_server.jira_mcp_server" 2>&1 | tail -3`
Expected: no `SyntaxError`. An `ImportError` for optional deps is acceptable; a syntax error is not.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/api/app.py \
        mcp_servers/servers/jira_server/jira_mcp_server.py \
        .do/app.example.yaml
git commit -m "fix(ticketing): route MCP internal-ticket closes through TicketService"
```

---

### Task 9: Document the Jira webhook setup

**Files:**
- Modify: `README.md` (insert before the `POST /chat/notify` section, ~line 629)

No existing doc mentions `/webhook/jira` — `grep -rln "webhook/jira" --include="*.md" .` returns nothing. A future adopter currently has no way to discover it.

- [ ] **Step 1: Add the section**

```markdown
### Jira webhook (`POST /webhook/jira`)

Anansi is the single author of ticket updates in Telegram. Jira notifies
Anansi; Anansi decides what, where, and whether to post. **Turn off any
direct Jira → Telegram integration** (native, Automation rule, or n8n flow)
before enabling this, or every ticket change is announced twice.

**1. Set the shared secret** on the `chat-orchestrator` component:

```bash
JIRA_WEBHOOK_SECRET=<a long random string>
```

The endpoint is fail-closed: with no secret configured it rejects every
request rather than accepting unauthenticated ones.

**2. Create the webhook** in Jira: *Settings → System → Webhooks → Create*.

| Field | Value |
|---|---|
| URL | `https://<your-anansi-host>/webhook/jira` |
| Secret | the same `JIRA_WEBHOOK_SECRET` value |
| Issue events | **Issue updated**, **Comment created** |
| JQL filter | `project = OPS` (match your `JIRA_PROJECT_KEY`) |

Jira Cloud signs the request body with HMAC-SHA256 and sends the digest as
`X-Hub-Signature: sha256=<hex>`. Anansi verifies it with
`hmac.compare_digest` and returns 401 on a mismatch.

**Both event types are required:**

- **Comment created** — public ("Reply to customer") comments are relayed to
  the escalation group, forwarded to the customer when exactly one
  organization matches, and mirrored into `ticket_comments` so closing
  summaries can read Jira and internal ticket history from one table. The
  bot's own comments are filtered out by author email, so no loop forms.
- **Issue updated** — status transitions. Closure is detected via Jira's
  `statusCategory` (`done`), not status *names*, so custom workflow statuses
  like "Resolved" or "Completed" work without configuration.

**What gets posted.** On a status transition, and on any comment an LLM
judges operationally significant, Anansi renders a ticket card — reference,
status, summary, and a short summary of recent comments — and places it
against the ticket's existing Telegram message: edited in place while that
message is still on screen, or posted as a fresh reply once roughly five
messages have gone by. Internal (Jira-less) tickets produce the identical
card from the same code path, so behavior does not change if you later turn
Jira off.

**Optional, for clickable ticket links:**

```bash
JIRA_BASE_URL=https://<your-site>.atlassian.net
```

**Verifying it works.** After saving the webhook, transition a test issue and
check the orchestrator logs:

```bash
doctl apps logs <app-id> anansi-bot --type run --tail 100 | grep -i "jira webhook\|ticket update"
```

A `Jira webhook HMAC mismatch` line means the secret differs between Jira and
the app spec. Silence on a real transition usually means the issue has no
active escalation mapping — expected for tickets Anansi did not file.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document the Jira webhook setup and the ticket update flow"
```

---

### Task 10: Full verification

**Files:** none

- [ ] **Step 1: Run the full test suite**

Run: `cd chat_orchestrator && python -m pytest tests -v 2>&1 | tail -25`
Expected: no new failures versus the baseline captured when the worktree was created.

- [ ] **Step 2: Run pre-commit across everything**

Run from the worktree root:

```bash
pre-commit run --all-files
```

Expected: all hooks pass. **If `test-wiring` reports untracked files under any `tests/` directory**, that is the CLAUDE.md trap — those tests were silently dropped from their commits. Vet each for operator data, then:

```bash
git add -f chat_orchestrator/tests/services/ticketing/test_chat_watermark.py \
           chat_orchestrator/tests/services/ticketing/test_update_render.py \
           chat_orchestrator/tests/services/ticketing/test_update_notifier.py
```

- [ ] **Step 3: Confirm the tests actually reached the commits**

Run: `git log --stat --oneline -12 | grep -c "test_update_notifier\|test_update_render\|test_chat_watermark"`
Expected: at least `3`. A `0` means the force-adds did not happen — go back to Step 2.

- [ ] **Step 4: Re-run pre-commit to confirm clean**

Run: `pre-commit run --all-files`
Expected: all hooks pass, no untracked test files reported.

- [ ] **Step 5: Commit anything the hooks fixed**

```bash
git status --short
git commit -am "chore: pre-commit fixes" || echo "nothing to commit"
```

---

## Operator checklist (post-merge, not code)

1. No migration to apply — chat position is derived from existing tables. Nothing to do here.
2. **Turn off the existing direct Jira → Telegram integration** (native app, Automation rule, or n8n flow). This is what stops the double-posting; the code change alone does not.
3. Confirm `JIRA_WEBHOOK_SECRET` matches between the app spec and the Jira webhook config, and that the webhook subscribes to both *Issue updated* and *Comment created*.
4. Optionally set `JIRA_BASE_URL` for clickable ticket links.
5. If Task 8 was implemented, add `CHAT_ORCHESTRATOR_URL` to the `tools-service` component and redeploy.
