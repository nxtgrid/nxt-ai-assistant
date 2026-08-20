# Context Page Search Design

## Goal

Add a live search box to the top of the Context admin page
(`anansi_app/nicegui_app/pages/knowledge_modules.py`), matching the placement
and interaction pattern of the Prompts page's search box, so an operator can
filter the list of context modules by slug, title, summary, or markdown body
content instead of scrolling through every pinned/on-demand group by hand.

## Current state

- The Prompts page (`prompts.py`) already has this exact pattern: a
  `ui.input` search box above the list, wired to `on_value_change(refresh)`,
  filtering an in-memory `PromptRow` list by `prompt_id`/`description`
  substring match before grouping.
- The Prompts page's Context tab (the per-prompt module picker) has a
  narrower version of the same idea: `filter_module_rows` filters
  `KnowledgeTabRow` by slug/title/summary.
- The top-level Context page (`knowledge_modules.py`) has neither — no
  search box, and its `ModuleRow` view model doesn't carry `summary` or the
  raw `body` text at all (only a derived `chars` count), so there is nothing
  to filter against yet.

## Design

### `ModuleRow` gains two fields

`summary: str = ""` and `body: str = ""`, populated in `build_module_rows`
from `m.summary` and `m.body or ""`. Both are already resident in memory
during `build_module_rows` today (see `chars = len(m.body or "")`), so this
adds no new store read.

### New `filter_context_rows(rows, query)` in `knowledge_modules.py`

Case-insensitive substring match against slug, title, summary, and body;
an empty/whitespace query is a no-op passthrough. This is named distinctly
from `prompts.py`'s existing `filter_module_rows` rather than reusing that
name: that function filters a different row type (`KnowledgeTabRow`, no
body field) for a different surface (the per-prompt module picker), and
`test_knowledge_modules_page.py` already imports from both modules in one
file — a distinct name avoids a same-name-different-behavior import
collision.

### `render()` wiring

A `search_input = ui.input(placeholder="Search context modules…").classes("w-full")`,
placed directly after the page's caption label and before `list_container`
— the same position and bare styling (no `clearable`/`dense` props) as the
Prompts page's top-level search box. Wired via
`search_input.on_value_change(lambda: refresh())`. Inside `refresh()`,
immediately after `rows = build_module_rows(store.all_modules())`, apply
`rows = filter_context_rows(rows, search_input.value or "")` before
`group_module_rows`. The "+ New context module" button stays unconditional
— it is rendered before the empty-rows check regardless of query state.

### Empty states

Two distinct messages, matching whether the *unfiltered* list is empty or
the *query* just doesn't match anything:

- No modules at all, no query: keep the existing "No context modules yet.
  Use /learn in Telegram to add one."
- Modules exist but none match the query: new "No context modules match
  your search."

## Scope and boundaries

- `prompts.py`'s own `filter_module_rows`/`KnowledgeTabRow` (the per-prompt
  module picker) is untouched — different surface, not part of this
  request.
- No visual change to `_render_row` — summary and body become searchable
  but are not newly displayed; this mirrors how body is already used today
  (only its length is shown, never the text itself).
- No debouncing, highlighting, or server-side query pushdown — matches
  every existing search pattern in this admin tool, and module counts here
  are small enough that per-keystroke in-memory filtering is not a
  performance concern.
- Sections stay grouped by mode (Pinned / On-demand) exactly as today,
  filtered before grouping so a mode with zero matches doesn't render an
  empty section; expansion state is untouched (both sections already
  default to expanded, unconditionally — search doesn't change that).

## Verification

Unit tests in `anansi_app/tests/test_knowledge_modules_page.py`:

1. Update `test_build_module_rows_reports_size` for the new `summary`/`body`
   fields on `ModuleRow`.
2. `filter_context_rows`: match on slug, on title, on summary, on body
   substring; case-insensitivity; empty query returns everything unchanged;
   a query matching nothing returns an empty list.
