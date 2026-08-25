# Knowledge Modules for Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator attach knowledge modules — including live/JIT ones (directory, graph, episodic, gdoc) — to a skill, reusing `prompt_knowledge_overrides` as-is (no migration), and fix the unreadable prompts picker in the Knowledge Modules modal along the way.

**Architecture:** A skill's pins live in the same `prompt_knowledge_overrides` table, keyed `f"skill:{skill_id}"`. A skill run composes inline + JIT knowledge once, at the start of the run, into the run's `system_instructions` (currently always `""`). One shared, entity-agnostic NiceGUI widget (search + tick list) is reused across all four prompt/skill × module-picking directions, replacing the current cramped chip-select for prompts.

**Tech Stack:** Python, NiceGUI (anansi_app admin UI), LangGraph/asyncio (chat_orchestrator), Supabase/Postgres, pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-knowledge-modules-for-skills-design.md`

---

## Before you start

This plan was written and verified inside a dedicated worktree:
`/Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills`
on branch `feat/knowledge-modules-for-skills` (branched fresh from `origin/main`,
which already includes PR #144's merged context-module dialog work).

Environments are already set up in this worktree:
- `chat_orchestrator/.venv` via `uv sync --extra dev` (covers `shared/` too).
- `.venv-validate` at the worktree root, matching CI's "Validate" job exactly:
  `pip install -r mcp_servers/requirements.txt pytest pytest-asyncio "markdown>=3.10.2"`
  (anansi_app's own `requirements.txt` is deliberately **not** installed — nicegui
  is stubbed by `anansi_app/tests/conftest.py`).

Baseline (already confirmed green before this plan's changes):
- `chat_orchestrator`: 2259 passed
- `shared`: 839 passed
- `anansi_app`: 426 passed

Commands used throughout this plan (run from the **worktree root** unless a
task says otherwise):

```bash
# chat_orchestrator + shared
cd chat_orchestrator
GOOGLE_API_KEY=test-key CHAT_DB_URL=https://placeholder.supabase.co \
CHAT_DB_SERVICE_KEY=placeholder MODEL_THINKING=gemini-pro-latest \
MODEL_FAST=gemini-flash-latest MODEL_LITE=gemini-2.5-flash-lite \
FALLBACK_MODEL=gemini-2.5-flash-lite uv run pytest tests/ -q
GOOGLE_API_KEY=test-key CHAT_DB_URL=https://placeholder.supabase.co \
CHAT_DB_SERVICE_KEY=placeholder MODEL_THINKING=gemini-pro-latest \
MODEL_FAST=gemini-flash-latest MODEL_LITE=gemini-2.5-flash-lite \
FALLBACK_MODEL=gemini-2.5-flash-lite uv run pytest ../shared -q -n auto
```

```bash
# anansi_app (from worktree root, NOT inside anansi_app/)
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests -q
```

Commit after every task (each task's last step says so explicitly).

---

## Phase 1 — Backend plumbing

Composes a skill's knowledge (inline + JIT) once per run and hands it to the
one slot that already exists for it. Fully testable and mergeable before any
UI exists — an operator could hand-insert a `prompt_knowledge_overrides` row
via SQL today and a skill run would already pick it up once this phase lands.

### Task 1: `skill_prompt_id` helper

**Files:**
- Modify: `shared/prompts/skills.py`
- Test: `shared/tests/test_skills_catalog.py`

- [x] **Step 1: Write the failing test**

Add to `shared/tests/test_skills_catalog.py` (new top-level test, anywhere
after the imports):

```python
from shared.prompts.skills import SKILL_PIN_PREFIX, skill_prompt_id


def test_skill_prompt_id_prefixes_the_skill_id():
    assert skill_prompt_id("11111111-1111-1111-1111-111111111111") == (
        "skill:11111111-1111-1111-1111-111111111111"
    )


def test_skill_pin_prefix_is_the_literal_prefix_used():
    assert SKILL_PIN_PREFIX == "skill:"
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && uv run pytest ../shared/tests/test_skills_catalog.py -q -k skill_prompt_id`
Expected: FAIL with `ImportError: cannot import name 'SKILL_PIN_PREFIX'`

- [x] **Step 3: Write minimal implementation**

In `shared/prompts/skills.py`, add right after the module docstring (before
`from __future__ import annotations` stays first; add these lines after the
existing imports, before the `Skill` dataclass):

```python
# Separate from skill_runner.py's SKILL_EXPERT_PREFIX (routes
# matched_expert_id in a different table, for a different purpose) even
# though both happen to be "skill:" -- this one is the key convention for
# prompt_knowledge_overrides.prompt_id, defined here in shared/ because
# anansi_app has no `orchestrator` package to import skill_runner from.
SKILL_PIN_PREFIX = "skill:"


def skill_prompt_id(skill_id: str) -> str:
    """The prompt_knowledge_overrides.prompt_id key for a skill's pins."""
    return f"{SKILL_PIN_PREFIX}{skill_id}"
```

Update the `__all__` list at the bottom of the file to include the two new names:

```python
__all__ = [
    "SKILL_CATALOG",
    "SKILL_PIN_PREFIX",
    "Skill",
    "SkillCatalogStore",
    "render_skill_catalog",
    "select_skills_for_context",
    "skill_prompt_id",
]
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd chat_orchestrator && uv run pytest ../shared/tests/test_skills_catalog.py -q`
Expected: PASS, full file green (no regressions in the rest of the file)

- [x] **Step 5: Commit**

```bash
git add shared/prompts/skills.py shared/tests/test_skills_catalog.py
git commit -m "feat(knowledge): add skill_prompt_id helper

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Extract `compose_knowledge_text`

**Files:**
- Modify: `shared/prompts/knowledge.py`
- Modify: `shared/prompts/core.py:168-193` (`PromptLibrary._compose_knowledge`)
- Test: `shared/tests/test_prompt_knowledge.py`
- Verify unchanged: `shared/tests/test_prompt_knowledge_wiring.py`

- [x] **Step 1: Write the failing test**

Add to `shared/tests/test_prompt_knowledge.py` (it already imports
`KnowledgeModule`, `budget_inlined`, `diff_prompt_pins`, `render_inlined`,
`select_for_prompt` from `shared.prompts.knowledge`, and has a `_module()`
helper — reuse both):

```python
from shared.prompts.knowledge import compose_knowledge_text  # add to the existing import block
from shared.prompts.types import RequestScope  # already imported at top of file


class _FakeKnowledgeStore:
    def __init__(self, modules, pins):
        self._modules = modules
        self._pins = pins

    def all_modules(self):
        return self._modules

    def overrides_for(self, prompt_id):
        return self._pins.get(prompt_id, {})


def test_compose_knowledge_text_renders_pinned_modules():
    # This file's _module(slug, tags=(...), scope=..., body="B") defaults
    # body to the literal string "B" -- asserted on directly below.
    module = _module("comms", tags=[])
    store = _FakeKnowledgeStore([module], {"skill:abc": {"comms": True}})

    text, used = compose_knowledge_text(store, "skill:abc", RequestScope())

    assert "B" in text
    assert used == ["comms"]


def test_compose_knowledge_text_returns_none_and_empty_when_nothing_pinned():
    store = _FakeKnowledgeStore([_module("comms")], {})

    text, used = compose_knowledge_text(store, "skill:abc", RequestScope())

    assert text is None
    assert used == []
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && uv run pytest ../shared/tests/test_prompt_knowledge.py -q -k compose_knowledge_text`
Expected: FAIL with `ImportError: cannot import name 'compose_knowledge_text'`

- [x] **Step 3: Write minimal implementation**

In `shared/prompts/knowledge.py`, add a new free function right after
`render_inlined` (before `class KnowledgeStore:`):

```python
def compose_knowledge_text(
    store: "KnowledgeStore",
    prompt_id: str,
    scope: RequestScope,
) -> Tuple[Optional[str], List[str]]:
    """Resolve, budget and render the inline (non-JIT) knowledge a pinning
    id (a prompt id or skill:<uuid>, both live in prompt_knowledge_overrides)
    uses. Never raises.

    Shared by PromptLibrary._compose_knowledge (prompts) and skill_runner.py
    (skills) -- there must be exactly one place this selection logic lives.
    """
    try:
        modules = store.all_modules()
        pins = store.overrides_for(prompt_id)
    except Exception:
        LOGGER.opt(exception=True).warning(
            f"Knowledge lookup failed for '{prompt_id}'; rendering without it"
        )
        return None, []

    chosen = [m for m in select_for_prompt(modules, pins, scope) if not m.is_jit]
    chosen = [m for m in chosen if m.body]

    inlined, _dropped = budget_inlined(chosen)
    return render_inlined(inlined), [m.slug for m in inlined]
```

`RequestScope` is already imported at the top of `knowledge.py`
(`from shared.prompts.types import RequestScope`) — this function only adds
new usages of names already imported in the file (`select_for_prompt`,
`budget_inlined`, `render_inlined`, `LOGGER`, `Tuple`, `Optional`, `List` are
all already present per the file's existing imports).

Now make `PromptLibrary._compose_knowledge` in `shared/prompts/core.py`
delegate to it, replacing its current body:

```python
    def _compose_knowledge(
        self, spec: PromptSpec, scope: RequestScope
    ) -> Tuple[Optional[str], List[str]]:
        """Resolve, budget and render this prompt's knowledge. Never raises."""
        if self._knowledge is None:
            return None, []
        return compose_knowledge_text(self._knowledge, spec.id, scope)
```

Add the import at the top of `shared/prompts/core.py`, in its existing
`from shared.prompts.knowledge import (...)` block (find it near the top of
the file and add `compose_knowledge_text` to that tuple of names).

- [x] **Step 4: Run test to verify it passes**

```bash
cd chat_orchestrator
uv run pytest ../shared/tests/test_prompt_knowledge.py -q
uv run pytest ../shared/tests/test_prompt_knowledge_wiring.py -q
```
Expected: PASS on both files — the wiring file's
`test_compose_uses_pins_not_tags` (which calls
`library._compose_knowledge(spec, RequestScope())` directly) must still pass
unchanged, proving the extraction didn't alter behavior.

- [x] **Step 5: Commit**

```bash
git add shared/prompts/knowledge.py shared/prompts/core.py shared/tests/test_prompt_knowledge.py
git commit -m "refactor(knowledge): extract compose_knowledge_text

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: `KNOWLEDGE_STORE` singleton

**Files:**
- Modify: `shared/prompts/knowledge.py`
- Modify: `shared/prompts/core.py:285-297` (`_build_default_library`)
- Test: `shared/tests/test_prompt_knowledge_store.py`

- [x] **Step 1: Write the failing test**

Add to `shared/tests/test_prompt_knowledge_store.py`:

```python
def test_knowledge_store_singleton_exists_and_is_a_knowledge_store():
    from shared.prompts.knowledge import KNOWLEDGE_STORE, KnowledgeStore

    assert isinstance(KNOWLEDGE_STORE, KnowledgeStore)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && uv run pytest ../shared/tests/test_prompt_knowledge_store.py -q -k singleton`
Expected: FAIL with `ImportError: cannot import name 'KNOWLEDGE_STORE'`

- [x] **Step 3: Write minimal implementation**

At the bottom of `shared/prompts/knowledge.py`, after the `KnowledgeStore`
class definition (mirroring `shared/prompts/skills.py`'s
`SKILL_CATALOG = SkillCatalogStore.from_env()` pattern exactly):

```python
# Module-level singleton, same reasoning as shared.prompts.skills.SKILL_CATALOG:
# built once at import time so its TTL cache is actually shared across every
# caller (PromptLibrary's own rendering AND skill_runner.py's composition),
# rather than each constructing an independent client + cache.
KNOWLEDGE_STORE = KnowledgeStore.from_env()
```

Add `"KNOWLEDGE_STORE"` to `knowledge.py`'s `__all__` if it has one (check —
if the file has no `__all__` list, skip this; don't introduce one just for
this).

In `shared/prompts/core.py`, change `_build_default_library()`:

```python
def _build_default_library() -> PromptLibrary:
    from shared.prompts.gdoc import GDocStore
    from shared.prompts.knowledge import KNOWLEDGE_STORE
    from shared.prompts.overrides import OverrideStore

    overrides = OverrideStore.from_env()
    gdoc_store = GDocStore(doc_id_for=overrides.doc_id_for)
    return PromptLibrary(
        overrides=overrides,
        gdoc_body_for=gdoc_store.body_for,
        invalidate_gdoc=gdoc_store.invalidate,
        knowledge=KNOWLEDGE_STORE,
    )
```

- [x] **Step 4: Run test to verify it passes**

```bash
cd chat_orchestrator
uv run pytest ../shared/tests/test_prompt_knowledge_store.py -q
uv run pytest ../shared -q -n auto
```
Expected: PASS, full `shared/` suite still green (839+2 passed).

- [x] **Step 5: Commit**

```bash
git add shared/prompts/knowledge.py shared/prompts/core.py shared/tests/test_prompt_knowledge_store.py
git commit -m "feat(knowledge): share one KnowledgeStore singleton

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: `resolve_jit_context_for` public wrapper

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/jit_context_resolver.py`
- Modify: `chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py:244-257`
- Test: `chat_orchestrator/tests/test_jit_context_resolver.py`

- [x] **Step 1: Write the failing test**

Add to `chat_orchestrator/tests/test_jit_context_resolver.py` (it already has
`_module()`, `_FakeStore`, `_FakeProvider` helpers and imports
`JitContextResolver`, `ProviderRegistry`, `ResolutionContext`, `RequestScope`
— reuse them):

```python
from unittest.mock import patch

from orchestrator.services.jit_context_resolver import resolve_jit_context_for


@pytest.mark.asyncio
async def test_resolve_jit_context_for_delegates_to_the_process_wide_resolver():
    module = _module("graph-overview", source="graph")
    store = _FakeStore([module], {"graph-overview": True})
    provider = _FakeProvider("graph", text="Entity types: grid, meter.")
    registry = ProviderRegistry()
    registry.register(provider)
    resolver = JitContextResolver(store=store, registry=registry)

    with patch(
        "orchestrator.services.jit_context_resolver.get_jit_resolver", return_value=resolver
    ):
        text, used = await resolve_jit_context_for("skill:abc", user_context=None, grid=None)

    assert "Entity types: grid, meter." in text
    assert used == ["graph-overview"]


@pytest.mark.asyncio
async def test_resolve_jit_context_for_fails_open_on_error():
    with patch(
        "orchestrator.services.jit_context_resolver.get_jit_resolver",
        side_effect=RuntimeError("boom"),
    ):
        text, used = await resolve_jit_context_for("skill:abc", user_context=None, grid=None)

    assert text == ""
    assert used == []
```

Confirmed against `shared/prompts/providers.py`: `ProviderRegistry()` takes
no constructor arguments, and `.register(provider)` (keyed internally off
`provider.source`) is the only way to add one — the test code above already
uses this exact shape, matching how every other test in this same file
registers a `_FakeProvider`.

- [x] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && uv run pytest tests/test_jit_context_resolver.py -q -k resolve_jit_context_for`
Expected: FAIL with `ImportError: cannot import name 'resolve_jit_context_for'`

- [x] **Step 3: Write minimal implementation**

In `chat_orchestrator/orchestrator/services/jit_context_resolver.py`, add
after `get_jit_resolver()`:

```python
async def resolve_jit_context_for(
    prompt_id: str,
    user_context: Optional[Any],
    grid: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Resolve provider-backed context modules for a pinning id. Fail open.

    Shared by prepare_context.py (a live conversation turn) and
    skill_runner.py (a skill run) -- both just need "this id's JIT modules,
    for this caller" and neither should re-implement the try/except.
    """
    try:
        from shared.prompts.providers import ResolutionContext

        ctx = ResolutionContext.from_user_context(user_context, grid=grid)
        return await get_jit_resolver().resolve_for_prompt(prompt_id, ctx)
    except Exception as e:
        LOGGER.warning(f"JIT context resolution failed (continuing without): {e}")
        return "", []
```

Add `Any` to the file's `from typing import ...` line if not already
present (it currently imports `List, Optional, Tuple` — check and extend to
`Any, List, Optional, Tuple`). Add `"resolve_jit_context_for"` to the file's
`__all__` list.

Now make `prepare_context.py`'s `_fetch_jit_context` delegate to it:

```python
async def _fetch_jit_context(
    prompt_id: str,
    user_context: Optional[UserContext],
    grid: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Resolve provider-backed context modules. Fail open."""
    from orchestrator.services.jit_context_resolver import resolve_jit_context_for

    return await resolve_jit_context_for(prompt_id, user_context, grid=grid)
```

(This makes the `ResolutionContext` import inside the old function body
redundant — remove it along with the old inline try/except, since
`resolve_jit_context_for` now owns that logic.)

- [x] **Step 4: Run test to verify it passes**

```bash
cd chat_orchestrator
export GOOGLE_API_KEY=test-key CHAT_DB_URL=https://placeholder.supabase.co CHAT_DB_SERVICE_KEY=placeholder MODEL_THINKING=gemini-pro-latest MODEL_FAST=gemini-flash-latest MODEL_LITE=gemini-2.5-flash-lite FALLBACK_MODEL=gemini-2.5-flash-lite
uv run pytest tests/test_jit_context_resolver.py -q
uv run pytest tests/ -q -k prepare_context
```
Expected: PASS on both.

- [x] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/jit_context_resolver.py \
        chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py \
        chat_orchestrator/tests/test_jit_context_resolver.py
git commit -m "refactor(jit): share resolve_jit_context_for between chat and skill runs

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: `resolve_scope_grid_from_user_context` public wrapper

**Files:**
- Modify: `shared/grid_scope.py`
- Modify: `chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py:224-241`
- Test: `shared/tests/test_grid_scope.py`

- [x] **Step 1: Write the failing test**

Add to `shared/tests/test_grid_scope.py`. Its existing tests patch
dependencies via `monkeypatch.setattr("<module path as a string>", ...)`
(e.g. `monkeypatch.setattr("shared.auth.get_auth_service", lambda: _Auth())`)
rather than importing and patching objects directly — the test below
matches that same string-path style, applied to `resolve_scope_grid` itself:

```python
from types import SimpleNamespace

from shared.grid_scope import resolve_scope_grid_from_user_context


@pytest.mark.asyncio
async def test_resolve_scope_grid_from_user_context_returns_none_for_no_context():
    assert await resolve_scope_grid_from_user_context(None) is None


@pytest.mark.asyncio
async def test_resolve_scope_grid_from_user_context_unpacks_the_right_fields(monkeypatch):
    captured = {}

    async def fake_resolve_scope_grid(chat_id, topic_id, organization_ids, is_staff):
        captured.update(
            chat_id=chat_id, topic_id=topic_id,
            organization_ids=organization_ids, is_staff=is_staff,
        )
        return "grid-a"

    monkeypatch.setattr("shared.grid_scope.resolve_scope_grid", fake_resolve_scope_grid)

    user_context = SimpleNamespace(
        chat_id="-100999", topic_id="42", organization_ids=["7"], is_staff=False,
    )
    result = await resolve_scope_grid_from_user_context(user_context)

    assert result == "grid-a"
    assert captured == {
        "chat_id": "-100999", "topic_id": "42",
        "organization_ids": ["7"], "is_staff": False,
    }
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && uv run pytest ../shared/tests/test_grid_scope.py -q -k resolve_scope_grid_from_user_context`
Expected: FAIL with `ImportError`

- [x] **Step 3: Write minimal implementation**

In `shared/grid_scope.py`, add after `resolve_scope_grid`:

```python
async def resolve_scope_grid_from_user_context(user_context: Optional[Any]) -> Optional[str]:
    """resolve_scope_grid, given a UserContext-shaped object instead of its
    four unpacked fields. Duck-typed, not imported from orchestrator.models
    -- this module lives in shared/, which must not depend on the
    orchestrator package. Returns None immediately for None (no conversation
    channel, no permission set to check).
    """
    if user_context is None:
        return None
    return await resolve_scope_grid(
        chat_id=getattr(user_context, "chat_id", None),
        topic_id=getattr(user_context, "topic_id", None),
        organization_ids=getattr(user_context, "organization_ids", None) or [],
        is_staff=bool(getattr(user_context, "is_staff", False)),
    )
```

Add `Any` to the file's `from typing import ...` line (currently
`Any, Dict, List, Optional, Tuple` — `Any` is likely already there; verify).
Add `"resolve_scope_grid_from_user_context"` to `__all__`.

Now make `prepare_context.py`'s `_resolve_scope_grid` delegate to it:

```python
async def _resolve_scope_grid(user_context: Optional[UserContext]) -> Optional[str]:
    """Which grid this conversation is about, for RequestScope. Never raises.

    Resolved once here and handed to _fetch_jit_context rather than being
    looked up inside it, so the gather below stays a single round of
    concurrent work. See shared/grid_scope.py for the two signals and why
    anything ambiguous stays None.
    """
    from shared.grid_scope import resolve_scope_grid_from_user_context

    return await resolve_scope_grid_from_user_context(user_context)
```

- [x] **Step 4: Run test to verify it passes**

```bash
cd chat_orchestrator
uv run pytest ../shared/tests/test_grid_scope.py -q
```
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add shared/grid_scope.py chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py \
        shared/tests/test_grid_scope.py
git commit -m "refactor(grid-scope): share resolve_scope_grid_from_user_context

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Wire composition into `skill_runner.py`

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/skill_runner.py`
- Test: `chat_orchestrator/tests/experts/test_skill_runner.py`

- [x] **Step 1: Write the failing tests**

Add a new test class to `chat_orchestrator/tests/experts/test_skill_runner.py`
(it already imports `AsyncMock, MagicMock, patch` from `unittest.mock`, and
`UserContext` from `orchestrator.models.schemas` — reuse both):

```python
class TestResolveSkillSystemInstructions:
    @pytest.mark.asyncio
    async def test_combines_inline_and_jit_text(self):
        from orchestrator.experts.skill_runner import _resolve_skill_system_instructions

        user_context = UserContext(
            user_id="u", user_email="ops@example.com", source="telegram",
            organization_ids=["7"],
        )
        with patch(
            "orchestrator.experts.skill_runner.compose_knowledge_text",
            return_value=("# Technical Knowledge\n\nInline body.", ["comms"]),
        ), patch(
            "orchestrator.experts.skill_runner.resolve_jit_context_for",
            new=AsyncMock(return_value=("# Live Context\n\nLive body.", ["entity-graph"])),
        ), patch(
            "orchestrator.experts.skill_runner.resolve_scope_grid_from_user_context",
            new=AsyncMock(return_value=None),
        ):
            result = await _resolve_skill_system_instructions(
                "11111111-1111-1111-1111-111111111111", user_context, {}
            )

        assert "Inline body." in result
        assert "Live body." in result

    @pytest.mark.asyncio
    async def test_empty_when_nothing_pinned(self):
        from orchestrator.experts.skill_runner import _resolve_skill_system_instructions

        with patch(
            "orchestrator.experts.skill_runner.compose_knowledge_text",
            return_value=(None, []),
        ), patch(
            "orchestrator.experts.skill_runner.resolve_jit_context_for",
            new=AsyncMock(return_value=("", [])),
        ), patch(
            "orchestrator.experts.skill_runner.resolve_scope_grid_from_user_context",
            new=AsyncMock(return_value=None),
        ):
            result = await _resolve_skill_system_instructions("id", None, {})

        assert result == ""

    @pytest.mark.asyncio
    async def test_prefers_an_explicit_grid_over_the_channel_resolver(self):
        from orchestrator.experts.skill_runner import _resolve_skill_system_instructions

        with patch(
            "orchestrator.experts.skill_runner.compose_knowledge_text", return_value=(None, [])
        ) as mock_compose, patch(
            "orchestrator.experts.skill_runner.resolve_jit_context_for",
            new=AsyncMock(return_value=("", [])),
        ) as mock_jit, patch(
            "orchestrator.experts.skill_runner.resolve_scope_grid_from_user_context",
            new=AsyncMock(return_value="should-not-be-used"),
        ) as mock_channel_grid:
            await _resolve_skill_system_instructions(
                "id", None, {"grid": {"grid_name": "grid-a"}}
            )

        mock_channel_grid.assert_not_awaited()
        assert mock_compose.call_args.args[2].grid == "grid-a"
        assert mock_jit.call_args.kwargs["grid"] == "grid-a"
```

These two assertions are pinned to the exact call shape Step 3 below
implements: `compose_knowledge_text(KNOWLEDGE_STORE, prompt_id, scope)`
(positional — `scope` is `args[2]`) and
`resolve_jit_context_for(prompt_id, user_context, grid=grid)` (`grid` passed
by keyword).

- [x] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && uv run pytest tests/experts/test_skill_runner.py -q -k ResolveSkillSystemInstructions`
Expected: FAIL with `ImportError: cannot import name '_resolve_skill_system_instructions'`

- [x] **Step 3: Write minimal implementation**

Add these imports near the top of `skill_runner.py` (alongside the existing
`from orchestrator...` / `from shared...` import block):

```python
from shared.prompts.knowledge import KNOWLEDGE_STORE, compose_knowledge_text
from shared.prompts.skills import skill_prompt_id
from shared.prompts.types import RequestScope
from shared.grid_scope import resolve_scope_grid_from_user_context
from orchestrator.services.jit_context_resolver import resolve_jit_context_for
```

Add this function before `run_skill_packet`:

```python
async def _resolve_skill_system_instructions(
    skill_id: str,
    user_context: Optional["UserContext"],
    skill_inputs: Dict[str, Any],
) -> str:
    """This skill's knowledge modules (inline + JIT), composed once for this
    run. Empty string when none are pinned or knowledge storage is
    unavailable -- matches the pre-existing default exactly.

    Grid preference: an explicit grid on the skill's own inputs first (a
    grid-anchored skill knows what it's about), falling back to the same
    chat-channel/permission-based resolver a live conversation uses. Never
    guessed -- see shared/grid_scope.py.
    """
    grid = (skill_inputs.get("grid") or {}).get("grid_name")
    if not grid:
        grid = await resolve_scope_grid_from_user_context(user_context)

    org_id_str = (
        user_context.organization_ids[0]
        if user_context and user_context.organization_ids
        else None
    )
    scope = RequestScope(organization_id=org_id_str, grid=grid)
    prompt_id = skill_prompt_id(skill_id)

    inline_text, _inline_used = compose_knowledge_text(KNOWLEDGE_STORE, prompt_id, scope)
    jit_text, _jit_used = await resolve_jit_context_for(prompt_id, user_context, grid=grid)

    return "\n\n".join(t for t in (inline_text, jit_text) if t)
```

Now wire it into `run_skill_packet`. Find the existing block:

```python
    expert_config = _SyntheticExpertConfig(
        expert_id=expert_id,
        display_name=skill.get("title") or skill_id,
    )

    org_id = None
    if user_context and user_context.organization_ids:
        org_id = int(user_context.organization_ids[0])

    metadata = state.get("metadata") or {}
    skill_inputs = metadata.get("skill_inputs") or {}
```

Replace it with (reordered so `skill_inputs` exists before it's needed, and
`system_instructions` is resolved before constructing `expert_config`):

```python
    org_id = None
    if user_context and user_context.organization_ids:
        org_id = int(user_context.organization_ids[0])

    metadata = state.get("metadata") or {}
    skill_inputs = metadata.get("skill_inputs") or {}

    system_instructions = await _resolve_skill_system_instructions(
        skill_id, user_context, skill_inputs
    )
    expert_config = _SyntheticExpertConfig(
        expert_id=expert_id,
        display_name=skill.get("title") or skill_id,
        system_instructions=system_instructions,
    )
```

Note `org_id_str` inside `_resolve_skill_system_instructions` is the
**string** form of `user_context.organization_ids[0]`, independent of the
`org_id = int(...)` variable a few lines below it (used for the packet row's
own `organization_id` column) — `RequestScope.organization_id` is
`Optional[str]` and does a string-equality check against `org:`-scoped
modules, so reusing the int-cast variable there would silently break that
match.

- [x] **Step 4: Run test to verify it passes**

```bash
cd chat_orchestrator
export GOOGLE_API_KEY=test-key CHAT_DB_URL=https://placeholder.supabase.co CHAT_DB_SERVICE_KEY=placeholder MODEL_THINKING=gemini-pro-latest MODEL_FAST=gemini-flash-latest MODEL_LITE=gemini-2.5-flash-lite FALLBACK_MODEL=gemini-2.5-flash-lite
uv run pytest tests/experts/test_skill_runner.py -q
uv run pytest tests/ -q
```
Expected: PASS, full chat_orchestrator suite still green (2259+ passed).

- [x] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/skill_runner.py \
        chat_orchestrator/tests/experts/test_skill_runner.py
git commit -m "feat(skills): compose inline + JIT knowledge into a skill run's system_instructions

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

**Phase 1 checkpoint:** run the full backend suite once more before moving
to the UI:

```bash
cd chat_orchestrator
export GOOGLE_API_KEY=test-key CHAT_DB_URL=https://placeholder.supabase.co CHAT_DB_SERVICE_KEY=placeholder MODEL_THINKING=gemini-pro-latest MODEL_FAST=gemini-flash-latest MODEL_LITE=gemini-2.5-flash-lite FALLBACK_MODEL=gemini-2.5-flash-lite
uv run pytest tests/ -q && uv run pytest ../shared -q -n auto
```

---

## Phase 2 — Shared UI widget extraction

Moves the Prompts page's existing Context-tab widget into its own module,
generalized to work for any pinning id (prompt or skill), with no behavior
change for prompts. This is what both new UI surfaces in Phases 3-4 build on.

### Task 7: Create `knowledge_picker.py` — pure logic

**Files:**
- Create: `anansi_app/nicegui_app/pages/knowledge_picker.py`
- Modify: `anansi_app/nicegui_app/pages/prompts.py:67-113` (remove the moved code)
- Modify: `anansi_app/tests/test_knowledge_modules_page.py` (remove the 3 relocated tests + fixture + cross-import)
- Create: `anansi_app/tests/test_knowledge_picker.py`

- [x] **Step 1: Write the failing test**

Create `anansi_app/tests/test_knowledge_picker.py`:

```python
"""Tests for the shared prompt/skill <-> knowledge-module picker widget
(moved and generalized from the Prompts page's original Context tab --
see test_knowledge_modules_page.py's git history for the pre-move tests
this file replaces)."""

from nicegui_app.pages.knowledge_picker import PickerRow, build_picker_rows, filter_picker_rows


def _module(slug, chars=40, summary="s", is_jit=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        slug=slug, title=slug.title(), body="b" * chars, summary=summary, is_jit=is_jit,
    )


def test_picker_rows_list_all_modules_with_attached_state():
    rows = build_picker_rows([_module("beta"), _module("alpha")], {"alpha": True})
    assert rows == [
        PickerRow(slug="alpha", title="Alpha", chars=40, checked=True, summary="s"),
        PickerRow(slug="beta", title="Beta", chars=40, checked=False, summary="s"),
    ]


def test_picker_rows_with_no_pins_checks_nothing():
    rows = build_picker_rows([_module("alpha")], {})
    assert [r.checked for r in rows] == [False]


def test_picker_rows_carries_is_jit_through():
    rows = build_picker_rows([_module("live-one", is_jit=True)], {})
    assert rows[0].is_jit is True


def _picker_rows_fixture():
    return [
        PickerRow(
            slug="azimuth-calculation", title="Azimuth Calculation",
            chars=318, checked=False,
            summary="How PV azimuth is measured.",
        ),
        PickerRow(
            slug="victron-led", title="Victron Quattro Codes",
            chars=2438, checked=True,
            summary="Decoding inverter LED error states.",
        ),
    ]


def test_filter_picker_rows_matches_slug_title_and_summary():
    rows = _picker_rows_fixture()
    assert [r.slug for r in filter_picker_rows(rows, "azimuth")] == ["azimuth-calculation"]
    assert [r.slug for r in filter_picker_rows(rows, "LED")] == ["victron-led"]
    assert len(filter_picker_rows(rows, "")) == 2
```

- [x] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_knowledge_picker.py -q`
(from the worktree root)
Expected: FAIL with `ModuleNotFoundError: No module named 'nicegui_app.pages.knowledge_picker'`

- [x] **Step 3: Write minimal implementation**

Create `anansi_app/nicegui_app/pages/knowledge_picker.py`:

```python
"""Shared search-and-tick widget: which knowledge modules a prompt or a
skill uses (both are pinning ids in prompt_knowledge_overrides -- see
shared/prompts/knowledge.py and shared/prompts/skills.py's skill_prompt_id).

Moved and generalized from the Prompts page's original Context tab, which
was prompt-specific in name only -- nothing in this logic ever depended on
being a prompt. See test_knowledge_picker.py; the pre-move tests lived in
test_knowledge_modules_page.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List


@dataclass(frozen=True)
class PickerRow:
    slug: str
    title: str
    chars: int
    checked: bool
    summary: str = ""
    # A provider-backed module (see shared/prompts/knowledge.py's is_jit) has
    # no stored body -- chars is 0 (correct for the budget sum, matching
    # budget_inlined's "unresolved costs nothing" rule) but that reads as
    # "empty" rather than "resolved at request time" unless a row can say
    # which one it is.
    is_jit: bool = False


def build_picker_rows(modules: List[Any], pins: dict) -> List[PickerRow]:
    """Every module as a pickable row, flagged with this entity's current pins.

    Unlike the tag-era version this hides nothing: the picker is how an
    operator discovers modules, so an unpinned module must still be findable.
    """
    return [
        PickerRow(
            slug=module.slug,
            title=module.title,
            # None for a provider-backed module -- len(None) would crash
            # (this hazard is the same one build_module_rows had; see
            # knowledge_modules.py's build_module_rows for the sibling fix).
            chars=len(module.body or ""),
            checked=bool(pins.get(module.slug)),
            summary=module.summary,
            is_jit=getattr(module, "is_jit", False),
        )
        for module in sorted(modules, key=lambda m: m.slug)
    ]


def filter_picker_rows(rows: List[PickerRow], query: str) -> List[PickerRow]:
    """Case-insensitive substring match over slug, title and summary."""
    needle = query.strip().lower()
    if not needle:
        return list(rows)
    return [
        r
        for r in rows
        if needle in r.slug.lower() or needle in r.title.lower() or needle in r.summary.lower()
    ]


__all__ = ["PickerRow", "build_picker_rows", "filter_picker_rows"]
```

Now remove the moved code from `anansi_app/nicegui_app/pages/prompts.py`:
delete the `KnowledgeTabRow` dataclass (lines 67-79), `build_knowledge_tab`
(lines 82-101) and `filter_module_rows` (lines 104-113) entirely — Task 8
replaces their one call site with an import from the new module.

Update `anansi_app/tests/test_knowledge_modules_page.py`:
- Remove the import `from nicegui_app.pages.prompts import KnowledgeTabRow, build_knowledge_tab, filter_module_rows`
  (line 24) entirely — nothing in this file needs it any more.
- Remove `test_knowledge_tab_lists_all_modules_with_attached_state`,
  `test_knowledge_tab_with_no_pins_checks_nothing`,
  `test_filter_modules_matches_slug_title_and_summary`, and
  `_knowledge_tab_rows_fixture` (they now live in `test_knowledge_picker.py`,
  renamed as above).

- [x] **Step 4: Run test to verify it passes**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_knowledge_picker.py anansi_app/tests/test_knowledge_modules_page.py -q
```
Expected: PASS on both files (prompts.py itself won't import cleanly again
until Task 8 fixes its one broken call site — if this step errors on an
`ImportError` from `prompts.py`, that's expected; it's fixed next).

- [x] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/knowledge_picker.py \
        anansi_app/nicegui_app/pages/prompts.py \
        anansi_app/tests/test_knowledge_picker.py \
        anansi_app/tests/test_knowledge_modules_page.py
git commit -m "refactor(context-ui): move picker row logic to knowledge_picker.py

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: `render_module_picker` + refactor Prompts page

**Files:**
- Modify: `anansi_app/nicegui_app/pages/knowledge_picker.py`
- Modify: `anansi_app/nicegui_app/pages/prompts.py:505-587` (the Context tab panel body)
- Test: `anansi_app/tests/test_prompts_dialog.py`

- [x] **Step 1: Write the failing test**

Add to `anansi_app/tests/test_prompts_dialog.py` (it reads `prompts.py`'s
source as text and asserts a substring — the same convention already used
for `test_prompts_dialog_body_defaults_to_preview`/
`test_prompts_dialog_has_viewport_scroll_container`; check the file's top
for its `PROMPTS_PATH`-style constant and reuse it):

```python
def test_context_tab_delegates_to_the_shared_picker():
    src = PROMPTS_PATH.read_text()

    assert "render_module_picker(row.prompt_id, k_store, user_email, show_budget=True)" in src
```

- [x] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_prompts_dialog.py -q -k shared_picker`
Expected: FAIL (substring not present yet)

- [x] **Step 3: Write minimal implementation**

Add to `anansi_app/nicegui_app/pages/knowledge_picker.py` (after
`filter_picker_rows`):

```python
def render_module_picker(
    pinning_id: str, store: Any, user_email: str, *, show_budget: bool
) -> None:
    """Search + tick which modules `pinning_id` (a prompt id or a
    skill:<uuid>, both live in prompt_knowledge_overrides) uses, with its
    own independent Save button -- lifted verbatim from the Prompts page's
    original Context tab, generalized to any pinning id.

    `show_budget` is False for the reverse-direction picker's context (a
    module -> which prompts/skills use it); there, a module's own size is
    shown elsewhere on that same form, and summing many different entities'
    inlined-char totals wouldn't mean anything.
    """
    from nicegui import ui

    from shared.prompts.knowledge import INLINE_BUDGET_CHARS

    if not store._client:  # noqa: SLF001 -- readiness check, matches every other call site
        ui.label(
            "⚠️ Context storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY)."
        ).classes("text-warning")
        return

    ui.label(
        "Context modules this uses. Every module you tick is inlined "
        "in full. Built-in modules resolve per request and have no fixed "
        "size until they do, so they don't count towards the budget below."
    ).classes("text-caption")

    all_modules = store.all_modules()
    pins = store.overrides_for(pinning_id)
    selected: "set[str]" = {m.slug for m in all_modules if pins.get(m.slug)}

    search = ui.input(placeholder="Search modules…").classes("w-full").props("clearable dense")
    picked_label = ui.label().classes("text-caption text-bold")
    options = ui.column().classes("w-full gap-0").style("max-height: 340px; overflow-y: auto")

    def redraw() -> None:
        options.clear()
        rows = filter_picker_rows(
            build_picker_rows(all_modules, {s: True for s in selected}),
            search.value or "",
        )
        if show_budget:
            inlined_chars = sum(r.chars for r in rows if r.checked)
            over = inlined_chars > INLINE_BUDGET_CHARS
            picked_label.text = (
                f"{len(selected)} selected · {inlined_chars} chars "
                f"of {INLINE_BUDGET_CHARS} budget"
                + (" · over budget, some will be dropped at render" if over else "")
            )
            picked_label.classes(
                replace="text-caption text-bold " + ("text-negative" if over else "")
            )
        else:
            picked_label.text = f"{len(selected)} selected"
        with options:
            if not rows:
                ui.label("No modules match.").classes("text-italic text-caption")
            for r in rows:
                def toggle(e, slug=r.slug) -> None:
                    if e.value:
                        selected.add(slug)
                    else:
                        selected.discard(slug)
                    redraw()

                with ui.row().classes("items-center no-wrap w-full"):
                    ui.checkbox(value=r.checked, on_change=toggle).props("dense")
                    with ui.column().classes("gap-0"):
                        size_text = "live" if r.is_jit else f"{r.chars} chars"
                        ui.label(f"{r.title}  ·  {size_text}")
                        if r.summary:
                            ui.label(r.summary).classes("text-caption")

    async def save_pins() -> None:
        try:
            store.set_prompt_modules(pinning_id, sorted(selected), actor=user_email)
            store.invalidate()
            ui.notify("Context updated", type="positive")
        except Exception as e:  # noqa: BLE001 -- surfaced to the operator
            ui.notify(f"Save failed: {e}", type="negative")

    search.on_value_change(redraw)
    redraw()
    with ui.row().classes("justify-end w-full q-mt-sm"):
        ui.button("Save context", on_click=save_pins).props("color=primary")
```

Add `"render_module_picker"` to the file's `__all__`.

Now replace `prompts.py`'s Context tab panel body (the block from
`with ui.tab_panel(knowledge_tab):` through the `ui.button("Save context", ...)`
line) with:

```python
            with ui.tab_panel(knowledge_tab):
                from nicegui_app.pages.knowledge_picker import render_module_picker
                from shared.prompts.knowledge import KnowledgeStore

                k_store = KnowledgeStore.from_env()
                render_module_picker(row.prompt_id, k_store, user_email, show_budget=True)
```

- [x] **Step 4: Run test to verify it passes**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests -q
```
Expected: PASS, full anansi_app suite green (426+ passed — this refactor
must not lose or break any existing test).

- [x] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/knowledge_picker.py \
        anansi_app/nicegui_app/pages/prompts.py \
        anansi_app/tests/test_prompts_dialog.py
git commit -m "refactor(context-ui): Prompts page Context tab uses the shared picker

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

**Phase 2 checkpoint:**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests -q
```

---

## Phase 3 — Knowledge Modules modal

Fixes the unreadable prompts picker, adds a skills picker next to it, and
fixes a raw-uuid leak this feature would otherwise introduce into the
existing "used by" summary line.

### Task 9: `render_entity_picker` (reverse direction)

**Files:**
- Modify: `anansi_app/nicegui_app/pages/knowledge_picker.py`
- Test: `anansi_app/tests/test_knowledge_picker.py`

- [x] **Step 1: Write the failing test**

Add to `anansi_app/tests/test_knowledge_picker.py`:

```python
class _FakeElement:
    def __init__(self):
        self.children = []

    def classes(self, *_a, **_k):
        return self

    def props(self, *_a, **_k):
        return self

    def style(self, *_a, **_k):
        return self

    def clear(self):
        self.children = []

    def on_value_change(self, _callback):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_render_entity_picker_returns_a_getter_seeded_from_checked_rows(monkeypatch):
    import nicegui_app.pages.knowledge_picker as knowledge_picker

    fake_ui = type(
        "FakeUi",
        (),
        {
            "label": staticmethod(lambda *a, **k: _FakeElement()),
            "input": staticmethod(lambda *a, **k: _FakeElement()),
            "column": staticmethod(lambda *a, **k: _FakeElement()),
            "row": staticmethod(lambda *a, **k: _FakeElement()),
            "checkbox": staticmethod(lambda *a, **k: _FakeElement()),
        },
    )()
    monkeypatch.setattr(knowledge_picker, "ui", fake_ui, raising=False)

    rows = [
        PickerRow(slug="a", title="A", chars=0, checked=True, summary=""),
        PickerRow(slug="b", title="B", chars=0, checked=False, summary=""),
    ]
    get_selected = knowledge_picker.render_entity_picker(rows, label="Used by these prompts")

    assert get_selected() == ["a"]
```

`render_entity_picker` imports `nicegui.ui` inside the function body (same
pattern as `render_module_picker` above), which is what makes
`monkeypatch.setattr(knowledge_picker, "ui", fake_ui, raising=False)` alone
insufficient if the import happens fresh on every call — write the
implementation in Step 3 to import `ui` at **module level** in
`knowledge_picker.py` instead (a plain `from nicegui import ui` at the top of
the file, alongside the existing imports) specifically so this monkeypatch
works; adjust `render_module_picker` from Task 8 to do the same for
consistency (it currently imports `ui` locally — hoist that import to the
top of the file too while you're here, it's dead weight as a local import
once the module already imports `ui` at top level).

- [x] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_knowledge_picker.py -q -k render_entity_picker`
(from the worktree root)
Expected: FAIL with `AttributeError: module ... has no attribute 'render_entity_picker'`

- [x] **Step 3: Write minimal implementation**

At the top of `knowledge_picker.py`, change `from typing import Any, List`
to `from typing import Any, Callable, List`, and add `from nicegui import ui`
to the imports. Remove the now-redundant local `from nicegui import ui`
inside `render_module_picker`'s body (added in Task 8).

Add after `render_module_picker`:

```python
def render_entity_picker(
    rows: List[PickerRow], *, label: str, search_placeholder: str = "Search…"
) -> "Callable[[], List[str]]":
    """Search + tick which entities (prompts, or skills) use ONE module --
    the reverse direction from render_module_picker. No budget footer: a
    module's own size is shown elsewhere on the same form.

    Returns a zero-argument getter for the currently-ticked slugs/ids,
    rather than a Save button -- knowledge_modules.py must union this
    picker's selection with a second one (skills) before writing once (see
    resolve_pins_to_save; two separate saves would have the second call's
    diff delete the first call's pins).
    """
    selected: "set[str]" = {r.slug for r in rows if r.checked}

    ui.label(label).classes("text-caption text-bold")
    search = ui.input(placeholder=search_placeholder).classes("w-full").props("clearable dense")
    options = ui.column().classes("w-full gap-0").style("max-height: 260px; overflow-y: auto")

    def redraw() -> None:
        options.clear()
        current = [
            PickerRow(slug=r.slug, title=r.title, chars=r.chars, checked=(r.slug in selected), summary=r.summary)
            for r in rows
        ]
        visible = filter_picker_rows(current, search.value or "")
        with options:
            if not visible:
                ui.label("No matches.").classes("text-italic text-caption")
            for r in visible:
                def toggle(e, slug=r.slug) -> None:
                    if e.value:
                        selected.add(slug)
                    else:
                        selected.discard(slug)

                with ui.row().classes("items-center no-wrap w-full"):
                    ui.checkbox(value=r.checked, on_change=toggle).props("dense")
                    ui.label(f"{r.title}  ·  {r.summary}" if r.summary else r.title)

    search.on_value_change(redraw)
    redraw()
    return lambda: sorted(selected)
```

Add `"render_entity_picker"` to `__all__`.

- [x] **Step 4: Run test to verify it passes**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_knowledge_picker.py -q
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests -q
```
Expected: PASS on both (full suite still green).

- [x] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/knowledge_picker.py anansi_app/tests/test_knowledge_picker.py
git commit -m "feat(context-ui): add render_entity_picker (module -> prompts/skills)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 10: `Skill.status` + `all_skills(active_only=)`

**Files:**
- Modify: `shared/prompts/skills.py`
- Test: `shared/tests/test_skills_catalog.py`

- [x] **Step 1: Write the failing test**

Add to `shared/tests/test_skills_catalog.py`, inside `TestSkillCatalogStore`
(reuse its existing `_fake_supabase_client` helper):

```python
    def test_active_only_false_fetches_every_status(self):
        client = _fake_supabase_client([])
        store = SkillCatalogStore(client=client)

        store.all_skills(active_only=False)

        assert "status" not in client.last_query.filters

    def test_active_only_true_is_still_the_default(self):
        client = _fake_supabase_client([])
        store = SkillCatalogStore(client=client)

        store.all_skills()

        assert client.last_query.filters.get("status") == "active"

    def test_active_only_and_all_are_cached_separately(self):
        client = _fake_supabase_client([])
        store = SkillCatalogStore(client=client, ttl_seconds=300)

        store.all_skills(active_only=True)
        store.all_skills(active_only=False)

        assert client.query_count == 2
```

Also add, near the top-level `_skill()` helper:

```python
def test_skill_status_defaults_to_active():
    assert _skill().status == "active"
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && uv run pytest ../shared/tests/test_skills_catalog.py -q -k "active_only or status_defaults"`
Expected: FAIL (`status` attribute doesn't exist yet; `active_only` kwarg
not accepted yet)

- [x] **Step 3: Write minimal implementation**

In `shared/prompts/skills.py`, add a `status` field to `Skill`:

```python
@dataclass(frozen=True)
class Skill:
    """Catalog-relevant fields only -- not the full row (no steps/inputs).

    Fetching a skill's steps is a Phase 4/5 (builder, runner) concern; the
    catalog only ever needs enough to render one line and decide visibility.
    """

    id: str
    slug: str
    title: str
    summary: str
    staff_only: bool
    # Included so a caller with active_only=False (the knowledge-module
    # picker, which must be able to pin a draft skill before it goes live)
    # can still show status. Defaults to "active" so every existing caller
    # (active_only=True, which never selects this column) keeps working
    # unchanged.
    status: str = "active"
```

Change `SkillCatalogStore`'s cache fields and `all_skills`:

```python
    def __init__(self, client=None, ttl_seconds: int = 300) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._cache: Dict[bool, List[Skill]] = {}
        self._expires: Dict[bool, float] = {}

    @classmethod
    def from_env(cls) -> "SkillCatalogStore":
        from shared.config.db_credentials import chat_db_service_key, chat_db_url

        url, key = chat_db_url(), chat_db_service_key()
        if not (url and key):
            return cls(client=None)
        try:
            from supabase import create_client

            return cls(client=create_client(url, key))
        except Exception:
            LOGGER.opt(exception=True).warning("Could not build the skill catalog store client")
            return cls(client=None)

    def invalidate(self) -> None:
        self._cache = {}
        self._expires = {}

    def all_skills(self, *, active_only: bool = True) -> List[Skill]:
        """Active skills by default. active_only=False includes draft/
        disabled/unusable too -- for the knowledge-module picker, which must
        let an operator pin a module to a skill before it goes live.

        Cached separately per active_only value: they're different result
        sets, and a naive shared cache slot would return the wrong one
        within the TTL window.
        """
        import time

        if active_only in self._cache and time.time() < self._expires.get(active_only, 0):
            return self._cache[active_only]
        if not self._client:
            return []
        try:
            query = self._client.table("skills").select(
                "id, slug, title, summary, staff_only, status"
            )
            if active_only:
                query = query.eq("status", "active")
            result = query.execute()
            self._cache[active_only] = [Skill(**row) for row in (result.data or [])]
        except Exception:
            LOGGER.opt(exception=True).warning("Skill catalog fetch failed; continuing without")
            self._cache[active_only] = []
        self._expires[active_only] = time.time() + self._ttl
        return self._cache[active_only]
```

Add `Dict` to the file's `from typing import ...` line if not already
imported (check the current import list; it likely has `List, Optional`
already — add `Dict`).

- [x] **Step 4: Run test to verify it passes**

```bash
cd chat_orchestrator
uv run pytest ../shared/tests/test_skills_catalog.py -q
uv run pytest ../shared -q -n auto
```
Expected: PASS on both — every pre-existing test in this file
(`test_no_client_returns_empty_list`, `test_fetches_and_parses_rows_into_skill_objects`,
`test_query_filters_to_active_status_only`, `test_result_is_cached_within_ttl`,
`test_invalidate_forces_a_fresh_fetch`, `test_expired_cache_forces_a_fresh_fetch`,
`test_query_failure_degrades_to_empty_not_raise`) must still pass unchanged.

- [x] **Step 5: Commit**

```bash
git add shared/prompts/skills.py shared/tests/test_skills_catalog.py
git commit -m "feat(skills): add Skill.status and all_skills(active_only=)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 11: Fix the "used by" raw-id leak

**Files:**
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py`
- Test: `anansi_app/tests/test_knowledge_modules_page.py`

Found while implementing this plan, not in the original spec: once a skill
can pin a module, `knowledge_modules.py`'s existing `describe_usage`/
`build_module_rows` would join a raw `"skill:<uuid>"` string straight into
the row's "used by" caption — an admin-UI regression this feature would
otherwise silently introduce. Fixing it here, before the picker that creates
skill pins even exists, so there's never a window where a pinned skill shows
as a raw uuid.

- [x] **Step 1: Write the failing test**

Add to `anansi_app/tests/test_knowledge_modules_page.py`:

```python
def test_build_module_rows_resolves_a_skill_pin_to_its_title():
    rows = build_module_rows(
        [_module("comms")],
        {"comms": ["customer.system", "skill:11111111-1111-1111-1111-111111111111"]},
        skill_titles={"11111111-1111-1111-1111-111111111111": "Find Tickets"},
    )
    assert rows[0].used_by == ["customer.system", "🎬 Find Tickets"]


def test_build_module_rows_falls_back_to_the_raw_id_for_an_unknown_skill():
    """E.g. a skill deleted after the pin was made -- prompt_knowledge_overrides
    has no FK on skill ids, so a stale pin can outlive its skill."""
    rows = build_module_rows(
        [_module("comms")],
        {"comms": ["skill:22222222-2222-2222-2222-222222222222"]},
        skill_titles={},
    )
    assert rows[0].used_by == ["🎬 22222222-2222-2222-2222-222222222222"]
```

- [x] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_knowledge_modules_page.py -q -k skill_pin`
(from the worktree root)
Expected: FAIL with `TypeError: build_module_rows() got an unexpected keyword argument 'skill_titles'`

- [x] **Step 3: Write minimal implementation**

In `anansi_app/nicegui_app/pages/knowledge_modules.py`, add near the top
(with the other module-level imports) — this needs `SKILL_PIN_PREFIX`:

```python
from shared.prompts.skills import SKILL_PIN_PREFIX
```

Add a new pure function right before `build_module_rows`:

```python
def resolve_pin_label(pin_id: str, skill_titles: "dict[str, str]") -> str:
    """A prompt id shows as-is; a skill:<uuid> id shows its title (falling
    back to the raw id if the skill's title isn't known -- e.g. a skill
    deleted after the pin was made; prompt_knowledge_overrides has no FK on
    skill ids to clean this up automatically, matching how a retired
    prompt id's stale pin already behaved before skills existed)."""
    if pin_id.startswith(SKILL_PIN_PREFIX):
        skill_id = pin_id[len(SKILL_PIN_PREFIX):]
        return f"🎬 {skill_titles.get(skill_id, skill_id)}"
    return pin_id
```

Change `build_module_rows`'s signature and body:

```python
def build_module_rows(
    modules: List[Any],
    pins: "dict[str, List[str]] | None" = None,
    skill_titles: "dict[str, str] | None" = None,
) -> List[ModuleRow]:
    """Rows for the list. ``pins`` is module_id -> prompt/skill ids, fetched
    once. ``skill_titles`` is skill_id -> title, for resolving a skill pin
    to something readable (see resolve_pin_label).

    Optional so callers that only need identity/size (and every test that
    predates usage display) keep working; omitting it renders every module
    as unattached, which is why the page itself always passes it.
    """
    pins = pins or {}
    skill_titles = skill_titles or {}
    rows = []
    for m in sorted(modules, key=lambda m: m.slug):
        body = m.body or ""
        chars = len(body)
        source = getattr(m, "source", "manual")
        rows.append(
            ModuleRow(
                slug=m.slug, title=m.title, tags=list(m.tags), scope=m.scope,
                chars=chars, source=source,
                size_label="live" if source in PROVIDER_SOURCES else f"{chars} chars",
                summary=m.summary,
                body=body,
                used_by=[resolve_pin_label(pid, skill_titles) for pid in pins.get(m.id, [])],
            )
        )
    return rows
```

Now wire it into `render()`'s `refresh()`. Add the import at the top of
`render()` alongside the existing `from shared.prompts.knowledge import KnowledgeStore`:

```python
    from shared.prompts.skills import SKILL_CATALOG
```

And change `refresh()`:

```python
    def refresh() -> None:
        list_container.clear()
        store.invalidate()
        SKILL_CATALOG.invalidate()
        skill_titles = {s.id: s.title for s in SKILL_CATALOG.all_skills(active_only=False)}
        rows = build_module_rows(store.all_modules(), store.all_prompt_pins(), skill_titles)
        all_empty = not rows
        rows = filter_context_rows(rows, search_input.value or "")
```
(the rest of `refresh()` is unchanged — only the two lines building `rows`
and the new `skill_titles` line above them change).

- [x] **Step 4: Run test to verify it passes**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_knowledge_modules_page.py -q
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests -q
```
Expected: PASS on both.

- [x] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/knowledge_modules.py \
        anansi_app/tests/test_knowledge_modules_page.py
git commit -m "fix(context-ui): resolve a skill pin to its title, not a raw uuid

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 12: Replace the prompts chip-select (the readability fix)

**Files:**
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py:805-836` (approx — the `prompt_options`/`prompts_select` block inside `_open_edit_dialog`)
- Test: `anansi_app/tests/test_knowledge_modules_dialog.py`

- [x] **Step 1: Write the failing test**

Add to `anansi_app/tests/test_knowledge_modules_dialog.py` (reuse its
existing `KNOWLEDGE_MODULES_PATH` constant):

```python
def test_prompts_picker_uses_the_shared_searchable_widget_not_a_chip_select():
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert 'use-chips' not in src
    assert "render_entity_picker(prompt_rows" in src
```

- [x] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_knowledge_modules_dialog.py -q -k searchable_widget`
(from the worktree root)
Expected: FAIL (`use-chips` still present, `render_entity_picker` not called yet)

- [x] **Step 3: Write minimal implementation**

In `_open_edit_dialog`, find:

```python
        prompt_options = {
            pid: prompt_option_label(pid, PROMPTS.spec(pid).description)
            for pid in sorted(PROMPTS.ids())
        }
        prompts_select = ui.select(
            prompt_options,
            value=list(existing_pins),
            multiple=True,
            label="Used by these prompts",
        ).classes("w-full").props("use-chips")
```

Replace with:

```python
        from nicegui_app.pages.knowledge_picker import PickerRow, render_entity_picker

        existing_prompt_pins = {
            pid for pid in existing_pins if not pid.startswith(SKILL_PIN_PREFIX)
        }
        prompt_rows = [
            PickerRow(
                slug=pid, title=pid, chars=0,
                checked=(pid in existing_prompt_pins),
                summary=PROMPTS.spec(pid).description,
            )
            for pid in sorted(PROMPTS.ids())
        ]
        get_selected_prompts = render_entity_picker(
            prompt_rows, label="Used by these prompts", search_placeholder="Search prompts…"
        )
```

Note `existing_pins` (computed earlier in this function, at
`existing_pins = store.prompts_pinning(existing.id) if existing else []`)
now needs splitting into prompt-only and skill-only subsets — Task 13 adds
the skill half of this split; for this task, only the prompt half
(`existing_prompt_pins` above) is needed since Task 13 hasn't added any
skill pins yet.

`prompt_option_label` loses its only call site in this change (superseded by
`PickerRow`'s `summary` field feeding `render_entity_picker`'s caption) —
**do not delete the function itself.**
`test_knowledge_modules_page.py::test_prompt_option_label_combines_id_and_description`,
`test_prompt_option_label_truncates_a_long_description`, and
`test_prompt_option_label_falls_back_to_bare_id_when_no_description` (lines
225-237) test it directly and must keep passing; Task 13 doesn't touch it
either.

Update the `save()` function's `set_prompt_pins` call to use the getter
instead of `prompts_select.value`:

```python
                store.set_prompt_pins(
                    module_id, list(get_selected_prompts() or []), actor=user_email
                )
```

(Task 13 replaces this line again with the full union — this is an
intermediate, still-correct state where only prompts exist.)

- [x] **Step 4: Run test to verify it passes**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_knowledge_modules_dialog.py anansi_app/tests/test_knowledge_modules_page.py -q
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests -q
```
Expected: PASS on all three. `test_knowledge_modules_page.py`'s three
`prompt_option_label` tests (lines 225-237, noted above) test the function
directly and are untouched by this task. No existing test asserts
`use-chips`/`prompts_select` in the dialog's source, so nothing needs
removing on that front.

- [x] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/knowledge_modules.py \
        anansi_app/tests/test_knowledge_modules_dialog.py
git commit -m "fix(context-ui): replace the prompts chip-select with a searchable picker

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 13: Skills picker + union-before-save

**Files:**
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py`
- Test: `anansi_app/tests/test_knowledge_modules_page.py`
- Test: `anansi_app/tests/test_knowledge_modules_dialog.py`

- [ ] **Step 1: Write the failing tests**

Add to `anansi_app/tests/test_knowledge_modules_page.py` (this is the
critical regression test for the wipe-out trap the spec called out — it
must be a real, direct test of the union logic, not a source-text
assertion):

```python
def test_resolve_pins_to_save_unions_prompts_and_skills():
    from nicegui_app.pages.knowledge_modules import resolve_pins_to_save

    result = resolve_pins_to_save(
        ["customer.system"], ["11111111-1111-1111-1111-111111111111"]
    )
    assert result == ["customer.system", "skill:11111111-1111-1111-1111-111111111111"]


def test_resolve_pins_to_save_with_no_skills_matches_old_behavior():
    from nicegui_app.pages.knowledge_modules import resolve_pins_to_save

    assert resolve_pins_to_save(["customer.system", "staff.system"], []) == [
        "customer.system", "staff.system",
    ]


def test_resolve_pins_to_save_with_no_prompts():
    from nicegui_app.pages.knowledge_modules import resolve_pins_to_save

    assert resolve_pins_to_save([], ["abc"]) == ["skill:abc"]
```

Add to `anansi_app/tests/test_knowledge_modules_dialog.py`:

```python
def test_skills_picker_is_present_and_distinctly_labeled():
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert 'label="Used by these skills"' in src
    assert "resolve_pins_to_save(" in src
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_knowledge_modules_page.py anansi_app/tests/test_knowledge_modules_dialog.py -q -k "resolve_pins_to_save or skills_picker"
```
Expected: FAIL (`resolve_pins_to_save` doesn't exist; the skills label isn't
in the source yet)

- [ ] **Step 3: Write minimal implementation**

In `anansi_app/nicegui_app/pages/knowledge_modules.py`, add near the top
(with the other module-level pure functions, e.g. right after
`resolve_pin_label` from Task 11):

```python
def resolve_pins_to_save(prompt_ids: List[str], skill_ids: List[str]) -> List[str]:
    """The full id list for one set_prompt_pins call -- prompts and skills
    share prompt_knowledge_overrides' key space, so writing them via two
    separate calls would have the second call's diff (current - selected)
    delete the first call's pins (see knowledge.py's diff_prompt_pins).
    This must always be a single call with the union."""
    return list(prompt_ids) + [skill_prompt_id(sid) for sid in skill_ids]
```

Add the import: `from shared.prompts.skills import SKILL_PIN_PREFIX,
skill_prompt_id, SKILL_CATALOG` — combine with Task 11's existing
`from shared.prompts.skills import SKILL_PIN_PREFIX, SKILL_CATALOG` import
into one line rather than two.

Right after Task 12's `get_selected_prompts = render_entity_picker(...)`
call, add the skills picker:

```python
        existing_skill_pins = {
            pid[len(SKILL_PIN_PREFIX):]
            for pid in existing_pins
            if pid.startswith(SKILL_PIN_PREFIX)
        }
        all_skills = SKILL_CATALOG.all_skills(active_only=False)
        skill_rows = [
            PickerRow(
                slug=s.id, title=s.title, chars=0,
                checked=(s.id in existing_skill_pins),
                summary=f"/{s.slug} · {s.status}",
            )
            for s in sorted(all_skills, key=lambda s: s.title)
        ]
        get_selected_skills = render_entity_picker(
            skill_rows, label="Used by these skills", search_placeholder="Search skills…"
        )
```

Change the `save()` function's pin-saving line (last touched in Task 12)
to the real union:

```python
                store.set_prompt_pins(
                    module_id,
                    resolve_pins_to_save(get_selected_prompts(), get_selected_skills()),
                    actor=user_email,
                )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests -q
```
Expected: PASS, full anansi_app suite green.

- [ ] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/knowledge_modules.py \
        anansi_app/tests/test_knowledge_modules_page.py \
        anansi_app/tests/test_knowledge_modules_dialog.py
git commit -m "feat(context-ui): add a skills picker, union both before saving pins

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

**Phase 3 checkpoint:**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests -q
```

---

## Phase 4 — Skill editor

### Task 14: "Context" card in the workflow editor

**Files:**
- Modify: `anansi_app/nicegui_app/pages/skills.py`
- Test: `anansi_app/tests/test_skills_page.py`

- [ ] **Step 1: Write the failing test**

Add to `anansi_app/tests/test_skills_page.py`:

```python
def test_editor_has_a_context_card_only_for_an_existing_workflow():
    import inspect

    from nicegui_app.pages import skills

    src = inspect.getsource(skills._open_editor)

    assert 'ui.label("Context")' in src
    # Gated on `if row:` the same way title/slug already are -- a brand-new,
    # unsaved workflow has no skill_id to key pins on.
    assert "skill_prompt_id(row[" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_skills_page.py -q -k context_card`
(from the worktree root)
Expected: FAIL (neither string present yet)

- [ ] **Step 3: Write minimal implementation**

In `anansi_app/nicegui_app/pages/skills.py`, find the end of the "Identity &
schedule" card (the block ending with `first_run = ui.input(...)`, still
inside `with ui.card().classes("w-full gap-2"):`), and right after that
`with ui.card():` block closes (back at the same indentation as that
`with ui.card():` line itself, still inside the outer
`with ui.column().classes("w-full gap-4"):`), add a third card:

```python
            if row:
                with ui.card().classes("w-full gap-2"):
                    ui.label("Context").classes("text-subtitle2")
                    ui.label(
                        "Knowledge modules this workflow's steps can draw on, "
                        "resolved once at the start of each run."
                    ).classes("text-xs text-gray-500")

                    from nicegui_app.pages.knowledge_picker import render_module_picker
                    from shared.prompts.knowledge import KnowledgeStore
                    from shared.prompts.skills import skill_prompt_id

                    k_store = KnowledgeStore.from_env()
                    render_module_picker(
                        skill_prompt_id(row["id"]), k_store, user_email, show_budget=True
                    )
```

This sits as a sibling of the `with ui.card().classes("w-full gap-2"):`
(Identity & schedule) block above it, and above the `# Now fill the
Workflow card placeholder...` comment / `with steps_card:` block that
follows — same visual stacking column, third card down. Verify by reading
the surrounding ~40 lines after making the edit: the outer
`with ui.column().classes("w-full gap-4"):` should now have exactly three
children in source order: `steps_card` (placeholder), the Identity &
schedule card, and this new Context card, gated on `if row:`.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests/test_skills_page.py -q
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests -q
```
Expected: PASS, full anansi_app suite green.

- [ ] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/skills.py anansi_app/tests/test_skills_page.py
git commit -m "feat(workflows): add a Context card to the workflow editor

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Phase 5 — Docs and final verification

### Task 15: Update README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the cross-reference sentence**

Find the sentence (currently around line 84):

> **context modules** (what it's told as fact — see [Context](#context-curated-and-attached-knowledge) above), and **Skills** (multi-step automations it can run — see [Skills](#skills-operator-authored-automations) above)

Change it to note the two now connect:

> **context modules** (what it's told as fact — see [Context](#context-curated-and-attached-knowledge) above; a module can attach to a prompt or to a skill), and **Skills** (multi-step automations it can run — see [Skills](#skills-operator-authored-automations) above)

- [ ] **Step 2: Fix the stale "per prompt" phrasing**

Find (currently around line 1278):

> ✅ Context modules grouped by source — Built-in (code-generated), Curated (typed in the admin UI), External (attached Google Doc/Sheet) — each scoped (everywhere or one organization) and pinned explicitly per prompt

Change `per prompt` to `per prompt or skill`.

- [ ] **Step 3: Add one line to the Context section itself**

Find the `#context-curated-and-attached-knowledge` section's body text and
add one sentence noting a module can also attach to a skill from that
skill's own "Context" card in the Workflows editor (mirroring however the
existing text there already describes attaching from a prompt's own Context
tab) — read that section's current wording first and match its voice rather
than pasting a mismatched tone.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: note knowledge modules can attach to skills too

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 16: Full verification pass

**Files:** none (verification only)

- [ ] **Step 1: Full backend suites**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills/chat_orchestrator
export GOOGLE_API_KEY=test-key CHAT_DB_URL=https://placeholder.supabase.co CHAT_DB_SERVICE_KEY=placeholder MODEL_THINKING=gemini-pro-latest MODEL_FAST=gemini-flash-latest MODEL_LITE=gemini-2.5-flash-lite FALLBACK_MODEL=gemini-2.5-flash-lite
uv run pytest tests/ -q
uv run pytest ../shared -q -n auto
```
Expected: all green, no regressions vs. the 2259 / 839 baseline (higher
counts now, from this plan's new tests).

- [ ] **Step 2: Full anansi_app suite**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
PYTHONPATH="$PWD:$PWD/anansi_app" .venv-validate/bin/pytest anansi_app/tests -q
```
Expected: all green, no regressions vs. the 426 baseline.

- [ ] **Step 3: pre-commit**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/wt-knowledge-modules-for-skills
pip install -q pre-commit 2>/dev/null || true
pre-commit run --all-files
```
If `test-wiring` (or any hook) flags an untracked file under a `tests/`
directory, force-add it (`git add -f <path>`) after checking it holds no
operator data — see CLAUDE.md's checklist. Re-run `pre-commit run --all-files`
until clean.

- [ ] **Step 4: Report and hand off**

Summarize what changed, the final test counts across all three suites, and
that no schema migration was needed, per the spec's "reuse tables" goal.
Do not push or open a PR without the user's go-ahead — follow
`superpowers:finishing-a-development-branch` for how to wrap up from here.
