# Ticket Grid Field Org Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate the Jira/internal ticket "Grid" field when the exact `(chat_id, topic_id)` match fails, by falling back to the customer's organization (using its only grid, or a grid mentioned in their own chat history), and flag genuinely ambiguous cases for staff instead of leaving them silently blank.

**Architecture:** Two new pure/near-pure helpers (a text-mention fuzzy search in `shared/utils/grid_matcher.py`, and an org-resolution orchestrator in a new `chat_orchestrator/orchestrator/services/ticketing/grid_resolution.py` module), wired into `escalation_service.py`'s existing `track_as_ticket()` only when its current exact-match lookup finds nothing.

**Tech Stack:** Python, `rapidfuzz` (already a dependency), pytest (`asyncio_mode = "auto"`, no decorators needed).

**Spec:** `docs/superpowers/specs/2026-08-18-ticket-grid-field-org-fallback-design.md`

---

## Task 1: Free-text grid-name mention search

**Files:**
- Modify: `shared/utils/grid_matcher.py`
- Test: `shared/tests/test_grid_matcher.py` (new file)

- [ ] **Step 1: Write the failing tests**

Create `shared/tests/test_grid_matcher.py`:

```python
from shared.utils.grid_matcher import find_grid_mention


def test_find_grid_mention_matches_a_name_mentioned_in_a_longer_sentence():
    result = find_grid_mention(
        "The grid in KUDI is down right now", ["Kudi", "Site Alpha"]
    )
    assert result == "Kudi"


def test_find_grid_mention_matches_a_multi_word_name():
    result = find_grid_mention(
        "can someone check site alpha please, the meters are offline",
        ["Kudi", "Site Alpha"],
    )
    assert result == "Site Alpha"


def test_find_grid_mention_returns_none_when_no_candidate_is_mentioned():
    result = find_grid_mention(
        "my meter is broken and I have no power", ["Kudi", "Site Alpha"]
    )
    assert result is None


def test_find_grid_mention_matches_a_typo_that_still_clears_the_threshold():
    # "ste alpha" (missing the "i") scores exactly 90 against "Site Alpha" --
    # right at the threshold, proving this isn't an exact-match-only check.
    result = find_grid_mention(
        "the outage seems to be at ste alpha right now", ["Kudi", "Site Alpha"]
    )
    assert result == "Site Alpha"


def test_find_grid_mention_rejects_an_ambiguous_match():
    # "kudi" alone scores 100 against both candidates -- too close to call.
    result = find_grid_mention("kudi", ["Kudi A", "Kudi B"])
    assert result is None


def test_find_grid_mention_returns_none_for_empty_text():
    assert find_grid_mention("", ["Kudi"]) is None


def test_find_grid_mention_returns_none_for_empty_candidate_list():
    assert find_grid_mention("the grid in kudi is down", []) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd chat_orchestrator && uv run pytest ../shared/tests/test_grid_matcher.py -v`
Expected: FAIL with `ImportError: cannot import name 'find_grid_mention'`

- [ ] **Step 3: Implement**

Edit `shared/utils/grid_matcher.py` — add the new threshold constant next to the existing one:

Old:
```python
# Minimum similarity score (0-100) for a fuzzy match to be accepted
DEFAULT_FUZZY_THRESHOLD = 80
```

New:
```python
# Minimum similarity score (0-100) for a fuzzy match to be accepted
DEFAULT_FUZZY_THRESHOLD = 80

# find_grid_mention searches free text (a whole chat message, not a single
# extracted value), which carries far more incidental partial-match risk
# than find_best_grid_match's structured-field use case -- hence a higher bar.
TEXT_MENTION_FUZZY_THRESHOLD = 90
```

Then add the new function immediately after `find_best_grid_match`'s closing `except ImportError` block (i.e. right before `def parse_multi_site_args`):

Old:
```python
    except ImportError:
        LOGGER.warning("rapidfuzz not installed - fuzzy matching unavailable")
        return (None, False, 0)


def parse_multi_site_args(raw_args: str) -> List[str]:
```

New:
```python
    except ImportError:
        LOGGER.warning("rapidfuzz not installed - fuzzy matching unavailable")
        return (None, False, 0)


def find_grid_mention(
    text: str,
    valid_names: List[str],
    threshold: int = TEXT_MENTION_FUZZY_THRESHOLD,
) -> Optional[str]:
    """
    Search free-form text for a mention of one of the given grid names.

    ``find_best_grid_match`` uses ``token_sort_ratio``, built for comparing
    two short, comparable-length strings (e.g. correcting a provided grid
    name against a list of options) -- not for finding a name's approximate
    presence inside a much longer block of text. This uses
    ``fuzz.partial_ratio`` instead, which scores the best-matching substring
    of the longer text against each candidate name.

    Args:
        text: Free-form text to search (e.g. a customer's chat message).
        valid_names: Grid names to search for.
        threshold: Minimum similarity score (0-100) to accept a match.

    Returns:
        The matched name, or None if no candidate reaches the threshold, or
        if the top two candidates are too close to call (ambiguous).
    """
    if not text or not valid_names:
        return None

    try:
        from rapidfuzz import fuzz
    except ImportError:
        LOGGER.warning("rapidfuzz not installed - grid mention search unavailable")
        return None

    text_lower = text.lower()
    scored = sorted(
        (
            (name, fuzz.partial_ratio(name.lower(), text_lower))
            for name in valid_names
            if name
        ),
        key=lambda pair: pair[1],
        reverse=True,
    )
    if not scored:
        return None

    best_name, best_score = scored[0]
    if best_score < threshold:
        return None

    if len(scored) >= 2:
        _, second_score = scored[1]
        if best_score - second_score < 10:
            LOGGER.warning(
                f"Ambiguous grid mention in text: top candidates "
                f"{[name for name, _ in scored[:2]]} scored "
                f"{best_score:.0f}%, {second_score:.0f}%. Rejecting."
            )
            return None

    LOGGER.info(f"Found grid mention '{best_name}' in text (score={best_score:.0f}%)")
    return best_name


def parse_multi_site_args(raw_args: str) -> List[str]:
```

(`List`/`Optional` are already imported at the top of this file — no new imports needed.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd chat_orchestrator && uv run pytest ../shared/tests/test_grid_matcher.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add shared/utils/grid_matcher.py
git add -f shared/tests/test_grid_matcher.py  # tests/ is gitignored by default -- new files need -f
git commit -m "feat(ticketing): add free-text grid-name mention search

find_best_grid_match's token_sort_ratio compares two short strings and
isn't suited to finding a grid name mentioned somewhere inside a full
chat message. find_grid_mention uses partial_ratio instead, with a
higher threshold and the same ambiguity guard, for that job.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: Org/text-mention grid resolution orchestrator

**Files:**
- Create: `chat_orchestrator/orchestrator/services/ticketing/grid_resolution.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_grid_resolution.py` (new file)

- [ ] **Step 1: Write the failing tests**

Create `chat_orchestrator/tests/services/ticketing/test_grid_resolution.py`:

```python
"""resolve_grid_name -- org-based and text-mention grid fallback for ticket
creation, used when escalation_service.py's exact (chat_id, topic_id) match
finds nothing (e.g. a customer DM, which has no topic id to match on).
"""

from __future__ import annotations

from typing import List, Optional

from orchestrator.services.ticketing.grid_resolution import GridResolution, resolve_grid_name


class _FakeAuthService:
    def __init__(
        self, grid_names: Optional[List[str]] = None, error: Optional[Exception] = None
    ) -> None:
        self._grid_names = grid_names or []
        self._error = error
        self.calls: List[str] = []

    async def get_grid_names_for_organization(self, organization_id: str) -> List[str]:
        self.calls.append(organization_id)
        if self._error is not None:
            raise self._error
        return self._grid_names


def _user_message(content: str) -> dict:
    return {"role": "user", "content": content}


def _bot_message(content: str) -> dict:
    return {"role": "model", "content": content}


async def test_resolve_grid_name_returns_empty_when_organization_id_is_missing():
    result = await resolve_grid_name(organization_id=None, messages=[])

    assert result == GridResolution()


async def test_resolve_grid_name_uses_the_only_grid_when_org_has_exactly_one(monkeypatch):
    fake = _FakeAuthService(grid_names=["Kudi"])
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)

    result = await resolve_grid_name(organization_id=42, messages=[])

    assert result == GridResolution(grid_name="Kudi")
    assert fake.calls == ["42"]


async def test_resolve_grid_name_returns_empty_when_org_has_zero_grids(monkeypatch):
    fake = _FakeAuthService(grid_names=[])
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)

    result = await resolve_grid_name(organization_id=42, messages=[])

    assert result == GridResolution()


async def test_resolve_grid_name_matches_a_grid_mentioned_in_user_messages(monkeypatch):
    fake = _FakeAuthService(grid_names=["Kudi", "Site Alpha"])
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)
    messages = [_user_message("the grid in kudi is down")]

    result = await resolve_grid_name(organization_id=42, messages=messages)

    assert result == GridResolution(grid_name="Kudi")


async def test_resolve_grid_name_ignores_mentions_in_non_user_messages(monkeypatch):
    fake = _FakeAuthService(grid_names=["Kudi", "Site Alpha"])
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)
    messages = [_bot_message("I'll check on Kudi for you"), _user_message("ok thanks")]

    result = await resolve_grid_name(organization_id=42, messages=messages)

    assert result == GridResolution(candidates=["Kudi", "Site Alpha"])


async def test_resolve_grid_name_returns_candidates_when_multi_grid_org_has_no_text_match(
    monkeypatch,
):
    fake = _FakeAuthService(grid_names=["Kudi", "Site Alpha"])
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)
    messages = [_user_message("my meter is broken")]

    result = await resolve_grid_name(organization_id=42, messages=messages)

    assert result == GridResolution(candidates=["Kudi", "Site Alpha"])


async def test_resolve_grid_name_degrades_to_empty_when_auth_service_raises(monkeypatch):
    fake = _FakeAuthService(error=RuntimeError("db unreachable"))
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)

    result = await resolve_grid_name(organization_id=42, messages=[])

    assert result == GridResolution()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd chat_orchestrator && uv run pytest tests/services/ticketing/test_grid_resolution.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.services.ticketing.grid_resolution'`

- [ ] **Step 3: Implement**

Create `chat_orchestrator/orchestrator/services/ticketing/grid_resolution.py`:

```python
"""Org- and text-mention-based fallback grid resolution for ticket creation.

Used by escalation_service.py's track_as_ticket() when the exact
(customer_chat_id, customer_topic_id) match against
grids.internal_telegram_group_chat_id/thread_id finds nothing -- e.g. a
customer DM, or a group chat that isn't using Telegram's forum/topics
feature, neither of which has a topic id to match on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.utils.grid_matcher import find_grid_mention
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class GridResolution:
    """Result of the org/text-mention grid fallback.

    ``candidates`` is populated only in the genuinely ambiguous case (the
    organization has 2+ grids and no confident text match was found) --
    that's what the caller uses to flag the created ticket for a human
    instead of leaving it silently blank.
    """

    grid_name: Optional[str] = None
    candidates: List[str] = field(default_factory=list)


async def resolve_grid_name(
    *,
    organization_id: Optional[int],
    messages: List[Dict[str, Any]],
) -> GridResolution:
    """Resolve a grid name for an organization whose exact chat/topic match
    (escalation_service.py's own lookup, run before this is called) failed.

    - If the org has exactly one grid, use it.
    - If the org has 2+ grids, search recent customer ("user"-role) messages
      for a mention of one of them.
    - Otherwise, return the org's grid names as ``candidates`` so the caller
      can flag the ticket -- or nothing at all if the organization or its
      grids couldn't be resolved, since there's nothing meaningful to flag.

    Every failure degrades to an empty ``GridResolution()``; this is a
    data-quality enrichment and must never raise into ticket creation.
    """
    if not organization_id:
        return GridResolution()

    try:
        from shared.auth import get_auth_service

        grid_names = await get_auth_service().get_grid_names_for_organization(
            str(organization_id)
        )
    except Exception as e:
        LOGGER.debug(f"Could not fetch grids for organization {organization_id}: {e}")
        return GridResolution()

    if not grid_names:
        return GridResolution()

    if len(grid_names) == 1:
        return GridResolution(grid_name=grid_names[0])

    text = "\n".join(
        m.get("content", "") for m in messages if m.get("role") == "user" and m.get("content")
    )
    matched = find_grid_mention(text, grid_names)
    if matched:
        return GridResolution(grid_name=matched)

    return GridResolution(candidates=grid_names)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd chat_orchestrator && uv run pytest tests/services/ticketing/test_grid_resolution.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/grid_resolution.py
git add -f chat_orchestrator/tests/services/ticketing/test_grid_resolution.py  # new file under tests/ -- needs -f
git commit -m "feat(ticketing): add org/text-mention grid resolution fallback

resolve_grid_name: if an org has exactly one grid, use it; if it has
several, search the customer's own recent messages for a mention of
one of them; otherwise return the candidate list for the caller to
flag instead of silently leaving the ticket's Grid field blank.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: Wire the fallback into ticket creation

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/escalation_service.py:2117-2270` (`track_as_ticket`)
- Modify: `chat_orchestrator/tests/services/test_escalation_service_ticketing.py` (fixtures + new tests)

- [ ] **Step 1: Extend the `_FakeSupabase` test fixture**

`_FakeSupabase` currently hardcodes `get_session`'s return (no `organization_id`) and `get_messages`'s return (always `[]`). Both need to be configurable per-test.

Old:
```python
        self.reactivate_calls: List[str] = []
```

New:
```python
        self.reactivate_calls: List[str] = []
        # Grid-fallback fixtures (org id returned by get_session, and the
        # messages get_messages returns) -- configure per-test.
        self.session_organization_id: Optional[int] = None
        self.get_messages_return: List[Dict[str, Any]] = []
```

Old:
```python
    async def get_session(self, _sid):
        return SimpleNamespace(id=uuid.uuid4())
```

New:
```python
    async def get_session(self, _sid):
        return SimpleNamespace(id=uuid.uuid4(), organization_id=self.session_organization_id)
```

Old:
```python
    async def get_messages(self, **_k):
        return []
```

New:
```python
    async def get_messages(self, **_k):
        return self.get_messages_return
```

- [ ] **Step 2: Extend the `_FakeBackend` test fixture**

It needs to record the `TicketCreateRequest` it was called with (to assert on `grid_name`), and record `add_comment` calls (to assert on the flag comment).

Old:
```python
    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        ref: str = "REF-1",
        url: Optional[str] = None,
        dedup: Optional[str] = None,
    ) -> None:
        self.name = name
        self._available = available
        self._ref = ref
        self._url = url
        self._dedup = dedup
        self.create_calls = 0
```

New:
```python
    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        ref: str = "REF-1",
        url: Optional[str] = None,
        dedup: Optional[str] = None,
    ) -> None:
        self.name = name
        self._available = available
        self._ref = ref
        self._url = url
        self._dedup = dedup
        self.create_calls = 0
        self.create_requests: List[Any] = []
        self.add_comment_calls: List[tuple] = []
```

Old:
```python
    async def create_ticket(self, req) -> TicketResult:
        self.create_calls += 1
        return TicketResult(ref=self._ref, backend=self.name, url=self._url)
```

New:
```python
    async def create_ticket(self, req) -> TicketResult:
        self.create_calls += 1
        self.create_requests.append(req)
        return TicketResult(ref=self._ref, backend=self.name, url=self._url)
```

Old:
```python
    async def add_comment(self, ref, body, public: bool = False) -> bool:
        return True
```

New:
```python
    async def add_comment(self, ref, body, public: bool = False) -> bool:
        self.add_comment_calls.append((ref, body, public))
        return True
```

- [ ] **Step 3: Write the failing tests**

This file doesn't import `AsyncMock` yet -- add it to the existing import line:

Old:
```python
from typing import Any, Dict, List, Optional

from orchestrator.services.escalation_service import EscalationService
```

New:
```python
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

from orchestrator.services.escalation_service import EscalationService
```

Then insert the following immediately before `async def test_auto_create_jira_renders_link():` (`chat_orchestrator/tests/services/test_escalation_service_ticketing.py:745`):

```python
# ---------------------------------------------------------------------------
# track_as_ticket — grid org/text-mention fallback (customer_topic_id is
# None in _base_mapping, so the exact chat/topic match is always skipped
# here and every one of these exercises the new fallback tiers).
# ---------------------------------------------------------------------------


def _multi_grid_mapping(**overrides) -> Dict[str, Any]:
    mapping = _base_mapping()
    mapping.update(overrides)
    return mapping


async def test_track_as_ticket_uses_org_single_grid_fallback_when_topic_id_missing(
    monkeypatch,
):
    raw = _FakeRaw()
    raw.table("escalations").rows = [{"id": "mapping-abcd1234", "state": "processing"}]
    supa = _FakeSupabase(raw)
    supa.session_organization_id = 42
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, ref="OPS-200")
    internal = _FakeBackend("internal", ref="TKT-000002")
    _install_ticket_service(svc, jira, internal)
    svc._send_telegram_message = AsyncMock(return_value={"ok": True, "result": {"message_id": 1}})
    fake_auth = SimpleNamespace(get_grid_names_for_organization=AsyncMock(return_value=["Kudi"]))
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake_auth)

    result = await svc.track_as_ticket(escalation_mapping=_multi_grid_mapping())

    assert result["success"] is True
    assert jira.create_requests[0].grid_name == "Kudi"
    assert jira.add_comment_calls == []
    fake_auth.get_grid_names_for_organization.assert_awaited_once_with("42")


async def test_track_as_ticket_matches_grid_mentioned_in_chat_history_when_org_has_multiple(
    monkeypatch,
):
    raw = _FakeRaw()
    raw.table("escalations").rows = [{"id": "mapping-abcd1234", "state": "processing"}]
    supa = _FakeSupabase(raw)
    supa.session_organization_id = 42
    supa.get_messages_return = [{"role": "user", "content": "the grid in kudi is down"}]
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, ref="OPS-201")
    internal = _FakeBackend("internal", ref="TKT-000003")
    _install_ticket_service(svc, jira, internal)
    svc._send_telegram_message = AsyncMock(return_value={"ok": True, "result": {"message_id": 1}})
    fake_auth = SimpleNamespace(
        get_grid_names_for_organization=AsyncMock(return_value=["Kudi", "Site Alpha"])
    )
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake_auth)

    result = await svc.track_as_ticket(escalation_mapping=_multi_grid_mapping())

    assert result["success"] is True
    assert jira.create_requests[0].grid_name == "Kudi"
    assert jira.add_comment_calls == []


async def test_track_as_ticket_flags_ticket_for_staff_when_grid_is_still_ambiguous(monkeypatch):
    raw = _FakeRaw()
    raw.table("escalations").rows = [{"id": "mapping-abcd1234", "state": "processing"}]
    supa = _FakeSupabase(raw)
    supa.session_organization_id = 42
    supa.get_messages_return = [{"role": "user", "content": "my meter is broken"}]
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, ref="OPS-202")
    internal = _FakeBackend("internal", ref="TKT-000004")
    _install_ticket_service(svc, jira, internal)
    svc._send_telegram_message = AsyncMock(return_value={"ok": True, "result": {"message_id": 1}})
    fake_auth = SimpleNamespace(
        get_grid_names_for_organization=AsyncMock(return_value=["Kudi", "Site Alpha"])
    )
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake_auth)

    result = await svc.track_as_ticket(escalation_mapping=_multi_grid_mapping())

    assert result["success"] is True
    assert jira.create_requests[0].grid_name is None
    assert len(jira.add_comment_calls) == 1
    ref, body, public = jira.add_comment_calls[0]
    assert ref == "OPS-202"
    assert public is False
    assert "Kudi" in body and "Site Alpha" in body


async def test_track_as_ticket_leaves_grid_unset_with_no_flag_when_org_unresolvable():
    raw = _FakeRaw()
    raw.table("escalations").rows = [{"id": "mapping-abcd1234", "state": "processing"}]
    supa = _FakeSupabase(raw)
    supa.session_organization_id = None  # org itself never resolved
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, ref="OPS-203")
    internal = _FakeBackend("internal", ref="TKT-000005")
    _install_ticket_service(svc, jira, internal)
    svc._send_telegram_message = AsyncMock(return_value={"ok": True, "result": {"message_id": 1}})

    result = await svc.track_as_ticket(escalation_mapping=_multi_grid_mapping())

    assert result["success"] is True
    assert jira.create_requests[0].grid_name is None
    assert jira.add_comment_calls == []
```

- [ ] **Step 4: Run to verify the new tests fail**

Run: `cd chat_orchestrator && uv run pytest tests/services/test_escalation_service_ticketing.py -k track_as_ticket_uses_org_single_grid -v`
Expected: FAIL — `jira.create_requests[0].grid_name` is not `"Kudi"` (still `None`; the fallback isn't wired in yet)

- [ ] **Step 5: Implement**

Edit `chat_orchestrator/orchestrator/services/escalation_service.py`.

**5a.** Make `session_obj` safe to reference later (it's currently only assigned inside a nested `if`/`try`, so it can be undefined):

Old:
```python
            supabase_client = self._get_supabase_client()
            raw_messages: list = []
            if supabase_client:
                try:
                    session_obj = await supabase_client.get_session(session_id)
                    if session_obj and session_obj.id:
```

New:
```python
            supabase_client = self._get_supabase_client()
            raw_messages: list = []
            session_obj = None
            if supabase_client:
                try:
                    session_obj = await supabase_client.get_session(session_id)
                    if session_obj and session_obj.id:
```

**5b.** Add the org/text-mention fallback right after the existing exact-match block:

Old:
```python
            # 4. Resolve grid name from customer chat/topic for the JIRA grid field
            grid_name = None
            try:
                if customer_chat_id and customer_topic_id:
                    from shared.auth import get_auth_service

                    auth_pool = await get_auth_service()._get_db_pool()
                    async with auth_pool.acquire() as auth_conn:
                        grid_row = await auth_conn.fetchrow(
                            """
                            SELECT name FROM grids
                            WHERE internal_telegram_group_chat_id::text = $1
                              AND internal_telegram_group_thread_id::text = $2
                              AND deleted_at IS NULL
                            LIMIT 1
                            """,
                            str(customer_chat_id),
                            str(customer_topic_id),
                        )
                        if grid_row:
                            grid_name = grid_row["name"]
            except Exception as e:
                LOGGER.debug(f"Could not resolve grid for JIRA ticket: {e}")
```

New:
```python
            # 4. Resolve grid name from customer chat/topic for the JIRA grid field
            grid_name = None
            try:
                if customer_chat_id and customer_topic_id:
                    from shared.auth import get_auth_service

                    auth_pool = await get_auth_service()._get_db_pool()
                    async with auth_pool.acquire() as auth_conn:
                        grid_row = await auth_conn.fetchrow(
                            """
                            SELECT name FROM grids
                            WHERE internal_telegram_group_chat_id::text = $1
                              AND internal_telegram_group_thread_id::text = $2
                              AND deleted_at IS NULL
                            LIMIT 1
                            """,
                            str(customer_chat_id),
                            str(customer_topic_id),
                        )
                        if grid_row:
                            grid_name = grid_row["name"]
            except Exception as e:
                LOGGER.debug(f"Could not resolve grid for JIRA ticket: {e}")

            # 4b. No exact chat/topic registration (e.g. a DM, or a group not
            # using Telegram's forum/topics feature) -- fall back to the
            # customer's organization: use its only grid if it has just one,
            # or search their own recent messages for a mention of one of
            # several. Genuinely ambiguous cases (2+ grids, no text match)
            # are flagged on the ticket after creation instead of staying
            # silently blank -- see grid_flag_candidates below.
            grid_flag_candidates: List[str] = []
            if grid_name is None:
                from orchestrator.services.ticketing.grid_resolution import resolve_grid_name

                # getattr, not direct attribute access: session_obj is a real
                # ChatSessionModel in production (always has this field), but
                # some tests substitute a bare SimpleNamespace(id=...) that
                # doesn't -- this must degrade to None, not raise, either way.
                grid_resolution = await resolve_grid_name(
                    organization_id=getattr(session_obj, "organization_id", None),
                    messages=messages,
                )
                grid_name = grid_resolution.grid_name
                grid_flag_candidates = grid_resolution.candidates
```

**5c.** Flag the ticket for staff when grid stayed ambiguous, right after the existing `attach_ticket` best-effort block:

Old:
```python
            try:
                await self._escalations.attach_ticket(mapping_id, result.ticket_id)
            except Exception:
                LOGGER.warning(
                    "Could not attach canonical escalation {} to ticket {} -- "
                    "durable dedup won't find this ticket on a future retry",
                    mapping_id,
                    ticket_ref,
                    exc_info=True,
                )

            # escalations.state is now "tracked" (set by attach_ticket above),
```

New:
```python
            try:
                await self._escalations.attach_ticket(mapping_id, result.ticket_id)
            except Exception:
                LOGGER.warning(
                    "Could not attach canonical escalation {} to ticket {} -- "
                    "durable dedup won't find this ticket on a future retry",
                    mapping_id,
                    ticket_ref,
                    exc_info=True,
                )

            # Grid stayed unset and the org had 2+ candidates (see 4b above)
            # -- flag it on the ticket instead of leaving today's silent
            # blank. Best-effort: never turn an already-created ticket into
            # a reported failure over a missing internal comment.
            if grid_flag_candidates:
                candidates_text = ", ".join(grid_flag_candidates)
                LOGGER.warning(
                    "Ticket {} created with no Grid set -- organization has {} "
                    "candidate grids: {}",
                    ticket_ref,
                    len(grid_flag_candidates),
                    candidates_text,
                )
                try:
                    await self._tickets.add_comment(
                        ticket_ref,
                        "⚠️ Grid could not be auto-resolved for this ticket. "
                        f"This organization has {len(grid_flag_candidates)} grids: "
                        f"{candidates_text}. Please set the Grid field manually.",
                        public=False,
                    )
                except Exception:
                    LOGGER.warning(
                        "Could not post grid-ambiguity flag comment on {}",
                        ticket_ref,
                        exc_info=True,
                    )

            # escalations.state is now "tracked" (set by attach_ticket above),
```

- [ ] **Step 6: Run to verify the new tests pass**

Run: `cd chat_orchestrator && uv run pytest tests/services/test_escalation_service_ticketing.py -k "track_as_ticket_uses_org_single_grid or track_as_ticket_matches_grid_mentioned or track_as_ticket_flags_ticket_for_staff or track_as_ticket_leaves_grid_unset" -v`
Expected: 4 passed

- [ ] **Step 7: Run the full file to check for regressions**

The fixture changes in Steps 1-2 are shared by every test in this file — confirm nothing else broke.

Run: `cd chat_orchestrator && uv run pytest tests/services/test_escalation_service_ticketing.py -q`
Expected: all tests pass (same count as before this task, plus the 4 new ones)

- [ ] **Step 8: Commit**

```bash
git add chat_orchestrator/orchestrator/services/escalation_service.py
git add chat_orchestrator/tests/services/test_escalation_service_ticketing.py  # already tracked, no -f needed
git commit -m "feat(ticketing): wire grid org-fallback into ticket creation

track_as_ticket now falls back to resolve_grid_name() whenever the
exact (chat_id, topic_id) match finds nothing, and posts an internal
comment listing candidate grids when the org has several and none was
mentioned in the customer's own messages -- replacing today's
completely silent blank Grid field.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run every touched suite together**

```bash
cd chat_orchestrator && uv run pytest tests/services/test_escalation_service_ticketing.py tests/services/ticketing/test_grid_resolution.py -q
uv run pytest ../shared/tests/test_grid_matcher.py -q
```

Expected: all pass.

- [ ] **Step 2: Run pre-commit on every file this plan touched**

```bash
pre-commit run --files \
  shared/utils/grid_matcher.py \
  shared/tests/test_grid_matcher.py \
  chat_orchestrator/orchestrator/services/ticketing/grid_resolution.py \
  chat_orchestrator/tests/services/ticketing/test_grid_resolution.py \
  chat_orchestrator/orchestrator/services/escalation_service.py \
  chat_orchestrator/tests/services/test_escalation_service_ticketing.py
```

Expected: `ruff check` passes and the `test-wiring` hook confirms both new test files are tracked (this is the check that would have caught a missed `-f`).

- [ ] **Step 3: Confirm nothing was left uncommitted**

```bash
git status --short
```

Expected: empty (everything from Tasks 1-3 was committed at the end of each task).

No commit for this task — it's verification-only; if Step 2 or 3 finds something, fix it and fold the fix into the relevant task's commit via `git commit --amend` (only if not yet pushed) rather than adding a new one.
