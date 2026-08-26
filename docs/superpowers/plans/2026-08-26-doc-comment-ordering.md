# Doc Comment Ordering and Deferred Edits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make comment-driven Google Doc editing able to answer a comment whose instruction depends on the rest of the document ("add a summary here after finishing the rest"), by giving the generator the document text, ordering edits by real document position, and running dependent comments in a second pass.

**Architecture:** Three defects, three fixes, all inside `process_doc_edits`' comment-driven branch. (1) The generator currently receives no document text at all — fetch the markdown once and pass it as `section_context` with a budget large enough to summarise from. (2) Edits are sequenced by `reversed(creation order)`, which is not document order — sort by the character offset of each comment's quoted text in the markdown. (3) There is no notion of a comment that depends on other comments — one cheap LLM classification pass splits the batch into "now" and "after", and the second pass re-reads the document before it runs. All new logic lands in a new `edit_ordering.py` module as small pure functions plus one LLM call, so it is unit-testable without Drive.

**Tech Stack:** Python 3.11/3.12, pytest + pytest-asyncio, the repo's `shared.prompts` library (bundled `.prompt` files with `{{variable}}` substitution), `shared.llm` generation gateway, Google Drive `comments` API, Apps Script bridge for Doc writes.

---

## Context: what is broken today

Read this before starting. All three are in [`process_doc_edits.py`](../../../chat_orchestrator/orchestrator/experts/handlers/doc_editor/process_doc_edits.py)'s `MODE 1: Comment-driven` branch.

**1. The generator never sees the document.** The loop calls
`generate_replacement_markdown(instruction=..., highlighted_text=..., expert_context=..., user_email=...)`
and leaves `section_context` at its `""` default. That renders
`doc_editing.edit_highlighted` with an empty `{{context_block}}`, so the model
gets the comment text, the highlighted run, up to 14 allowlisted workflow-state
keys, and nothing else. "Summarise the rest of the document" is unanswerable —
the model invents a plausible summary and the thread is resolved `Done:`.
Instruction-driven mode (Mode 2) *does* pass `markdown[:1500]`; only the comment
batch flies blind.

**2. The ordering is not document ordering.** The current code:

```python
# Process in reverse order so earlier edits don't shift positions
# of later target text. Comments are returned in creation order;
# reversing approximates bottom-to-top document order.
comments = list(reversed(comments))
```

It sorts by *creation* time and hopes that tracks position. A summary comment
added last — the normal way you would author one — reverses to the **front**.
Nothing in the repo tests or pins Drive's list ordering.

**3. There is no dependency mechanism.** One scan, one pass, each comment an
independent LLM call, written and resolved immediately. No deferral, no re-read
between edits.

### Out of scope (found while specifying this, fix separately)

`shared/prompts/library/annotations.resolve_values.prompt` — the **Sheets**
filler's prompt — writes its placeholders as `{catalogue_block}` /
`{requests_block}` (single braces), but `shared/prompts/render.py`'s
`_VARIABLE` regex only substitutes `{{name}}`. The catalogue and the requests
are never interpolated. Verified:

```bash
PYTHONPATH=. chat_orchestrator/.venv/bin/python -c "from shared.prompts import PROMPTS; print('{catalogue_block}' in PROMPTS.text('annotations.resolve_values', catalogue_block='X', requests_block='Y'))"
```

prints `True`. `shared/tests/test_prompt_misc.py::test_annotations_prompt_renders_with_a_catalogue_and_requests`
passes only because both of its assertion strings (`energy.total_kwp`, `the
total peak capacity`) also appear in the prompt's own static example text. Do
**not** fix that here — it is a different code path (`fill_annotations`, Sheets
only). It matters for this plan in one way only: **Task 3 must use `{{var}}`
double braces and must assert substitution against a sentinel that does not
appear in the prompt body.**

---

## File Structure

| Path | Responsibility |
|---|---|
| **Create** `chat_orchestrator/orchestrator/experts/handlers/doc_editor/edit_ordering.py` | All ordering logic: locate a quote in the markdown, sort by position, parse and run the deferral classifier, partition into passes. Pure functions except the one LLM call. |
| **Create** `shared/prompts/library/doc_editor.order_edits.prompt` | The deferral classification prompt. |
| **Create** `chat_orchestrator/tests/experts/test_edit_ordering.py` | Unit tests for the above. **Gitignored — needs `git add -f`.** |
| **Modify** `shared/utils/doc_editing.py` | Extract `build_context_block()`; add a `context_limit` parameter to `generate_replacement_markdown`. |
| **Modify** `chat_orchestrator/orchestrator/experts/handlers/doc_editor/process_doc_edits.py` | Fetch markdown, classify, partition, run two passes. Extract the per-comment loop into `_apply_edits`. |
| **Modify** `chat_orchestrator/tests/test_prompt_parity.py` | `SAMPLE_VARS` entry for the new prompt. |
| **Modify** `chat_orchestrator/tests/prompt_checksums.json` | Checksum for the new prompt. |

### Two invariants that will bite you

**A. Classify before you sort.** The classifier prompt numbers comments `1..N`
by their position in the list you hand it. `partition_by_pass` maps those
numbers back. So the order of operations is fixed: **classify on the scan-order
list → partition → position-sort each pass separately.** Sorting first and
classifying second silently mismatches the numbering.

**B. Import inside the handler function, not at module top.** Every test in
Task 5 patches `shared.utils.doc_editing.<name>` and
`orchestrator.experts.handlers.doc_editor.edit_ordering.<name>`. A module-top
`from x import y` binds the name at import time and the patch will not take.
The file already uses function-local imports throughout — keep doing that.

### Test-suite mechanics

- Run orchestrator tests **from `chat_orchestrator/`**: its `pyproject.toml`
  sets `asyncio_mode = "auto"` and `pythonpath = [".", ".."]`. From the repo
  root you get the root `pyproject.toml` instead (`asyncio_mode = strict`) and
  async tests error out. CI uses `working-directory: chat_orchestrator`.
- Async tests still carry an explicit `@pytest.mark.asyncio` — matches
  `test_create_from_template.py` and survives being run from either directory.
- `chat_orchestrator/tests/` is gitignored (`.gitignore:118`). A plain
  `git add` on a new test file is a **silent no-op**. Every commit step below
  that touches a test file uses `git add -f`. Same for this plan document
  (`.gitignore:151`).

---

## Task 1: Position-based ordering

**Files:**
- Create: `chat_orchestrator/orchestrator/experts/handlers/doc_editor/edit_ordering.py`
- Test: `chat_orchestrator/tests/experts/test_edit_ordering.py`

- [ ] **Step 1: Write the failing tests**

Create `chat_orchestrator/tests/experts/test_edit_ordering.py`:

```python
"""Ordering comment-driven doc edits: position, deferral, and partitioning."""

import pytest

from orchestrator.experts.handlers.doc_editor.edit_ordering import (
    document_position,
    order_by_position,
)

MARKDOWN = """## Executive summary

SUMMARY PLACEHOLDER

## Findings

The inverter tripped twice in March.

## Recommendations

Replace the DC fuse.
"""


def _comment(comment_id, quoted):
    return {"comment_id": comment_id, "highlighted_text": quoted, "instruction": "edit it"}


def test_position_is_the_character_offset_of_the_quote():
    assert document_position(MARKDOWN, "SUMMARY PLACEHOLDER") == MARKDOWN.find(
        "SUMMARY PLACEHOLDER"
    )


def test_an_absent_quote_has_no_position():
    assert document_position(MARKDOWN, "nothing like this in the doc") == -1


def test_an_empty_quote_has_no_position():
    assert document_position(MARKDOWN, "") == -1


def test_an_html_escaped_quote_still_matches():
    """Drive serves quotedFileContent as text/html — see Annotation's docstring."""
    markdown = "Costs rose 5% & margins fell."
    assert document_position(markdown, "5% &amp; margins") == markdown.find("5% & margins")


def test_a_multi_line_quote_falls_back_to_its_first_line():
    assert document_position(MARKDOWN, "Replace the DC fuse.\nAND SOMETHING ELSE") == (
        MARKDOWN.find("Replace the DC fuse.")
    )


def test_edits_run_bottom_to_top():
    ordered = order_by_position(
        [
            _comment("top", "SUMMARY PLACEHOLDER"),
            _comment("bottom", "Replace the DC fuse."),
            _comment("middle", "The inverter tripped twice in March."),
        ],
        MARKDOWN,
    )
    assert [c["comment_id"] for c in ordered] == ["bottom", "middle", "top"]


def test_unlocatable_comments_sort_last():
    ordered = order_by_position(
        [
            _comment("ghost", "text that was deleted"),
            _comment("real", "SUMMARY PLACEHOLDER"),
        ],
        MARKDOWN,
    )
    assert [c["comment_id"] for c in ordered] == ["real", "ghost"]


def test_ordering_an_empty_batch_is_not_an_error():
    assert order_by_position([], MARKDOWN) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_edit_ordering.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named 'orchestrator.experts.handlers.doc_editor.edit_ordering'`.

- [ ] **Step 3: Write the implementation**

Create `chat_orchestrator/orchestrator/experts/handlers/doc_editor/edit_ordering.py`:

```python
"""Deciding what order a document's comment edits run in.

Two separate problems live here and they are solved differently on purpose.

*Position* -- an edit that rewrites text shifts everything below it, so edits
run bottom-to-top. That is arithmetic, not judgement: the quoted text either
appears in the document markdown at some offset or it does not, and `str.find`
answers it for free. It replaced a `reversed(creation order)` approximation
that was only ever right by luck.

*Dependency* -- "add a summary here once the rest is written" cannot be
answered until the other comments have been applied. Nothing about the text
reveals that; it is in what the instruction means. That one needs the model,
and it is the only thing here that costs a call.
"""

import html
import json
import logging
from typing import Any, Dict, List, Set

from shared.prompts import PROMPTS

LOGGER = logging.getLogger(__name__)

# How much of the document the generator and the classifier each get. The
# 1500-char default in generate_replacement_markdown is sized for "here is the
# paragraph around the bit you are rewriting"; an instruction like "summarise
# everything above" is unanswerable from 1500 characters, which is the whole
# reason this plan exists. 12k keeps a full ordinary report in the prompt while
# staying far below the model's window.
DOC_CONTEXT_CHAR_LIMIT = 12000


def document_position(markdown: str, quoted_text: str) -> int:
    """Character offset of a comment's quoted text in the document markdown.

    Returns -1 when the quote cannot be located -- the text was edited since
    the comment was made, or the markdown conversion renders it differently.

    Drive serves `quotedFileContent` as text/html (see `Annotation` in
    shared/utils/file_annotations.py), so it is unescaped before matching.
    """
    if not quoted_text:
        return -1

    needle = html.unescape(quoted_text).strip()
    if not needle:
        return -1

    position = markdown.find(needle)
    if position != -1:
        return position

    # A Docs quote can span a paragraph break that the markdown conversion
    # renders with different whitespace. The first line is enough to place it.
    first_line = needle.splitlines()[0].strip()
    if first_line and first_line != needle:
        return markdown.find(first_line)
    return -1


def order_by_position(comments: List[Dict[str, Any]], markdown: str) -> List[Dict[str, Any]]:
    """Bottom-to-top document order, so an edit never shifts a later target.

    Comments whose quote cannot be located sort last: their write will fail to
    find its target whatever we do, and running them last keeps that failure
    from disturbing the ones that can still succeed.
    """
    located = []
    unlocated = []
    for comment in comments:
        position = document_position(markdown, comment.get("highlighted_text", ""))
        if position < 0:
            unlocated.append(comment)
        else:
            located.append((position, comment))

    located.sort(key=lambda pair: pair[0], reverse=True)
    return [comment for _, comment in located] + unlocated
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_edit_ordering.py -q
```

Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/doc_editor/edit_ordering.py && git add -f chat_orchestrator/tests/experts/test_edit_ordering.py && git commit -m "feat(doc-editor): order comment edits by document position, not creation time"
```

---

## Task 2: A context budget the generator can summarise from

**Files:**
- Modify: `shared/utils/doc_editing.py` (the `context_block` construction inside `generate_replacement_markdown`, currently at `shared/utils/doc_editing.py:277-279`)
- Test: `shared/tests/test_doc_context_block.py` (**gitignored — `git add -f`**)

- [ ] **Step 1: Write the failing test**

Create `shared/tests/test_doc_context_block.py`:

```python
"""The SURROUNDING CONTEXT block and its per-caller truncation budget."""

from shared.utils.doc_editing import build_context_block


def test_no_context_produces_no_block():
    assert build_context_block("") == ""


def test_a_block_is_labelled_for_the_model():
    assert build_context_block("the whole doc") == "\nSURROUNDING CONTEXT:\nthe whole doc"


def test_the_default_budget_matches_the_single_edit_path():
    """Mode 2 passed markdown[:1500]; the default must not change its prompt."""
    assert build_context_block("x" * 5000).endswith("x" * 1500)
    assert len(build_context_block("x" * 5000)) == len("\nSURROUNDING CONTEXT:\n") + 1500


def test_a_caller_can_ask_for_a_larger_budget():
    block = build_context_block("y" * 20000, context_limit=12000)
    assert len(block) == len("\nSURROUNDING CONTEXT:\n") + 12000


def test_context_shorter_than_the_budget_is_passed_whole():
    assert build_context_block("short", context_limit=12000).endswith("short")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_doc_context_block.py -q
```

Expected: `ImportError: cannot import name 'build_context_block' from 'shared.utils.doc_editing'`.

- [ ] **Step 3: Write the implementation**

In `shared/utils/doc_editing.py`, add this function immediately above `generate_replacement_markdown`:

```python
def build_context_block(section_context: str, context_limit: int = 1500) -> str:
    """The SURROUNDING CONTEXT block, truncated to the caller's budget.

    The default is sized for a single instruction-driven edit, where the point
    is "here is the paragraph around the bit you are rewriting". The
    comment-driven batch passes the whole document and a much larger limit --
    an instruction like "summarise the sections above" is unanswerable from
    1500 characters, and answering it from nothing at all is what this
    parameter exists to stop.
    """
    if not section_context:
        return ""
    return f"\nSURROUNDING CONTEXT:\n{section_context[:context_limit]}"
```

Then change `generate_replacement_markdown`'s signature from:

```python
async def generate_replacement_markdown(
    instruction: str,
    highlighted_text: str,
    section_context: str = "",
    expert_context: dict[str, Any] | None = None,
    user_email: str | None = None,
) -> str:
```

to:

```python
async def generate_replacement_markdown(
    instruction: str,
    highlighted_text: str,
    section_context: str = "",
    expert_context: dict[str, Any] | None = None,
    user_email: str | None = None,
    context_limit: int = 1500,
) -> str:
```

Add one line to its docstring's `Args:` block, after the `user_email` line:

```
        context_limit: How much of section_context to include. The default
            suits a single section edit; the comment-driven batch raises it
            so an instruction can refer to the whole document.
```

And replace this body fragment:

```python
    context_block = ""
    if section_context:
        context_block = f"\nSURROUNDING CONTEXT:\n{section_context[:1500]}"
```

with:

```python
    context_block = build_context_block(section_context, context_limit)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_doc_context_block.py ../shared/tests/test_doc_editing_compat.py ../shared/tests/test_prompt_misc.py -q
```

Expected: all pass. `test_doc_editing_compat.py` and `test_prompt_misc.py` are there to prove the re-export surface and the `edit_highlighted` rendering are untouched.

- [ ] **Step 5: Commit**

```bash
git add shared/utils/doc_editing.py && git add -f shared/tests/test_doc_context_block.py && git commit -m "feat(doc-editing): let callers set the document context budget"
```

---

## Task 3: The deferral classifier

**Files:**
- Create: `shared/prompts/library/doc_editor.order_edits.prompt`
- Modify: `chat_orchestrator/orchestrator/experts/handlers/doc_editor/edit_ordering.py`
- Modify: `chat_orchestrator/tests/test_prompt_parity.py` (`SAMPLE_VARS`)
- Modify: `chat_orchestrator/tests/prompt_checksums.json`
- Test: `chat_orchestrator/tests/experts/test_edit_ordering.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `chat_orchestrator/tests/experts/test_edit_ordering.py`:

```python
# ── the deferral classifier ──────────────────────────────────────────────


def test_the_ordering_prompt_actually_substitutes_its_variables():
    """A bare PromptLibrary, and sentinels absent from the prompt's own text.

    Both halves matter. The bare library (never the shared PROMPTS singleton)
    keeps a developer's local .env from resolving this against the live
    chat_db prompts table -- see the note atop test_prompt_parity.py. The
    sentinels guard against the failure that annotations.resolve_values is
    sitting in right now: single-brace {placeholders} that render() never
    substitutes, under a test whose assertion strings happened to also appear
    in the prompt's static body, so it passed while the model got nothing.
    """
    from shared.prompts import PromptLibrary

    text = PromptLibrary().text(
        "doc_editor.order_edits",
        comments_block="ZZCOMMENTSENTINELZZ",
        markdown="ZZMARKDOWNSENTINELZZ",
    )
    assert "ZZCOMMENTSENTINELZZ" in text
    assert "ZZMARKDOWNSENTINELZZ" in text
    assert "{{" not in text
    assert "{comments_block}" not in text


def test_parse_deferred_reads_a_plain_json_array():
    from orchestrator.experts.handlers.doc_editor.edit_ordering import parse_deferred

    assert parse_deferred(
        '[{"request": 1, "deferred": false}, {"request": 2, "deferred": true}]'
    ) == {2}


def test_parse_deferred_strips_a_code_fence():
    from orchestrator.experts.handlers.doc_editor.edit_ordering import parse_deferred

    assert parse_deferred('```json\n[{"request": 3, "deferred": true}]\n```') == {3}


def test_parse_deferred_ignores_entries_that_are_not_deferred():
    from orchestrator.experts.handlers.doc_editor.edit_ordering import parse_deferred

    assert parse_deferred('[{"request": 1}, {"request": 2, "deferred": "yes"}]') == set()


def test_unparseable_ordering_degrades_to_a_single_pass():
    from orchestrator.experts.handlers.doc_editor.edit_ordering import parse_deferred

    assert parse_deferred("I could not decide, sorry") == set()
    assert parse_deferred('{"request": 1}') == set()
    assert parse_deferred("") == set()


@pytest.mark.asyncio
async def test_a_single_comment_never_costs_an_llm_call(monkeypatch):
    """Nothing to order, and this is the common case — it must stay free."""
    from orchestrator.experts.handlers.doc_editor import edit_ordering

    async def _explode(*args, **kwargs):
        raise AssertionError("classify_deferred must not reach the model here")

    monkeypatch.setattr(edit_ordering, "_classify", _explode)
    assert await edit_ordering.classify_deferred([_comment("a", "x")], MARKDOWN) == set()
    assert await edit_ordering.classify_deferred([], MARKDOWN) == set()


@pytest.mark.asyncio
async def test_a_failing_classifier_never_blocks_the_edit_run(monkeypatch):
    from orchestrator.experts.handlers.doc_editor import edit_ordering

    async def _boom(*args, **kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(edit_ordering, "_classify", _boom)
    result = await edit_ordering.classify_deferred(
        [_comment("a", "x"), _comment("b", "y")], MARKDOWN
    )
    assert result == set()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_edit_ordering.py -q
```

Expected: failures — `PromptNotFound`/`KeyError` for `doc_editor.order_edits`, and `ImportError` for `parse_deferred`.

- [ ] **Step 3: Write the prompt**

Create `shared/prompts/library/doc_editor.order_edits.prompt`. **Double braces on the variables** — `render.py`'s `_VARIABLE` is `\{\{\s*([A-Za-z0-9_]+)\s*\}\}` and single braces are left untouched:

```
---
id: doc_editor.order_edits
description: Decides which document comments can only be answered once the other comments have been applied.
owner: eng
component: shared
overridable: true
model: fast
output: json
schema:
  type: array
  items:
    type: object
    required: [request, deferred]
    properties:
      request: {type: integer}
      deferred: {type: boolean}
      reason: {type: string}
sections: []
variables: [comments_block, markdown]
access:
  view: [ops, eng]
  edit: []
  publish: []
---
Someone left comments on a Google Doc asking for edits, and they are about to
be applied automatically. Decide which of them can only be answered once the
OTHER comments in this batch have already been applied.

DOCUMENT (may be truncated):
{{markdown}}

COMMENTS
{{comments_block}}

Defer a comment when answering it needs the rest of the document to be
finished — "summarise the sections above", "add an executive summary once the
rest is written", "list every recommendation made below", "write the
conclusion last".

Do NOT defer a comment just because it sits near the top of the document,
names another section, or is long. Rewriting a paragraph, filling in a figure,
fixing tone, tightening a list: all ordinary edits, none of them deferred.

Default to not deferred. Deferring costs the writer a second pass, and is only
worth it when the answer genuinely depends on text another comment in this
batch is going to produce.

Return ONLY a JSON array, one object per comment, in the same order, no fences:
[{"request": 1, "deferred": false, "reason": "rewrites one paragraph in place"},
 {"request": 2, "deferred": true, "reason": "summarises the finished document"}]
```

- [ ] **Step 4: Write the classifier**

Append to `chat_orchestrator/orchestrator/experts/handlers/doc_editor/edit_ordering.py`:

```python
def parse_deferred(text: str) -> Set[int]:
    """The 1-based request numbers the model marked deferred.

    Deliberately tolerant. An ordering pass is an optimisation on top of work
    the user actually asked for -- a response we cannot read degrades to a
    single-pass run, which is exactly today's behaviour, never to a failed
    edit run.
    """
    body = text.strip()
    if "```" in body:
        body = body.split("```")[1]
        if body.startswith("json"):
            body = body[4:]

    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError, IndexError):
        LOGGER.warning("Could not parse the edit ordering response; running a single pass")
        return set()

    if not isinstance(parsed, list):
        return set()

    return {
        int(item["request"])
        for item in parsed
        if isinstance(item, dict) and item.get("deferred") is True and "request" in item
    }


def build_comments_block(comments: List[Dict[str, Any]]) -> str:
    """The numbered request list the classifier prompt is built around.

    The numbering is positional: `parse_deferred` maps the model's answers
    back through it, so this must be built from the same list, in the same
    order, that `partition_by_pass` is later given.
    """
    return "\n".join(
        f'{i}. instruction: "{comment.get("instruction", "")}"\n'
        f'   quoted text: "{(comment.get("highlighted_text") or "")[:200]}"'
        for i, comment in enumerate(comments, start=1)
    )


async def _classify(comments_block: str, markdown: str) -> str:
    """The raw model response. Split out so tests can fail it deliberately."""
    from orchestrator.config.settings import get_settings
    from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway

    settings = get_settings()
    gateway = get_default_generation_gateway(default_model=settings.gemini.model)

    prompt = PROMPTS.text(
        "doc_editor.order_edits",
        comments_block=comments_block,
        markdown=markdown[:DOC_CONTEXT_CHAR_LIMIT],
    )
    response = await gateway.generate(
        [LLMMessage(role="user", text=prompt)],
        GenerationOptions(model=settings.gemini.model, temperature=0.1, max_output_tokens=1000),
    )
    return str(response.text)


async def classify_deferred(comments: List[Dict[str, Any]], markdown: str) -> Set[int]:
    """Which comments (1-based, in the given order) belong in the second pass.

    One extra model call per run, skipped entirely below two comments -- there
    is nothing to order, and one comment is the common case. Every failure
    path returns an empty set: ordering must never be the reason an edit the
    user asked for does not happen.
    """
    if len(comments) < 2:
        return set()

    try:
        return parse_deferred(await _classify(build_comments_block(comments), markdown))
    except Exception as e:
        LOGGER.warning(f"Edit ordering pass failed; running every comment in one pass: {e}")
        return set()
```

- [ ] **Step 5: Register sample variables for the prompt-parity suite**

In `chat_orchestrator/tests/test_prompt_parity.py`, add to `SAMPLE_VARS` — keep it alphabetical, so directly after the `"doc_editor.locate_edits"` entry:

```python
    "doc_editor.order_edits": {
        "comments_block": '1. instruction: "summarise the above"\n   quoted text: "TBD"',
        "markdown": "# Title\n\nBody",
    },
```

- [ ] **Step 6: Add the new prompt to the checksum snapshot**

The snapshot is regenerated by deleting it, not hand-edited. Delete, regenerate, then **check the diff is exactly one added line** — a wholesale regeneration would silently absorb unrelated drift, which is the one thing this snapshot exists to catch:

```bash
cd chat_orchestrator && rm tests/prompt_checksums.json && uv run pytest tests/test_prompt_parity.py -q && uv run pytest tests/test_prompt_parity.py -q
```

Expected: first run skips with `snapshot created; re-run to verify`, second run passes.

```bash
git diff --stat chat_orchestrator/tests/prompt_checksums.json && git diff chat_orchestrator/tests/prompt_checksums.json
```

Expected: exactly one added line, for `doc_editor.order_edits`. If any other line changed, stop — something else drifted and needs explaining before you commit.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_edit_ordering.py tests/test_prompt_parity.py -q && uv run pytest ../shared/tests/test_prompt_misc.py -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add shared/prompts/library/doc_editor.order_edits.prompt chat_orchestrator/orchestrator/experts/handlers/doc_editor/edit_ordering.py && git add -f chat_orchestrator/tests/experts/test_edit_ordering.py chat_orchestrator/tests/test_prompt_parity.py chat_orchestrator/tests/prompt_checksums.json && git commit -m "feat(doc-editor): classify which comment edits must wait for the rest of the document"
```

---

## Task 4: Partitioning into passes

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/handlers/doc_editor/edit_ordering.py`
- Test: `chat_orchestrator/tests/experts/test_edit_ordering.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `chat_orchestrator/tests/experts/test_edit_ordering.py`:

```python
# ── partitioning ─────────────────────────────────────────────────────────


def test_partition_splits_on_the_classifier_numbering():
    from orchestrator.experts.handlers.doc_editor.edit_ordering import partition_by_pass

    comments = [_comment("a", "x"), _comment("b", "y"), _comment("c", "z")]
    first, second = partition_by_pass(comments, {2})
    assert [c["comment_id"] for c in first] == ["a", "c"]
    assert [c["comment_id"] for c in second] == ["b"]


def test_nothing_deferred_leaves_a_single_pass():
    from orchestrator.experts.handlers.doc_editor.edit_ordering import partition_by_pass

    comments = [_comment("a", "x"), _comment("b", "y")]
    first, second = partition_by_pass(comments, set())
    assert len(first) == 2
    assert second == []


def test_a_request_number_out_of_range_is_ignored():
    """A hallucinated index must not drop a comment or crash the run."""
    from orchestrator.experts.handlers.doc_editor.edit_ordering import partition_by_pass

    comments = [_comment("a", "x")]
    first, second = partition_by_pass(comments, {7})
    assert [c["comment_id"] for c in first] == ["a"]
    assert second == []
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_edit_ordering.py -q -k partition
```

Expected: `ImportError: cannot import name 'partition_by_pass'`.

- [ ] **Step 3: Write the implementation**

Append to `chat_orchestrator/orchestrator/experts/handlers/doc_editor/edit_ordering.py`:

```python
def partition_by_pass(
    comments: List[Dict[str, Any]], deferred: Set[int]
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split into (first pass, second pass), preserving the order given.

    `deferred` holds 1-based positions in `comments` exactly as the classifier
    was shown them, so this must be called with the same list, in the same
    order, that `build_comments_block` was given -- before any position sort
    reorders it. A number outside the range is ignored rather than trusted:
    the model does not get to drop a comment the user left.
    """
    first: List[Dict[str, Any]] = []
    second: List[Dict[str, Any]] = []
    for index, comment in enumerate(comments, start=1):
        (second if index in deferred else first).append(comment)
    return first, second
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_edit_ordering.py -q
```

Expected: all pass (Tasks 1, 3 and 4 tests together).

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/doc_editor/edit_ordering.py && git add -f chat_orchestrator/tests/experts/test_edit_ordering.py && git commit -m "feat(doc-editor): partition comment edits into two passes"
```

---

## Task 5: Wire the two-pass loop into the handler

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/handlers/doc_editor/process_doc_edits.py`
- Test: `chat_orchestrator/tests/experts/test_two_pass_doc_edits.py` (**gitignored — `git add -f`**)

- [ ] **Step 1: Write the failing test**

Create `chat_orchestrator/tests/experts/test_two_pass_doc_edits.py`:

```python
"""The comment-driven branch: two passes, document order, fresh context."""

import pytest

from orchestrator.experts.handlers.doc_editor.process_doc_edits import process_doc_edits

BEFORE = """## Executive summary

SUMMARY PLACEHOLDER

## Findings

FINDINGS PLACEHOLDER
"""

AFTER = """## Executive summary

SUMMARY PLACEHOLDER

## Findings

The inverter tripped twice in March.
"""


class _FakeContext:
    """Only the surface process_doc_edits actually touches."""

    def __init__(self, inputs):
        self._inputs = inputs
        self.packet_state = {}
        self.effective_email = "editor@example.com"
        self.progress = []

    def get_input(self, key, default=None):
        return self._inputs.get(key, default)

    async def send_progress_to_user(self, message):
        self.progress.append(message)


# Creation order matters here, and it is deliberately the awkward one: the
# summary comment was added LAST, which is how someone actually writes one --
# you notice you want a summary after the rest of the template exists. The old
# `reversed(creation order)` therefore put the summary FIRST, which is exactly
# backwards. Flip these two entries and the headline test passes against the
# unfixed code, proving nothing.
_FINDINGS = {
    "comment_id": "findings",
    "instruction": "Write up the March inverter trips",
    "highlighted_text": "FINDINGS PLACEHOLDER",
    "author_email": "a@x.com",
    "created_time": "2026-08-26T09:00:00Z",
}
_SUMMARY = {
    "comment_id": "summary",
    "instruction": "Add a summary here after finishing the rest",
    "highlighted_text": "SUMMARY PLACEHOLDER",
    "author_email": "a@x.com",
    "created_time": "2026-08-26T09:01:00Z",
}


@pytest.fixture
def wired(monkeypatch):
    """Patch every I/O seam and record what the handler did, in order."""
    import shared.utils.doc_editing as doc_editing
    import shared.utils.gdrive_doc_fetcher as fetcher
    from orchestrator.experts.handlers.doc_editor import edit_ordering

    calls = {"edits": [], "generated": [], "scans": 0, "pins": 0}
    fetches = [BEFORE, AFTER]

    async def _scan(doc_id):
        """First call: both threads open. Second: pass one resolved 'findings'."""
        calls["scans"] += 1
        return [_FINDINGS, _SUMMARY] if calls["scans"] == 1 else [_SUMMARY]

    def _fetch(doc_id):
        return fetches.pop(0) if fetches else AFTER

    async def _classify(comments, markdown):
        return {2}  # the summary comment, second in scan order

    async def _generate(instruction, highlighted_text, **kwargs):
        calls["generated"].append((highlighted_text, kwargs.get("section_context", "")))
        return f"generated for {highlighted_text}"

    async def _edit(doc_id, target_text, replacement_markdown, comment_id=None):
        calls["edits"].append(comment_id)
        return {"success": True, "elements_written": 1}

    async def _pin(doc_id):
        calls["pins"] += 1
        return True

    monkeypatch.setattr(doc_editing, "scan_comments", _scan)
    monkeypatch.setattr(doc_editing, "generate_replacement_markdown", _generate)
    monkeypatch.setattr(doc_editing, "edit_section", _edit)
    monkeypatch.setattr(doc_editing, "pin_revision", _pin)
    monkeypatch.setattr(fetcher, "fetch_google_doc_markdown", _fetch)
    monkeypatch.setattr(edit_ordering, "classify_deferred", _classify)
    return calls


@pytest.mark.asyncio
async def test_the_deferred_edit_is_written_last(wired):
    await process_doc_edits(_FakeContext({"document_id": "doc-1"}))
    assert wired["edits"] == ["findings", "summary"]


@pytest.mark.asyncio
async def test_the_deferred_edit_sees_the_finished_document(wired):
    await process_doc_edits(_FakeContext({"document_id": "doc-1"}))
    contexts = {highlighted: context for highlighted, context in wired["generated"]}
    summary_context = contexts["SUMMARY PLACEHOLDER"]
    assert "The inverter tripped twice in March." in summary_context
    assert "FINDINGS PLACEHOLDER" not in summary_context


@pytest.mark.asyncio
async def test_the_first_pass_also_gets_document_context(wired):
    """The bug this plan exists to fix: section_context used to be always ''."""
    await process_doc_edits(_FakeContext({"document_id": "doc-1"}))
    contexts = {highlighted: context for highlighted, context in wired["generated"]}
    findings_context = contexts["FINDINGS PLACEHOLDER"]
    assert "Executive summary" in findings_context


@pytest.mark.asyncio
async def test_the_revision_is_pinned_once_for_the_whole_run(wired):
    await process_doc_edits(_FakeContext({"document_id": "doc-1"}))
    assert wired["pins"] == 1


@pytest.mark.asyncio
async def test_both_edits_are_reported(wired):
    result = await process_doc_edits(_FakeContext({"document_id": "doc-1"}))
    assert result.data["succeeded"] == 2
    assert result.data["failed"] == 0
    assert result.data["deferred"] == 1
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_two_pass_doc_edits.py -q
```

Expected: failures — `wired["edits"] == ["summary", "findings"]` (current `reversed(creation order)`), and empty `section_context`.

- [ ] **Step 3: Add the imports and the two helpers**

In `chat_orchestrator/orchestrator/experts/handlers/doc_editor/process_doc_edits.py`, add `asyncio` to the stdlib imports at the top so the blocking Drive fetch does not sit on the event loop:

```python
import asyncio
import json
import logging
from typing import Any, Dict
```

Add both helpers immediately above `process_doc_edits`' `@register_step` decorator:

```python
async def _fetch_markdown(doc_id: str) -> str:
    """The document as markdown, off the event loop.

    fetch_google_doc_markdown is a blocking Drive call. Mode 2 has always
    called it inline; the comment-driven batch calls it twice per run, so it
    goes through a thread.
    """
    from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown

    markdown = await asyncio.to_thread(fetch_google_doc_markdown, doc_id)
    if not markdown:
        LOGGER.warning(f"Could not fetch {doc_id} as markdown — editing without document context")
    return markdown or ""


async def _refresh_second_pass(doc_id: str, second_pass: list[dict]) -> list[dict]:
    """Re-scan so the second pass matches text as it stands after pass one.

    One comments.list call rather than one comments.get per comment. A comment
    that pass one resolved, or that a human resolved mid-run, simply stops
    coming back and is dropped -- the correct outcome either way.
    """
    from shared.utils.doc_editing import scan_comments

    wanted = {comment["comment_id"] for comment in second_pass}
    return [comment for comment in await scan_comments(doc_id) if comment["comment_id"] in wanted]


async def _apply_edits(
    context: StepContext, doc_id: str, comments: list[dict], markdown: str
) -> list[dict]:
    """Generate and write one replacement per comment, in the order given."""
    from orchestrator.experts.handlers.doc_editor.edit_ordering import DOC_CONTEXT_CHAR_LIMIT
    from shared.utils.doc_editing import edit_section, generate_replacement_markdown

    results = []
    for comment in comments:
        highlighted = comment["highlighted_text"]
        comment_id = comment["comment_id"]

        if not highlighted:
            results.append({"comment_id": comment_id, "status": "skipped"})
            continue

        try:
            replacement = await generate_replacement_markdown(
                instruction=comment["instruction"],
                highlighted_text=highlighted,
                section_context=markdown,
                expert_context=context.packet_state,
                user_email=context.effective_email,
                context_limit=DOC_CONTEXT_CHAR_LIMIT,
            )

            result = await edit_section(
                doc_id=doc_id,
                target_text=highlighted,
                replacement_markdown=replacement,
                comment_id=comment_id,
            )

            if result.get("success"):
                results.append({"comment_id": comment_id, "status": "done"})
            else:
                results.append(
                    {"comment_id": comment_id, "status": "failed", "error": result.get("error")}
                )

        except Exception as e:
            LOGGER.error(f"Edit failed for comment {comment_id}: {e}")
            from shared.utils.error_messages import sanitize_error_for_user

            results.append(
                {
                    "comment_id": comment_id,
                    "status": "failed",
                    "error": sanitize_error_for_user(str(e)),
                }
            )
    return results
```

- [ ] **Step 4: Replace the comment-driven branch**

In `process_doc_edits`, replace everything from `# ── MODE 1: Comment-driven ──` to the end of the function with:

```python
        # ── MODE 1: Comment-driven ──
        await context.send_progress_to_user("Checking for @anansibot comments...")

        comments = await scan_comments(doc_id)
        if not comments:
            return StepResult(
                data={"edits": 0},
                progress_message="No pending @anansibot comments found.",
            )

        if len(comments) > MAX_EDITS_PER_RUN:
            LOGGER.warning(f"Capping edits from {len(comments)} to {MAX_EDITS_PER_RUN}")
            comments = comments[:MAX_EDITS_PER_RUN]

        from orchestrator.experts.handlers.doc_editor import edit_ordering

        markdown = await _fetch_markdown(doc_id)

        # Classify against the scan-order list, because the prompt numbers the
        # comments by their position in it. Only then sort into document order.
        deferred = await edit_ordering.classify_deferred(comments, markdown) if markdown else set()
        first_pass, second_pass = edit_ordering.partition_by_pass(comments, deferred)
        first_pass = edit_ordering.order_by_position(first_pass, markdown)

        await context.send_progress_to_user(f"Processing {len(comments)} edit(s)...")

        requester = context.effective_email or "unknown"
        LOGGER.info(
            f"Doc editor: {requester} editing doc {doc_id} "
            f"({len(first_pass)} now, {len(second_pass)} after the rest)"
        )

        # Pin revision once before the batch
        await pin_revision(doc_id)

        results = await _apply_edits(context, doc_id, first_pass, markdown)

        deferred_count = len(second_pass)
        if second_pass:
            await context.send_progress_to_user(
                f"Writing {deferred_count} edit(s) that needed the finished document..."
            )
            fresh_markdown = await _fetch_markdown(doc_id) or markdown
            second_pass = await _refresh_second_pass(doc_id, second_pass)
            second_pass = edit_ordering.order_by_position(second_pass, fresh_markdown)
            results += await _apply_edits(context, doc_id, second_pass, fresh_markdown)

        succeeded = sum(1 for r in results if r["status"] == "done")
        failed = sum(1 for r in results if r["status"] == "failed")

        return StepResult(
            data={
                "edit_results": results,
                "succeeded": succeeded,
                "failed": failed,
                "deferred": deferred_count,
            },
            progress_message=f"Edited {succeeded} section(s)"
            + (f", {failed} failed" if failed else ""),
        )
```

- [ ] **Step 5: Route Mode 2's fetch through the same helper**

Still in `process_doc_edits`, in the instruction-driven branch, replace:

```python
        from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown

        markdown = fetch_google_doc_markdown(doc_id)
        if not markdown:
            return StepResult(error="Could not fetch document as markdown.")
```

with:

```python
        markdown = await _fetch_markdown(doc_id)
        if not markdown:
            return StepResult(error="Could not fetch document as markdown.")
```

Mode 2 keeps its 1500-char budget: it still calls `generate_replacement_markdown(..., section_context=markdown[:1500])` with no `context_limit`, so its prompt is byte-for-byte what it was. The only change is that the blocking Drive call no longer runs on the event loop.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_two_pass_doc_edits.py tests/experts/test_process_doc_edits_contract.py tests/experts/test_edit_ordering.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/doc_editor/process_doc_edits.py && git add -f chat_orchestrator/tests/experts/test_two_pass_doc_edits.py && git commit -m "feat(doc-editor): run deferred comment edits in a second pass over the finished document"
```

---

## Task 6: Full verification

**Files:** none — this task only runs things.

- [ ] **Step 1: Run the orchestrator suite**

```bash
cd chat_orchestrator && uv run pytest tests/ -q
```

Expected: all pass. `tests/experts/test_parameter_confirmation.py` wipes the global step registry with no teardown, so suite-order failures in registry lookups are pre-existing and unrelated — snapshot contracts at module scope, as `test_process_doc_edits_contract.py` already does.

- [ ] **Step 2: Run the shared suite**

```bash
cd chat_orchestrator && uv run pytest ../shared -q -n auto
```

Expected: all pass. If `test_prompt_parity.py` or a test with "bundled" in its name fails **locally only**, check `chat_orchestrator/.env` before suspecting the code — real `CHAT_DB_URL` / `GOOGLE_SERVICE_ACCOUNT_JSON` values make the `PROMPTS` singleton resolve against live data. Inspect without printing secrets:

```bash
awk -F= '{print $1"="length($2)}' chat_orchestrator/.env
```

- [ ] **Step 3: Run pre-commit across the whole repo**

```bash
pre-commit run --all-files
```

This is the only check that catches a test file that was never actually committed. If `test-wiring` reports untracked files under any `tests/` directory, vet them for operator data and `git add -f` each one, then re-run until clean.

- [ ] **Step 4: Confirm every new file actually reached the commits**

```bash
git log --oneline -6 && git show --stat HEAD~4..HEAD | grep -E "edit_ordering|order_edits|two_pass|context_block|prompt_checksums"
```

Expected: all five new paths appear. A missing test file here means a plain `git add` silently dropped it.

- [ ] **Step 5: Commit the plan document**

```bash
git add -f docs/superpowers/plans/2026-08-26-doc-comment-ordering.md && git commit -m "docs(plan): comment-edit ordering and deferred second pass"
```

---

## Manual verification against a real document

Automated tests cover ordering and context assembly; they cannot prove Drive
and Apps Script behave. Before merging, run once against a scratch Doc:

1. Create a Google Doc with an `## Executive summary` section containing
   `SUMMARY PLACEHOLDER`, and two or three sections below it with their own
   placeholder text.
2. Comment `@anansi-chatbot add a summary here after finishing the rest of the
   document` on `SUMMARY PLACEHOLDER` — **add this comment last**, so creation
   order and document order disagree and the old code path would get it wrong.
3. Comment `@anansi-chatbot` with a concrete instruction on each lower
   placeholder.
4. Run `process_doc_edits` against the doc id.
5. Check: the lower sections filled first; the summary reflects **what those
   sections actually now say**, not invented content; every thread resolved;
   one pinned revision in version history.

The single highest-value check is step 5's middle clause. A summary that reads
plausibly but describes content the document does not contain means
`section_context` is not reaching the model — re-check that `_apply_edits`
passes `context_limit=DOC_CONTEXT_CHAR_LIMIT` and that the second pass is
using `fresh_markdown`, not the pre-edit `markdown`.

## What this plan deliberately does not change

- **`MAX_EDITS_PER_RUN = 10`** still applies before ordering, so the
  classifier never sees more than ten comments. The cap still selects by
  creation order, which is arbitrary — out of scope here.
- **The Docs failure path stays silent.** When a write finds no target,
  `edit_section` correctly refuses to resolve the thread, but unlike the
  Sheets path it posts no `reply_without_resolving` explaining why. Worth
  fixing; not part of this change.
- **`annotations.resolve_values`' single-brace placeholders** — see "Out of
  scope" above. Different path, separate fix.
