# Context Page Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a live search box to the top of the Context admin page that filters context modules by slug, title, summary, and markdown body — mirroring the Prompts page's existing search pattern.

**Architecture:** `ModuleRow` (the Context page's view model in `knowledge_modules.py`) gains `summary`/`body` fields, populated from data `build_module_rows` already holds in memory. A new `filter_context_rows(rows, query)` does a case-insensitive substring match across slug/title/summary/body. `render()` adds a persistent search `ui.input` above the list, wired into the existing `refresh()` re-render cycle exactly like the Prompts page's own search box (`prompts.py`'s `search_input`).

**Tech Stack:** Python 3, NiceGUI, pytest.

**Reference:** Design spec at `docs/superpowers/specs/2026-08-20-context-page-search-design.md`.

**Test runner used throughout this plan** (this worktree has no venv of its own; it reuses the repo's shared root venv, which already has every dependency `anansi_app/tests` needs — confirmed working, 289 passed, 0 failures, at worktree creation):

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
PYTHONPATH="$PWD:$PWD/anansi_app" /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.venv/bin/python -m pytest anansi_app/tests/test_knowledge_modules_page.py -q
```

---

### Task 1: `ModuleRow` gains `summary`/`body`

**Files:**
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py:56-83` (the `ModuleRow` dataclass and `build_module_rows`)
- Test: `anansi_app/tests/test_knowledge_modules_page.py:32-39` (`test_build_module_rows_reports_size`)

- [ ] **Step 1: Update the test to expect the new fields (it will fail until Step 3)**

In `anansi_app/tests/test_knowledge_modules_page.py`, replace:

```python
def test_build_module_rows_reports_size():
    rows = build_module_rows([_module("comms")])
    assert rows == [
        ModuleRow(
            slug="comms", title="Comms", tags=["grid_ops"], scope="sector", mode="pinned",
            chars=40, source="manual", size_label="40 chars",
        )
    ]
```

with:

```python
def test_build_module_rows_reports_size():
    rows = build_module_rows([_module("comms")])
    assert rows == [
        ModuleRow(
            slug="comms", title="Comms", tags=["grid_ops"], scope="sector", mode="pinned",
            chars=40, source="manual", size_label="40 chars",
            summary="s", body="b" * 40,
        )
    ]
```

(`_module()`'s current defaults are `summary="s"` and `body="b" * 40` — see lines 19-29 of the same file — so this is exactly what `build_module_rows` will produce once Step 3 lands.)

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
PYTHONPATH="$PWD:$PWD/anansi_app" /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.venv/bin/python -m pytest anansi_app/tests/test_knowledge_modules_page.py::test_build_module_rows_reports_size -v
```

Expected: FAIL — `TypeError: ModuleRow.__init__() got an unexpected keyword argument 'summary'`.

- [ ] **Step 3: Add the fields to `ModuleRow` and populate them in `build_module_rows`**

In `anansi_app/nicegui_app/pages/knowledge_modules.py`, replace:

```python
@dataclass(frozen=True)
class ModuleRow:
    slug: str
    title: str
    tags: List[str]
    scope: str
    mode: str
    chars: int
    source: str = "manual"
    size_label: str = ""


def build_module_rows(modules: List[Any]) -> List[ModuleRow]:
    rows = []
    for m in sorted(modules, key=lambda m: m.slug):
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

with:

```python
@dataclass(frozen=True)
class ModuleRow:
    slug: str
    title: str
    tags: List[str]
    scope: str
    mode: str
    chars: int
    source: str = "manual"
    size_label: str = ""
    # Not shown in _render_row -- only used to make a module findable via
    # the search box (see filter_context_rows). Same reasoning as chars
    # above: already in memory here, so carrying it costs nothing new.
    summary: str = ""
    body: str = ""


def build_module_rows(modules: List[Any]) -> List[ModuleRow]:
    rows = []
    for m in sorted(modules, key=lambda m: m.slug):
        body = m.body or ""
        chars = len(body)
        source = getattr(m, "source", "manual")
        rows.append(
            ModuleRow(
                slug=m.slug, title=m.title, tags=list(m.tags), scope=m.scope,
                mode=m.mode, chars=chars, source=source,
                # A provider body has no size until it resolves, and it
                # resolves differently per caller -- a number here would be
                # a fiction.
                size_label="live" if source in PROVIDER_SOURCES else f"{chars} chars",
                summary=m.summary,
                body=body,
            )
        )
    return rows
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
PYTHONPATH="$PWD:$PWD/anansi_app" /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.venv/bin/python -m pytest anansi_app/tests/test_knowledge_modules_page.py -q
```

Expected: PASS — all tests in the file green (this also exercises `test_row_reports_live_instead_of_a_char_count_for_jit_modules` and `test_row_carries_the_source`, which only assert single fields and are unaffected by the new ones).

- [ ] **Step 5: Commit**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
git add anansi_app/nicegui_app/pages/knowledge_modules.py anansi_app/tests/test_knowledge_modules_page.py
git commit -m "feat(context): carry summary and body on ModuleRow"
```

---

### Task 2: `filter_context_rows`

**Files:**
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py` (new function, placed directly after `group_module_rows`)
- Test: `anansi_app/tests/test_knowledge_modules_page.py` (import list, `_module()` fixture, new tests)

- [ ] **Step 1: Write the failing tests**

In `anansi_app/tests/test_knowledge_modules_page.py`, update the import block — replace:

```python
from nicegui_app.pages.knowledge_modules import (
    ModuleRow,
    body_is_editable,
    build_module_rows,
    group_module_rows,
    module_is_deletable,
    preview_module_body,
    prompt_option_label,
    validate_module,
)
```

with:

```python
from nicegui_app.pages.knowledge_modules import (
    ModuleRow,
    body_is_editable,
    build_module_rows,
    filter_context_rows,
    group_module_rows,
    module_is_deletable,
    preview_module_body,
    prompt_option_label,
    validate_module,
)
```

Then give `_module()` overridable `summary`/`body` (existing call sites are unaffected — these are the exact same values as the current hardcoded ones, just promoted to defaults). Replace:

```python
def _module(slug, tags=("grid_ops",), mode="pinned"):
    return KnowledgeModule(
        id=slug,
        slug=slug,
        title=slug.title(),
        summary="s",
        body="b" * 40,
        tags=list(tags),
        scope="sector",
        mode=mode,
    )
```

with:

```python
def _module(slug, tags=("grid_ops",), mode="pinned", summary="s", body="b" * 40):
    return KnowledgeModule(
        id=slug,
        slug=slug,
        title=slug.title(),
        summary=summary,
        body=body,
        tags=list(tags),
        scope="sector",
        mode=mode,
    )
```

Then add these new tests directly after `test_build_module_rows_reports_size` (i.e. right after its closing `]` / blank lines, before `test_knowledge_tab_lists_all_modules_with_pinned_state`):

```python
def test_filter_context_rows_matches_slug_title_summary_and_body():
    modules = [
        _module(
            "azimuth-calc",
            summary="How PV azimuth is measured.",
            body="Uses the sun's position and panel tilt.",
        ),
        _module(
            "victron-led",
            summary="Decoding inverter LED error states.",
            body="Flash codes and their fault meanings.",
        ),
    ]
    rows = build_module_rows(modules)

    # slug
    assert [r.slug for r in filter_context_rows(rows, "azimuth-calc")] == ["azimuth-calc"]
    # title (slug.title() -> "Victron-Led")
    assert [r.slug for r in filter_context_rows(rows, "Victron")] == ["victron-led"]
    # summary
    assert [r.slug for r in filter_context_rows(rows, "inverter LED")] == ["victron-led"]
    # body
    assert [r.slug for r in filter_context_rows(rows, "panel tilt")] == ["azimuth-calc"]


def test_filter_context_rows_is_case_insensitive():
    rows = build_module_rows([_module("azimuth-calc", body="Panel Tilt matters.")])
    assert [r.slug for r in filter_context_rows(rows, "PANEL tilt")] == ["azimuth-calc"]


def test_filter_context_rows_empty_query_returns_everything_unchanged():
    rows = build_module_rows([_module("a"), _module("b")])
    assert filter_context_rows(rows, "") == rows
    assert filter_context_rows(rows, "   ") == rows


def test_filter_context_rows_no_match_returns_empty():
    rows = build_module_rows([_module("azimuth-calc")])
    assert filter_context_rows(rows, "no such module") == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
PYTHONPATH="$PWD:$PWD/anansi_app" /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.venv/bin/python -m pytest anansi_app/tests/test_knowledge_modules_page.py -k filter_context_rows -v
```

Expected: FAIL to collect — `ImportError: cannot import name 'filter_context_rows'`.

- [ ] **Step 3: Implement `filter_context_rows`**

In `anansi_app/nicegui_app/pages/knowledge_modules.py`, directly after `group_module_rows` (after its `return [(MODE_LABELS.get(m, m), by_mode[m]) for m in order]` line, before `def prompt_option_label`), add:

```python
def filter_context_rows(rows: List[ModuleRow], query: str) -> List[ModuleRow]:
    """Case-insensitive substring match over slug, title, summary and body.

    Mirrors prompts.py's own top-of-page search box and its
    filter_module_rows helper in spirit, but is deliberately a separate
    function/name: it filters a different row type (ModuleRow, which has a
    body field KnowledgeTabRow doesn't), and test_knowledge_modules_page.py
    already imports from both modules in one file -- reusing the name would
    force an import alias for no benefit.
    """
    needle = query.strip().lower()
    if not needle:
        return list(rows)
    return [
        r
        for r in rows
        if needle in r.slug.lower()
        or needle in r.title.lower()
        or needle in r.summary.lower()
        or needle in r.body.lower()
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
PYTHONPATH="$PWD:$PWD/anansi_app" /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.venv/bin/python -m pytest anansi_app/tests/test_knowledge_modules_page.py -q
```

Expected: PASS — all tests in the file green.

- [ ] **Step 5: Commit**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
git add anansi_app/nicegui_app/pages/knowledge_modules.py anansi_app/tests/test_knowledge_modules_page.py
git commit -m "feat(context): add filter_context_rows (slug/title/summary/body)"
```

---

### Task 3: Wire the search box into `render()`

**Files:**
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py` (the `render` function — line numbers shift after Tasks 1-2's edits, so locate it by the `async def render(user_email: str) -> None:` signature rather than a line range)

`render()`'s NiceGUI wiring has no existing unit test in this codebase (the Prompts page's own `search_input` wiring isn't unit-tested either — only its pure helpers are). This task's verification step is the full regression suite, not a new test.

- [ ] **Step 1: Add the search box and wire it into `refresh()`**

In `anansi_app/nicegui_app/pages/knowledge_modules.py`, replace:

```python
    store = KnowledgeStore.from_env()
    if not store._client:  # noqa: SLF001 -- readiness check, same as the Prompts page
        ui.label(
            "⚠️ Context storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY). "
            "Modules can't be listed or saved."
        ).classes("text-warning")
        return

    list_container = ui.column().classes("w-full gap-0")

    def refresh() -> None:
        list_container.clear()
        store.invalidate()
        rows = build_module_rows(store.all_modules())
        with list_container:
            with ui.row().classes("justify-end w-full"):
                ui.button(
                    "+ New context module",
                    on_click=lambda: _open_edit_dialog(None, store, refresh, user_email),
                ).props("color=primary")
            if not rows:
                ui.label("No context modules yet. Use /learn in Telegram to add one.").classes(
                    "text-italic"
                )
                return
            for label, group in group_module_rows(rows):
                section = ui.expansion(f"{label}  ·  {len(group)}", value=True).classes(
                    "w-full q-mb-sm"
                )
                section.props(f'header-class="text-h6 text-weight-bold" {DISCLOSURE_ICONS}')
                with section:
                    for row in group:
                        _render_row(row, store, refresh, user_email)

    refresh()
```

with:

```python
    store = KnowledgeStore.from_env()
    if not store._client:  # noqa: SLF001 -- readiness check, same as the Prompts page
        ui.label(
            "⚠️ Context storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY). "
            "Modules can't be listed or saved."
        ).classes("text-warning")
        return

    # Placed after the readiness check (not right below the caption): with
    # storage unconfigured we return above and never build a list, so a
    # search box here would have nothing to search -- same placement logic
    # as the Prompts page's search_input, which only ever filters a list
    # that's actually going to render.
    search_input = ui.input(placeholder="Search context modules…").classes("w-full")
    list_container = ui.column().classes("w-full gap-0")

    def refresh() -> None:
        list_container.clear()
        store.invalidate()
        rows = build_module_rows(store.all_modules())
        all_empty = not rows
        rows = filter_context_rows(rows, search_input.value or "")
        with list_container:
            with ui.row().classes("justify-end w-full"):
                ui.button(
                    "+ New context module",
                    on_click=lambda: _open_edit_dialog(None, store, refresh, user_email),
                ).props("color=primary")
            if not rows:
                # Two different reasons for an empty list need two different
                # messages: genuinely no modules yet (keep the /learn hint)
                # vs. modules exist but this search matched none of them.
                message = (
                    "No context modules yet. Use /learn in Telegram to add one."
                    if all_empty
                    else "No context modules match your search."
                )
                ui.label(message).classes("text-italic")
                return
            for label, group in group_module_rows(rows):
                section = ui.expansion(f"{label}  ·  {len(group)}", value=True).classes(
                    "w-full q-mb-sm"
                )
                section.props(f'header-class="text-h6 text-weight-bold" {DISCLOSURE_ICONS}')
                with section:
                    for row in group:
                        _render_row(row, store, refresh, user_email)

    search_input.on_value_change(lambda: refresh())
    refresh()
```

- [ ] **Step 2: Run the full anansi_app suite to confirm no regressions**

Run:

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
PYTHONPATH="$PWD:$PWD/anansi_app" /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.venv/bin/python -m pytest anansi_app/tests -q
```

Expected: PASS — same count as the Task 1/2 runs plus the pre-existing suite (289 at worktree creation, unchanged by this task since it adds no new test), 0 failures.

- [ ] **Step 3: Commit**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
git add anansi_app/nicegui_app/pages/knowledge_modules.py
git commit -m "feat(context): add a top-of-page search box, like the Prompts page"
```

---

### Task 4: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full anansi_app suite one more time**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
PYTHONPATH="$PWD:$PWD/anansi_app" /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.venv/bin/python -m pytest anansi_app/tests -q
```

Expected: PASS, 0 failures.

- [ ] **Step 2: Run `pre-commit run --all-files`**

Per this repo's CLAUDE.md, plain `pytest`/`git status` passing is not sufficient evidence of a clean commit — `pre-commit run --all-files` catches things they don't (e.g. a new test file silently dropped by a plain `git add` because `tests/` is gitignored by default). This task doesn't add a new test *file* (only edits an existing, already-tracked one), so that specific hazard doesn't apply here, but running the hook is still the required final check.

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
pre-commit run --all-files
```

Expected: all hooks pass. If `test-wiring` or any lint hook reports a problem, fix it and re-run this step until clean.

- [ ] **Step 3: Confirm everything is committed**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/feat/context-page-search
git status --short
git log --oneline origin/main..HEAD
```

Expected: `git status --short` is empty (nothing uncommitted); `git log` shows exactly the three feature commits from Tasks 1-3 (plus the design-spec commit already made during brainstorming) ahead of `origin/main`.
