# P1 — Resolvable Context Modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Stage 1 of 4.** This is the first plan in the context-architecture programme and has no prior stage to check.

**Goal:** Make `KnowledgeModule.body` resolvable at render time by a provider keyed on the existing `knowledge_modules.source` column, so a Google Doc, a GraphRAG entity summary, a grids/orgs/users directory and an episodic distillation all become attachable context modules through the mechanism that already exists.

**Architecture:** `manual` and `gdoc` bodies resolve synchronously inside `PromptLibrary` (where `_compose_knowledge` already runs). `graph`, `directory` and `episodic` bodies resolve asynchronously in a new `JitContextResolver` in the orchestrator, which reads the same `prompt_knowledge_overrides` pins and emits its output into `context_message`. Everything else — the admin list, per-prompt pinning, pinned/on-demand tiers, budgeting, the scope gate — is unchanged.

**Tech Stack:** Python 3.12+, Supabase (postgres + pgvector), NiceGUI admin app, MCP servers, pytest, pre-commit (ruff + test-wiring).

**Spec:** `docs/superpowers/specs/2026-08-19-resolvable-context-modules-design.md`

---

## Critical Context for the Implementer

Read this before Task 1.

### Migrations are applied by hand

Merging a `.sql` file into `db/migrations/` does **not** apply it to production
chat_db. Migration `0016_chat_messages_topic.sql` is still unapplied and producing
live PGRST204 errors. Every migration in this plan must be pasted into the Supabase
SQL editor against chat_db by a human before the code depending on it deploys.

Migration numbers here are indicative. Check what `db/migrations/` actually holds
and take the next free number.

### `.gitignore` denies `tests/` and `docs/superpowers/plans/`

A plain `git add` on a new file under any `tests/` directory is a **silent no-op**:
the commit succeeds, the file never reaches the remote, CI never runs it. Every
commit step below that adds a test file uses `git add -f`. Before declaring any
phase done, run `pre-commit run --all-files` (not just `pytest`) and confirm the
`test-wiring` hook is clean.

### Do not import the `PROMPTS` singleton in tests

`shared.prompts.PROMPTS` is built at import time with DB and Google-Doc lookups
wired up whenever `CHAT_DB_URL` / `CHAT_DB_SERVICE_KEY` / `GOOGLE_SERVICE_ACCOUNT_JSON`
are present in the environment. A local `chat_orchestrator/.env` with real
credentials makes any test touching it non-hermetic. Construct a bare
`PromptLibrary()` instead, or monkeypatch `_db_body_for` / `_gdoc_body_for` to `None`.

### `PromptLibrary.render()` is synchronous

`shared/prompts/core.py:199`. Do not make it async — `PROMPTS.text()` is called from
dozens of sync call sites including scripts and MCP servers. This is why the plan
splits resolution across two components. If you find yourself adding `async` to
`render`, stop and re-read the spec's "sync/async boundary" section.

### The entity graph has no permission columns

`entities`, `relationships`, `entity_mentions` and `relationship_evidence`
(`db/schema/chat_db.sql:373-427`) carry no `allowed_organization_ids` or any
equivalent. The only path to row-level permission is
`entities → entity_mentions → documents.allowed_organization_ids`. There is no
shortcut and no RLS policy to lean on.

### Verify the current state before starting

```bash
python -c "from shared.prompts import PROMPTS; print(PROMPTS.resolve('customer.system')[1:])"
```

If this prints `PromptSource.DB` or `PromptSource.GDOC`, production is not reading
the bundled files — relevant to Phase 5 onward.

---

## File Structure

**Phase 1 — model**
- Create: `db/migrations/0017_context_module_providers.sql`
- Modify: `shared/prompts/knowledge.py` — `KnowledgeModule` gains `source`/`source_ref`, `body` becomes optional, add `is_jit`; `KnowledgeStore.all_modules()` selects the new columns
- Modify: `shared/tests/test_prompt_knowledge.py`

**Phase 2 — provider seam**
- Create: `shared/prompts/providers.py` — `ResolutionContext`, `ContextProvider` protocol, `ProviderRegistry`
- Create: `shared/tests/test_context_providers.py`

**Phase 3 — resolver**
- Create: `chat_orchestrator/orchestrator/services/jit_context_resolver.py`
- Create: `chat_orchestrator/tests/test_jit_context_resolver.py`
- Modify: `chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py`

**Phase 4 — directory provider**
- Create: `chat_orchestrator/orchestrator/services/providers/directory_provider.py`
- Create: `chat_orchestrator/tests/test_directory_provider.py`
- Create: `scripts/seed_context_provider_modules.py`
- Modify: `prepare_context.py` — remove `_fetch_enrichment` (last task of the phase only)

**Phase 5 — gdoc provider**
- Create: `shared/prompts/providers_gdoc.py`
- Create: `shared/tests/test_gdoc_provider.py`
- Modify: `shared/prompts/core.py` — `_compose_knowledge` resolves `gdoc` bodies

**Phase 6 — graph provider**
- Create: `db/migrations/0018_summarize_entity_graph.sql`
- Create: `chat_orchestrator/orchestrator/services/providers/graph_provider.py`
- Create: `chat_orchestrator/tests/test_graph_provider.py`

**Phase 7 — episodic provider**
- Create: `db/migrations/0019_episodic_distillations.sql`
- Create: `chat_orchestrator/orchestrator/services/providers/episodic_provider.py`
- Create: `chat_orchestrator/tests/test_episodic_provider.py`
- Create: `scripts/distill_episodic_memory.py`

**Phase 8 — MCP tool**
- Modify: `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py`
- Modify: `mcp_servers/tests/servers/knowledge_server/test_knowledge_module_tool.py`

**Phase 9 — admin UI**
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py`
- Modify: `anansi_app/tests/test_knowledge_modules_page.py`

---

# Phase 1 — Model and migration

### Task 1: Migration for provider sources

**Files:**
- Create: `db/migrations/0017_context_module_providers.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 0017_context_module_providers.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 1 of docs/superpowers/plans/2026-08-20-p1-resolvable-context-modules.md.
-- Lets a knowledge module declare that its body comes from a provider resolved
-- at render time rather than from the `body` column. Existing rows are all
-- 'manual' or 'ingested' and are unaffected.

BEGIN;

ALTER TABLE knowledge_modules
    DROP CONSTRAINT IF EXISTS knowledge_modules_source_chk;

ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_source_chk
        CHECK (source IN ('manual', 'gdoc', 'ingested', 'graph', 'directory', 'episodic'));

-- A provider-backed module stores no body.
ALTER TABLE knowledge_modules
    ALTER COLUMN body DROP NOT NULL;

ALTER TABLE knowledge_modules
    DROP CONSTRAINT IF EXISTS knowledge_modules_body_required_chk;

ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_body_required_chk
        CHECK (source IN ('graph', 'directory', 'episodic') OR body IS NOT NULL);

COMMIT;
```

- [ ] **Step 2: Mirror it into the schema file**

Apply the same three changes to `db/schema/chat_db.sql` at the
`CREATE TABLE IF NOT EXISTS knowledge_modules` block (line ~850): change the
`body text NOT NULL` line to `body text`, and replace the
`knowledge_modules_source_chk` constraint with the six-value version above, adding
`knowledge_modules_body_required_chk` after it.

- [ ] **Step 3: Commit**

```bash
git add db/migrations/0017_context_module_providers.sql db/schema/chat_db.sql
git commit -m "feat(context): allow provider-backed knowledge module sources"
```

---

### Task 2: `KnowledgeModule` carries its source

**Files:**
- Modify: `shared/prompts/knowledge.py:26-38`
- Test: `shared/tests/test_prompt_knowledge.py`

- [ ] **Step 1: Write the failing test**

Append to `shared/tests/test_prompt_knowledge.py`:

```python
def test_module_defaults_to_manual_source():
    assert _module("comms").source == "manual"


def test_manual_module_is_not_jit():
    assert _module("comms").is_jit is False


def test_provider_sources_are_jit():
    for source in ("graph", "directory", "episodic"):
        module = KnowledgeModule(
            id=source, slug=source, title=source, summary="s", body=None, source=source
        )
        assert module.is_jit is True, source


def test_gdoc_module_is_not_jit():
    """gdoc resolves synchronously inside PromptLibrary, not via the async resolver."""
    module = KnowledgeModule(
        id="d", slug="d", title="D", summary="s", body=None, source="gdoc", source_ref="abc123"
    )
    assert module.is_jit is False
    assert module.source_ref == "abc123"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_prompt_knowledge.py -k "source or jit" -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'source'`

- [ ] **Step 3: Add the fields**

In `shared/prompts/knowledge.py`, replace the `KnowledgeModule` dataclass:

```python
@dataclass(frozen=True)
class KnowledgeModule:
    id: str
    slug: str
    title: str
    summary: str
    body: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    scope: str = "sector"
    mode: str = "pinned"
    source: str = "manual"
    source_ref: Optional[str] = None

    @property
    def is_site_scoped(self) -> bool:
        return self.scope.startswith("site:")

    @property
    def is_jit(self) -> bool:
        """Whether this module's body needs async resolution.

        `gdoc` is deliberately excluded: it resolves synchronously inside
        PromptLibrary via a TTL-cached fetch, the same way prompt-level doc
        overrides already do. Only sources needing per-request permission
        filtering against the database are JIT.
        """
        return self.source in JIT_SOURCES
```

Add above the dataclass:

```python
JIT_SOURCES: Tuple[str, ...] = ("graph", "directory", "episodic")
```

`body` moving to a default requires no call-site change: every existing
construction passes it positionally or by keyword.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_prompt_knowledge.py -v`
Expected: PASS, all pre-existing tests still green

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/knowledge.py shared/tests/test_prompt_knowledge.py
git commit -m "feat(context): KnowledgeModule declares its body source"
```

---

### Task 3: `budget_pinned` and `render_pinned` tolerate a null body

**Files:**
- Modify: `shared/prompts/knowledge.py`
- Test: `shared/tests/test_prompt_knowledge.py`

A JIT module reaching `budget_pinned` before its body resolves would crash on
`len(module.body)`. It must be counted as zero-cost, because its real cost is not
known until the resolver runs.

- [ ] **Step 1: Write the failing test**

```python
def test_budget_treats_unresolved_body_as_zero_cost():
    jit = KnowledgeModule(
        id="g", slug="graph", title="Graph", summary="s", body=None, source="graph"
    )
    kept, dropped = budget_pinned([jit])
    assert kept == [jit]
    assert dropped == []


def test_render_pinned_skips_unresolved_bodies():
    jit = KnowledgeModule(
        id="g", slug="graph", title="Graph", summary="s", body=None, source="graph"
    )
    assert render_pinned([jit]) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_prompt_knowledge.py -k "unresolved" -v`
Expected: FAIL with `TypeError: object of type 'NoneType' has no len()`

- [ ] **Step 3: Guard both functions**

In `budget_pinned`, replace `size = len(module.body)` with:

```python
        # An unresolved provider body costs nothing here -- its real size is
        # only known once JitContextResolver runs, outside this budget.
        size = len(module.body or "")
```

In `render_pinned`, replace the `parts` comprehension with:

```python
    parts = [f"## {m.title}\n\n{m.body.strip()}" for m in modules if m.body]
    if not parts:
        return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_prompt_knowledge.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/knowledge.py shared/tests/test_prompt_knowledge.py
git commit -m "fix(context): budget and render tolerate unresolved module bodies"
```

---

### Task 4: `KnowledgeStore` selects the new columns

**Files:**
- Modify: `shared/prompts/knowledge.py` — `all_modules()`
- Test: `shared/tests/test_prompt_knowledge_store.py`

- [ ] **Step 1: Write the failing test**

Append to `shared/tests/test_prompt_knowledge_store.py`:

```python
def test_all_modules_reads_source_columns():
    """The store must select source/source_ref or every module looks manual."""

    class _Result:
        data = [
            {
                "id": "1", "slug": "graph-overview", "title": "Graph", "summary": "s",
                "body": None, "tags": [], "scope": "sector", "mode": "pinned",
                "source": "graph", "source_ref": None,
            }
        ]

    class _Table:
        def __init__(self):
            self.selected = ""

        def select(self, columns):
            self.selected = columns
            return self

        def eq(self, *_a, **_k):
            return self

        def execute(self):
            return _Result()

    class _Client:
        def __init__(self):
            self.table_obj = _Table()

        def table(self, _name):
            return self.table_obj

    client = _Client()
    store = KnowledgeStore(client=client)
    modules = store.all_modules()

    assert "source" in client.table_obj.selected
    assert "source_ref" in client.table_obj.selected
    assert modules[0].source == "graph"
    assert modules[0].is_jit is True
```

Ensure the file imports `KnowledgeStore` from `shared.prompts.knowledge`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_prompt_knowledge_store.py -k source_columns -v`
Expected: FAIL — `assert "source" in "id, slug, title, summary, body, tags, scope, mode"`

- [ ] **Step 3: Widen the select**

In `KnowledgeStore.all_modules()`:

```python
            result = (
                self._client.table("knowledge_modules")
                .select(
                    "id, slug, title, summary, body, tags, scope, mode, source, source_ref"
                )
                .eq("is_active", True)
                .execute()
            )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_prompt_knowledge_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/knowledge.py shared/tests/test_prompt_knowledge_store.py
git commit -m "feat(context): knowledge store reads source and source_ref"
```

---

### Task 5: Phase 1 verification

- [ ] **Step 1: Run the full suites that touch prompts**

Run: `python -m pytest shared/tests/ chat_orchestrator/tests/ -q`
Expected: PASS, no new failures

- [ ] **Step 2: Run pre-commit**

Run: `pre-commit run --all-files`
Expected: all hooks pass. If `test-wiring` reports untracked files under `tests/`,
vet them for operator data and `git add -f` each one, then re-run.

- [ ] **Step 3: Apply migration 0017 to production chat_db**

Paste `db/migrations/0017_context_module_providers.sql` into the Supabase SQL
editor against chat_db and run it. Confirm:

```sql
SELECT conname FROM pg_constraint WHERE conname LIKE 'knowledge_modules%chk';
```
Expected: `knowledge_modules_mode_chk`, `knowledge_modules_source_chk`,
`knowledge_modules_body_required_chk`

Nothing observable changes yet. This phase is deliberately inert.

---

# Phase 2 — The provider seam

### Task 6: `ResolutionContext`

**Files:**
- Create: `shared/prompts/providers.py`
- Test: `shared/tests/test_context_providers.py`

- [ ] **Step 1: Write the failing test**

Create `shared/tests/test_context_providers.py`:

```python
"""Context provider seam: resolution context, protocol, registry."""

import pytest

from shared.prompts.providers import ProviderRegistry, ResolutionContext
from shared.prompts.types import RequestScope


def test_resolution_context_defaults_are_empty_and_unprivileged():
    ctx = ResolutionContext(scope=RequestScope())
    assert ctx.organization_ids == ()
    assert ctx.role_ids == ()
    assert ctx.is_staff is False
    assert ctx.user_email is None


def test_resolution_context_is_hashable():
    """Providers cache on it; a mutable context would be a correctness bug."""
    ctx = ResolutionContext(scope=RequestScope(organization_id="7"), organization_ids=("7",))
    assert hash(ctx) == hash(
        ResolutionContext(scope=RequestScope(organization_id="7"), organization_ids=("7",))
    )


def test_resolution_context_rejects_a_list_of_orgs():
    """Lists are unhashable and would silently defeat provider caching."""
    with pytest.raises(TypeError):
        hash(ResolutionContext(scope=RequestScope(), organization_ids=["7"]))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_context_providers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.prompts.providers'`

- [ ] **Step 3: Create the module**

Create `shared/prompts/providers.py`:

```python
"""The seam between a knowledge module and whatever produces its body.

A module whose `source` is not 'manual' has no stored body. This module
defines what resolves one: the per-request authorization context a provider
needs, the provider protocol itself, and a registry mapping source -> provider.

Deliberately split from knowledge.py, which stays a pure value/selection
module with no I/O and no async.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol, Tuple, runtime_checkable

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.types import RequestScope


@dataclass(frozen=True)
class ResolutionContext:
    """Who is asking, for authorization purposes.

    Deliberately separate from RequestScope, which answers "which modules
    apply to this conversation" (a selection question). This answers "what
    may this caller see" (an authorization question). Conflating them would
    let a visibility rule silently become an access-control rule.

    Tuples, not lists: providers cache on this object, and an unhashable
    field would defeat that silently rather than loudly.
    """

    scope: RequestScope
    user_email: Optional[str] = None
    organization_ids: Tuple[str, ...] = ()
    role_ids: Tuple[str, ...] = ()
    is_staff: bool = False


@runtime_checkable
class ContextProvider(Protocol):
    """Produces a module's body at render time.

    Returning None means "nothing to contribute" and is normal, not an
    error -- an episodic module for a grid with no distillation yet, for
    instance. Raising is also survivable: the resolver catches it.
    """

    source: str

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]: ...


@dataclass
class ProviderRegistry:
    """Maps a module's `source` to the provider that resolves it."""

    _providers: Dict[str, ContextProvider] = field(default_factory=dict)

    def register(self, provider: ContextProvider) -> None:
        self._providers[provider.source] = provider

    def get(self, source: str) -> Optional[ContextProvider]:
        return self._providers.get(source)

    def sources(self) -> Tuple[str, ...]:
        return tuple(sorted(self._providers))


__all__ = ["ContextProvider", "ProviderRegistry", "ResolutionContext"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_context_providers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/providers.py
git add -f shared/tests/test_context_providers.py
git commit -m "feat(context): add the context provider seam"
```

---

### Task 7: `ResolutionContext.from_user_context`

**Files:**
- Modify: `shared/prompts/providers.py`
- Test: `shared/tests/test_context_providers.py`

- [ ] **Step 1: Write the failing test**

```python
def test_from_user_context_maps_permission_fields():
    class _UC:
        user_email = "tech@example.com"
        organization_ids = ["7", "9"]
        roles = ["ops"]
        is_staff = True

    ctx = ResolutionContext.from_user_context(_UC(), grid="Alpha")
    assert ctx.user_email == "tech@example.com"
    assert ctx.organization_ids == ("7", "9")
    assert ctx.role_ids == ("ops",)
    assert ctx.is_staff is True
    assert ctx.scope.grid == "Alpha"
    assert ctx.scope.organization_id == "7"


def test_from_user_context_handles_none():
    ctx = ResolutionContext.from_user_context(None)
    assert ctx.organization_ids == ()
    assert ctx.is_staff is False
    assert ctx.scope.organization_id is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_context_providers.py -k from_user_context -v`
Expected: FAIL with `AttributeError: type object 'ResolutionContext' has no attribute 'from_user_context'`

- [ ] **Step 3: Add the constructor**

Add to `ResolutionContext`:

```python
    @classmethod
    def from_user_context(cls, user_context, grid: Optional[str] = None) -> "ResolutionContext":
        """Build from an orchestrator UserContext (or None).

        Duck-typed rather than importing orchestrator.models.schemas: this
        module lives in `shared`, which must not depend on the orchestrator.
        Mirrors instructions_provider.py's existing convention of taking
        organization_ids[0] as the scope org.
        """
        if user_context is None:
            return cls(scope=RequestScope(grid=grid))
        org_ids = tuple(getattr(user_context, "organization_ids", None) or ())
        return cls(
            scope=RequestScope(grid=grid, organization_id=org_ids[0] if org_ids else None),
            user_email=getattr(user_context, "user_email", None),
            organization_ids=org_ids,
            role_ids=tuple(getattr(user_context, "roles", None) or ()),
            is_staff=bool(getattr(user_context, "is_staff", False)),
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_context_providers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/providers.py shared/tests/test_context_providers.py
git commit -m "feat(context): build ResolutionContext from a UserContext"
```

---

# Phase 3 — The resolver

### Task 8: `JitContextResolver` selects and dispatches

**Files:**
- Create: `chat_orchestrator/orchestrator/services/jit_context_resolver.py`
- Test: `chat_orchestrator/tests/test_jit_context_resolver.py`

- [ ] **Step 1: Write the failing test**

Create `chat_orchestrator/tests/test_jit_context_resolver.py`:

```python
"""JIT context resolution: selection, concurrency, fail-open, rendering."""

import asyncio

import pytest

from orchestrator.services.jit_context_resolver import JitContextResolver
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ProviderRegistry, ResolutionContext
from shared.prompts.types import RequestScope


def _module(slug, source="graph", mode="pinned", scope="sector"):
    return KnowledgeModule(
        id=slug, slug=slug, title=slug.title(), summary=f"About {slug}.",
        body=None, scope=scope, mode=mode, source=source,
    )


class _FakeStore:
    def __init__(self, modules, pins):
        self._modules = modules
        self._pins = pins

    def all_modules(self):
        return self._modules

    def overrides_for(self, _prompt_id):
        return self._pins


class _FakeProvider:
    def __init__(self, source, text=None, raises=None, delay=0.0):
        self.source = source
        self._text = text
        self._raises = raises
        self._delay = delay
        self.calls = 0

    async def resolve(self, module, ctx):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise self._raises
        return self._text


def _resolver(modules, pins, providers):
    registry = ProviderRegistry()
    for p in providers:
        registry.register(p)
    return JitContextResolver(store=_FakeStore(modules, pins), registry=registry)


@pytest.mark.asyncio
async def test_resolves_a_pinned_jit_module():
    provider = _FakeProvider("graph", text="Entity types: Meter, DCU.")
    resolver = _resolver([_module("graph-overview")], {"graph-overview": True}, [provider])

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert "Entity types: Meter, DCU." in text
    assert used == ["graph-overview"]
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_ignores_unpinned_modules():
    provider = _FakeProvider("graph", text="X")
    resolver = _resolver([_module("graph-overview")], {}, [provider])

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert text == ""
    assert used == []
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_ignores_non_jit_modules():
    """A manual module's body is PromptLibrary's job, not the resolver's."""
    manual = KnowledgeModule(
        id="m", slug="m", title="M", summary="s", body="stored", source="manual"
    )
    provider = _FakeProvider("manual", text="should not be called")
    resolver = _resolver([manual], {"m": True}, [provider])

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert text == ""
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_a_raising_provider_does_not_break_the_others():
    good = _FakeProvider("graph", text="Graph body")
    bad = _FakeProvider("directory", raises=RuntimeError("boom"))
    resolver = _resolver(
        [_module("graph-overview"), _module("directory", source="directory")],
        {"graph-overview": True, "directory": True},
        [good, bad],
    )

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert "Graph body" in text
    assert used == ["graph-overview"]


@pytest.mark.asyncio
async def test_a_provider_returning_none_contributes_nothing():
    provider = _FakeProvider("episodic", text=None)
    resolver = _resolver(
        [_module("episodic", source="episodic")], {"episodic": True}, [provider]
    )

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert text == ""
    assert used == []


@pytest.mark.asyncio
async def test_a_slow_provider_times_out_without_blocking():
    slow = _FakeProvider("graph", text="late", delay=5.0)
    resolver = _resolver([_module("graph-overview")], {"graph-overview": True}, [slow])
    resolver.timeout_seconds = 0.05

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert text == ""
    assert used == []


@pytest.mark.asyncio
async def test_module_with_no_registered_provider_is_skipped():
    resolver = _resolver([_module("graph-overview")], {"graph-overview": True}, [])

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert text == ""
    assert used == []


@pytest.mark.asyncio
async def test_scope_gate_still_applies():
    """A site-scoped module stays out of a conversation about another site."""
    provider = _FakeProvider("graph", text="Alpha graph")
    resolver = _resolver(
        [_module("graph-overview", scope="site:Alpha")], {"graph-overview": True}, [provider]
    )

    text, _ = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope(grid="Beta"))
    )

    assert text == ""
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_on_demand_jit_module_renders_a_catalog_line_not_a_body():
    provider = _FakeProvider("graph", text="full body")
    resolver = _resolver(
        [_module("graph-overview", mode="on_demand")], {"graph-overview": True}, [provider]
    )

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert "graph-overview" in text
    assert "About graph-overview." in text
    assert "full body" not in text
    assert provider.calls == 0
    assert used == ["graph-overview"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/test_jit_context_resolver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.services.jit_context_resolver'`

- [ ] **Step 3: Write the resolver**

Create `chat_orchestrator/orchestrator/services/jit_context_resolver.py`:

```python
"""Resolves just-in-time context modules for one request.

PromptLibrary.render() is synchronous, and graph/directory/episodic bodies
need async, permission-filtered database work. Rather than make render()
async -- it is called from dozens of sync sites including scripts and MCP
servers -- those three sources resolve here and their output is appended to
context_message.

The pins are read through KnowledgeStore.overrides_for, the same call
PromptLibrary._compose_knowledge uses. There must never be a second
mechanism for deciding what a prompt is attached to.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

from shared.prompts.knowledge import KnowledgeModule, select_for_prompt
from shared.prompts.providers import ProviderRegistry, ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 5.0


class JitContextResolver:
    """Resolves the provider-backed modules a prompt pins."""

    def __init__(self, store=None, registry: Optional[ProviderRegistry] = None) -> None:
        if store is None:
            from shared.prompts.knowledge import KnowledgeStore

            store = KnowledgeStore.from_env()
        self._store = store
        self._registry = registry if registry is not None else ProviderRegistry()
        self.timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    async def resolve_for_prompt(
        self, prompt_id: str, ctx: ResolutionContext
    ) -> Tuple[str, List[str]]:
        """Return (text_block, slugs_used). Never raises."""
        try:
            modules = self._store.all_modules()
            pins = self._store.overrides_for(prompt_id)
        except Exception:
            LOGGER.warning(
                f"JIT module lookup failed for '{prompt_id}'; continuing without", exc_info=True
            )
            return "", []

        chosen = [m for m in select_for_prompt(modules, pins, ctx.scope) if m.is_jit]
        if not chosen:
            return "", []

        pinned = [m for m in chosen if m.mode == "pinned"]
        on_demand = [m for m in chosen if m.mode != "pinned"]

        resolved = await self._resolve_all(pinned, ctx)

        blocks: List[str] = []
        used: List[str] = []

        if resolved:
            body = "\n\n".join(f"## {m.title}\n\n{text.strip()}" for m, text in resolved)
            blocks.append(f"# Live Context\n\n{body}")
            used.extend(m.slug for m, _ in resolved)

        if on_demand:
            lines = "\n".join(f"- `{m.slug}` — {m.summary}" for m in sorted(on_demand, key=lambda m: m.slug))
            blocks.append(
                "# Available Live Context\n\n"
                "Fetch any of these with the `get_knowledge_module` tool when relevant:\n\n"
                + lines
            )
            used.extend(m.slug for m in on_demand)

        return "\n\n".join(blocks), used

    async def _resolve_all(
        self, modules: List[KnowledgeModule], ctx: ResolutionContext
    ) -> List[Tuple[KnowledgeModule, str]]:
        """Resolve concurrently. A failure drops one module, never the batch."""
        pending = [(m, self._registry.get(m.source)) for m in modules]
        runnable = [(m, p) for m, p in pending if p is not None]

        for module, provider in pending:
            if provider is None:
                LOGGER.warning(
                    f"Module '{module.slug}' declares source '{module.source}' "
                    f"with no registered provider; skipping"
                )

        if not runnable:
            return []

        results = await asyncio.gather(
            *(self._resolve_one(m, p, ctx) for m, p in runnable),
            return_exceptions=True,
        )

        out: List[Tuple[KnowledgeModule, str]] = []
        for (module, _), result in zip(runnable, results):
            if isinstance(result, BaseException):
                LOGGER.warning(
                    f"Provider '{module.source}' failed for module '{module.slug}': {result}"
                )
                continue
            if result:
                out.append((module, result))
        return out

    async def _resolve_one(self, module, provider, ctx: ResolutionContext) -> Optional[str]:
        return await asyncio.wait_for(
            provider.resolve(module, ctx), timeout=self.timeout_seconds
        )


__all__ = ["JitContextResolver"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/test_jit_context_resolver.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/jit_context_resolver.py
git add -f chat_orchestrator/tests/test_jit_context_resolver.py
git commit -m "feat(context): add JitContextResolver"
```

---

### Task 9: Wire the resolver into `prepare_context`

**Files:**
- Modify: `chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py`
- Test: `chat_orchestrator/tests/test_jit_context_resolver.py`

With no providers registered this is a no-op, which is the point: the wiring ships
and is verified before any provider exists.

- [ ] **Step 1: Write the failing test**

Append to `chat_orchestrator/tests/test_jit_context_resolver.py`:

```python
@pytest.mark.asyncio
async def test_fetch_jit_context_returns_empty_when_nothing_registered():
    from orchestrator.graphs.nodes.prepare_context import _fetch_jit_context

    text, used = await _fetch_jit_context("staff.system", None)
    assert text == ""
    assert used == []


@pytest.mark.asyncio
async def test_fetch_jit_context_never_raises(monkeypatch):
    import orchestrator.graphs.nodes.prepare_context as pc

    def _boom(*_a, **_k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(pc, "get_jit_resolver", _boom)
    text, used = await pc._fetch_jit_context("staff.system", None)
    assert text == ""
    assert used == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/test_jit_context_resolver.py -k _fetch_jit -v`
Expected: FAIL with `ImportError: cannot import name '_fetch_jit_context'`

- [ ] **Step 3: Add the fetcher and the singleton**

Add to `chat_orchestrator/orchestrator/services/jit_context_resolver.py`:

```python
_RESOLVER: Optional[JitContextResolver] = None


def get_jit_resolver() -> JitContextResolver:
    """Process-wide resolver, so provider-internal caches are actually reused.

    Providers are registered here at first construction. A provider that
    cannot be built (missing credentials, missing table) is simply absent;
    modules naming it log a warning per request and contribute nothing.
    """
    global _RESOLVER
    if _RESOLVER is None:
        _RESOLVER = JitContextResolver()
    return _RESOLVER
```

Add `get_jit_resolver` to that module's `__all__`.

In `prepare_context.py`, add the import near the other module-level imports:

```python
from orchestrator.services.jit_context_resolver import get_jit_resolver
```

and add this function next to `_fetch_enrichment`:

```python
async def _fetch_jit_context(
    prompt_id: str,
    user_context: Optional[UserContext],
    grid: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Resolve provider-backed context modules. Fail open."""
    try:
        from shared.prompts.providers import ResolutionContext

        ctx = ResolutionContext.from_user_context(user_context, grid=grid)
        return await get_jit_resolver().resolve_for_prompt(prompt_id, ctx)
    except Exception as e:
        LOGGER.warning(f"JIT context resolution failed (continuing without): {e}")
        return "", []
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/test_jit_context_resolver.py -v`
Expected: PASS

- [ ] **Step 5: Call it from `prepare_context`**

`prepare_context` must know which prompt was rendered. `InstructionsProvider`
picks `staff.system` or `customer.system` from `user_context.is_staff`
(`instructions_provider.py:373-381`), so derive it the same way rather than
threading a new return value through.

In `prepare_context`, add to the `asyncio.gather` call — append as the last
awaitable and the last tuple element:

```python
    _prompt_id = "staff.system" if (user_context and user_context.is_staff) else "customer.system"

    (
        (system_instructions, context_message, prompt_provenance),
        troubleshooting_procedures,
        rag_docs,
        verification_instructions,
        (enrichment_context, grid_names),
        user_preferences,
        (jit_context, jit_used),
    ) = await asyncio.gather(
        _fetch_instructions(user_context, entity_ctx),
        _fetch_troubleshooting(),
        _fetch_rag_context(user_input, user_email, user_context),
        _fetch_verification_instructions(),
        _fetch_enrichment(user_context),
        _fetch_user_preferences(user_context),
        _fetch_jit_context(_prompt_id, user_context),
    )
```

Then, immediately after the existing enrichment append block, add:

```python
    # Append just-in-time context modules
    if jit_context:
        context_message = (
            f"{context_message}\n\n{jit_context}" if context_message else jit_context
        )
        LOGGER.info(
            f"Added JIT context: {len(jit_used)} module(s) "
            f"({', '.join(jit_used)}), {len(jit_context)} chars"
        )
```

- [ ] **Step 6: Run the orchestrator suite**

Run: `python -m pytest chat_orchestrator/tests/ -q`
Expected: PASS, no new failures

- [ ] **Step 7: Commit**

```bash
git add chat_orchestrator/orchestrator/services/jit_context_resolver.py \
        chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py \
        chat_orchestrator/tests/test_jit_context_resolver.py
git commit -m "feat(context): wire JIT context resolution into prepare_context"
```

---

# Phase 4 — Directory provider

### Task 10: `DirectoryProvider` renders grids, organizations and users

**Files:**
- Create: `chat_orchestrator/orchestrator/services/providers/__init__.py`
- Create: `chat_orchestrator/orchestrator/services/providers/directory_provider.py`
- Test: `chat_orchestrator/tests/test_directory_provider.py`

- [ ] **Step 1: Write the failing test**

Create `chat_orchestrator/tests/test_directory_provider.py`:

```python
"""Directory provider: grids, organizations and users, permission-filtered."""

import pytest

from orchestrator.services.providers.directory_provider import (
    DirectoryProvider,
    render_directory,
)
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.prompts.types import RequestScope


def _module():
    return KnowledgeModule(
        id="d", slug="directory", title="Directory", summary="Known entities.",
        body=None, source="directory",
    )


def test_render_includes_every_populated_section():
    text = render_directory(
        grids=["Alpha", "Beta"], organizations=["Org A"], users=["Ada L."]
    )
    assert "Alpha, Beta" in text
    assert "Org A" in text
    assert "Ada L." in text


def test_render_omits_empty_sections():
    text = render_directory(grids=["Alpha"], organizations=[], users=[])
    assert "Alpha" in text
    assert "organizations" not in text.lower()


def test_render_returns_none_when_everything_is_empty():
    assert render_directory(grids=[], organizations=[], users=[]) is None


def test_render_includes_the_disambiguation_hint():
    text = render_directory(grids=["Alpha"], organizations=[], users=[])
    assert "matches a grid" in text


@pytest.mark.asyncio
async def test_customer_sees_only_their_own_org_grids():
    class _Auth:
        async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
            if include_all:
                return ["Alpha", "Beta", "Gamma"]
            return {"7": ["Alpha"], "9": ["Gamma"]}[organization_id]

    provider = DirectoryProvider(auth_service=_Auth(), jira_fetcher=None)
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7",), is_staff=False
    )

    text = await provider.resolve(_module(), ctx)

    assert "Alpha" in text
    assert "Beta" not in text
    assert "Gamma" not in text


@pytest.mark.asyncio
async def test_staff_sees_every_grid():
    class _Auth:
        async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
            return ["Alpha", "Beta", "Gamma"] if include_all else ["Alpha"]

    provider = DirectoryProvider(auth_service=_Auth(), jira_fetcher=None)
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=("7",), is_staff=True)

    text = await provider.resolve(_module(), ctx)

    assert "Alpha" in text and "Beta" in text and "Gamma" in text


@pytest.mark.asyncio
async def test_customer_never_sees_jira_users_or_organizations():
    """Jira data is staff-only -- the pre-existing rule in ContextEnrichmentProvider."""

    class _Auth:
        async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
            return ["Alpha"]

    class _Jira:
        async def participants(self):
            return ["Ada L."]

        async def organizations(self):
            return ["Org A"]

    provider = DirectoryProvider(auth_service=_Auth(), jira_fetcher=_Jira())
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7",), is_staff=False
    )

    text = await provider.resolve(_module(), ctx)

    assert "Ada L." not in text
    assert "Org A" not in text


@pytest.mark.asyncio
async def test_a_failing_auth_service_yields_no_grids_not_an_exception():
    class _Auth:
        async def get_grid_names_for_organization(self, **_k):
            raise RuntimeError("auth down")

    provider = DirectoryProvider(auth_service=_Auth(), jira_fetcher=None)
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=("7",), is_staff=True)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_source_is_directory():
    assert DirectoryProvider(auth_service=None, jira_fetcher=None).source == "directory"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/test_directory_provider.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.services.providers'`

- [ ] **Step 3: Write the provider**

Create `chat_orchestrator/orchestrator/services/providers/__init__.py`:

```python
"""Async context providers for just-in-time knowledge modules."""
```

Create `chat_orchestrator/orchestrator/services/providers/directory_provider.py`:

```python
"""The grids / organizations / users directory, as a context module.

Replaces ContextEnrichmentProvider's hardcoded injection at
prepare_context.py's _fetch_enrichment. Same data, same staff gate, but
reachable as a module an operator can attach to specific prompts or detach
entirely -- which the hardcoded path never allowed.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

GRID_CACHE_TTL = 300
JIRA_CACHE_TTL = 600

# Keyed on the permission set, never on the module -- caching a staff-wide
# grid list under a key a customer request can hit is exactly the bug this
# provider exists to make impossible.
_CACHE: Dict[Tuple, Tuple[float, Any]] = {}


def _cached(key: Tuple, ttl: float):
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _store(key: Tuple, ttl: float, value: Any) -> Any:
    _CACHE[key] = (time.time() + ttl, value)
    return value


def render_directory(
    grids: List[str], organizations: List[str], users: List[str]
) -> Optional[str]:
    """Format the directory. None when there is nothing to say."""
    parts: List[str] = []
    if grids:
        parts.append(f"Available grids: {', '.join(grids)}")
    if organizations:
        parts.append(f"Available organizations: {', '.join(organizations)}")
    if users:
        parts.append(f"Team members: {', '.join(users)}")
    if not parts:
        return None
    parts.append(
        "When a user mentions a name, check if it matches a grid, team member, "
        "or organization above."
    )
    return "\n".join(parts)


class DirectoryProvider:
    """Resolves the `directory` source."""

    source = "directory"

    def __init__(self, auth_service: Any = None, jira_fetcher: Any = None) -> None:
        if auth_service is None:
            from shared.auth import get_auth_service

            auth_service = get_auth_service()
        self._auth = auth_service
        self._jira = jira_fetcher

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]:
        grids = await self._grids(ctx)
        users: List[str] = []
        organizations: List[str] = []
        if ctx.is_staff and self._jira is not None:
            users = await self._jira_users()
            organizations = await self._jira_organizations()
        return render_directory(grids, organizations, users)

    async def _grids(self, ctx: ResolutionContext) -> List[str]:
        key = ("grids", "all" if ctx.is_staff else ctx.organization_ids)
        hit = _cached(key, GRID_CACHE_TTL)
        if hit is not None:
            return list(hit)
        try:
            if ctx.is_staff:
                names = await self._auth.get_grid_names_for_organization(include_all=True)
            elif ctx.organization_ids:
                names = await self._auth.get_grid_names_for_organization(
                    organization_id=ctx.organization_ids[0]
                )
            else:
                names = []
        except Exception as e:
            LOGGER.warning(f"Directory grid lookup failed: {e}")
            return []
        return list(_store(key, GRID_CACHE_TTL, list(names)))

    async def _jira_users(self) -> List[str]:
        hit = _cached(("jira_users",), JIRA_CACHE_TTL)
        if hit is not None:
            return list(hit)
        try:
            names = await self._jira.participants()
        except Exception as e:
            LOGGER.warning(f"Directory user lookup failed: {e}")
            return []
        return list(_store(("jira_users",), JIRA_CACHE_TTL, list(names)))

    async def _jira_organizations(self) -> List[str]:
        hit = _cached(("jira_orgs",), JIRA_CACHE_TTL)
        if hit is not None:
            return list(hit)
        try:
            names = await self._jira.organizations()
        except Exception as e:
            LOGGER.warning(f"Directory organization lookup failed: {e}")
            return []
        return list(_store(("jira_orgs",), JIRA_CACHE_TTL, list(names)))


__all__ = ["DirectoryProvider", "render_directory"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/test_directory_provider.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/providers/
git add -f chat_orchestrator/tests/test_directory_provider.py
git commit -m "feat(context): add DirectoryProvider"
```

---

### Task 11: Parity test against the hardcoded path

**Files:**
- Test: `chat_orchestrator/tests/test_directory_provider.py`

The hardcoded path can only be deleted on evidence. This test is that evidence.

- [ ] **Step 1: Write the test**

```python
def test_render_directory_matches_the_hardcoded_formatter():
    """Parity with ContextEnrichmentProvider._format_enrichment_text.

    Guards the deletion in Task 14. The only intended difference is the
    label wording: 'Jira Ops team members' becomes 'Team members' and
    'Available JIRA organizations' becomes 'Available organizations',
    because the module is no longer Jira-specific by definition.
    """
    from orchestrator.services.context_enrichment import ContextEnrichmentProvider

    grids = ["Alpha", "Beta"]
    users = ["Ada L."]
    orgs = ["Org A"]

    old = ContextEnrichmentProvider._format_enrichment_text(
        ContextEnrichmentProvider.__new__(ContextEnrichmentProvider), grids, users, orgs
    )
    new = render_directory(grids=grids, organizations=orgs, users=users)

    for name in grids + users + orgs:
        assert name in old and name in new
    assert "matches a grid" in old and "matches a grid" in new
```

- [ ] **Step 2: Run it**

Run: `python -m pytest chat_orchestrator/tests/test_directory_provider.py -k parity -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add chat_orchestrator/tests/test_directory_provider.py
git commit -m "test(context): parity between DirectoryProvider and the hardcoded path"
```

---

### Task 12: Register the provider

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/jit_context_resolver.py`
- Test: `chat_orchestrator/tests/test_jit_context_resolver.py`

- [ ] **Step 1: Write the failing test**

```python
def test_default_registry_includes_the_directory_provider():
    from orchestrator.services.jit_context_resolver import build_default_registry

    assert "directory" in build_default_registry().sources()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/test_jit_context_resolver.py -k default_registry -v`
Expected: FAIL with `ImportError: cannot import name 'build_default_registry'`

- [ ] **Step 3: Add the builder**

In `jit_context_resolver.py`, add above `get_jit_resolver`:

```python
def build_default_registry() -> ProviderRegistry:
    """Every provider that can be constructed in this process.

    A provider whose dependencies are missing is omitted rather than
    registered-and-broken: a module naming it then logs one clear "no
    registered provider" warning per request instead of a stack trace.
    """
    registry = ProviderRegistry()
    try:
        from orchestrator.services.providers.directory_provider import DirectoryProvider

        registry.register(DirectoryProvider())
    except Exception:
        LOGGER.warning("DirectoryProvider unavailable", exc_info=True)
    return registry
```

and change `get_jit_resolver` to use it:

```python
        _RESOLVER = JitContextResolver(registry=build_default_registry())
```

Add `build_default_registry` to `__all__`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/test_jit_context_resolver.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/jit_context_resolver.py \
        chat_orchestrator/tests/test_jit_context_resolver.py
git commit -m "feat(context): register DirectoryProvider in the default registry"
```

---

### Task 13: Seed the singleton modules

**Files:**
- Create: `scripts/seed_context_provider_modules.py`
- Test: `shared/tests/test_seed_context_provider_modules.py`

- [ ] **Step 1: Write the failing test**

Create `shared/tests/test_seed_context_provider_modules.py`:

```python
"""Seeding the provider-backed singleton modules."""

from scripts.seed_context_provider_modules import SEED_MODULES, rows_to_insert


def test_seeds_directory_and_graph_only():
    assert {m["slug"] for m in SEED_MODULES} == {"directory", "entity-graph"}


def test_seeded_modules_have_no_body():
    assert all(m["body"] is None for m in SEED_MODULES)


def test_seeded_modules_have_a_summary():
    """on_demand selection is summary-only; a blank one is invisible."""
    assert all(m["summary"].strip() for m in SEED_MODULES)


def test_rows_to_insert_skips_existing_slugs():
    rows = rows_to_insert(existing_slugs={"directory"})
    assert [r["slug"] for r in rows] == ["entity-graph"]


def test_rows_to_insert_is_empty_when_all_exist():
    assert rows_to_insert(existing_slugs={"directory", "entity-graph"}) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_seed_context_provider_modules.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.seed_context_provider_modules'`

- [ ] **Step 3: Write the script**

Create `scripts/seed_context_provider_modules.py`:

```python
"""Create the singleton provider-backed context modules. Idempotent.

Both are created pinned to no prompts. Attaching them is a deliberate
operator action in the Context admin page -- seeding must never silently
change what any prompt renders.

Usage:
    python scripts/seed_context_provider_modules.py            # dry run
    python scripts/seed_context_provider_modules.py --apply
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Set

SEED_MODULES: List[Dict[str, Any]] = [
    {
        "slug": "directory",
        "title": "Known Grids, Organizations and People",
        "summary": (
            "The grids, organizations and team members this caller may see. "
            "Use to disambiguate a name mentioned in a message."
        ),
        "body": None,
        "tags": ["directory"],
        "scope": "sector",
        "mode": "pinned",
        "source": "directory",
        "source_ref": None,
    },
    {
        "slug": "entity-graph",
        "title": "Knowledge Graph Overview",
        "summary": (
            "Entity types, relationship types and example entities in the knowledge "
            "graph. Use to decide what to search for before querying the graph."
        ),
        "body": None,
        "tags": ["graph"],
        "scope": "sector",
        "mode": "pinned",
        "source": "graph",
        "source_ref": None,
    },
]


def rows_to_insert(existing_slugs: Set[str]) -> List[Dict[str, Any]]:
    """The seed rows not already present. Never updates an existing row."""
    return [m for m in SEED_MODULES if m["slug"] not in existing_slugs]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = parser.parse_args()

    from shared.config.db_credentials import chat_db_service_key, chat_db_url
    from supabase import create_client

    url, key = chat_db_url(), chat_db_service_key()
    if not (url and key):
        print("CHAT_DB_URL / CHAT_DB_SERVICE_KEY are not set", file=sys.stderr)
        return 1

    client = create_client(url, key)
    existing = {
        row["slug"]
        for row in (client.table("knowledge_modules").select("slug").execute().data or [])
    }
    rows = rows_to_insert(existing)

    if not rows:
        print("Nothing to seed; both modules already exist.")
        return 0

    for row in rows:
        print(f"  + {row['slug']} (source={row['source']}, mode={row['mode']})")

    if not args.apply:
        print(f"\nDry run. {len(rows)} module(s) would be created. Re-run with --apply.")
        return 0

    client.table("knowledge_modules").insert(rows).execute()
    print(f"\nCreated {len(rows)} module(s). Attach them to prompts in the Context page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_seed_context_provider_modules.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/seed_context_provider_modules.py
git add -f shared/tests/test_seed_context_provider_modules.py
git commit -m "feat(context): seed the directory and entity-graph modules"
```

---

### Task 14: Retire the hardcoded enrichment path

**Files:**
- Modify: `chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py`

**Do not start this task until the `directory` module has been seeded in
production, attached to `staff.system` and `customer.system`, and one live
request has been confirmed to contain the directory text from the JIT path.**
The parity test in Task 11 is necessary but not sufficient — it proves the
formatter matches, not that the wiring reaches production.

- [ ] **Step 1: Confirm production parity**

Check the orchestrator logs for a line of the form:

```
Added JIT context: 1 module(s) (directory), N chars
```

If absent, the module is not attached or the provider is failing — resolve that
before proceeding. Do not delete the fallback while it is the only working path.

- [ ] **Step 2: Remove the enrichment fetch**

In `prepare_context.py`, delete the `_fetch_enrichment` function entirely, remove
`_fetch_enrichment(user_context)` from the `asyncio.gather` call, and remove
`(enrichment_context, grid_names)` from the destructuring tuple.

Delete the enrichment append block:

```python
    # Append enrichment context
    if enrichment_context:
        ...
```

- [ ] **Step 3: Preserve the grid-name lookup**

`grid_names` is still used by the topic-scoped grid hint at the bottom of
`prepare_context`. Replace the tuple element with a dedicated fetch — the hint
needs the raw list, not the rendered directory text:

```python
async def _fetch_grid_names(user_context: Optional[UserContext]) -> List[str]:
    """Grid names for the topic-scoped grid hint. Fail open."""
    try:
        from orchestrator.services.providers.directory_provider import DirectoryProvider
        from shared.prompts.providers import ResolutionContext

        ctx = ResolutionContext.from_user_context(user_context)
        return await DirectoryProvider()._grids(ctx)
    except Exception as e:
        LOGGER.warning(f"Grid name lookup failed (continuing without): {e}")
        return []
```

Add `_fetch_grid_names(user_context)` to the `gather` in place of
`_fetch_enrichment(user_context)`, and destructure it as `grid_names` rather than
`(enrichment_context, grid_names)`.

- [ ] **Step 4: Run the orchestrator suite**

Run: `python -m pytest chat_orchestrator/tests/ -q`
Expected: PASS. Any test asserting on enrichment text in `prepare_context` should
now assert on the JIT path instead — update it rather than deleting the assertion.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py
git commit -m "refactor(context): retire hardcoded enrichment for the directory module"
```

---

# Phase 5 — Google Doc provider

### Task 15: `GDocProvider` resolves a doc body

**Files:**
- Create: `shared/prompts/providers_gdoc.py`
- Test: `shared/tests/test_gdoc_provider.py`

- [ ] **Step 1: Write the failing test**

Create `shared/tests/test_gdoc_provider.py`:

```python
"""Google Doc-backed context modules."""

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers_gdoc import GDocProvider


def _module(source_ref="doc-abc"):
    return KnowledgeModule(
        id="d", slug="procedures", title="Procedures", summary="How-tos.",
        body=None, source="gdoc", source_ref=source_ref,
    )


def test_resolves_a_doc_body():
    provider = GDocProvider(fetch=lambda doc_id: f"body of {doc_id}")
    assert provider.body_for(_module()) == "body of doc-abc"


def test_a_module_without_a_source_ref_resolves_to_none():
    provider = GDocProvider(fetch=lambda doc_id: "never")
    assert provider.body_for(_module(source_ref=None)) is None


def test_a_failing_fetch_resolves_to_none():
    def _boom(_doc_id):
        raise RuntimeError("403")

    assert GDocProvider(fetch=_boom).body_for(_module()) is None


def test_an_empty_doc_resolves_to_none():
    assert GDocProvider(fetch=lambda _d: "   ").body_for(_module()) is None


def test_results_are_cached_per_doc():
    calls = []

    def _fetch(doc_id):
        calls.append(doc_id)
        return "body"

    provider = GDocProvider(fetch=_fetch, ttl_seconds=300)
    provider.body_for(_module())
    provider.body_for(_module())
    assert calls == ["doc-abc"]


def test_invalidate_forces_a_refetch():
    calls = []

    def _fetch(doc_id):
        calls.append(doc_id)
        return "body"

    provider = GDocProvider(fetch=_fetch, ttl_seconds=300)
    provider.body_for(_module())
    provider.invalidate()
    provider.body_for(_module())
    assert calls == ["doc-abc", "doc-abc"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_gdoc_provider.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.prompts.providers_gdoc'`

- [ ] **Step 3: Write the provider**

Create `shared/prompts/providers_gdoc.py`:

```python
"""Google Drive-backed context module bodies.

Synchronous by design: this resolves inside PromptLibrary._compose_knowledge,
which is sync, and mirrors the TTL-cached GDocStore that already backs
prompt-level doc overrides. It is not a ContextProvider in the async sense --
see the plan's "sync/async boundary" note.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, Optional, Tuple

from shared.prompts.knowledge import KnowledgeModule
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DEFAULT_TTL_SECONDS = 300


class GDocProvider:
    """Resolves the `gdoc` source by Drive file id."""

    source = "gdoc"

    def __init__(
        self,
        fetch: Optional[Callable[[str], Optional[str]]] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._fetch = fetch or _default_fetch
        self._ttl = ttl_seconds
        self._cache: Dict[str, Tuple[float, Optional[str]]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    def body_for(self, module: KnowledgeModule) -> Optional[str]:
        """The doc's text, or None. Never raises."""
        doc_id = module.source_ref
        if not doc_id:
            LOGGER.warning(f"Module '{module.slug}' is gdoc-sourced but has no source_ref")
            return None

        hit = self._cache.get(doc_id)
        if hit and hit[0] > time.time():
            return hit[1]

        try:
            body = self._fetch(doc_id)
        except Exception:
            LOGGER.warning(f"Google Doc fetch failed for module '{module.slug}'", exc_info=True)
            return None

        body = body.strip() if body else None
        self._cache[doc_id] = (time.time() + self._ttl, body or None)
        return body or None


def _default_fetch(doc_id: str) -> Optional[str]:
    """Export a Drive file as text via the existing service-account plumbing."""
    from shared.prompts.gdoc import fetch_doc_text

    return fetch_doc_text(doc_id)


__all__ = ["GDocProvider"]
```

- [ ] **Step 4: Check the export helper exists**

Run: `command grep -n "def fetch_doc_text\|def _fetch\|export" shared/prompts/gdoc.py`

If `fetch_doc_text` does not exist, extract the doc-body fetch that `GDocStore.body_for`
already performs into a module-level `fetch_doc_text(doc_id) -> Optional[str]` and
have `GDocStore.body_for` call it. Do not duplicate the Drive client setup.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_gdoc_provider.py -v`
Expected: PASS, 6 tests

- [ ] **Step 6: Commit**

```bash
git add shared/prompts/providers_gdoc.py shared/prompts/gdoc.py
git add -f shared/tests/test_gdoc_provider.py
git commit -m "feat(context): add GDocProvider for doc-backed modules"
```

---

### Task 16: `_compose_knowledge` resolves gdoc bodies

**Files:**
- Modify: `shared/prompts/core.py:150-175`
- Test: `shared/tests/test_prompt_knowledge_wiring.py`

- [ ] **Step 1: Write the failing test**

Append to `shared/tests/test_prompt_knowledge_wiring.py`:

```python
def test_pinned_gdoc_module_body_is_resolved_at_render():
    from shared.prompts.core import PromptLibrary
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(
        id="d", slug="procs", title="Procedures", summary="How-tos.",
        body=None, mode="pinned", source="gdoc", source_ref="doc-1",
    )

    class _Knowledge:
        def all_modules(self):
            return [module]

        def overrides_for(self, _prompt_id):
            return {"procs": True}

    class _Gdoc:
        def body_for(self, m):
            return f"live body for {m.source_ref}"

    library = PromptLibrary(knowledge=_Knowledge(), gdoc_module_provider=_Gdoc())
    rendered = library.render("staff.system")

    assert "live body for doc-1" in (rendered.context_text or "")
    assert "procs" in rendered.knowledge_used


def test_gdoc_module_that_fails_to_resolve_is_dropped_not_rendered_empty():
    from shared.prompts.core import PromptLibrary
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(
        id="d", slug="procs", title="Procedures", summary="How-tos.",
        body=None, mode="pinned", source="gdoc", source_ref="doc-1",
    )

    class _Knowledge:
        def all_modules(self):
            return [module]

        def overrides_for(self, _prompt_id):
            return {"procs": True}

    class _Gdoc:
        def body_for(self, _m):
            return None

    library = PromptLibrary(knowledge=_Knowledge(), gdoc_module_provider=_Gdoc())
    rendered = library.render("staff.system")

    assert "Procedures" not in (rendered.context_text or "")
    assert "procs" not in rendered.knowledge_used
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_prompt_knowledge_wiring.py -k gdoc_module -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'gdoc_module_provider'`

- [ ] **Step 3: Resolve gdoc bodies in `_compose_knowledge`**

In `shared/prompts/core.py`, add the constructor parameter — after `knowledge`:

```python
        gdoc_module_provider: Optional[Any] = None,
```

and in `__init__`:

```python
        self._gdoc_modules = gdoc_module_provider
```

Then in `_compose_knowledge`, immediately after the `chosen = select_for_prompt(...)`
line, insert:

```python
        # A gdoc module has no stored body; resolve it here, synchronously,
        # the same way prompt-level doc overrides already resolve. JIT
        # sources (graph/directory/episodic) are handled by
        # JitContextResolver instead and are skipped entirely.
        chosen = [m for m in chosen if not m.is_jit]
        chosen = [self._with_resolved_body(m) for m in chosen]
        chosen = [m for m in chosen if m.body]
```

and add the helper method to `PromptLibrary`:

```python
    def _with_resolved_body(self, module):
        """Fill in a gdoc module's body. Other sources pass through."""
        if module.source != "gdoc" or self._gdoc_modules is None:
            return module
        return dataclasses.replace(module, body=self._gdoc_modules.body_for(module))
```

The `chosen = [m for m in chosen if m.body]` line also drops an on-demand gdoc
module whose doc is unreachable, which is correct: advertising a catalog entry the
model cannot fetch is worse than omitting it.

- [ ] **Step 4: Wire it into the default library**

In `_build_default_library`:

```python
    from shared.prompts.providers_gdoc import GDocProvider

    return PromptLibrary(
        overrides=overrides,
        gdoc_body_for=gdoc_store.body_for,
        invalidate_gdoc=gdoc_store.invalidate,
        knowledge=KnowledgeStore.from_env(),
        gdoc_module_provider=GDocProvider(),
    )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest shared/tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add shared/prompts/core.py shared/tests/test_prompt_knowledge_wiring.py
git commit -m "feat(context): resolve gdoc module bodies during render"
```

---

# Phase 6 — Graph provider

### Task 17: `summarize_entity_graph` RPC

**Files:**
- Create: `db/migrations/0018_summarize_entity_graph.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 0018_summarize_entity_graph.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 6 of docs/superpowers/plans/2026-08-20-p1-resolvable-context-modules.md.
--
-- entities/relationships/entity_mentions carry NO permission columns. The only
-- path to row-level permission is
--   entities -> entity_mentions -> documents.allowed_organization_ids
-- so every aggregate here goes through that join. p_org_ids IS NULL means
-- unrestricted (staff), matching search_chunks_with_permissions' convention.

BEGIN;

CREATE OR REPLACE FUNCTION summarize_entity_graph(
    p_org_ids    uuid[] DEFAULT NULL,
    p_max_types  int    DEFAULT 20,
    p_examples   int    DEFAULT 3
)
RETURNS TABLE (
    kind          text,     -- 'entity' | 'relationship'
    type_name     text,
    item_count    bigint,
    examples      text[]
) LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    WITH visible_entities AS (
        SELECT DISTINCT e.id, e.name, e.type
        FROM entities e
        WHERE p_org_ids IS NULL
           OR EXISTS (
               SELECT 1
               FROM entity_mentions em
               JOIN documents d ON d.id = em.document_id
               WHERE em.entity_id = e.id
                 AND d.allowed_organization_ids && p_org_ids
           )
    ),
    entity_types AS (
        SELECT 'entity'::text AS kind,
               ve.type        AS type_name,
               count(*)       AS item_count,
               (array_agg(ve.name ORDER BY ve.name))[1:p_examples] AS examples
        FROM visible_entities ve
        GROUP BY ve.type
        ORDER BY count(*) DESC
        LIMIT p_max_types
    ),
    rel_types AS (
        SELECT 'relationship'::text  AS kind,
               r.relationship_type   AS type_name,
               count(*)              AS item_count,
               ARRAY[]::text[]       AS examples
        FROM relationships r
        JOIN visible_entities s ON s.id = r.source_entity_id
        JOIN visible_entities t ON t.id = r.target_entity_id
        GROUP BY r.relationship_type
        ORDER BY count(*) DESC
        LIMIT p_max_types
    )
    SELECT * FROM entity_types
    UNION ALL
    SELECT * FROM rel_types;
END;
$$;

COMMIT;
```

Note both endpoints of a relationship must be visible — a relationship whose far
end lives in another org's documents is not surfaced at all.

- [ ] **Step 2: Mirror into the schema file**

Append the same function to `db/schema/chat_db.sql` after the RAG search RPCs.

- [ ] **Step 3: Apply to production and verify**

```sql
SELECT * FROM summarize_entity_graph(NULL, 20, 3) LIMIT 5;
```
Expected: rows with `kind`, `type_name`, `item_count`, `examples`

- [ ] **Step 4: Commit**

```bash
git add db/migrations/0018_summarize_entity_graph.sql db/schema/chat_db.sql
git commit -m "feat(context): add permission-filtered entity graph summary RPC"
```

---

### Task 18: `GraphProvider` renders the ontology primer

**Files:**
- Create: `chat_orchestrator/orchestrator/services/providers/graph_provider.py`
- Test: `chat_orchestrator/tests/test_graph_provider.py`

- [ ] **Step 1: Write the failing test**

Create `chat_orchestrator/tests/test_graph_provider.py`:

```python
"""Graph provider: the ontology primer, permission-filtered."""

import pytest

from orchestrator.services.providers.graph_provider import GraphProvider, render_primer
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.prompts.types import RequestScope


def _module():
    return KnowledgeModule(
        id="g", slug="entity-graph", title="Knowledge Graph", summary="Ontology.",
        body=None, source="graph",
    )


def _rows():
    return [
        {"kind": "entity", "type_name": "Meter", "item_count": 120,
         "examples": ["M-001", "M-002", "M-003"]},
        {"kind": "entity", "type_name": "DCU", "item_count": 18,
         "examples": ["DCU-7721"]},
        {"kind": "relationship", "type_name": "connected_to", "item_count": 140,
         "examples": []},
    ]


def test_primer_lists_entity_types_with_counts():
    text = render_primer(_rows())
    assert "Meter" in text and "120" in text
    assert "DCU" in text and "18" in text


def test_primer_lists_relationship_types():
    text = render_primer(_rows())
    assert "connected_to" in text and "140" in text


def test_primer_includes_examples_for_entity_types():
    assert "M-001" in render_primer(_rows())


def test_primer_returns_none_for_no_rows():
    assert render_primer([]) is None


def test_primer_stays_compact():
    """It is pinned into every request that attaches the module."""
    assert len(render_primer(_rows())) < 1500


@pytest.mark.asyncio
async def test_staff_query_passes_null_org_ids():
    seen = {}

    class _Client:
        def rpc(self, name, params):
            seen["name"] = name
            seen["params"] = params
            return self

        def execute(self):
            class _R:
                data = _rows()
            return _R()

    provider = GraphProvider(client=_Client())
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=("7",), is_staff=True)

    await provider.resolve(_module(), ctx)

    assert seen["name"] == "summarize_entity_graph"
    assert seen["params"]["p_org_ids"] is None


@pytest.mark.asyncio
async def test_customer_query_passes_their_org_ids():
    seen = {}

    class _Client:
        def rpc(self, name, params):
            seen["params"] = params
            return self

        def execute(self):
            class _R:
                data = _rows()
            return _R()

    provider = GraphProvider(client=_Client())
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7", "9"), is_staff=False
    )

    await provider.resolve(_module(), ctx)

    assert seen["params"]["p_org_ids"] == ["7", "9"]


@pytest.mark.asyncio
async def test_a_customer_with_no_orgs_gets_nothing_not_everything():
    """The fail-safe that matters: no orgs must never mean unrestricted."""

    class _Client:
        def rpc(self, *_a, **_k):
            raise AssertionError("must not query at all")

    provider = GraphProvider(client=_Client())
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=(), is_staff=False)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_a_failing_rpc_resolves_to_none():
    class _Client:
        def rpc(self, *_a, **_k):
            raise RuntimeError("function does not exist")

    provider = GraphProvider(client=_Client())
    ctx = ResolutionContext(scope=RequestScope(), is_staff=True)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_no_client_resolves_to_none():
    ctx = ResolutionContext(scope=RequestScope(), is_staff=True)
    assert await GraphProvider(client=None).resolve(_module(), ctx) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/test_graph_provider.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the provider**

Create `chat_orchestrator/orchestrator/services/providers/graph_provider.py`:

```python
"""The knowledge graph's ontology, as a context module.

Renders what P4's agentic graph tools need the model to already know: which
entity types exist, which relationship types connect them, and a few real
entity names so the model can pattern-match its own queries. Built here and
reused there -- do not render a second primer in the MCP layer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

MAX_TYPES = 20
EXAMPLES_PER_TYPE = 3


def render_primer(rows: List[Dict[str, Any]]) -> Optional[str]:
    """Format summarize_entity_graph rows as a compact ontology primer."""
    entities = [r for r in rows if r.get("kind") == "entity"]
    relationships = [r for r in rows if r.get("kind") == "relationship"]
    if not entities and not relationships:
        return None

    lines: List[str] = []
    if entities:
        lines.append("Entity types in the knowledge graph:")
        for row in entities:
            examples = ", ".join(row.get("examples") or [])
            suffix = f" — e.g. {examples}" if examples else ""
            lines.append(f"- {row['type_name']} ({row['item_count']}){suffix}")
    if relationships:
        if lines:
            lines.append("")
        lines.append("Relationship types:")
        for row in relationships:
            lines.append(f"- {row['type_name']} ({row['item_count']})")
    return "\n".join(lines)


class GraphProvider:
    """Resolves the `graph` source."""

    source = "graph"

    def __init__(self, client: Any = None) -> None:
        self._client = client if client is not None else _default_client()

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]:
        if self._client is None:
            LOGGER.warning("GraphProvider has no database client; skipping")
            return None

        # Staff see everything; everyone else is scoped to their orgs. A
        # caller with no orgs gets nothing -- never NULL, which the RPC
        # reads as unrestricted.
        if ctx.is_staff:
            org_ids = None
        elif ctx.organization_ids:
            org_ids = list(ctx.organization_ids)
        else:
            return None

        try:
            result = self._client.rpc(
                "summarize_entity_graph",
                {
                    "p_org_ids": org_ids,
                    "p_max_types": MAX_TYPES,
                    "p_examples": EXAMPLES_PER_TYPE,
                },
            ).execute()
        except Exception as e:
            LOGGER.warning(f"summarize_entity_graph failed: {e}")
            return None

        return render_primer(result.data or [])


def _default_client() -> Any:
    from shared.config.db_credentials import chat_db_service_key, chat_db_url

    url, key = chat_db_url(), chat_db_service_key()
    if not (url and key):
        return None
    try:
        from supabase import create_client

        return create_client(url, key)
    except Exception:
        LOGGER.warning("Could not build the graph provider client", exc_info=True)
        return None


__all__ = ["GraphProvider", "render_primer"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/test_graph_provider.py -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Register it**

In `jit_context_resolver.build_default_registry`, add:

```python
    try:
        from orchestrator.services.providers.graph_provider import GraphProvider

        registry.register(GraphProvider())
    except Exception:
        LOGGER.warning("GraphProvider unavailable", exc_info=True)
```

- [ ] **Step 6: Run the orchestrator suite**

Run: `python -m pytest chat_orchestrator/tests/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add chat_orchestrator/orchestrator/services/providers/graph_provider.py \
        chat_orchestrator/orchestrator/services/jit_context_resolver.py
git add -f chat_orchestrator/tests/test_graph_provider.py
git commit -m "feat(context): add GraphProvider ontology primer"
```

---

# Phase 7 — Episodic provider

### Task 19: `episodic_distillations` table

**Files:**
- Create: `db/migrations/0019_episodic_distillations.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 0019_episodic_distillations.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 7 of docs/superpowers/plans/2026-08-20-p1-resolvable-context-modules.md.
--
-- Distilled historic understanding per grid / per organization, generated by a
-- nightly batch (scripts/distill_episodic_memory.py) and read at render time by
-- EpisodicProvider. Deliberately NOT conversation_summaries, which is per-session
-- and wired to progressive within-session summarization -- a different lifecycle.
--
-- edited_by set = an operator corrected this row by hand; the nightly job leaves
-- it alone thereafter.

BEGIN;

CREATE TABLE IF NOT EXISTS episodic_distillations (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    anchor_type    text NOT NULL,
    anchor_id      text NOT NULL,
    anchor_name    text,
    summary        text NOT NULL,
    message_count  integer NOT NULL DEFAULT 0,
    covers_from    timestamptz,
    covers_to      timestamptz,
    generated_at   timestamptz NOT NULL DEFAULT now(),
    edited_by      text,
    CONSTRAINT episodic_anchor_type_chk CHECK (anchor_type IN ('grid', 'organization')),
    CONSTRAINT episodic_anchor_unique UNIQUE (anchor_type, anchor_id)
);

CREATE INDEX IF NOT EXISTS episodic_distillations_anchor_idx
    ON episodic_distillations (anchor_type, anchor_id);

COMMIT;
```

- [ ] **Step 2: Mirror into `db/schema/chat_db.sql`**

Add the same `CREATE TABLE` and index after the `user_preferences` block, and add
`('episodic_distillations')` to the `update_updated_at` trigger loop's VALUES list
only if you also add an `updated_at` column — this table uses `generated_at`
instead, so **do not** add it to that loop.

- [ ] **Step 3: Apply to production and verify**

```sql
SELECT count(*) FROM episodic_distillations;
```
Expected: `0`

- [ ] **Step 4: Commit**

```bash
git add db/migrations/0019_episodic_distillations.sql db/schema/chat_db.sql
git commit -m "feat(context): add episodic_distillations table"
```

---

### Task 20: `EpisodicProvider` reads a distillation

**Files:**
- Create: `chat_orchestrator/orchestrator/services/providers/episodic_provider.py`
- Test: `chat_orchestrator/tests/test_episodic_provider.py`

- [ ] **Step 1: Write the failing test**

Create `chat_orchestrator/tests/test_episodic_provider.py`:

```python
"""Episodic provider: per-grid / per-org distillation lookup."""

import pytest

from orchestrator.services.providers.episodic_provider import EpisodicProvider
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.prompts.types import RequestScope


def _module():
    return KnowledgeModule(
        id="e", slug="episodic", title="Prior History", summary="What happened before.",
        body=None, source="episodic",
    )


class _Client:
    def __init__(self, rows, permitted_grids=None):
        self._rows = rows
        self._permitted = permitted_grids
        self.filters = {}

    def table(self, _name):
        return self

    def select(self, _cols):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def limit(self, _n):
        return self

    def execute(self):
        class _R:
            pass

        r = _R()
        r.data = [
            row
            for row in self._rows
            if all(row.get(k) == v for k, v in self.filters.items())
        ]
        return r


@pytest.mark.asyncio
async def test_returns_the_distillation_for_the_scoped_grid():
    client = _Client([
        {"anchor_type": "grid", "anchor_id": "Alpha", "anchor_name": "Alpha",
         "summary": "Recurring inverter faults since June."},
    ])
    provider = EpisodicProvider(client=client, grid_access=lambda *_a: True)
    ctx = ResolutionContext(scope=RequestScope(grid="Alpha"), is_staff=True)

    text = await provider.resolve(_module(), ctx)

    assert "Recurring inverter faults since June." in text


@pytest.mark.asyncio
async def test_returns_nothing_when_scope_names_no_anchor():
    client = _Client([{"anchor_type": "grid", "anchor_id": "Alpha", "summary": "x"}])
    provider = EpisodicProvider(client=client, grid_access=lambda *_a: True)
    ctx = ResolutionContext(scope=RequestScope(), is_staff=True)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_returns_nothing_when_no_distillation_exists_yet():
    provider = EpisodicProvider(client=_Client([]), grid_access=lambda *_a: True)
    ctx = ResolutionContext(scope=RequestScope(grid="Alpha"), is_staff=True)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_a_caller_without_grid_access_gets_nothing():
    client = _Client([
        {"anchor_type": "grid", "anchor_id": "Alpha", "summary": "secret history"},
    ])
    provider = EpisodicProvider(client=client, grid_access=lambda *_a: False)
    ctx = ResolutionContext(
        scope=RequestScope(grid="Alpha"), organization_ids=("9",), is_staff=False
    )

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_organization_scope_is_used_when_no_grid_is_named():
    client = _Client([
        {"anchor_type": "organization", "anchor_id": "7", "summary": "Org-wide billing issues."},
    ])
    provider = EpisodicProvider(client=client, grid_access=lambda *_a: True)
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7",), is_staff=False
    )

    text = await provider.resolve(_module(), ctx)

    assert "Org-wide billing issues." in text


@pytest.mark.asyncio
async def test_a_failing_query_resolves_to_none():
    class _Boom:
        def table(self, _n):
            raise RuntimeError("relation does not exist")

    provider = EpisodicProvider(client=_Boom(), grid_access=lambda *_a: True)
    ctx = ResolutionContext(scope=RequestScope(grid="Alpha"), is_staff=True)

    assert await provider.resolve(_module(), ctx) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/test_episodic_provider.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the provider**

Create `chat_orchestrator/orchestrator/services/providers/episodic_provider.py`:

```python
"""Distilled prior history for the grid or organization in scope.

Read-only. Generation is scripts/distill_episodic_memory.py, run nightly --
distilling at render time would put an LLM call on the critical path of every
request that pins this module.

Grid is preferred over organization when the scope names both: it is the more
specific anchor, matching how site-scoped knowledge modules already beat
sector-scoped ones in budget_pinned.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


class EpisodicProvider:
    """Resolves the `episodic` source."""

    source = "episodic"

    def __init__(
        self,
        client: Any = None,
        grid_access: Optional[Callable[[str, ResolutionContext], bool]] = None,
    ) -> None:
        self._client = client if client is not None else _default_client()
        self._grid_access = grid_access or _default_grid_access

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]:
        if self._client is None:
            return None

        if ctx.scope.grid:
            anchor_type, anchor_id = "grid", ctx.scope.grid
            if not self._grid_access(ctx.scope.grid, ctx):
                LOGGER.info(f"Episodic history for grid '{ctx.scope.grid}' withheld: no access")
                return None
        elif ctx.scope.organization_id:
            anchor_type, anchor_id = "organization", ctx.scope.organization_id
            if not (ctx.is_staff or anchor_id in ctx.organization_ids):
                return None
        else:
            return None

        try:
            result = (
                self._client.table("episodic_distillations")
                .select("anchor_name, summary")
                .eq("anchor_type", anchor_type)
                .eq("anchor_id", anchor_id)
                .limit(1)
                .execute()
            )
        except Exception as e:
            LOGGER.warning(f"Episodic distillation lookup failed: {e}")
            return None

        rows = result.data or []
        if not rows:
            return None
        summary = (rows[0].get("summary") or "").strip()
        return summary or None


def _default_grid_access(grid: str, ctx: ResolutionContext) -> bool:
    """Staff see every grid; everyone else needs it in their permitted set.

    Deliberately conservative: an unresolvable grid is treated as denied.
    """
    if ctx.is_staff:
        return True
    try:
        from shared.auth import get_auth_service
        import asyncio

        names = asyncio.get_event_loop().run_until_complete(
            get_auth_service().get_grid_names_for_organization(
                organization_id=ctx.organization_ids[0] if ctx.organization_ids else None
            )
        )
        return grid in (names or [])
    except Exception:
        LOGGER.warning(f"Grid access check failed for '{grid}'; denying", exc_info=True)
        return False


def _default_client() -> Any:
    from shared.config.db_credentials import chat_db_service_key, chat_db_url

    url, key = chat_db_url(), chat_db_service_key()
    if not (url and key):
        return None
    try:
        from supabase import create_client

        return create_client(url, key)
    except Exception:
        LOGGER.warning("Could not build the episodic provider client", exc_info=True)
        return None


__all__ = ["EpisodicProvider"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/test_episodic_provider.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Register it**

In `build_default_registry`:

```python
    try:
        from orchestrator.services.providers.episodic_provider import EpisodicProvider

        registry.register(EpisodicProvider())
    except Exception:
        LOGGER.warning("EpisodicProvider unavailable", exc_info=True)
```

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/services/providers/episodic_provider.py \
        chat_orchestrator/orchestrator/services/jit_context_resolver.py
git add -f chat_orchestrator/tests/test_episodic_provider.py
git commit -m "feat(context): add EpisodicProvider"
```

---

### Task 21: Nightly distillation job

**Files:**
- Create: `scripts/distill_episodic_memory.py`
- Test: `shared/tests/test_distill_episodic_memory.py`

- [ ] **Step 1: Write the failing test**

Create `shared/tests/test_distill_episodic_memory.py`:

```python
"""Episodic distillation batch: selection and write rules."""

from scripts.distill_episodic_memory import (
    anchors_to_refresh,
    build_distillation_prompt,
)


def _row(anchor_id, edited_by=None, message_count=50):
    return {
        "anchor_type": "grid",
        "anchor_id": anchor_id,
        "edited_by": edited_by,
        "message_count": message_count,
    }


def test_refreshes_an_anchor_with_no_existing_row():
    assert anchors_to_refresh(["Alpha"], existing=[]) == ["Alpha"]


def test_refreshes_an_existing_generated_row():
    assert anchors_to_refresh(["Alpha"], existing=[_row("Alpha")]) == ["Alpha"]


def test_never_overwrites_a_hand_edited_row():
    existing = [_row("Alpha", edited_by="ops@example.com")]
    assert anchors_to_refresh(["Alpha"], existing=existing) == []


def test_refreshes_only_the_anchors_asked_for():
    existing = [_row("Alpha"), _row("Beta")]
    assert anchors_to_refresh(["Beta"], existing=existing) == ["Beta"]


def test_prompt_includes_the_anchor_and_the_messages():
    prompt = build_distillation_prompt("Alpha", ["inverter tripped", "replaced fuse"])
    assert "Alpha" in prompt
    assert "inverter tripped" in prompt
    assert "replaced fuse" in prompt


def test_prompt_asks_for_durable_lessons_not_a_transcript():
    prompt = build_distillation_prompt("Alpha", ["x"])
    assert "transcript" in prompt.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_distill_episodic_memory.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the script**

Create `scripts/distill_episodic_memory.py`:

```python
"""Distil recent chat history per grid / organization into episodic memory.

Run nightly. Reads chat_messages, writes episodic_distillations. Read at
render time by EpisodicProvider -- nothing here is on a request's critical
path.

Reuses orchestrator.experts.entity_fanout for "every eligible grid /
organization" rather than adding a fifth enumeration, the same decision
0013_skill_scheduling.sql made.

Usage:
    python scripts/distill_episodic_memory.py --anchor-type grid
    python scripts/distill_episodic_memory.py --anchor-type grid --apply
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List

LOOKBACK_DAYS = 30
MAX_MESSAGES = 300
TARGET_WORDS = 200


def anchors_to_refresh(candidates: List[str], existing: List[Dict[str, Any]]) -> List[str]:
    """Which anchors the batch should regenerate.

    A row with edited_by set was corrected by a human and is never
    overwritten -- an operator's correction outranks the batch.
    """
    protected = {row["anchor_id"] for row in existing if row.get("edited_by")}
    return [a for a in candidates if a not in protected]


def build_distillation_prompt(anchor_name: str, messages: List[str]) -> str:
    """The prompt that turns raw messages into durable lessons."""
    joined = "\n".join(f"- {m}" for m in messages)
    return (
        f"Below are recent support and operations messages about {anchor_name}.\n\n"
        f"{joined}\n\n"
        f"Write a distilled understanding of {anchor_name} in about {TARGET_WORDS} "
        "words. Capture durable, still-true facts: recurring faults, equipment "
        "quirks, decisions taken and why, and anything a technician arriving today "
        "would need to know.\n\n"
        "This is not a transcript or a summary of the conversation. Omit anything "
        "resolved, superseded, or specific to one exchange. Write plain prose with "
        "no preamble."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor-type", choices=["grid", "organization"], required=True)
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = parser.parse_args()

    import asyncio

    from shared.config.db_credentials import chat_db_service_key, chat_db_url
    from supabase import create_client

    url, key = chat_db_url(), chat_db_service_key()
    if not (url and key):
        print("CHAT_DB_URL / CHAT_DB_SERVICE_KEY are not set", file=sys.stderr)
        return 1
    client = create_client(url, key)

    from orchestrator.experts.entity_fanout import resolve_entities

    candidates = asyncio.run(resolve_entities(args.anchor_type))
    names = [e["name"] for e in candidates]

    existing = (
        client.table("episodic_distillations")
        .select("anchor_id, edited_by")
        .eq("anchor_type", args.anchor_type)
        .execute()
        .data
        or []
    )
    targets = anchors_to_refresh(names, existing)

    if not targets:
        print("Nothing to refresh.")
        return 0

    print(f"{len(targets)} anchor(s) to refresh: {', '.join(targets)}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to generate and write.")
        return 0

    from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway

    gateway = get_default_generation_gateway()

    for name in targets:
        rows = (
            client.table("chat_messages")
            .select("content")
            .ilike("content", f"%{name}%")
            .order("created_at", desc=True)
            .limit(MAX_MESSAGES)
            .execute()
            .data
            or []
        )
        messages = [r["content"] for r in rows if r.get("content")]
        if not messages:
            print(f"  {name}: no messages, skipped")
            continue

        prompt = build_distillation_prompt(name, messages)
        response = asyncio.run(
            gateway.generate(
                [LLMMessage(role="user", content=prompt)],
                GenerationOptions(),
            )
        )
        summary = (getattr(response, "text", "") or "").strip()
        if not summary:
            print(f"  {name}: model returned nothing, skipped")
            continue

        client.table("episodic_distillations").upsert(
            {
                "anchor_type": args.anchor_type,
                "anchor_id": name,
                "anchor_name": name,
                "summary": summary,
                "message_count": len(messages),
            },
            on_conflict="anchor_type,anchor_id",
        ).execute()
        print(f"  {name}: {len(messages)} messages -> {len(summary)} chars")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Verify the fan-out and gateway APIs match**

Run: `command grep -n "def resolve_entities\|SUPPORTED_ANCHOR" chat_orchestrator/orchestrator/experts/entity_fanout.py`
Run: `command grep -n "def generate" shared/llm/*.py | head`

If the function names differ, adapt the two call sites above to the real
signatures. Do not add a second enumeration or a second gateway.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_distill_episodic_memory.py -v`
Expected: PASS, 6 tests

- [ ] **Step 6: Commit**

```bash
git add scripts/distill_episodic_memory.py
git add -f shared/tests/test_distill_episodic_memory.py
git commit -m "feat(context): nightly episodic distillation batch"
```

---

# Phase 8 — MCP tool

### Task 22: `get_knowledge_module` resolves provider bodies

**Files:**
- Modify: `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py:242-258`
- Test: `mcp_servers/tests/servers/knowledge_server/test_knowledge_module_tool.py`

- [ ] **Step 1: Write the failing test**

Append to `mcp_servers/tests/servers/knowledge_server/test_knowledge_module_tool.py`:

```python
def test_a_provider_backed_module_reports_that_it_cannot_be_fetched_here():
    """A JIT body needs the caller's permissions, which this tool does not have.

    Returning an empty body would read to the model as "this module is empty",
    which is worse than a clear explanation it can act on.
    """
    from shared.prompts.knowledge import KnowledgeModule

    class _Store:
        def all_modules(self):
            return [
                KnowledgeModule(
                    id="g", slug="entity-graph", title="Graph", summary="Ontology.",
                    body=None, source="graph",
                )
            ]

    text = fetch_knowledge_module("entity-graph", store=_Store())

    assert "entity-graph" in text
    assert "empty" not in text.lower()
    assert "automatically" in text.lower()


def test_a_gdoc_module_body_is_resolved():
    from shared.prompts.knowledge import KnowledgeModule

    class _Store:
        def all_modules(self):
            return [
                KnowledgeModule(
                    id="d", slug="procs", title="Procedures", summary="How-tos.",
                    body=None, source="gdoc", source_ref="doc-1",
                )
            ]

    class _Gdoc:
        def body_for(self, m):
            return f"resolved {m.source_ref}"

    text = fetch_knowledge_module("procs", store=_Store(), gdoc_provider=_Gdoc())

    assert "resolved doc-1" in text


def test_a_manual_module_still_returns_its_stored_body():
    from shared.prompts.knowledge import KnowledgeModule

    class _Store:
        def all_modules(self):
            return [
                KnowledgeModule(
                    id="m", slug="comms", title="Comms", summary="s", body="stored body"
                )
            ]

    assert "stored body" in fetch_knowledge_module("comms", store=_Store())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest mcp_servers/tests/ -k knowledge_module -v`

Note: there is no `conftest.py` under `mcp_servers/tests/`; several files rely on
collection-order side effects for `sys.path`. Run the full `mcp_servers/tests/`
directory, not a single file.

Expected: FAIL with `TypeError: fetch_knowledge_module() got an unexpected keyword argument 'gdoc_provider'`

- [ ] **Step 3: Update the fetcher**

Replace `fetch_knowledge_module` in `knowledge_mcp_server.py`:

```python
def fetch_knowledge_module(slug: str, store: Any = None, gdoc_provider: Any = None) -> str:
    """Return one knowledge module's full body by slug.

    Backs the on-demand tier: the model sees only slug and summary in context
    (the '# Available Knowledge' catalog block PromptLibrary.render composes)
    and calls this when it decides a module is relevant.

    A gdoc module resolves here. A graph/directory/episodic module cannot:
    its body depends on the caller's row-level permissions, which this
    server does not carry, so it is composed into context automatically
    instead. Say so plainly rather than returning an empty body, which the
    model would read as "this module has no content".
    """
    from shared.prompts.knowledge import KnowledgeStore

    store = store or KnowledgeStore.from_env()
    modules = {m.slug: m for m in store.all_modules()}
    if not modules:
        return "No knowledge modules are configured."
    module = modules.get(slug)
    if not module:
        return f"No knowledge module named '{slug}'. Available: " + ", ".join(sorted(modules))

    if module.is_jit:
        return (
            f"'{slug}' is live context. It is composed into your context "
            f"automatically when relevant and cannot be fetched on demand here."
        )

    body = module.body
    if module.source == "gdoc":
        if gdoc_provider is None:
            from shared.prompts.providers_gdoc import GDocProvider

            gdoc_provider = GDocProvider()
        body = gdoc_provider.body_for(module)

    if not body:
        return f"Knowledge module '{slug}' could not be loaded from its source."

    return f"# {module.title}\n\n{body}"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest mcp_servers/tests/ -q`
Expected: PASS

- [ ] **Step 5: Regenerate the tool manifest**

The tool's schema text is unchanged, but regenerate anyway to confirm nothing
drifted, and verify no server was dropped from the export:

```bash
python mcp_servers/scripts/export_tools.py
git diff --stat mcp_servers/tool_definitions.json
```

Expected: no change, or only the knowledge server's entry. If whole servers vanish
from the JSON, the venv is missing their dependencies — fix that before committing.

- [ ] **Step 6: Commit**

```bash
git add mcp_servers/servers/knowledge_server/knowledge_mcp_server.py \
        mcp_servers/tool_definitions.json
git add -f mcp_servers/tests/servers/knowledge_server/test_knowledge_module_tool.py
git commit -m "feat(context): resolve provider-backed modules in get_knowledge_module"
```

---

# Phase 9 — Admin UI

### Task 23: Source badges and read-only bodies

**Files:**
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py`
- Test: `anansi_app/tests/test_knowledge_modules_page.py`

- [ ] **Step 1: Write the failing test**

Append to `anansi_app/tests/test_knowledge_modules_page.py`:

```python
def test_row_reports_live_instead_of_a_char_count_for_jit_modules():
    from anansi_app.nicegui_app.pages.knowledge_modules import build_module_rows
    from shared.prompts.knowledge import KnowledgeModule

    modules = [
        KnowledgeModule(id="g", slug="entity-graph", title="Graph", summary="s",
                        body=None, source="graph"),
        KnowledgeModule(id="m", slug="comms", title="Comms", summary="s",
                        body="12345"),
    ]
    rows = {r.slug: r for r in build_module_rows(modules)}

    assert rows["entity-graph"].size_label == "live"
    assert rows["comms"].size_label == "5 chars"


def test_row_carries_the_source():
    from anansi_app.nicegui_app.pages.knowledge_modules import build_module_rows
    from shared.prompts.knowledge import KnowledgeModule

    rows = build_module_rows([
        KnowledgeModule(id="g", slug="entity-graph", title="G", summary="s",
                        body=None, source="graph")
    ])
    assert rows[0].source == "graph"


def test_body_is_editable_only_for_manual_modules():
    from anansi_app.nicegui_app.pages.knowledge_modules import body_is_editable

    assert body_is_editable("manual") is True
    assert body_is_editable("ingested") is True
    for source in ("gdoc", "graph", "directory", "episodic"):
        assert body_is_editable(source) is False, source


def test_singleton_providers_cannot_be_deleted():
    from anansi_app.nicegui_app.pages.knowledge_modules import module_is_deletable

    assert module_is_deletable("graph") is False
    assert module_is_deletable("directory") is False
    assert module_is_deletable("gdoc") is True
    assert module_is_deletable("episodic") is True
    assert module_is_deletable("manual") is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest anansi_app/tests/test_knowledge_modules_page.py -v`
Expected: FAIL with `AttributeError: 'ModuleRow' object has no attribute 'size_label'`

- [ ] **Step 3: Implement**

In `anansi_app/nicegui_app/pages/knowledge_modules.py`, add `source` and
`size_label` to the `ModuleRow` dataclass and set them in `build_module_rows`:

```python
@dataclass
class ModuleRow:
    slug: str
    title: str
    tags: List[str]
    scope: str
    mode: str
    chars: int
    source: str = "manual"
    size_label: str = ""
```

```python
def build_module_rows(modules: List[Any]) -> List[ModuleRow]:
    rows = []
    for m in modules:
        chars = len(m.body or "")
        source = getattr(m, "source", "manual")
        rows.append(
            ModuleRow(
                slug=m.slug, title=m.title, tags=list(m.tags), scope=m.scope,
                mode=m.mode, chars=chars, source=source,
                # A provider body has no size until it resolves, and it
                # resolves differently per caller -- a number here would be
                # a fiction.
                size_label="live" if source in PROVIDER_SOURCES else f"{chars} chars",
            )
        )
    return rows
```

Add near the top:

```python
# Sources whose body is produced at render time rather than stored.
PROVIDER_SOURCES = ("gdoc", "graph", "directory", "episodic")

# Exactly one of each exists; deleting it only makes the capability
# unreachable, so the UI refuses.
SINGLETON_SOURCES = ("graph", "directory")


def body_is_editable(source: str) -> bool:
    """Only a stored body can be edited here."""
    return source not in PROVIDER_SOURCES


def module_is_deletable(source: str) -> bool:
    return source not in SINGLETON_SOURCES
```

In `_render_row`, use `row.size_label` in place of `f"{row.chars} chars"` and add
the source to the same line:

```python
                ui.label(
                    f"{row.slug} · {row.source} · {row.scope} · {row.mode} · {row.size_label}"
                ).classes(
```

In `_open_edit_dialog`, after the body textarea is constructed, add:

```python
        source = getattr(existing, "source", "manual") if existing else "manual"
        if not body_is_editable(source):
            body_input.props("readonly").classes("opacity-60")
            ui.label(
                {
                    "gdoc": "Body comes from the attached Google Doc.",
                    "graph": "Body is generated from the knowledge graph at request time, "
                             "filtered to what the caller may see.",
                    "directory": "Body lists the grids, organizations and people the "
                                 "caller may see, at request time.",
                    "episodic": "Body is the stored distillation for the grid or "
                                "organization in scope.",
                }[source]
            ).classes("text-xs text-gray-500")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest anansi_app/tests/test_knowledge_modules_page.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/knowledge_modules.py \
        anansi_app/tests/test_knowledge_modules_page.py
git commit -m "feat(context): show source and lock provider bodies in the Context page"
```

---

### Task 24: Preview a provider module

**Files:**
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py`
- Test: `anansi_app/tests/test_knowledge_modules_page.py`

Without this an operator cannot tell what a JIT module actually contributes, which
makes the feature unauditable.

- [ ] **Step 1: Write the failing test**

```python
def test_preview_resolves_against_the_viewing_operator():
    import asyncio

    from anansi_app.nicegui_app.pages.knowledge_modules import preview_module_body
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(
        id="d", slug="directory", title="Directory", summary="s", body=None, source="directory"
    )

    class _Provider:
        source = "directory"

        async def resolve(self, m, ctx):
            return f"resolved for staff={ctx.is_staff}"

    text = asyncio.run(preview_module_body(module, _Provider(), is_staff=True))
    assert text == "resolved for staff=True"


def test_preview_reports_an_empty_resolution_clearly():
    import asyncio

    from anansi_app.nicegui_app.pages.knowledge_modules import preview_module_body
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(
        id="e", slug="episodic", title="Episodic", summary="s", body=None, source="episodic"
    )

    class _Provider:
        source = "episodic"

        async def resolve(self, m, ctx):
            return None

    text = asyncio.run(preview_module_body(module, _Provider(), is_staff=True))
    assert "nothing" in text.lower()


def test_preview_reports_a_provider_failure_rather_than_raising():
    import asyncio

    from anansi_app.nicegui_app.pages.knowledge_modules import preview_module_body
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(
        id="g", slug="graph", title="Graph", summary="s", body=None, source="graph"
    )

    class _Provider:
        source = "graph"

        async def resolve(self, m, ctx):
            raise RuntimeError("RPC missing")

    text = asyncio.run(preview_module_body(module, _Provider(), is_staff=True))
    assert "RPC missing" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest anansi_app/tests/test_knowledge_modules_page.py -k preview -v`
Expected: FAIL with `ImportError: cannot import name 'preview_module_body'`

- [ ] **Step 3: Implement**

```python
async def preview_module_body(module: Any, provider: Any, is_staff: bool) -> str:
    """Resolve a provider module for display in the admin UI.

    Resolves against the viewing operator's own permissions, not a
    privileged context -- an operator previewing a module should see what
    they themselves would get, and never be shown data they could not
    otherwise reach.
    """
    from shared.prompts.providers import ResolutionContext
    from shared.prompts.types import RequestScope

    ctx = ResolutionContext(scope=RequestScope(), is_staff=is_staff)
    try:
        body = await provider.resolve(module, ctx)
    except Exception as e:
        return f"Provider failed: {e}"
    return body or "Resolved to nothing for your permissions."
```

In `_open_edit_dialog`, for a non-editable source, add a Preview button that calls
this and writes the result into a read-only expansion panel below the body field.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest anansi_app/tests/test_knowledge_modules_page.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/knowledge_modules.py \
        anansi_app/tests/test_knowledge_modules_page.py
git commit -m "feat(context): preview provider-backed module bodies"
```

---

### Task 25: Final verification and PR

- [ ] **Step 1: Run every suite**

Run: `python -m pytest shared/tests/ chat_orchestrator/tests/ anansi_app/tests/ mcp_servers/tests/ -q`
Expected: PASS

- [ ] **Step 2: Run pre-commit**

Run: `pre-commit run --all-files`
Expected: all hooks pass. If `test-wiring` reports untracked test files, vet them
for operator data and `git add -f` each, then re-run.

- [ ] **Step 3: Confirm every new test file is tracked**

Run: `git log --stat --oneline -25 | command grep -c "tests/"`
Cross-check against the 9 test files this plan creates or modifies. A file passing
locally but absent from the commit means CI never runs it.

- [ ] **Step 4: Confirm all three migrations are applied**

```sql
SELECT conname FROM pg_constraint WHERE conname = 'knowledge_modules_body_required_chk';
SELECT proname FROM pg_proc WHERE proname = 'summarize_entity_graph';
SELECT to_regclass('public.episodic_distillations');
```
Expected: one row each, none null.

- [ ] **Step 5: Open the PR**

```bash
git push -u origin feat/context-resolvable-modules
gh pr create --title "feat(context): resolvable context modules" --body "$(cat <<'EOF'
Makes `KnowledgeModule.body` resolvable at render time by a provider keyed on
`knowledge_modules.source`, so a Google Doc, the GraphRAG entity summary, the
grids/organizations/people directory and per-grid episodic distillation all
become context modules attachable through the mechanism that already exists.

Stage 1 of 4 in the context-architecture programme.

Spec: `docs/superpowers/specs/2026-08-19-resolvable-context-modules-design.md`
Plan: `docs/superpowers/plans/2026-08-20-p1-resolvable-context-modules.md`

## Migrations — apply by hand before merging

- `0017_context_module_providers.sql`
- `0018_summarize_entity_graph.sql`
- `0019_episodic_distillations.sql`

## Notes for the reviewer

- `PromptLibrary.render()` stays synchronous. `manual`/`gdoc` resolve inside it;
  `graph`/`directory`/`episodic` resolve in `JitContextResolver` because they need
  async permission-filtered queries. Both read pins through the same
  `KnowledgeStore.overrides_for` call.
- `entities`/`relationships` have no permission columns; every graph query filters
  via `entity_mentions -> documents.allowed_organization_ids`. A caller with no
  orgs gets nothing rather than everything.
- The hardcoded `_fetch_enrichment` path is removed only after production parity
  was confirmed (Task 14).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Handoff to P2

Record in the PR description, for the next stage to read:

- Whether `GDocProvider` needed a new `fetch_doc_text` helper in `shared/prompts/gdoc.py`
- The final signature of `preview_module_body`
- Whether `_fetch_enrichment` was actually deleted, or deferred pending production parity
