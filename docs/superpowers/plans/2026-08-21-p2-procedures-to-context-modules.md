# P2 — Procedures Out of the System Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

---

## ⚠️ Before Task 1 — check the prior stage

**Stage 2 of 4.** Stage 1 is P1 (`docs/superpowers/plans/2026-08-20-p1-resolvable-context-modules.md`).

Read the P1 PR before starting — it may have changed things this plan assumes:

```bash
gh pr list --search "resolvable context modules" --state all
gh pr view <PR#> --json title,body,state,mergedAt
gh pr diff <PR#> --name-only
```

Check specifically:

| assumption | how to verify | if it changed |
|---|---|---|
| `KnowledgeModule.body` is `Optional[str]` with `source`/`source_ref` | `command grep -n "source_ref" shared/prompts/knowledge.py` | Task 4's dedup helper still works, but adjust any construction |
| `validate_module` still enforces a non-empty summary for `on_demand` | `command grep -n "on_demand" anansi_app/nicegui_app/pages/knowledge_modules.py` | Task 5 depends on it; if removed, add the check to the migration instead |
| `GDocProvider` exists at `shared/prompts/providers_gdoc.py` | `ls shared/prompts/providers_gdoc.py` | only affects the optional Task 9; skip it if absent |
| the `source` CHECK constraint accepts the six values | `SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'knowledge_modules_source_chk';` | migration 0017 was not applied — apply it first |

**If P1 has not merged, this plan can still run.** It writes `source='manual'`
modules, which the pre-P1 schema already accepts. Only Task 9 (live-linked
procedures) requires P1.

---

**Goal:** Move troubleshooting procedures out of `customer.system`'s Google Doc — where every customer conversation carries all of them — into individual on-demand context modules the model fetches when a symptom matches.

**Architecture:** A dry-run-by-default migration script reuses `ProcedureProvider._parse_procedures` to split the live prompt body into `Procedure` objects, generates symptom-first summaries via LLM for review, and writes one `knowledge_modules` row each. The procedures are then removed from the Google Doc by hand, after production has confirmed the on-demand path works.

**Tech Stack:** Python 3.12+, Supabase, Google Docs API, pytest, pre-commit.

**Spec:** `docs/superpowers/specs/2026-08-19-procedures-to-context-modules-design.md`

---

## Critical Context for the Implementer

### The procedures are not where the name suggests

`troubleshooting.procedures` is a 19-line bundled placeholder that says no
procedures have been configured. It is **not** where the procedures live.

They are inside **`customer.system`** — the customer system prompt — in the Google
Doc that overrides it. `ProcedureProvider.get_procedures()`
(`chat_orchestrator/orchestrator/services/procedure_provider.py:50`) calls
`PROMPTS.text("customer.system")` and parses `## Procedure N: Title` headers out of
the result.

The bundled `customer.system.prompt` is 23,789 chars and contains **zero**
`## Procedure` headers. Every procedure exists only in the live Doc.

### Measure before you build

```bash
python -c "from shared.prompts import PROMPTS; b = PROMPTS.text('customer.system'); print(len(b))"
python -c "from shared.prompts import PROMPTS; print(PROMPTS.resolve('customer.system')[1:])"
```

`MAX_CONTEXT_CHARS = 30000` (`instructions_provider.py:105`), and `_cap_context`
clips at line 193-199. **If the live body exceeds 30,000 chars, procedures are
already being truncated mid-document today** and the tail ones never reach the
model at all. Record the number — it is the before-figure for this project and it
may change the urgency.

### `ProcedureProvider` is not being deleted

Its consumer is the *ingestion* flow — per-chunk matching of support examples to
procedures in `embed_and_store.py:820-839`. That is unrelated to serving procedures
to a conversation. Task 10 repoints it at `knowledge_modules`; it does not remove it.

### Summary quality is the whole project

An `on_demand` module is chosen by the model from its `summary` alone. A `### Purpose`
written for a human ("This procedure covers what to do when commissioning does not
complete") is a much weaker retrieval signal than a symptom-first line ("Meter
commissioning fails or hangs — no completion callback, meter stays in pending").
The dry run exists so a human reviews every summary before it is written.

### `.gitignore` denies `tests/`

`git add -f` every new test file. Run `pre-commit run --all-files` before claiming
done — a plain `git add` on a new test file is a silent no-op.

---

## File Structure

- Create: `scripts/migrate_procedures_to_modules.py` — dry-run-by-default migration
- Create: `shared/tests/test_migrate_procedures_to_modules.py` — pure-function tests
- Modify: `chat_orchestrator/orchestrator/services/procedure_provider.py` — read from modules (Task 10)
- Modify: `chat_orchestrator/tests/test_procedure_provider.py`

---

# Phase 1 — The migration script

### Task 1: Slug derivation

**Files:**
- Create: `scripts/migrate_procedures_to_modules.py`
- Test: `shared/tests/test_migrate_procedures_to_modules.py`

- [ ] **Step 1: Write the failing test**

Create `shared/tests/test_migrate_procedures_to_modules.py`:

```python
"""Procedure -> context module migration: pure functions only."""

import pytest

from scripts.migrate_procedures_to_modules import (
    detect_slug_collisions,
    procedure_to_module,
    slug_for_title,
)


def test_slug_is_derived_from_the_title_not_the_number():
    """Numbering is editorial and changes; a slug is a stable address."""
    assert slug_for_title("Commissioning Failed Troubleshooting") == (
        "procedure-commissioning-failed-troubleshooting"
    )


def test_slug_lowercases_and_hyphenates():
    assert slug_for_title("Meter Comms Loss") == "procedure-meter-comms-loss"


def test_slug_strips_punctuation():
    assert slug_for_title("DCU won't connect (E-402)") == "procedure-dcu-wont-connect-e-402"


def test_slug_collapses_repeated_separators():
    assert slug_for_title("Battery  --  Low  SoC") == "procedure-battery-low-soc"


def test_slug_rejects_an_empty_title():
    with pytest.raises(ValueError, match="empty"):
        slug_for_title("   ")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_migrate_procedures_to_modules.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.migrate_procedures_to_modules'`

- [ ] **Step 3: Write the slug function**

Create `scripts/migrate_procedures_to_modules.py`:

```python
"""Split the procedures out of customer.system into context modules.

Dry run by default. Reuses ProcedureProvider._parse_procedures so the
migration and the ingestion flow can never drift on what counts as a
procedure.

Usage:
    python scripts/migrate_procedures_to_modules.py            # dry run
    python scripts/migrate_procedures_to_modules.py --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from typing import Any, Dict, List

SLUG_PREFIX = "procedure-"

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_APOSTROPHE = re.compile(r"['’]")


def slug_for_title(title: str) -> str:
    """A stable address for a procedure, derived from its title.

    Deliberately not derived from the procedure number: numbering in the
    Doc is editorial and will change, while prompt pins reference the slug.
    """
    cleaned = _APOSTROPHE.sub("", (title or "").strip().lower())
    body = _NON_SLUG.sub("-", cleaned).strip("-")
    if not body:
        raise ValueError("cannot derive a slug from an empty title")
    return f"{SLUG_PREFIX}{body}"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_migrate_procedures_to_modules.py -k slug -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_procedures_to_modules.py
git add -f shared/tests/test_migrate_procedures_to_modules.py
git commit -m "feat(procedures): derive stable module slugs from procedure titles"
```

---

### Task 2: Procedure to module mapping

**Files:**
- Modify: `scripts/migrate_procedures_to_modules.py`
- Test: `shared/tests/test_migrate_procedures_to_modules.py`

- [ ] **Step 1: Write the failing test**

```python
class _Procedure:
    def __init__(self, number, title, purpose, full_text):
        self.id = f"procedure_{number}"
        self.number = number
        self.title = title
        self.purpose = purpose
        self.full_text = full_text


def _proc(title="Commissioning Failed", purpose="Covers failed commissioning.",
          full_text="## Procedure 1: Commissioning Failed\n\nSteps..."):
    return _Procedure(1, title, purpose, full_text)


def test_module_carries_title_body_and_slug():
    module = procedure_to_module(_proc())
    assert module["slug"] == "procedure-commissioning-failed"
    assert module["title"] == "Commissioning Failed"
    assert module["body"] == "## Procedure 1: Commissioning Failed\n\nSteps..."


def test_module_is_on_demand_and_manual():
    module = procedure_to_module(_proc())
    assert module["mode"] == "on_demand"
    assert module["source"] == "manual"
    assert module["scope"] == "sector"


def test_purpose_becomes_the_summary_when_no_override_is_given():
    assert procedure_to_module(_proc())["summary"] == "Covers failed commissioning."


def test_a_generated_summary_overrides_the_purpose():
    module = procedure_to_module(_proc(), summary="Meter stays pending, no callback.")
    assert module["summary"] == "Meter stays pending, no callback."


def test_a_procedure_with_no_purpose_and_no_summary_is_rejected():
    """An on_demand module with a blank summary is invisible to the model."""
    with pytest.raises(ValueError, match="summary"):
        procedure_to_module(_proc(purpose=""))


def test_module_is_tagged_for_filtering():
    assert "procedure" in procedure_to_module(_proc())["tags"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_migrate_procedures_to_modules.py -k module -v`
Expected: FAIL with `ImportError: cannot import name 'procedure_to_module'`

- [ ] **Step 3: Write the mapping**

Append to `scripts/migrate_procedures_to_modules.py`:

```python
def procedure_to_module(procedure: Any, summary: str = "") -> Dict[str, Any]:
    """Map one parsed Procedure to a knowledge_modules row.

    `summary` overrides the procedure's ### Purpose text -- the migration
    generates a symptom-first line and a human reviews it, because an
    on_demand module is selected from its summary alone.
    """
    resolved = (summary or getattr(procedure, "purpose", "") or "").strip()
    if not resolved:
        raise ValueError(
            f"procedure '{procedure.title}' has no ### Purpose and no generated "
            f"summary; an on_demand module without a summary is invisible to the model"
        )
    return {
        "slug": slug_for_title(procedure.title),
        "title": procedure.title.strip(),
        "summary": resolved,
        "body": procedure.full_text.strip(),
        "tags": ["procedure", "troubleshooting"],
        "scope": "sector",
        "mode": "on_demand",
        "source": "manual",
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_migrate_procedures_to_modules.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_procedures_to_modules.py shared/tests/test_migrate_procedures_to_modules.py
git commit -m "feat(procedures): map a parsed procedure to a context module row"
```

---

### Task 3: Collision detection

**Files:**
- Modify: `scripts/migrate_procedures_to_modules.py`
- Test: `shared/tests/test_migrate_procedures_to_modules.py`

Two similar titles can produce the same slug, which would silently overwrite one
procedure with another.

- [ ] **Step 1: Write the failing test**

```python
def test_no_collisions_for_distinct_titles():
    modules = [
        {"slug": "procedure-a", "title": "A"},
        {"slug": "procedure-b", "title": "B"},
    ]
    assert detect_slug_collisions(modules, existing_slugs=set()) == []


def test_reports_two_procedures_sharing_a_slug():
    modules = [
        {"slug": "procedure-meter-comms", "title": "Meter Comms"},
        {"slug": "procedure-meter-comms", "title": "Meter  comms!"},
    ]
    collisions = detect_slug_collisions(modules, existing_slugs=set())
    assert len(collisions) == 1
    assert "procedure-meter-comms" in collisions[0]


def test_reports_a_slug_that_already_exists_in_the_database():
    modules = [{"slug": "procedure-meter-comms", "title": "Meter Comms"}]
    collisions = detect_slug_collisions(
        modules, existing_slugs={"procedure-meter-comms"}
    )
    assert len(collisions) == 1
    assert "already exists" in collisions[0]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_migrate_procedures_to_modules.py -k collision -v`
Expected: FAIL with `ImportError: cannot import name 'detect_slug_collisions'`

- [ ] **Step 3: Write the detector**

```python
def detect_slug_collisions(
    modules: List[Dict[str, Any]], existing_slugs: set
) -> List[str]:
    """Human-readable collision reports. Empty means safe to apply."""
    problems: List[str] = []
    counts = Counter(m["slug"] for m in modules)

    for slug, count in sorted(counts.items()):
        if count > 1:
            titles = ", ".join(repr(m["title"]) for m in modules if m["slug"] == slug)
            problems.append(f"{count} procedures share the slug '{slug}': {titles}")

    for slug in sorted(set(counts) & existing_slugs):
        problems.append(f"slug '{slug}' already exists in knowledge_modules")

    return problems
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_migrate_procedures_to_modules.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_procedures_to_modules.py shared/tests/test_migrate_procedures_to_modules.py
git commit -m "feat(procedures): refuse to migrate on slug collisions"
```

---

### Task 4: Symptom-first summary generation

**Files:**
- Modify: `scripts/migrate_procedures_to_modules.py`
- Create: `shared/prompts/library/procedure.module_summary.prompt`
- Test: `shared/tests/test_migrate_procedures_to_modules.py`

- [ ] **Step 1: Write the prompt file**

Create `shared/prompts/library/procedure.module_summary.prompt`:

```
---
id: procedure.module_summary
description: Writes a symptom-first one-line summary for a procedure being migrated to a context module.
owner: ops
component: shared
overridable: true
model: fast
output: text
sections: []
variables: [title, purpose, body]
access:
  view: [ops, eng]
  edit: [ops]
  publish: [ops]
---
Write one line describing when a support agent should open the procedure below.

PROCEDURE TITLE: {{title}}

STATED PURPOSE: {{purpose}}

PROCEDURE BODY (first part):
{{body}}

The line will be the only thing an AI assistant sees when deciding whether to
fetch this procedure, so lead with the observable symptom — what a technician or
customer would actually report — not with what the procedure does.

Good: "Meter commissioning fails or hangs — stays pending, no completion callback."
Bad: "This procedure covers what to do when commissioning does not complete."

Write under 25 words. Output the line and nothing else.
```

- [ ] **Step 2: Write the failing test**

```python
def test_summary_generation_prompt_renders():
    from shared.prompts import PromptLibrary

    library = PromptLibrary()
    text = library.text(
        "procedure.module_summary",
        title="Commissioning Failed",
        purpose="Covers failed commissioning.",
        body="1. Check the DCU link...",
    )
    assert "Commissioning Failed" in text
    assert "symptom" in text.lower()


def test_generated_summaries_are_capped_for_review():
    from scripts.migrate_procedures_to_modules import truncate_body_for_prompt

    assert len(truncate_body_for_prompt("x" * 10_000)) <= 4000
    assert truncate_body_for_prompt("short") == "short"
```

Note this test constructs a bare `PromptLibrary()` rather than importing the
`PROMPTS` singleton — per `CLAUDE.md`, the singleton picks up live DB and Google
Doc overrides whenever real credentials are in the environment.

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_migrate_procedures_to_modules.py -k "prompt or capped" -v`
Expected: FAIL with `ImportError: cannot import name 'truncate_body_for_prompt'`

- [ ] **Step 4: Add the helper**

```python
MAX_BODY_CHARS_FOR_PROMPT = 4000


def truncate_body_for_prompt(body: str) -> str:
    """Cap the body sent to the summariser -- the opening is what matters."""
    if len(body) <= MAX_BODY_CHARS_FOR_PROMPT:
        return body
    return body[:MAX_BODY_CHARS_FOR_PROMPT]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_migrate_procedures_to_modules.py -v`
Expected: PASS

- [ ] **Step 6: Check prompt parity**

Run: `python -m pytest chat_orchestrator/tests/test_prompt_parity.py -v`

If this fails on the new prompt, add an entry for `procedure.module_summary` to its
variables fixture with the three variables above. If it fails on *other* prompts,
check `chat_orchestrator/.env` for live credentials before suspecting the codebase
— per `CLAUDE.md` that is a known local-environment trap, not a real failure.

- [ ] **Step 7: Commit**

```bash
git add scripts/migrate_procedures_to_modules.py \
        shared/prompts/library/procedure.module_summary.prompt \
        shared/tests/test_migrate_procedures_to_modules.py \
        chat_orchestrator/tests/test_prompt_parity.py
git commit -m "feat(procedures): generate symptom-first summaries for migration"
```

---

### Task 5: The migration entry point

**Files:**
- Modify: `scripts/migrate_procedures_to_modules.py`

- [ ] **Step 1: Write `main`**

```python
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="use each procedure's ### Purpose verbatim instead of generating summaries",
    )
    args = parser.parse_args()

    from shared.config.db_credentials import chat_db_service_key, chat_db_url
    from shared.prompts import PROMPTS
    from supabase import create_client

    # Provenance first: an operator must know whether they are reading the
    # live Doc or a bundled placeholder before trusting anything below.
    body, source, version = PROMPTS.resolve("customer.system")
    print(f"customer.system resolved from {source.value} (version={version}), {len(body)} chars")

    from orchestrator.services.procedure_provider import ProcedureProvider

    procedures = ProcedureProvider()._parse_procedures(PROMPTS.text("customer.system"))
    if not procedures:
        print(
            "No '## Procedure N: Title' headers found. If you expected some, "
            "customer.system is resolving to the bundled file rather than the "
            "live Google Doc -- check the provenance line above.",
            file=sys.stderr,
        )
        return 1

    print(f"\nParsed {len(procedures)} procedure(s):\n")

    modules: List[Dict[str, Any]] = []
    for procedure in procedures:
        summary = ""
        if not args.no_llm:
            summary = _generate_summary(procedure)
        try:
            modules.append(procedure_to_module(procedure, summary=summary))
        except ValueError as e:
            print(f"  ! {e}", file=sys.stderr)
            return 1

    client = create_client(chat_db_url(), chat_db_service_key())
    existing = {
        row["slug"]
        for row in (client.table("knowledge_modules").select("slug").execute().data or [])
    }

    collisions = detect_slug_collisions(modules, existing)
    if collisions:
        print("\nRefusing to migrate:", file=sys.stderr)
        for problem in collisions:
            print(f"  ! {problem}", file=sys.stderr)
        return 1

    total_body = 0
    for module in modules:
        total_body += len(module["body"])
        print(f"  {module['slug']}")
        print(f"    title:   {module['title']}")
        print(f"    summary: {module['summary']}")
        print(f"    body:    {len(module['body'])} chars\n")

    print(f"Total procedure text: {total_body} chars")
    print(f"customer.system is currently {len(body)} chars (MAX_CONTEXT_CHARS = 30000)")

    if not args.apply:
        print(
            "\nDry run. Review every summary above -- it is the only thing the model "
            "sees when deciding to fetch a procedure. Re-run with --apply to write."
        )
        return 0

    client.table("knowledge_modules").insert(modules).execute()
    print(f"\nCreated {len(modules)} module(s), pinned to no prompts.")
    print(
        "Next: attach them to customer.system and staff.system in the Context page, "
        "confirm in production that get_knowledge_module is being called, and only "
        "then remove the procedures from the Google Doc."
    )
    return 0


def _generate_summary(procedure: Any) -> str:
    """One symptom-first line. Falls back to ### Purpose on any failure."""
    import asyncio

    from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway
    from shared.prompts import PROMPTS

    try:
        prompt = PROMPTS.text(
            "procedure.module_summary",
            title=procedure.title,
            purpose=procedure.purpose or "(none given)",
            body=truncate_body_for_prompt(procedure.full_text),
        )
        response = asyncio.run(
            get_default_generation_gateway().generate(
                [LLMMessage(role="user", content=prompt)], GenerationOptions()
            )
        )
        return (getattr(response, "text", "") or "").strip()
    except Exception as e:
        print(f"    (summary generation failed for '{procedure.title}': {e})", file=sys.stderr)
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Verify the LLM gateway API matches**

Run: `command grep -n "def generate\|class GenerationOptions\|class LLMMessage" shared/llm/*.py | head`

Adapt the call if the real signature differs. Do not add a second gateway.

- [ ] **Step 3: Run the dry run against production**

Run: `python scripts/migrate_procedures_to_modules.py`

Expected: the provenance line reads `gdoc` or `db`, followed by N procedures with
generated summaries. **Read every summary.** A summary that restates the title
rather than naming a symptom must be hand-corrected before `--apply` — edit it in
the Context page after migration, or re-run with `--no-llm` and fix the `### Purpose`
text in the Doc first.

- [ ] **Step 4: Commit**

```bash
git add scripts/migrate_procedures_to_modules.py
git commit -m "feat(procedures): migration entry point with dry-run review"
```

---

# Phase 2 — Cutover

### Task 6: Apply the migration

- [ ] **Step 1: Record the before-figure**

```bash
python -c "from shared.prompts import PROMPTS; print(len(PROMPTS.text('customer.system')))"
```

Write the number in the PR description. If it exceeds 30,000, note that procedures
were already being clipped by `_cap_context` before this change.

- [ ] **Step 2: Apply**

Run: `python scripts/migrate_procedures_to_modules.py --apply`
Expected: `Created N module(s), pinned to no prompts.`

- [ ] **Step 3: Verify in the Context page**

Open `/knowledge-modules`. Confirm N new `procedure-*` rows, each `on_demand`, each
with a summary. Fix any weak summary here.

Nothing has changed for the model yet — the modules are pinned to no prompts.

---

### Task 7: Attach and verify on live traffic

- [ ] **Step 1: Attach**

In the Context page, attach every `procedure-*` module to `customer.system` and
`staff.system`.

- [ ] **Step 2: Confirm the catalog renders**

```bash
doctl apps logs 525c885e-c7e4-4721-b654-b724c1de5553 anansi-bot --type run --tail 200 \
  | command grep -i "knowledge"
```

Expected: the rendered prompt now carries an `# Available Knowledge` block listing
the procedure slugs.

- [ ] **Step 3: Confirm the model actually fetches one**

Send a message describing a symptom one procedure covers. Then:

```bash
doctl apps logs 525c885e-c7e4-4721-b654-b724c1de5553 anansi-bot --type run --tail 200 \
  | command grep "get_knowledge_module"
```

Expected: a call with the matching slug.

**If the model does not call it, stop here.** Do not proceed to Task 8. The fix is
better summaries, or promoting the most-used procedures to `pinned` — not
reverting, and not editing the Doc. Both paths are live right now, so there is no
urgency and nothing is broken.

- [ ] **Step 4: Record the outcome in the PR**

Note which symptom was tested, which slug was fetched, and how many procedures
have been exercised. This is the evidence Task 8 depends on.

---

### Task 8: Remove the procedures from the Doc

**This is a manual operator step against a live production Google Doc that is the
customer system prompt. It is not automated, deliberately.**

- [ ] **Step 1: Confirm Task 7 passed**

Do not start otherwise.

- [ ] **Step 2: Back up**

In Google Docs: File → Version history → Name current version, e.g.
"Before procedure extraction 2026-08-21".

- [ ] **Step 3: Delete the procedures section**

Remove every `## Procedure N: ...` block. Replace with:

```
## Procedures

Troubleshooting procedures are now context modules, managed in the Anansi admin
app under Context. They are offered to the assistant as a catalog and fetched on
demand when a symptom matches, rather than being included in every conversation.

Do not re-add procedure text here.
```

- [ ] **Step 4: Invalidate the cache**

```bash
python -c "from shared.prompts import PROMPTS; PROMPTS.invalidate_doc_cache()"
```

Or wait out the TTL. Then re-measure:

```bash
python -c "from shared.prompts import PROMPTS; print(len(PROMPTS.text('customer.system')))"
```

Expected: reduced by roughly the total-procedure-text figure the dry run reported.

- [ ] **Step 5: Confirm procedures still reach the model**

Repeat Task 7 Step 3's symptom test. The procedure must still be fetched — now
from the module rather than from the system prompt.

- [ ] **Step 6: Record the after-figure in the PR**

---

# Phase 3 — Repoint the ingestion consumer

### Task 9: `ProcedureProvider` reads from `knowledge_modules`

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/procedure_provider.py`
- Test: `chat_orchestrator/tests/test_procedure_provider.py`

With the Doc no longer containing procedures, `get_procedures()` returns an empty
list and per-chunk procedure matching in `embed_and_store` silently stops working.

- [ ] **Step 1: Write the failing test**

Create or append to `chat_orchestrator/tests/test_procedure_provider.py`:

```python
"""ProcedureProvider reads procedures from context modules."""

from orchestrator.services.procedure_provider import Procedure, ProcedureProvider


class _Store:
    def __init__(self, modules):
        self._modules = modules

    def all_modules(self):
        return self._modules


def _module(slug, title, summary, body):
    from shared.prompts.knowledge import KnowledgeModule

    return KnowledgeModule(
        id=slug, slug=slug, title=title, summary=summary, body=body,
        tags=["procedure"], mode="on_demand",
    )


def test_reads_procedures_from_tagged_modules():
    store = _Store([
        _module("procedure-comms", "Comms Loss", "Meter offline.", "## Steps\n1. Check link"),
    ])
    procedures = ProcedureProvider(store=store).get_procedures()

    assert len(procedures) == 1
    assert procedures[0].title == "Comms Loss"
    assert "Check link" in procedures[0].full_text


def test_purpose_comes_from_the_module_summary():
    store = _Store([_module("procedure-comms", "Comms Loss", "Meter offline.", "body")])
    assert ProcedureProvider(store=store).get_procedures()[0].purpose == "Meter offline."


def test_ignores_modules_not_tagged_as_procedures():
    from shared.prompts.knowledge import KnowledgeModule

    store = _Store([
        KnowledgeModule(id="x", slug="hps-tiers", title="HPS", summary="s",
                        body="b", tags=["reference"]),
    ])
    assert ProcedureProvider(store=store).get_procedures() == []


def test_procedure_ids_are_stable_across_reordering():
    """The id must not be a positional index -- chunk_procedure_map persists it."""
    store = _Store([
        _module("procedure-b", "B", "s", "b"),
        _module("procedure-a", "A", "s", "a"),
    ])
    ids = {p.title: p.id for p in ProcedureProvider(store=store).get_procedures()}

    store2 = _Store([
        _module("procedure-a", "A", "s", "a"),
        _module("procedure-b", "B", "s", "b"),
    ])
    ids2 = {p.title: p.id for p in ProcedureProvider(store=store2).get_procedures()}

    assert ids == ids2


def test_a_failing_store_yields_no_procedures():
    class _Boom:
        def all_modules(self):
            raise RuntimeError("db down")

    assert ProcedureProvider(store=_Boom()).get_procedures() == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/test_procedure_provider.py -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'store'`

- [ ] **Step 3: Repoint the provider**

In `procedure_provider.py`, add a constructor and replace `get_procedures`:

```python
    PROCEDURE_TAG = "procedure"

    def __init__(self, store: Any = None) -> None:
        self._store = store

    def _knowledge_store(self):
        if self._store is None:
            from shared.prompts.knowledge import KnowledgeStore

            self._store = KnowledgeStore.from_env()
        return self._store

    def get_procedures(self, force_reload: bool = False) -> List[Procedure]:
        """Procedures, read from the context modules tagged 'procedure'.

        Previously parsed out of the customer.system prompt body; procedures
        moved to knowledge_modules so they stop riding in every request (see
        docs/superpowers/specs/2026-08-19-procedures-to-context-modules-design.md).
        The Procedure shape is unchanged -- embed_and_store's per-chunk
        matching consumes it as before.
        """
        store = self._knowledge_store()
        if force_reload and hasattr(store, "invalidate"):
            store.invalidate()

        try:
            modules = store.all_modules()
        except Exception as e:
            LOGGER.exception(f"Error fetching procedure modules: {e}")
            return []

        procedures = [
            Procedure(
                # Slug, not a positional index: chunk_procedure_map persists
                # these ids, so a reordered list must not remap them.
                id=module.slug,
                number=index,
                title=module.title,
                purpose=module.summary,
                full_text=module.body or "",
            )
            for index, module in enumerate(
                sorted(
                    (m for m in modules if self.PROCEDURE_TAG in (m.tags or [])),
                    key=lambda m: m.slug,
                ),
                start=1,
            )
        ]
        LOGGER.info(f"Loaded {len(procedures)} procedures from context modules")
        return procedures
```

Keep `_parse_procedures` in place — the migration script in Task 1-5 calls it, and
removing it would break re-running the migration.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/test_procedure_provider.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Check the ingestion consumer still works**

Run: `python -m pytest chat_orchestrator/tests/ -k "embed_and_store or ingestion" -q`
Expected: PASS. `Procedure.id` is now a slug rather than `procedure_N`; if a test
asserts the old format, update the assertion — the slug is the correct stable id.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/services/procedure_provider.py
git add -f chat_orchestrator/tests/test_procedure_provider.py
git commit -m "refactor(procedures): read procedures from context modules"
```

---

### Task 10: Final verification and PR

- [ ] **Step 1: Run every suite**

Run: `python -m pytest shared/tests/ chat_orchestrator/tests/ anansi_app/tests/ -q`
Expected: PASS

- [ ] **Step 2: Run pre-commit**

Run: `pre-commit run --all-files`
Expected: all hooks pass. `git add -f` any untracked test files it reports, then
re-run.

- [ ] **Step 3: Confirm the test files are tracked**

Run: `git show --stat HEAD~5..HEAD | command grep "tests/"`
Expected: `test_migrate_procedures_to_modules.py` and `test_procedure_provider.py`
both present.

- [ ] **Step 4: Open the PR**

```bash
git push -u origin feat/procedures-as-context-modules
gh pr create --title "feat(procedures): move procedures out of the system prompt" --body "$(cat <<'EOF'
Troubleshooting procedures lived inline in `customer.system`'s Google Doc override,
so every customer conversation carried all of them regardless of relevance. They
are now individual on-demand context modules the model fetches when a symptom
matches.

Stage 2 of 4 in the context-architecture programme.

Spec: `docs/superpowers/specs/2026-08-19-procedures-to-context-modules-design.md`
Plan: `docs/superpowers/plans/2026-08-21-p2-procedures-to-context-modules.md`

## Measured

- `customer.system` before: __ chars
- `customer.system` after: __ chars
- procedures migrated: __
- was it exceeding MAX_CONTEXT_CHARS (30000) before? __

## Verification performed

- Symptom tested: __
- Slug fetched via `get_knowledge_module`: __
- Confirmed still working after the Doc edit: __

## Notes for the reviewer

- `ProcedureProvider._parse_procedures` is reused by the migration, not
  reimplemented, so the two cannot drift.
- `Procedure.id` is now the module slug rather than `procedure_N`. It is persisted
  in `chunk_procedure_map`, so a positional index would remap on reordering.
- The Google Doc edit was manual and is recorded in its version history.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Handoff to P3

Record in the PR description:

- The before/after `customer.system` character counts
- Whether any procedure needed a hand-written summary, and why
- Whether `_parse_procedures` was kept (it should be)

## Optional follow-up — live-linked procedures

If authoring procedures in the admin UI proves worse than Google Docs, convert each
`procedure-*` module to `source='gdoc'` with `source_ref` set to a per-procedure
Doc, using P1's `GDocProvider`. Do this only against demonstrated authoring
friction: a copied module is diffable, editable by ops, and has no runtime
dependency on Drive availability. Live-linking is the mechanism that created the
original tangle.
