# Context (Knowledge Modules) Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make curated context — not vector RAG — the primary way staff teach the bot facts: migrate the 14 module-shaped RAG documents into `knowledge_modules`, replace tag-based prompt selection with an explicit searchable multiselect, rewire `/learn` to author context modules (keeping the RAG flow as `/learn_rag`), and rename the UI to "Context".

**Architecture:** Five sequenced phases. Phase 1 moves data (read from `chunks`, write to `knowledge_modules`, delete originals). Phase 2 replaces tag-intersection selection with `prompt_knowledge_overrides` as the sole mechanism and builds the multiselect UI. Phase 3 adds a `context_expert` workflow that reuses the existing `fetch_document` / `improve_content` step handlers and adds module-specific dedup + storage. Phase 4 rewires commands. Phase 5 renames the UI. Each phase ends green and is independently shippable.

**Tech Stack:** Python 3.12+, Supabase (postgres + pgvector), NiceGUI admin app, MCP servers, pytest, pre-commit (ruff + test-wiring).

---

## Critical Context for the Implementer

Read this before Task 1. These are non-obvious facts verified against the live database and codebase on 2026-08-05.

### The data being migrated

The live chat DB holds **21 documents / 1,174 chunks / 225 entities**. Only 14 documents move:

- **Move (14):** every document with `metadata.doc_type == 'technical'` **except** `CET-rules.pdf`. All are `source_type='manual_input'`, `audience='staff'`, 134–3,642 chars each, **19,583 chars total**.
- **Leave behind (7):** `CET-rules.pdf` (a real 1,131-chunk regulatory PDF — genuine RAG material) and the 6 `doc_type='support_example'` documents (5 `NXT-3555-Procedure_*` + 1 customer complaint; these feed customer-mode retrieval where relevance is query-dependent).

### `raw_content` is NOT truncated by a bug — do not "fix" it

`documents.raw_content` holds a deliberate 500-char preview (`embed_and_store.py:335-337`, `content[:500] + "..."` → 503 chars). Full text lives in `chunks`. This was investigated and is by design.

**Consequence for migration: read the body by joining `chunks.content` ordered by `chunk_index` with `"\n\n"`.** Do not read `raw_content` (truncated) and do not trust `documents.content` (empty string on 10 of the 14 — that column was added after those rows were ingested). Verified: joined-chunk length matches `metadata.content_length` exactly for all 14.

### Everything is `on_demand`

`PINNED_BUDGET_CHARS = 20000` (`shared/prompts/knowledge.py:20`). The 14 modules total 19,583 chars — pinning them all would consume the entire budget and make `budget_pinned` silently drop modules. **All 14 migrate with `mode='on_demand'`**, contributing one catalog line each. An operator can promote individuals to `pinned` later in the UI.

Because `on_demand` modules are chosen by the model from their `summary` alone, summary quality is load-bearing. The migration generates summaries via LLM and prints them in a dry run for review before writing.

### `knowledge_modules` is currently empty

Zero rows. No migration/rollback conflict risk; the table is greenfield.

### Where the ingestion workflow actually lives

Workflow step sequences are **not in code**. They live in the `experts.definitions` prompt, resolved DB override → `EXPERT_INSTRUCTIONS_DOC_ID` Google Doc → bundled `shared/prompts/library/experts.definitions.prompt` (see `ExpertInstructionsProvider` docstring). The bundled `ingestion_expert` definition is at lines 278–358.

**Deployment risk — check this before Phase 3 ships:** if production has a live DB override or Google Doc override of `experts.definitions`, editing the bundled file has **no effect**. Verify with:

```bash
python -c "from shared.prompts import PROMPTS; print(PROMPTS.resolve('experts.definitions')[1:])"
```

If that prints `PromptSource.DB` or `PromptSource.GDOC`, the new expert definition must also be applied to that override (via the Prompts admin page or the Google Doc) — not just the bundled file. Task 18 covers this.

### Test files under `tests/` need `git add -f`

Per `CLAUDE.md`: `.gitignore` denies `tests/` by default. A plain `git add` on a new test file is a **silent no-op** — the commit succeeds, the file never reaches the remote, CI never runs it. Every commit step below that adds a new test file uses `git add -f` explicitly. Before declaring any phase done, run `pre-commit run --all-files` (not just `pytest`) and confirm the `test-wiring` hook is clean.

`docs/superpowers/plans/` is also gitignored — committing this plan needs `git add -f` too.

### Dead code you will be replacing

`build_knowledge_tab` (`anansi_app/nicegui_app/pages/prompts.py:58`) is defined and unit-tested but **never rendered**. The prompt detail dialog (`_open_detail_dialog`, line 206) has only Edit / Diff / History tabs. Phase 2 rewrites the function and builds the missing UI.

---

## File Structure

**Phase 1 — data migration**
- Create: `scripts/migrate_rag_docs_to_modules.py` — one-shot migration, dry-run by default
- Create: `shared/tests/test_migrate_rag_docs_to_modules.py` — pure-function tests (slug/title/body assembly)

**Phase 2 — selection mechanism**
- Modify: `shared/prompts/knowledge.py` — drop `select_modules`/`apply_overrides`, add `select_for_prompt`; add `KnowledgeStore.set_prompt_modules`
- Modify: `shared/prompts/core.py:118-141` — `_compose_knowledge` uses pins only
- Modify: `chat_orchestrator/orchestrator/services/instructions_provider.py:302-370` — thread `organization_id` into `RequestScope` so org-scoped modules actually render
- Modify: `shared/prompts/spec.py:46,99` — remove `knowledge_tags`
- Modify: 6 files in `shared/prompts/library/*.prompt` — remove `knowledge_tags:` frontmatter
- Modify: `anansi_app/nicegui_app/pages/prompts.py` — rewrite `build_knowledge_tab`, add Knowledge tab UI with searchable multiselect
- Modify: `shared/tests/test_prompt_knowledge.py`, `shared/tests/test_prompt_knowledge_wiring.py`, `shared/tests/test_prompt_spec.py`, `anansi_app/tests/test_knowledge_modules_page.py`, `chat_orchestrator/tests/test_instructions_provider_library.py`

**Phase 3 — /learn context flow**
- Create: `chat_orchestrator/orchestrator/experts/handlers/context_expert/__init__.py`
- Create: `.../context_expert/propose_module.py` — LLM drafts slug/title/summary from body
- Create: `.../context_expert/detect_module_duplicates.py` — slug/hash/title dedup vs `knowledge_modules`
- Create: `.../context_expert/select_prompts.py` — ask which prompts pin this module
- Create: `.../context_expert/prepare_module_approval.py` — approval summary
- Create: `.../context_expert/store_module.py` — write module + prompt pins
- Create: `chat_orchestrator/tests/test_context_expert.py`
- Modify: `shared/prompts/library/experts.definitions.prompt` — add `context_expert`, rename ingestion packet

**Phase 4 — commands**
- Modify: `chat_orchestrator/orchestrator/services/command_registry.py:849-866`
- Modify: `chat_orchestrator/orchestrator/experts/workflow_executor.py:1652,1969`

**Phase 5 — UI rename**
- Modify: `anansi_app/nicegui_app/layout.py:30`
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py` — headings/copy
- Modify: `anansi_app/nicegui_app/pages/documents.py:92` — empty-state text

---

# Phase 1 — Migrate the 14 documents

### Task 1: Migration script — pure helpers, with tests

**Files:**
- Create: `scripts/migrate_rag_docs_to_modules.py`
- Test: `shared/tests/test_migrate_rag_docs_to_modules.py`

The 14 documents have auto-generated titles that embed body text and an uploader name (e.g. `Technical: Azimuth Calculation Azimuth is defined as the angle... by Vaibhav Vaidya`). Rather than a fragile regex, the script carries an explicit curated mapping — there are only 14, and every one was reviewed.

- [ ] **Step 1: Write the failing tests**

```python
# shared/tests/test_migrate_rag_docs_to_modules.py
"""Pure helpers for the RAG-document -> knowledge_modules migration."""

import pytest

from scripts.migrate_rag_docs_to_modules import (
    CURATED,
    assemble_body,
    build_module_row,
    is_migration_candidate,
)


def test_candidate_selects_technical_but_not_cet_rules():
    assert is_migration_candidate(
        {"title": "Guidelines for Sizing PV to MPPT cables", "metadata": {"doc_type": "technical"}}
    )
    assert not is_migration_candidate(
        {"title": "CET-rules.pdf", "metadata": {"doc_type": "technical"}}
    )
    assert not is_migration_candidate(
        {"title": "NXT-3555-Procedure_1", "metadata": {"doc_type": "support_example"}}
    )


def test_assemble_body_joins_chunks_in_index_order():
    chunks = [
        {"chunk_index": 1, "content": "second"},
        {"chunk_index": 0, "content": "first"},
    ]
    assert assemble_body(chunks) == "first\n\nsecond"


def test_assemble_body_rejects_empty_chunks():
    with pytest.raises(ValueError, match="no chunks"):
        assemble_body([])


def test_curated_covers_exactly_fourteen_documents():
    assert len(CURATED) == 14
    assert len({entry["slug"] for entry in CURATED.values()}) == 14


def test_build_module_row_is_on_demand_and_traceable():
    doc = {
        "id": "doc-uuid-1",
        "title": "Technical: Azimuth Calculation Azimuth is defined as the angle... by Vaibhav Vaidya",
        "metadata": {"doc_type": "technical"},
    }
    row = build_module_row(doc, body="### Azimuth\n\nAngle from true north.", summary="How azimuth is measured.")
    assert row["slug"] == "azimuth-calculation"
    assert row["title"] == "Azimuth Calculation"
    assert row["mode"] == "on_demand"
    assert row["scope"] == "sector"
    assert row["tags"] == []
    assert row["source"] == "ingested"
    assert row["source_ref"] == "doc-uuid-1"
    assert row["body"] == "### Azimuth\n\nAngle from true north."
    assert row["summary"] == "How azimuth is measured."


def test_build_module_row_rejects_unknown_document():
    with pytest.raises(KeyError):
        build_module_row({"id": "x", "title": "Not In Curated List"}, body="b", summary="s")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest shared/tests/test_migrate_rag_docs_to_modules.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.migrate_rag_docs_to_modules'`

- [ ] **Step 3: Write the script**

```python
# scripts/migrate_rag_docs_to_modules.py
"""One-shot migration: module-shaped RAG documents -> knowledge_modules.

Reads the 14 curated `doc_type='technical'` documents (excluding CET-rules.pdf),
reassembles each body from its chunks, drafts a summary with the LLM, and
writes `knowledge_modules` rows with mode='on_demand'.

Bodies come from `chunks` joined on chunk_index -- NOT from `documents.raw_content`,
which stores a deliberate 500-char preview, nor `documents.content`, which is an
empty string on rows ingested before that column existed.

All 14 land as on_demand: together they are 19,583 chars against a 20,000-char
pinned budget (shared/prompts/knowledge.py PINNED_BUDGET_CHARS), so pinning them
would starve the budget and silently drop modules.

Usage:
    python -m scripts.migrate_rag_docs_to_modules              # dry run, prints everything
    python -m scripts.migrate_rag_docs_to_modules --write      # insert into knowledge_modules
    python -m scripts.migrate_rag_docs_to_modules --delete-source  # after verifying, drop the 14 docs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, Dict, List

EXCLUDED_TITLES = {"CET-rules.pdf"}

# Explicit slug/title per source document. Auto-generated titles embed body text
# and an uploader suffix ("Technical: X <body excerpt>... by <Name>"), so a regex
# would be guesswork -- all 14 were reviewed by hand instead.
CURATED: Dict[str, Dict[str, str]] = {
    "NXT Grid Power Plant Smoke Detector Battery": {
        "slug": "smoke-detector-battery",
        "title": "Smoke Detector Battery Specification",
    },
    "Technical: Pre-installation Voltage Matching for BYD/Pylontech Battery Modules Before... by Vaibhav Vaidya": {
        "slug": "battery-module-voltage-matching",
        "title": "Pre-installation Voltage Matching for BYD/Pylontech Battery Modules",
    },
    "Technical: Fuses vs. Breakers on the DC Side For... by Vaibhav Vaidya": {
        "slug": "dc-side-fuses-vs-breakers",
        "title": "Fuses vs. Breakers on the DC Side",
    },
    "Technical: High Current Wiring Requirements Requirement Brazing is mandatory... by Vaibhav Vaidya": {
        "slug": "high-current-wiring-requirements",
        "title": "High Current Wiring Requirements",
    },
    "Technical: Calin Meter Token Validity Top-Up or other tokens... by Vaibhav Vaidya": {
        "slug": "calin-meter-token-validity",
        "title": "Calin Meter Token Validity",
    },
    "Technical: Solcast Irradiance Data Evaluation Overview We utilize Solcast... by Vaibhav Vaidya": {
        "slug": "solcast-irradiance-evaluation",
        "title": "Solcast Irradiance Data Evaluation",
    },
    "Technical: Victron Quattro 15kVA Inverter Operating Power Limits Summary... by Vaibhav Vaidya": {
        "slug": "victron-quattro-15kva-power-limits",
        "title": "Victron Quattro 15kVA Inverter Operating Power Limits",
    },
    "Technical: Azimuth Calculation Azimuth is defined as the angle... by Vaibhav Vaidya": {
        "slug": "azimuth-calculation",
        "title": "Azimuth Calculation",
    },
    "Guidelines for Sizing PV to MPPT cables": {
        "slug": "pv-to-mppt-cable-sizing",
        "title": "Guidelines for Sizing PV to MPPT Cables",
    },
    "IEC Recommendations for trench depth and demarcation": {
        "slug": "iec-trench-depth-demarcation",
        "title": "IEC Recommendations for Trench Depth and Demarcation",
    },
    "Decoding Victron Inverter Quattro LED error codes": {
        "slug": "victron-quattro-led-codes",
        "title": "Decoding Victron Quattro Inverter LED Error Codes",
    },
    "Decoding Pylontech Battery LED error codes": {
        "slug": "pylontech-led-codes",
        "title": "Decoding Pylontech Battery LED Error Codes",
    },
    "Decoding BYD Battery BMS and BMU LED error codes": {
        "slug": "byd-bms-bmu-led-codes",
        "title": "Decoding BYD Battery BMS and BMU LED Error Codes",
    },
    "BYD LV Flex Module large scale failure event debug flow": {
        "slug": "byd-lv-flex-failure-debug-flow",
        "title": "BYD LV Flex Module Large-Scale Failure Debug Flow",
    },
}


def is_migration_candidate(doc: Dict[str, Any]) -> bool:
    """Technical documents only, minus the genuine RAG corpus (CET-rules.pdf)."""
    doc_type = (doc.get("metadata") or {}).get("doc_type")
    return doc_type == "technical" and doc.get("title") not in EXCLUDED_TITLES


def assemble_body(chunks: List[Dict[str, Any]]) -> str:
    """Full document text, rebuilt from its chunks in index order."""
    if not chunks:
        raise ValueError("cannot assemble a body from no chunks")
    ordered = sorted(chunks, key=lambda c: c["chunk_index"])
    return "\n\n".join(c["content"] for c in ordered)


def build_module_row(doc: Dict[str, Any], body: str, summary: str) -> Dict[str, Any]:
    """A knowledge_modules row for one migrated document."""
    curated = CURATED[doc["title"]]
    return {
        "slug": curated["slug"],
        "title": curated["title"],
        "summary": summary,
        "body": body,
        "tags": [],
        "scope": "sector",
        "mode": "on_demand",
        "source": "ingested",
        "source_ref": doc["id"],
        "updated_by": "migration:rag-docs-to-modules",
    }


SUMMARY_PROMPT = (
    "Write a single sentence, at most 20 words, describing what this technical "
    "reference covers. It is shown to an AI assistant as the only basis for "
    "deciding whether to fetch the full document, so name the specific equipment, "
    "standard or calculation involved. Reply with the sentence only.\n\n"
    "Title: {title}\n\nContent:\n{body}"
)


async def draft_summary(title: str, body: str) -> str:
    """LLM-drafted catalog line; falls back to the first sentence."""
    try:
        from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway

        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        gateway = get_default_generation_gateway(default_model=model)
        response = await gateway.generate(
            [LLMMessage(role="user", text=SUMMARY_PROMPT.format(title=title, body=body[:4000]))],
            GenerationOptions(model=model, temperature=0.2, max_output_tokens=100),
        )
        text = (response.text or "").strip()
        if text:
            return text
    except Exception as e:  # noqa: BLE001 -- a summary is not worth failing the migration over
        print(f"  ! summary generation failed for {title!r}: {e}")

    first = body.strip().split("\n\n")[0].strip().lstrip("#").strip()
    sentence = first.split(". ")[0].strip()
    return sentence if sentence.endswith(".") else f"{sentence}."


def _client():
    from dotenv import load_dotenv

    load_dotenv("chat_orchestrator/.env")
    from supabase import create_client

    url = os.getenv("CHAT_DB_URL")
    key = os.getenv("CHAT_DB_SERVICE_KEY")
    if not (url and key):
        raise SystemExit("CHAT_DB_URL / CHAT_DB_SERVICE_KEY not set")
    return create_client(url, key)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="insert rows into knowledge_modules")
    parser.add_argument(
        "--delete-source",
        action="store_true",
        help="delete the migrated documents from the RAG tables (run only after verifying --write)",
    )
    args = parser.parse_args()

    client = _client()
    docs = (
        client.table("documents")
        .select("id, title, metadata")
        .order("ingested_at", desc=True)
        .execute()
        .data
        or []
    )
    candidates = [d for d in docs if is_migration_candidate(d)]
    print(f"Found {len(candidates)} migration candidates (expected 14)")
    if len(candidates) != 14:
        raise SystemExit(
            f"Expected exactly 14 candidates, found {len(candidates)}. "
            "Refusing to proceed -- reconcile CURATED against the database first."
        )

    rows = []
    for doc in candidates:
        chunks = (
            client.table("chunks")
            .select("chunk_index, content")
            .eq("document_id", doc["id"])
            .order("chunk_index")
            .execute()
            .data
            or []
        )
        body = assemble_body(chunks)
        summary = await draft_summary(CURATED[doc["title"]]["title"], body)
        row = build_module_row(doc, body=body, summary=summary)
        rows.append(row)
        print(f"\n--- {row['slug']} ({len(body)} chars, {len(chunks)} chunks) ---")
        print(f"  title:   {row['title']}")
        print(f"  summary: {row['summary']}")

    total = sum(len(r["body"]) for r in rows)
    print(f"\nTotal body chars: {total} (pinned budget is 20000; all rows are on_demand)")

    if args.delete_source:
        ids = [r["source_ref"] for r in rows]
        for doc_id in ids:
            client.table("documents").delete().eq("id", doc_id).execute()
        print(f"\nDeleted {len(ids)} source documents (chunks/entity_mentions cascade)")
        return

    if not args.write:
        print("\nDry run. Re-run with --write to insert.")
        with open("migration_preview.json", "w") as f:
            json.dump(rows, f, indent=2)
        print("Preview written to migration_preview.json")
        return

    client.table("knowledge_modules").insert(rows).execute()
    print(f"\nInserted {len(rows)} knowledge_modules rows")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest shared/tests/test_migrate_rag_docs_to_modules.py -v`
Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_rag_docs_to_modules.py && git add -f shared/tests/test_migrate_rag_docs_to_modules.py && git commit -m "feat: add RAG-document to knowledge-module migration script"
```

---

### Task 2: Dry-run and review the migration

**Files:** none modified — this is a verification gate.

- [ ] **Step 1: Run the dry run**

Run: `.venv/bin/python -m scripts.migrate_rag_docs_to_modules`
Expected: `Found 14 migration candidates (expected 14)`, then 14 slug/title/summary blocks, then `Total body chars: 19583`.

If the candidate count is not 14, **stop** — the database has drifted from this plan. Reconcile `CURATED` before continuing.

- [ ] **Step 2: Review every generated summary**

Open `migration_preview.json`. For each of the 14, confirm the summary names the specific equipment/standard and would let a model decide whether to fetch the body. Edit any weak summary directly in `CURATED` by adding an explicit `"summary"` key and having `build_module_row` prefer it — or just fix it in the Context UI after the write. Summaries are the only thing the model sees for `on_demand` modules.

- [ ] **Step 3: Confirm nothing else moved**

Run: `.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('chat_orchestrator/.env')
import os; from supabase import create_client
c = create_client(os.getenv('CHAT_DB_URL'), os.getenv('CHAT_DB_SERVICE_KEY'))
docs = c.table('documents').select('title, metadata').execute().data
kept = [d['title'] for d in docs if (d.get('metadata') or {}).get('doc_type') != 'technical' or d['title'] == 'CET-rules.pdf']
print(f'{len(kept)} documents stay in RAG:'); [print(' -', t) for t in kept]
"`
Expected: 7 documents — `CET-rules.pdf` plus the 6 `support_example` rows.

---

### Task 3: Execute the migration

**Files:** none — database mutation only.

- [ ] **Step 1: Write the modules**

Run: `.venv/bin/python -m scripts.migrate_rag_docs_to_modules --write`
Expected: `Inserted 14 knowledge_modules rows`

- [ ] **Step 2: Verify in the database**

Run: `.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('chat_orchestrator/.env')
import os; from supabase import create_client
c = create_client(os.getenv('CHAT_DB_URL'), os.getenv('CHAT_DB_SERVICE_KEY'))
rows = c.table('knowledge_modules').select('slug, mode, source, source_ref').execute().data
print(f'{len(rows)} modules'); assert len(rows) == 14
assert all(r['mode'] == 'on_demand' for r in rows), 'expected all on_demand'
assert all(r['source'] == 'ingested' for r in rows)
assert all(r['source_ref'] for r in rows), 'every module must trace to its source document'
print('OK')
"`
Expected: `14 modules` then `OK`

- [ ] **Step 3: Verify the Context page renders them**

Open the admin app, go to `/knowledge-modules`. Expect an "On-demand · 14" section listing all 14 with their char counts. Spot-check one body against the original document.

- [ ] **Step 4: Delete the source documents**

Only after Steps 2 and 3 pass.

Run: `.venv/bin/python -m scripts.migrate_rag_docs_to_modules --delete-source`
Expected: `Deleted 14 source documents (chunks/entity_mentions cascade)`

- [ ] **Step 5: Verify RAG now holds only the 7 keepers**

Run: `.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('chat_orchestrator/.env')
import os; from supabase import create_client
c = create_client(os.getenv('CHAT_DB_URL'), os.getenv('CHAT_DB_SERVICE_KEY'))
d = c.table('documents').select('id', count='exact').execute()
ch = c.table('chunks').select('id', count='exact').execute()
print(f'documents={d.count} chunks={ch.count}')
assert d.count == 7, f'expected 7 documents, got {d.count}'
print('OK')
"`
Expected: `documents=7 chunks=1146` then `OK` (1,174 minus the 28 chunks belonging to the migrated 14)

---

# Phase 2 — Replace tag selection with explicit prompt pins

### Task 4: `select_for_prompt` replaces tag intersection

**Files:**
- Modify: `shared/prompts/knowledge.py:39-65`
- Test: `shared/tests/test_prompt_knowledge.py`

`select_modules` (tag intersection) and `apply_overrides` (force on/off on top of tags) collapse into one function: a module is selected iff an override row pins it, and its scope matches the request.

- [ ] **Step 1: Replace the selection tests**

Delete `test_selects_modules_sharing_a_tag`, `test_no_tags_selects_nothing`, `test_override_can_force_a_module_on`, and any other test importing `select_modules` or `apply_overrides` from `shared/tests/test_prompt_knowledge.py`. Update the imports at the top of that file to drop both names and add `select_for_prompt`. Then add:

```python
def test_selects_only_pinned_modules():
    modules = [_module("comms"), _module("billing")]
    picked = select_for_prompt(modules, {"comms": True})
    assert [m.slug for m in picked] == ["comms"]


def test_unpinned_override_excludes():
    modules = [_module("comms")]
    assert select_for_prompt(modules, {"comms": False}) == []


def test_no_pins_selects_nothing():
    assert select_for_prompt([_module("a")], {}) == []


def test_scope_still_gates_a_pinned_module():
    modules = [_module("abc", scope="site:ABC")]
    assert select_for_prompt(modules, {"abc": True}, RequestScope(grid="ABC"))
    assert select_for_prompt(modules, {"abc": True}, RequestScope(grid="XYZ")) == []


def test_sector_scope_applies_everywhere():
    modules = [_module("a", scope="sector")]
    assert len(select_for_prompt(modules, {"a": True}, RequestScope(grid="XYZ"))) == 1


def test_selection_is_slug_sorted():
    modules = [_module("zulu"), _module("alpha")]
    picked = select_for_prompt(modules, {"zulu": True, "alpha": True})
    assert [m.slug for m in picked] == ["alpha", "zulu"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest shared/tests/test_prompt_knowledge.py -v`
Expected: FAIL — `ImportError: cannot import name 'select_for_prompt'`

- [ ] **Step 3: Implement**

In `shared/prompts/knowledge.py`, delete `select_modules` (lines 39-46) and `apply_overrides` (lines 54-65), and add in their place:

```python
def select_for_prompt(
    modules: List[KnowledgeModule],
    pins: Dict[str, bool],
    scope: Optional[RequestScope] = None,
) -> List[KnowledgeModule]:
    """Modules this prompt pins, that the request's scope admits.

    Selection is explicit: an operator picks modules per prompt in the admin
    UI and that choice is stored in ``prompt_knowledge_overrides``. Scope is a
    separate, per-request gate -- a ``site:ABC`` module stays out of a
    conversation about another site even when the prompt pins it.
    """
    scope = scope or RequestScope()
    return sorted(
        (m for m in modules if pins.get(m.slug) and scope.matches(m.scope)),
        key=lambda m: m.slug,
    )
```

Update the module docstring's first paragraph to say selection is by explicit per-prompt pin rather than tag.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest shared/tests/test_prompt_knowledge.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/knowledge.py shared/tests/test_prompt_knowledge.py && git commit -m "refactor: select knowledge modules by explicit prompt pin, not tag"
```

---

### Task 5: `_compose_knowledge` uses pins

**Files:**
- Modify: `shared/prompts/core.py:16-20, 118-141`
- Test: `shared/tests/test_prompt_knowledge_wiring.py`

- [ ] **Step 1: Write the failing test**

Append to `shared/tests/test_prompt_knowledge_wiring.py`:

```python
def test_compose_uses_pins_not_tags(monkeypatch):
    """A pinned module renders even though the prompt declares no tags."""
    from shared.prompts.core import PromptLibrary
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(
        id="m1", slug="comms", title="Comms", summary="About comms.",
        body="Radio checks hourly.", tags=[], scope="sector", mode="pinned",
    )

    class _Store:
        def all_modules(self):
            return [module]

        def overrides_for(self, prompt_id):
            return {"comms": True}

    library = PromptLibrary(knowledge=_Store())
    spec = library.spec("staff.system")
    text, used = library._compose_knowledge(spec, RequestScope())
    assert "Radio checks hourly." in text
    assert used == ["comms"]
```

Add `from shared.prompts.types import RequestScope` to that file's imports if absent.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest shared/tests/test_prompt_knowledge_wiring.py::test_compose_uses_pins_not_tags -v`
Expected: FAIL — `ImportError` on `select_modules` (core.py still imports the deleted names)

- [ ] **Step 3: Implement**

In `shared/prompts/core.py`, change the import block at lines 16-20 to:

```python
from shared.prompts.knowledge import (
    budget_pinned,
    render_catalog,
    render_pinned,
    select_for_prompt,
)
```

Replace `_compose_knowledge`'s body (lines 118-141) with:

```python
    def _compose_knowledge(
        self, spec: PromptSpec, scope: RequestScope
    ) -> Tuple[Optional[str], List[str]]:
        """Resolve, budget and render this prompt's knowledge. Never raises."""
        if self._knowledge is None:
            return None, []
        try:
            modules = self._knowledge.all_modules()
            pins = self._knowledge.overrides_for(spec.id)
        except Exception:
            LOGGER.warning(
                f"Knowledge lookup failed for '{spec.id}'; rendering without it", exc_info=True
            )
            return None, []

        chosen = select_for_prompt(modules, pins, scope)
        pinned, _dropped = budget_pinned([m for m in chosen if m.mode == "pinned"])
        on_demand = [m for m in chosen if m.mode == "on_demand"]

        blocks = [b for b in (render_pinned(pinned), render_catalog(on_demand)) if b]
        used = [m.slug for m in pinned] + [m.slug for m in on_demand]
        return ("\n\n".join(blocks) or None), used
```

Note the `not spec.knowledge_tags` early-return is gone — a prompt with no pins now returns empty via `select_for_prompt` instead.

- [ ] **Step 4: Run the prompt suite**

Run: `.venv/bin/python -m pytest shared/tests/ -k prompt -v`
Expected: PASS (any failures here are Task 6's `knowledge_tags` references — note them and continue)

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/core.py shared/tests/test_prompt_knowledge_wiring.py && git commit -m "refactor: compose prompt knowledge from pins instead of tags"
```

---

### Task 6: Thread `RequestScope` into the staff/customer render path

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/instructions_provider.py:302-370`
- Test: `chat_orchestrator/tests/test_instructions_provider_library.py`

`RequestScope` is only ever constructed by its own default (`shared/prompts/core.py:168`) — every production caller renders with no scope, so `RequestScope().matches("site:ABC")` is `False` everywhere and any `site:`/`org:`-scoped module silently never renders. This task fixes the `organization_id` half, which is real and available today: `UserContext.organization_ids` is already resolved by auth before `get_instructions` runs (see the identical pattern at `instructions_provider.py:286`, `int(permissions.organization_ids[0])`).

The `grid`/`site:<name>` half is **not** fixed here, and the task does not pretend to. `RequestScope.grid` and `knowledge_modules.scope`'s `site:<name>` format are both name-keyed (`RequestScope.matches` lowercases and compares `scope[5:]` against a name). The only grid data reaching this layer is `EntityContext.grid_id` — a numeric id (`db/schema/auth_db.sql:136`, `grid_id integer`) — and `EntityContext` itself is constructed only from raw MCP-server input (`chat_orchestrator_mcp_server.py:248,292`), never populated in the primary Telegram chat path. There is no id-to-name resolver in this call path today (grep confirms every grid-name helper on `auth_service.py` takes a name, not an id). Wiring site scoping needs that resolver built first — a separate, larger change than this task, and out of scope here since none of the 14 migrated modules use `site:` scope.

- [ ] **Step 1: Write the failing test**

Append to `chat_orchestrator/tests/test_instructions_provider_library.py`:

```python
@pytest.mark.asyncio
async def test_customer_instructions_pass_organization_id_as_scope(monkeypatch):
    from shared.prompts.types import RequestScope

    captured = {}

    def capture_render(prompt_id, vars=None, scope=None):
        captured["prompt_id"] = prompt_id
        captured["scope"] = scope

        class _Rendered:
            system_text = "system"
            context_text = None

            def provenance(self):
                return "customer.system@bundled:default:abc12345"

        return _Rendered()

    monkeypatch.setattr(ip.PROMPTS, "render", capture_render)

    provider = ip.InstructionsProvider()
    await provider.get_customer_instructions(organization_id="42")

    assert captured["prompt_id"] == "customer.system"
    assert captured["scope"] == RequestScope(organization_id="42")


@pytest.mark.asyncio
async def test_staff_instructions_pass_organization_id_as_scope(monkeypatch):
    from shared.prompts.types import RequestScope

    captured = {}

    def capture_render(prompt_id, vars=None, scope=None):
        captured["scope"] = scope

        class _Rendered:
            system_text = "system"
            context_text = None

            def provenance(self):
                return "staff.system@bundled:default:abc12345"

        return _Rendered()

    monkeypatch.setattr(ip.PROMPTS, "render", capture_render)

    provider = ip.InstructionsProvider()
    await provider._get_staff_instructions_from_doc(organization_id="7")

    assert captured["scope"] == RequestScope(organization_id="7")


@pytest.mark.asyncio
async def test_get_instructions_derives_scope_from_user_context(monkeypatch):
    from orchestrator.models.schemas import UserContext
    from shared.prompts.types import RequestScope

    captured = {}

    def capture_render(prompt_id, vars=None, scope=None):
        captured["scope"] = scope

        class _Rendered:
            system_text = "system"
            context_text = None

            def provenance(self):
                return f"{prompt_id}@bundled:default:abc12345"

        return _Rendered()

    monkeypatch.setattr(ip.PROMPTS, "render", capture_render)

    provider = ip.InstructionsProvider()
    user_context = UserContext(
        user_id="u1", user_email="staff@example.com", is_staff=True,
        organization_ids=["3", "9"],
    )
    await provider.get_instructions(user_context)

    assert captured["scope"] == RequestScope(organization_id="3")


@pytest.mark.asyncio
async def test_get_instructions_with_no_organizations_scopes_to_none(monkeypatch):
    from orchestrator.models.schemas import UserContext
    from shared.prompts.types import RequestScope

    captured = {}

    def capture_render(prompt_id, vars=None, scope=None):
        captured["scope"] = scope

        class _Rendered:
            system_text = "system"
            context_text = None

            def provenance(self):
                return f"{prompt_id}@bundled:default:abc12345"

        return _Rendered()

    monkeypatch.setattr(ip.PROMPTS, "render", capture_render)

    provider = ip.InstructionsProvider()
    user_context = UserContext(
        user_id="u1", user_email="customer@example.com", is_staff=False,
        organization_ids=[],
    )
    await provider.get_instructions(user_context)

    assert captured["scope"] == RequestScope(organization_id=None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/test_instructions_provider_library.py -v -k "scope"`
Expected: FAIL — `TypeError: get_customer_instructions() got an unexpected keyword argument 'organization_id'`

- [ ] **Step 3: Implement**

In `chat_orchestrator/orchestrator/services/instructions_provider.py`, add the import:

```python
from shared.prompts.types import RequestScope
```

Change `get_customer_instructions` (line 302) to:

```python
    async def get_customer_instructions(
        self, organization_id: Optional[str] = None
    ) -> tuple[str, Optional[str]]:
        """
        Get customer-facing system instructions and optional context.

        Args:
            organization_id: Caller's organization, for org-scoped knowledge modules.
                None renders with no org scope (matches sector-scoped modules only).

        Returns:
            Tuple of (system_instructions, context_message)
            - system_instructions: Goes to the provider system-instruction channel
            - context_message: Goes as first user message (or None)
        """
        scope = RequestScope(organization_id=organization_id)
        rendered = PROMPTS.render("customer.system", scope=scope)
        self._last_provenance = prompt_metadata(rendered)
        context_message = _postprocess_context(rendered.context_text, extract_staff_groups=False)
        context_message = _cap_context(context_message)
```

(leave the rest of the method body — the `LOGGER.info` call and `return` — unchanged)

Change `_get_staff_instructions_from_doc` (line 355) to:

```python
    async def _get_staff_instructions_from_doc(
        self, organization_id: Optional[str] = None
    ) -> tuple[str, Optional[str]]:
        """
        Get staff instructions and optional context.

        Args:
            organization_id: Caller's organization, for org-scoped knowledge modules.
                None renders with no org scope (matches sector-scoped modules only).

        Returns:
            Tuple of (system_instructions, context_message)
            - system_instructions: Goes to the provider system-instruction channel
            - context_message: Goes as first user message (or None)
        """
        scope = RequestScope(organization_id=organization_id)
        rendered = PROMPTS.render("staff.system", scope=scope)
        self._last_provenance = prompt_metadata(rendered)
        context_message = _postprocess_context(rendered.context_text, extract_staff_groups=True)
        context_message = _cap_context(context_message)
```

(leave the rest of the method body unchanged)

In `get_instructions` (line 324), derive the org id from `user_context` and pass it down:

```python
    async def get_instructions(
        self,
        user_context: UserContext,
        entity_context: Optional[EntityContext] = None,
        task_type: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:
        """
        Get composed system instructions and optional context message.

        Args:
            user_context: User context with roles
            entity_context: Optional entity context (grid, meter, etc.)
            task_type: Optional task type (analysis, reporting, troubleshooting, etc.)

        Returns:
            Tuple of (system_instructions, context_message)
            - system_instructions: Goes to the provider system-instruction channel
            - context_message: Goes as first user message (or None)
        """
        organization_id = (
            user_context.organization_ids[0] if user_context.organization_ids else None
        )
        # Use is_staff flag from user_context (already resolved during auth)
        if user_context.is_staff:
            LOGGER.info(
                f"Using INTERNAL/STAFF mode for {user_context.user_email or user_context.user_id}"
            )
            return await self._get_staff_instructions_from_doc(organization_id=organization_id)
        else:
            LOGGER.info(
                f"Using CUSTOMER mode for {user_context.user_email or user_context.user_id}"
            )
            return await self.get_customer_instructions(organization_id=organization_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/test_instructions_provider_library.py -v`
Expected: PASS — all tests including the 4 new ones and the pre-existing `test_get_last_provenance_populated_after_customer_fetch` / `..._staff_fetch` (which call with no `organization_id`, exercising the default)

- [ ] **Step 5: Run the full orchestrator suite to catch other callers**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/services/instructions_provider.py chat_orchestrator/tests/test_instructions_provider_library.py && git commit -m "fix: thread organization_id into RequestScope for org-scoped knowledge modules"
```

---

### Task 7: Remove `knowledge_tags` from the spec and prompt files

**Files:**
- Modify: `shared/prompts/spec.py:46, 99`
- Modify: `shared/prompts/library/{verification.criteria,ticketing.correlation,customer.system,experts.definitions,staff.system,troubleshooting.procedures}.prompt`
- Test: `shared/tests/test_prompt_spec.py`

- [ ] **Step 1: Write the failing test**

Append to `shared/tests/test_prompt_spec.py`:

```python
def test_spec_has_no_knowledge_tags_field():
    """Knowledge selection moved to prompt_knowledge_overrides; tags are gone."""
    from shared.prompts.spec import PromptSpec

    assert not hasattr(PromptSpec("id", "d", "b", "c"), "knowledge_tags")


def test_legacy_knowledge_tags_frontmatter_is_ignored(tmp_path):
    """An old .prompt file with knowledge_tags still parses -- the key is dropped."""
    from shared.prompts.spec import parse_prompt_file

    text = (
        "---\nid: legacy.example\ndescription: Legacy prompt\n"
        "knowledge_tags: [grid_ops]\n---\nBody here.\n"
    )
    spec = parse_prompt_file(text, path="legacy.prompt")
    assert spec.id == "legacy.example"
    assert not hasattr(spec, "knowledge_tags")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest shared/tests/test_prompt_spec.py -v`
Expected: FAIL — `assert not hasattr(...)` fails; the field still exists

- [ ] **Step 3: Implement**

In `shared/prompts/spec.py`, delete line 46 (`knowledge_tags: List[str] = field(default_factory=list)`) and line 99 (`knowledge_tags=list(raw.get("knowledge_tags") or []),`). Unknown frontmatter keys are already ignored by `parse_prompt_file`, so legacy files keep parsing.

Then remove the `knowledge_tags:` line from all six `.prompt` files:

```bash
sed -i '' '/^knowledge_tags:/d' shared/prompts/library/verification.criteria.prompt shared/prompts/library/ticketing.correlation.prompt shared/prompts/library/customer.system.prompt shared/prompts/library/experts.definitions.prompt shared/prompts/library/staff.system.prompt shared/prompts/library/troubleshooting.procedures.prompt
```

- [ ] **Step 4: Verify no references remain and tests pass**

Run: `grep -rn "knowledge_tags" --include="*.py" --include="*.prompt" . | grep -v ".venv\|__pycache__\|worktrees\|test_prompt_spec"`
Expected: no output

Run: `.venv/bin/python -m pytest shared/tests/ -k prompt -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/spec.py shared/prompts/library/*.prompt shared/tests/test_prompt_spec.py && git commit -m "refactor: drop knowledge_tags from prompt specs"
```

---

### Task 8: `set_prompt_modules` — write pins from the prompt side

**Files:**
- Modify: `shared/prompts/knowledge.py` (add method to `KnowledgeStore`)
- Test: `shared/tests/test_prompt_knowledge_store.py`

`set_prompt_pins` reconciles one module across many prompts. The prompt editor needs the mirror: one prompt across many modules.

- [ ] **Step 1: Write the failing test**

Append to `shared/tests/test_prompt_knowledge_store.py`:

```python
def test_set_prompt_modules_reconciles_both_directions():
    from shared.prompts.knowledge import KnowledgeModule, KnowledgeStore

    modules = [
        KnowledgeModule(id="id-a", slug="alpha", title="Alpha", summary="a", body="A"),
        KnowledgeModule(id="id-b", slug="beta", title="Beta", summary="b", body="B"),
    ]
    upserted, deleted = [], []

    class _Table:
        def __init__(self, sink):
            self.sink = sink
            self._filters = {}

        def upsert(self, row):
            self.sink.append(row)
            return self

        def delete(self):
            return self

        def eq(self, col, val):
            self._filters[col] = val
            if len(self._filters) == 2:
                deleted.append(dict(self._filters))
            return self

        def execute(self):
            return None

    class _Client:
        def table(self, name):
            assert name == "prompt_knowledge_overrides"
            return _Table(upserted)

    store = KnowledgeStore(client=_Client())
    store._cache = modules
    store._expires = float("inf")
    store.overrides_for = lambda prompt_id: {"beta": True}

    store.set_prompt_modules("staff.system", ["alpha"], actor="ops@example.com")

    assert upserted == [
        {"prompt_id": "staff.system", "module_id": "id-a", "pinned": True,
         "updated_by": "ops@example.com"}
    ]
    assert deleted == [{"prompt_id": "staff.system", "module_id": "id-b"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest shared/tests/test_prompt_knowledge_store.py::test_set_prompt_modules_reconciles_both_directions -v`
Expected: FAIL — `AttributeError: 'KnowledgeStore' object has no attribute 'set_prompt_modules'`

- [ ] **Step 3: Implement**

Add to `KnowledgeStore` in `shared/prompts/knowledge.py`, after `set_prompt_pins`:

```python
    def set_prompt_modules(self, prompt_id: str, slugs: List[str], actor: str) -> None:
        """Reconcile this prompt's pinned modules to exactly ``slugs``.

        The prompt-editor counterpart to ``set_prompt_pins``: both write the
        same ``prompt_knowledge_overrides`` row, from opposite ends of the
        relationship.
        """
        if not self._client:
            return
        by_slug = {m.slug: m.id for m in self.all_modules()}
        current = set(self.overrides_for(prompt_id))
        to_add, to_remove = diff_prompt_pins(current, set(slugs))
        for slug in sorted(to_add):
            if slug not in by_slug:
                continue
            self._client.table("prompt_knowledge_overrides").upsert(
                {
                    "prompt_id": prompt_id,
                    "module_id": by_slug[slug],
                    "pinned": True,
                    "updated_by": actor,
                }
            ).execute()
        for slug in sorted(to_remove):
            if slug not in by_slug:
                continue
            self._client.table("prompt_knowledge_overrides").delete().eq(
                "prompt_id", prompt_id
            ).eq("module_id", by_slug[slug]).execute()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest shared/tests/test_prompt_knowledge_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/knowledge.py shared/tests/test_prompt_knowledge_store.py && git commit -m "feat: add KnowledgeStore.set_prompt_modules for prompt-side pinning"
```

---

### Task 9: Rewrite `build_knowledge_tab` as a picker view-model

**Files:**
- Modify: `anansi_app/nicegui_app/pages/prompts.py:49-87`
- Test: `anansi_app/tests/test_knowledge_modules_page.py`

The old signature took `(prompt_tags, modules, overrides)` and hid unpinned modules. The picker needs every module as a searchable option, so `origin` ("tag" vs "override") is meaningless now and goes away.

- [ ] **Step 1: Replace the tests**

In `anansi_app/tests/test_knowledge_modules_page.py`, delete every existing `build_knowledge_tab` test and add:

```python
def test_knowledge_tab_lists_all_modules_with_pinned_state():
    from nicegui_app.pages.prompts import KnowledgeTabRow, build_knowledge_tab

    modules = [_module("beta"), _module("alpha", mode="on_demand")]
    rows = build_knowledge_tab(modules, {"alpha": True})
    assert rows == [
        KnowledgeTabRow(slug="alpha", title="Alpha", mode="on_demand", chars=40, checked=True),
        KnowledgeTabRow(slug="beta", title="Beta", mode="pinned", chars=40, checked=False),
    ]


def test_knowledge_tab_with_no_pins_checks_nothing():
    from nicegui_app.pages.prompts import build_knowledge_tab

    rows = build_knowledge_tab([_module("alpha")], {})
    assert [r.checked for r in rows] == [False]


def test_filter_modules_matches_slug_title_and_summary():
    from nicegui_app.pages.prompts import filter_module_rows

    rows = build_knowledge_tab_rows_fixture()
    assert [r.slug for r in filter_module_rows(rows, "azimuth")] == ["azimuth-calculation"]
    assert [r.slug for r in filter_module_rows(rows, "LED")] == ["victron-led"]
    assert len(filter_module_rows(rows, "")) == 2


def build_knowledge_tab_rows_fixture():
    from nicegui_app.pages.prompts import KnowledgeTabRow

    return [
        KnowledgeTabRow(slug="azimuth-calculation", title="Azimuth Calculation",
                        mode="on_demand", chars=318, checked=False,
                        summary="How PV azimuth is measured."),
        KnowledgeTabRow(slug="victron-led", title="Victron Quattro Codes",
                        mode="on_demand", chars=2438, checked=True,
                        summary="Decoding inverter LED error states."),
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest anansi_app/tests/test_knowledge_modules_page.py -v`
Expected: FAIL — `TypeError: build_knowledge_tab() takes 3 positional arguments but 2 were given`

- [ ] **Step 3: Implement**

Replace `KnowledgeTabRow` and `build_knowledge_tab` in `anansi_app/nicegui_app/pages/prompts.py` (lines 48-87) with:

```python
@dataclass(frozen=True)
class KnowledgeTabRow:
    slug: str
    title: str
    mode: str
    chars: int
    checked: bool
    summary: str = ""


def build_knowledge_tab(modules: List[Any], pins: dict) -> List[KnowledgeTabRow]:
    """Every module as a pickable row, flagged with this prompt's current pins.

    Unlike the tag-era version this hides nothing: the picker is how an
    operator discovers modules, so an unpinned module must still be findable.
    """
    return [
        KnowledgeTabRow(
            slug=module.slug,
            title=module.title,
            mode=module.mode,
            chars=len(module.body),
            checked=bool(pins.get(module.slug)),
            summary=module.summary,
        )
        for module in sorted(modules, key=lambda m: m.slug)
    ]


def filter_module_rows(rows: List[KnowledgeTabRow], query: str) -> List[KnowledgeTabRow]:
    """Case-insensitive substring match over slug, title and summary."""
    needle = query.strip().lower()
    if not needle:
        return list(rows)
    return [
        r
        for r in rows
        if needle in r.slug.lower() or needle in r.title.lower() or needle in r.summary.lower()
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest anansi_app/tests/test_knowledge_modules_page.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/prompts.py anansi_app/tests/test_knowledge_modules_page.py && git commit -m "refactor: knowledge tab view-model lists all modules for picking"
```

---

### Task 10: Build the Knowledge tab UI in the prompt editor

**Files:**
- Modify: `anansi_app/nicegui_app/pages/prompts.py:206-310` (`_open_detail_dialog`)

This UI has never existed — `build_knowledge_tab` was dead code. Add a fourth tab with a search box and checkbox list, saving through `set_prompt_modules`.

- [ ] **Step 1: Add the tab**

In `_open_detail_dialog`, extend the tab strip at line 214-217:

```python
        with ui.tabs().classes("w-full") as tabs:
            edit_tab = ui.tab("Edit")
            knowledge_tab = ui.tab("Context")
            diff_tab = ui.tab("Diff vs default")
            history_tab = ui.tab("History")
```

- [ ] **Step 2: Add the panel**

Inside the `ui.tab_panels(...)` block, after the `edit_tab` panel closes and before the `diff_tab` panel, add:

```python
            with ui.tab_panel(knowledge_tab):
                from shared.prompts.knowledge import KnowledgeStore

                k_store = KnowledgeStore.from_env()
                if not k_store._client:  # noqa: SLF001 -- readiness check, as elsewhere on this page
                    ui.label(
                        "⚠️ Context storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY)."
                    ).classes("text-warning")
                else:
                    ui.label(
                        "Context modules this prompt uses. Pinned modules are inlined in full; "
                        "on-demand modules contribute only a summary line, and the model fetches "
                        "the body with get_knowledge_module when it decides it's relevant."
                    ).classes("text-caption")

                    all_modules = k_store.all_modules()
                    pins = k_store.overrides_for(row.prompt_id)
                    selected: set[str] = {m.slug for m in all_modules if pins.get(m.slug)}

                    search = ui.input(placeholder="Search modules…").classes("w-full").props(
                        "clearable dense"
                    )
                    picked_label = ui.label().classes("text-caption text-bold")
                    options = ui.column().classes("w-full gap-0").style(
                        "max-height: 340px; overflow-y: auto"
                    )

                    def redraw() -> None:
                        options.clear()
                        rows = filter_module_rows(
                            build_knowledge_tab(all_modules, {s: True for s in selected}),
                            search.value or "",
                        )
                        pinned_chars = sum(
                            r.chars for r in rows if r.checked and r.mode == "pinned"
                        )
                        picked_label.text = (
                            f"{len(selected)} selected · {pinned_chars} pinned chars "
                            f"of {PINNED_BUDGET_CHARS} budget"
                        )
                        picked_label.classes(
                            replace="text-caption text-bold "
                            + ("text-negative" if pinned_chars > PINNED_BUDGET_CHARS else "")
                        )
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
                                        ui.label(f"{r.title}  ·  {r.mode}  ·  {r.chars} chars")
                                        if r.summary:
                                            ui.label(r.summary).classes("text-caption")

                    async def save_pins() -> None:
                        try:
                            k_store.set_prompt_modules(
                                row.prompt_id, sorted(selected), actor=user_email
                            )
                            k_store.invalidate()
                            ui.notify("Context updated", type="positive")
                        except Exception as e:  # noqa: BLE001 -- surfaced to the operator
                            ui.notify(f"Save failed: {e}", type="negative")

                    search.on_value_change(redraw)
                    redraw()
                    with ui.row().classes("justify-end w-full q-mt-sm"):
                        ui.button("Save context", on_click=save_pins).props("color=primary")
```

- [ ] **Step 3: Add the imports**

At the top of `anansi_app/nicegui_app/pages/prompts.py`, add:

```python
from shared.prompts.knowledge import PINNED_BUDGET_CHARS
```

- [ ] **Step 4: Verify the page loads and the tab works**

Start the admin app, open `/prompts`, open any prompt, click the **Context** tab. Verify:
- all 14 migrated modules are listed, none checked
- typing `azimuth` narrows to one row; clearing restores 14
- checking two modules updates the counter; "Save context" notifies success
- reopening the dialog shows those two still checked

- [ ] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/prompts.py && git commit -m "feat: add searchable context-module picker to the prompt editor"
```

---

### Task 11: Phase 2 verification

- [ ] **Step 1: Full suite**

Run: `.venv/bin/python -m pytest shared/tests/ anansi_app/tests/ -v`
Expected: PASS

- [ ] **Step 2: Pre-commit**

Run: `pre-commit run --all-files`
Expected: all hooks pass. If `test-wiring` reports untracked files under `tests/`, vet them for operator data then `git add -f` each one and re-run.

- [ ] **Step 3: Confirm no orphan references**

Run: `grep -rn "select_modules\|apply_overrides\|knowledge_tags" --include="*.py" --include="*.prompt" . | grep -v ".venv\|__pycache__\|worktrees"`
Expected: no output

---

# Phase 3 — `/learn` authors context modules

### Task 12: `propose_module` — draft slug/title/summary from content

**Files:**
- Create: `chat_orchestrator/orchestrator/experts/handlers/context_expert/__init__.py`
- Create: `chat_orchestrator/orchestrator/experts/handlers/context_expert/propose_module.py`
- Test: `chat_orchestrator/tests/test_context_expert.py`

- [ ] **Step 1: Write the failing test**

```python
# chat_orchestrator/tests/test_context_expert.py
"""Context expert step handlers: module proposal, dedup, storage."""

import pytest

from orchestrator.experts.handlers.context_expert.propose_module import (
    normalize_slug,
    parse_proposal,
)


def test_normalize_slug_is_kebab_case():
    assert normalize_slug("Azimuth Calculation") == "azimuth-calculation"
    assert normalize_slug("BYD / Pylontech  Voltage!") == "byd-pylontech-voltage"
    assert normalize_slug("--already-kebab--") == "already-kebab"


def test_normalize_slug_rejects_empty():
    with pytest.raises(ValueError, match="empty slug"):
        normalize_slug("!!!")


def test_parse_proposal_reads_llm_json():
    raw = '{"slug": "Victron LED Codes", "title": "Victron LED Codes", "summary": "Decodes LED states."}'
    proposal = parse_proposal(raw)
    assert proposal == {
        "slug": "victron-led-codes",
        "title": "Victron LED Codes",
        "summary": "Decodes LED states.",
    }


def test_parse_proposal_rejects_missing_summary():
    with pytest.raises(ValueError, match="summary"):
        parse_proposal('{"slug": "a", "title": "A"}')


def test_parse_proposal_rejects_non_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_proposal("I think this should be called Azimuth.")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/test_context_expert.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.experts.handlers.context_expert'`

- [ ] **Step 3: Implement**

```python
# chat_orchestrator/orchestrator/experts/handlers/context_expert/__init__.py
"""Context Expert step handlers.

Authors curated context modules (`knowledge_modules`) from staff input, as
opposed to the ingestion_expert which embeds documents into the RAG corpus.

- propose_module: LLM drafts slug/title/summary from the improved body
- detect_module_duplicates: slug/hash/title collision check against existing modules
- select_prompts: ask which prompts should use this module
- prepare_module_approval: build the approval summary
- store_module: write knowledge_modules + prompt_knowledge_overrides

fetch_document and improve_content are reused unchanged from ingestion_expert --
step handlers register globally by name, so the workflow just names them.
"""

from orchestrator.experts.handlers.context_expert.detect_module_duplicates import (
    detect_module_duplicates,
)
from orchestrator.experts.handlers.context_expert.prepare_module_approval import (
    prepare_module_approval,
)
from orchestrator.experts.handlers.context_expert.propose_module import propose_module
from orchestrator.experts.handlers.context_expert.select_prompts import select_prompts
from orchestrator.experts.handlers.context_expert.store_module import store_module

__all__ = [
    "propose_module",
    "detect_module_duplicates",
    "select_prompts",
    "prepare_module_approval",
    "store_module",
]
```

```python
# chat_orchestrator/orchestrator/experts/handlers/context_expert/propose_module.py
"""Propose a context module's identity (slug, title, summary) from its body.

The summary is the load-bearing field: an on_demand module shows only its
summary in the prompt catalog, so it is the sole basis on which the model
decides whether to fetch the body.
"""

import json
import os
import re
from typing import Dict

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

PROPOSAL_PROMPT = (
    "You are naming a reusable knowledge module for an off-grid solar operations "
    "assistant. Given the content below, reply with JSON only:\n"
    '{"slug": "kebab-case-identifier", "title": "Human Readable Title", '
    '"summary": "One sentence, max 20 words, naming the specific equipment, '
    'standard or calculation covered."}\n\n'
    "The summary is the only thing an AI sees before deciding whether to load the "
    "full module, so make it specific.\n\nContent:\n{body}"
)


def normalize_slug(text: str) -> str:
    """Kebab-case identifier. Raises if nothing usable survives."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not slug:
        raise ValueError(f"empty slug derived from {text!r}")
    return slug


def parse_proposal(raw: str) -> Dict[str, str]:
    """Validate the LLM's JSON proposal and normalize the slug."""
    try:
        data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
    except json.JSONDecodeError as e:
        raise ValueError(f"proposal is not valid JSON: {raw[:120]!r}") from e
    for field in ("slug", "title", "summary"):
        if not str(data.get(field, "")).strip():
            raise ValueError(f"proposal is missing '{field}'")
    return {
        "slug": normalize_slug(str(data["slug"])),
        "title": str(data["title"]).strip(),
        "summary": str(data["summary"]).strip(),
    }


@register_step("propose_module")
async def propose_module(context: StepContext) -> StepResult:
    """Draft slug/title/summary for the content gathered so far."""
    body = context.get_state("improved_content") or context.get_state("document_content") or ""
    if not body.strip():
        return StepResult(success=False, error="No content to build a context module from.")

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    gateway = get_default_generation_gateway(default_model=model)
    response = await gateway.generate(
        [LLMMessage(role="user", text=PROPOSAL_PROMPT.format(body=body[:8000]))],
        GenerationOptions(model=model, temperature=0.2, response_format="json"),
    )

    try:
        proposal = parse_proposal(response.text or "")
    except ValueError as e:
        LOGGER.warning(f"Module proposal failed, falling back to title heuristic: {e}")
        first_line = body.strip().split("\n")[0].lstrip("#").strip()[:60] or "Untitled module"
        proposal = {
            "slug": normalize_slug(first_line),
            "title": first_line,
            "summary": f"{first_line}.",
        }

    context.set_state("module_slug", proposal["slug"])
    context.set_state("module_title", proposal["title"])
    context.set_state("module_summary", proposal["summary"])
    context.set_state("module_body", body)
    return StepResult(success=True, data=proposal)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/test_context_expert.py -v`
Expected: PASS — 5 passed

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/context_expert/ && git add -f chat_orchestrator/tests/test_context_expert.py && git commit -m "feat: add propose_module step for the context expert"
```

---

### Task 13: `detect_module_duplicates` — preserve dedup against modules

**Files:**
- Create: `chat_orchestrator/orchestrator/experts/handlers/context_expert/detect_module_duplicates.py`
- Test: `chat_orchestrator/tests/test_context_expert.py`

The RAG flow's `detect_duplicates` checks `documents` by `source_id`/`content_hash` and offers Replace/Incorporate/Skip. Modules are single-row, so "incorporate" (chunk-level merge) has no meaning — the choices are Replace / Keep both / Cancel.

- [ ] **Step 1: Write the failing tests**

Append to `chat_orchestrator/tests/test_context_expert.py`:

```python
from orchestrator.experts.handlers.context_expert.detect_module_duplicates import (
    classify_collision,
    hash_body,
    unique_slug,
)


def test_hash_body_ignores_whitespace_and_case():
    assert hash_body("Hello   World") == hash_body("hello world")


def test_classify_collision_identical_body():
    existing = [{"slug": "azimuth", "title": "Azimuth", "body": "Angle from north."}]
    assert classify_collision("azimuth", "Azimuth", "angle from   NORTH.", existing) == "identical"


def test_classify_collision_same_slug_different_body():
    existing = [{"slug": "azimuth", "title": "Azimuth", "body": "Angle from north."}]
    assert classify_collision("azimuth", "Azimuth", "Completely new text.", existing) == "slug_taken"


def test_classify_collision_same_title_different_slug():
    existing = [{"slug": "azimuth-calc", "title": "Azimuth", "body": "Angle."}]
    assert classify_collision("azimuth-v2", "Azimuth", "Other text.", existing) == "title_taken"


def test_classify_collision_none():
    assert classify_collision("brand-new", "Brand New", "Text.", []) == "none"


def test_unique_slug_appends_suffix():
    assert unique_slug("azimuth", {"azimuth", "azimuth-2"}) == "azimuth-3"
    assert unique_slug("fresh", {"azimuth"}) == "fresh"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/test_context_expert.py -v`
Expected: FAIL — `ModuleNotFoundError` on `detect_module_duplicates`

- [ ] **Step 3: Implement**

```python
# chat_orchestrator/orchestrator/experts/handlers/context_expert/detect_module_duplicates.py
"""Collision detection for context modules.

The RAG flow's chunk-level "incorporate" mode has no analogue here -- a module
is one row, so the operator's choices are replace, keep both, or cancel.
"""

import asyncio
import hashlib
import re
from typing import Any, Dict, List, Set

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


def hash_body(text: str) -> str:
    """SHA256 of normalized text -- lowercased, whitespace collapsed."""
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def classify_collision(
    slug: str, title: str, body: str, existing: List[Dict[str, Any]]
) -> str:
    """One of: identical, slug_taken, title_taken, none."""
    body_hash = hash_body(body)
    for module in existing:
        if hash_body(module.get("body", "")) == body_hash:
            return "identical"
    if any(m.get("slug") == slug for m in existing):
        return "slug_taken"
    if any(m.get("title", "").strip().lower() == title.strip().lower() for m in existing):
        return "title_taken"
    return "none"


def unique_slug(slug: str, taken: Set[str]) -> str:
    """First free ``slug``, ``slug-2``, ``slug-3``… ."""
    if slug not in taken:
        return slug
    n = 2
    while f"{slug}-{n}" in taken:
        n += 1
    return f"{slug}-{n}"


@register_step("detect_module_duplicates")
async def detect_module_duplicates(context: StepContext) -> StepResult:
    """Check the proposed module against existing ones and ask how to proceed."""
    from shared.prompts.knowledge import KnowledgeStore

    slug = context.get_state("module_slug") or ""
    title = context.get_state("module_title") or ""
    body = context.get_state("module_body") or ""

    store = await asyncio.to_thread(KnowledgeStore.from_env)
    modules = await asyncio.to_thread(store.all_modules)
    existing = [{"slug": m.slug, "title": m.title, "body": m.body} for m in modules]

    collision = classify_collision(slug, title, body, existing)
    context.set_state("module_collision", collision)
    LOGGER.info(f"Module collision check for {slug!r}: {collision}")

    if collision == "identical":
        match = next(m for m in existing if hash_body(m["body"]) == hash_body(body))
        context.set_state("collision_slug", match["slug"])
        return StepResult(
            success=True,
            data={"collision": collision, "existing_slug": match["slug"]},
            user_message=(
                f"This content is already stored as **{match['slug']}**. "
                "Nothing to add — cancelling."
            ),
            requires_user_input=False,
        )

    if collision in ("slug_taken", "title_taken"):
        suggested = unique_slug(slug, {m["slug"] for m in existing})
        context.set_state("suggested_slug", suggested)
        return StepResult(
            success=True,
            data={"collision": collision, "suggested_slug": suggested},
            user_message=(
                f"A module named **{slug if collision == 'slug_taken' else title}** already exists "
                f"with different content.\n\n"
                f"[BUTTONS]\nReplace the existing module\nKeep both (as {suggested})\nCancel\n[/BUTTONS]"
            ),
            requires_user_input=True,
        )

    return StepResult(success=True, data={"collision": "none"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/test_context_expert.py -v`
Expected: PASS — 11 passed

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/context_expert/detect_module_duplicates.py && git add -f chat_orchestrator/tests/test_context_expert.py && git commit -m "feat: add context-module duplicate detection"
```

---

### Task 14: `select_prompts` — ask which prompts use this module

**Files:**
- Create: `chat_orchestrator/orchestrator/experts/handlers/context_expert/select_prompts.py`
- Test: `chat_orchestrator/tests/test_context_expert.py`

- [ ] **Step 1: Write the failing tests**

Append to `chat_orchestrator/tests/test_context_expert.py`:

```python
from orchestrator.experts.handlers.context_expert.select_prompts import (
    format_prompt_choices,
    parse_prompt_selection,
)


def test_format_prompt_choices_numbers_them():
    text = format_prompt_choices([("staff.system", "Staff assistant"), ("customer.system", "Customer bot")])
    assert "1. staff.system — Staff assistant" in text
    assert "2. customer.system — Customer bot" in text


def test_parse_prompt_selection_accepts_numbers():
    ids = ["staff.system", "customer.system", "troubleshooting.procedures"]
    assert parse_prompt_selection("1, 3", ids) == ["staff.system", "troubleshooting.procedures"]


def test_parse_prompt_selection_accepts_ids():
    ids = ["staff.system", "customer.system"]
    assert parse_prompt_selection("customer.system", ids) == ["customer.system"]


def test_parse_prompt_selection_none_means_empty():
    assert parse_prompt_selection("none", ["staff.system"]) == []


def test_parse_prompt_selection_ignores_out_of_range():
    assert parse_prompt_selection("9", ["staff.system"]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/test_context_expert.py -v`
Expected: FAIL — `ModuleNotFoundError` on `select_prompts`

- [ ] **Step 3: Implement**

```python
# chat_orchestrator/orchestrator/experts/handlers/context_expert/select_prompts.py
"""Ask which prompts should use the new context module.

A module with no prompt pins renders nowhere, so this step is what makes
/learn actually take effect. Selection writes prompt_knowledge_overrides in
store_module.
"""

from typing import List, Tuple

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

NONE_WORDS = {"none", "no", "skip", "later", "nothing"}


def format_prompt_choices(choices: List[Tuple[str, str]]) -> str:
    """Numbered list of ``(prompt_id, description)`` for the user to pick from."""
    return "\n".join(f"{i}. {pid} — {desc}" for i, (pid, desc) in enumerate(choices, 1))


def parse_prompt_selection(reply: str, prompt_ids: List[str]) -> List[str]:
    """Resolve a reply of numbers and/or prompt ids to a list of prompt ids."""
    text = (reply or "").strip().lower()
    if not text or text in NONE_WORDS:
        return []
    picked: List[str] = []
    for token in (t.strip() for t in text.replace(";", ",").split(",")):
        if not token:
            continue
        if token.isdigit():
            index = int(token) - 1
            if 0 <= index < len(prompt_ids):
                picked.append(prompt_ids[index])
            continue
        for pid in prompt_ids:
            if token == pid.lower() and pid not in picked:
                picked.append(pid)
    return picked


@register_step("select_prompts")
async def select_prompts(context: StepContext) -> StepResult:
    """Present the prompt list, then record the user's picks."""
    from shared.prompts import PROMPTS

    choices = [(pid, PROMPTS.spec(pid).description) for pid in sorted(PROMPTS.ids())]
    prompt_ids = [pid for pid, _ in choices]

    reply = context.get_user_input()
    if not reply:
        context.set_state("prompt_choice_ids", prompt_ids)
        return StepResult(
            success=True,
            user_message=(
                "Which prompts should use this context module?\n\n"
                f"{format_prompt_choices(choices)}\n\n"
                "Reply with numbers (e.g. `1, 3`), prompt ids, or `none` to decide later."
            ),
            requires_user_input=True,
        )

    known = context.get_state("prompt_choice_ids") or prompt_ids
    selected = parse_prompt_selection(reply, known)
    context.set_state("module_prompt_ids", selected)
    LOGGER.info(f"Context module will be pinned to: {selected or '(none)'}")
    return StepResult(success=True, data={"prompt_ids": selected})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/test_context_expert.py -v`
Expected: PASS — 16 passed

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/context_expert/select_prompts.py && git add -f chat_orchestrator/tests/test_context_expert.py && git commit -m "feat: ask which prompts pin a new context module"
```

---

### Task 15: `prepare_module_approval` and `store_module`

**Files:**
- Create: `chat_orchestrator/orchestrator/experts/handlers/context_expert/prepare_module_approval.py`
- Create: `chat_orchestrator/orchestrator/experts/handlers/context_expert/store_module.py`
- Test: `chat_orchestrator/tests/test_context_expert.py`

- [ ] **Step 1: Write the failing tests**

Append to `chat_orchestrator/tests/test_context_expert.py`:

```python
from orchestrator.experts.handlers.context_expert.prepare_module_approval import (
    build_approval_text,
)
from orchestrator.experts.handlers.context_expert.store_module import (
    build_module_payload,
    resolve_mode,
)


def test_build_approval_text_shows_identity_and_targets():
    text = build_approval_text(
        slug="azimuth-calculation",
        title="Azimuth Calculation",
        summary="How PV azimuth is measured.",
        body="A" * 500,
        mode="on_demand",
        prompt_ids=["staff.system"],
    )
    assert "azimuth-calculation" in text
    assert "How PV azimuth is measured." in text
    assert "on_demand" in text
    assert "staff.system" in text
    assert "500 chars" in text


def test_build_approval_text_warns_when_unattached():
    text = build_approval_text(
        slug="x", title="X", summary="s", body="b", mode="on_demand", prompt_ids=[]
    )
    assert "not attached to any prompt" in text


def test_resolve_mode_defaults_to_on_demand():
    assert resolve_mode("A" * 100) == "on_demand"
    assert resolve_mode("A" * 5000) == "on_demand"


def test_build_module_payload_shape():
    payload = build_module_payload(
        slug="azimuth", title="Azimuth", summary="s", body="b",
        mode="on_demand", actor="ops@example.com",
    )
    assert payload == {
        "slug": "azimuth",
        "title": "Azimuth",
        "summary": "s",
        "body": "b",
        "tags": [],
        "scope": "sector",
        "mode": "on_demand",
        "source": "manual",
        "updated_by": "ops@example.com",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/test_context_expert.py -v`
Expected: FAIL — `ModuleNotFoundError` on `prepare_module_approval`

- [ ] **Step 3: Implement both handlers**

```python
# chat_orchestrator/orchestrator/experts/handlers/context_expert/prepare_module_approval.py
"""Show the operator exactly what will be stored, before it is stored."""

from typing import List

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step


def build_approval_text(
    slug: str, title: str, summary: str, body: str, mode: str, prompt_ids: List[str]
) -> str:
    """Approval summary for a proposed context module."""
    lines = [
        "**Ready to save this context module**",
        "",
        f"**Title:** {title}",
        f"**Slug:** `{slug}`",
        f"**Summary:** {summary}",
        f"**Mode:** {mode}",
        f"**Size:** {len(body)} chars",
        "",
    ]
    if prompt_ids:
        lines.append(f"**Used by:** {', '.join(prompt_ids)}")
    else:
        lines.append(
            "⚠️ This module is **not attached to any prompt**, so the bot will not see it "
            "until you attach it on the Context page."
        )
    lines += ["", "[BUTTONS]", "Save it", "Change the summary", "Cancel", "[/BUTTONS]"]
    return "\n".join(lines)


@register_step("prepare_module_approval")
async def prepare_module_approval(context: StepContext) -> StepResult:
    """Present the proposed module and wait for approval."""
    text = build_approval_text(
        slug=context.get_state("module_slug") or "",
        title=context.get_state("module_title") or "",
        summary=context.get_state("module_summary") or "",
        body=context.get_state("module_body") or "",
        mode=context.get_state("module_mode") or "on_demand",
        prompt_ids=context.get_state("module_prompt_ids") or [],
    )
    return StepResult(success=True, user_message=text, requires_user_input=True)
```

```python
# chat_orchestrator/orchestrator/experts/handlers/context_expert/store_module.py
"""Write the approved context module and its prompt pins."""

import asyncio
from typing import Any, Dict, List

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

APPROVE_WORDS = {"save", "save it", "yes", "approve", "approved", "ok", "confirm"}


def resolve_mode(body: str) -> str:
    """New modules are on_demand.

    Pinned modules are inlined into every render of every prompt that uses
    them and share a fixed character budget, so promoting one is a deliberate
    decision an operator makes on the Context page -- never a default.
    """
    return "on_demand"


def build_module_payload(
    slug: str, title: str, summary: str, body: str, mode: str, actor: str
) -> Dict[str, Any]:
    """A knowledge_modules row for a staff-authored module."""
    return {
        "slug": slug,
        "title": title,
        "summary": summary,
        "body": body,
        "tags": [],
        "scope": "sector",
        "mode": mode,
        "source": "manual",
        "updated_by": actor,
    }


@register_step("store_module")
async def store_module(context: StepContext) -> StepResult:
    """Persist the module, then reconcile its prompt pins."""
    reply = (context.get_user_input() or "").strip().lower()
    if reply and reply not in APPROVE_WORDS:
        return StepResult(
            success=True,
            user_message="Cancelled — nothing was saved.",
            requires_user_input=False,
        )

    from shared.prompts.knowledge import KnowledgeStore

    slug = context.get_state("module_slug") or ""
    replace = context.get_state("module_collision") in ("slug_taken", "title_taken") and bool(
        context.get_state("module_replace")
    )
    payload = build_module_payload(
        slug=slug,
        title=context.get_state("module_title") or "",
        summary=context.get_state("module_summary") or "",
        body=context.get_state("module_body") or "",
        mode=context.get_state("module_mode") or resolve_mode(context.get_state("module_body") or ""),
        actor=context.get_state("user_email") or "unknown",
    )

    store = await asyncio.to_thread(KnowledgeStore.from_env)
    if not store._client:  # noqa: SLF001 -- readiness check, mirrors the admin page
        return StepResult(success=False, error="Context storage is not configured.")

    try:
        if replace:
            await asyncio.to_thread(
                lambda: store._client.table("knowledge_modules")  # noqa: SLF001
                .update(payload)
                .eq("slug", slug)
                .execute()
            )
            module_id = next(m.id for m in store.all_modules() if m.slug == slug)
        else:
            result = await asyncio.to_thread(
                lambda: store._client.table("knowledge_modules")  # noqa: SLF001
                .insert(payload)
                .execute()
            )
            module_id = result.data[0]["id"]
    except Exception as e:
        LOGGER.exception(f"Failed to store context module {slug!r}: {e}")
        return StepResult(success=False, error="Could not save the context module.")

    prompt_ids: List[str] = context.get_state("module_prompt_ids") or []
    if prompt_ids:
        try:
            await asyncio.to_thread(
                store.set_prompt_pins,
                module_id,
                prompt_ids,
                context.get_state("user_email") or "unknown",
            )
        except Exception as e:
            LOGGER.warning(f"Module {slug!r} saved but pinning failed: {e}")

    await asyncio.to_thread(store.invalidate)
    context.set_state("stored_module_slug", slug)

    attached = f" and attached to {', '.join(prompt_ids)}" if prompt_ids else ""
    return StepResult(
        success=True,
        data={"slug": slug, "module_id": module_id, "prompt_ids": prompt_ids},
        user_message=f"✅ Saved **{payload['title']}** as `{slug}`{attached}.",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/test_context_expert.py -v`
Expected: PASS — 20 passed

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/context_expert/ && git add -f chat_orchestrator/tests/test_context_expert.py && git commit -m "feat: add context-module approval and storage steps"
```

---

### Task 16: Register the handler package

**Files:**
- Modify: wherever `ingestion_expert` handlers are imported for registration

Step handlers register via the `@register_step` decorator at import time, so the package must be imported during startup or the workflow will fail with "no handler".

- [ ] **Step 1: Find where ingestion_expert is imported for side effects**

Run: `grep -rn "handlers.ingestion_expert\|handlers import" --include="*.py" chat_orchestrator/ | grep -v tests | grep -v __pycache__`
Expected: one or more import sites — typically a `handlers/__init__.py` or the workflow executor's bootstrap.

- [ ] **Step 2: Add the parallel import**

Alongside each `ingestion_expert` import found above, add the matching `context_expert` import. If the site is `chat_orchestrator/orchestrator/experts/handlers/__init__.py`, add:

```python
from orchestrator.experts.handlers import context_expert  # noqa: F401 -- registers steps
```

- [ ] **Step 3: Verify registration**

Run: `cd chat_orchestrator && ../.venv/bin/python -c "
from orchestrator.experts import handlers  # noqa: F401
from orchestrator.experts.step_registry import get_step_registry
names = get_step_registry().list_handlers()
for step in ['propose_module', 'detect_module_duplicates', 'select_prompts', 'prepare_module_approval', 'store_module']:
    assert step in names, f'{step} not registered'
print('all context_expert steps registered')
"`
Expected: `all context_expert steps registered`

- [ ] **Step 4: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/__init__.py && git commit -m "chore: register context_expert step handlers at import"
```

---

### Task 17: Define the `context_ingestion` workflow

**Files:**
- Modify: `shared/prompts/library/experts.definitions.prompt`

- [ ] **Step 1: Add the expert definition**

Insert immediately before the `# Expert: grids_technical_reviewer` line (currently line 359):

```
# Expert: context_expert
System Instructions
You are the Context Expert. You help staff teach the bot durable facts — procedures, specifications, thresholds, error-code tables — that it should reliably know.
Your workflow:
1. Understand what the user wants the bot to know
2. Guide them to provide it (pasted text, or a Google Doc link)
3. Help them tighten the wording so it reads as a clear, standalone reference
4. Propose a name and a one-line summary for it
5. Check whether something similar is already stored
6. Ask which prompts should use it
7. Show them exactly what will be saved and get approval
8. Save it
Explain that this is different from /learn_rag: context modules are curated facts the bot is told directly, while /learn_rag adds searchable source documents.
Be conversational. Ask for confirmation before saving.
Tools
* gdrive_get_document
Packet Types
* context_ingestion
Packet: context_ingestion
Workflow
1. [llm] understand_request - Parse what the user wants the bot to learn
2. [function:fetch_document] - Retrieve content from paste, Google Doc, or file
3. [function:improve_content] - Quality check and iterative wording improvement
4. [function:propose_module] - Draft slug, title and summary via LLM
5. [function:detect_module_duplicates] - Check for an existing module with the same slug, title or body
6. [function:select_prompts] - Ask which prompts should use this module
7. [function:prepare_module_approval] - Show the proposed module and ask for approval
8. [function:store_module] - On approval, write the module and its prompt pins
9. [llm] report_completion - Confirm what was saved and where it applies
Inputs
source_type: string - Where content comes from (gdrive, text, telegram)
document_id: string - Google Doc ID or file reference
raw_text: string - Direct text content if pasted
State
document_content: string - Raw content from source
improved_content: string - After the improvement loop
module_slug: string - Proposed kebab-case identifier
module_title: string - Proposed human-readable title
module_summary: string - One-line catalog summary
module_body: string - Final body text
module_mode: string - pinned or on_demand (new modules default to on_demand)
module_collision: string - none, identical, slug_taken, title_taken
module_prompt_ids: list - Prompt ids that will pin this module
stored_module_slug: string - Slug after storage
Outputs
module_slug: string - Identifier of the stored module
prompt_ids: list - Prompts it was attached to
Settings
resumable: true
model: gemini-2.5-flash
________________
```

- [ ] **Step 2: Rename the ingestion packet**

In the `# Expert: ingestion_expert` block, change line 316 from `* document_ingestion` to `* rag_ingestion`, and line 317 from `Packet: document_ingestion` to `Packet: rag_ingestion`. Update its System Instructions first line to:

```
You are the RAG Ingestion Expert. Help users add source documents to the searchable knowledge base. For curated facts the bot should always know, direct users to /learn instead.
```

- [ ] **Step 3: Verify both experts parse**

Run: `cd chat_orchestrator && ../.venv/bin/python -c "
import asyncio
from orchestrator.services.expert_instructions_provider import ExpertInstructionsProvider
async def main():
    p = ExpertInstructionsProvider()
    ctx = await p.get_expert_config('context_expert')
    assert ctx, 'context_expert did not parse'
    steps = ctx.get_workflow_steps('context_ingestion')
    print(f'context_ingestion steps: {len(steps)}')
    assert len(steps) == 9, f'expected 9 steps, got {len(steps)}'
    assert await p.get_expert_for_packet_type('rag_ingestion') == 'ingestion_expert'
    assert await p.get_expert_for_packet_type('context_ingestion') == 'context_expert'
    print('OK')
asyncio.run(main())
"`
Expected: `context_ingestion steps: 9` then `OK`

- [ ] **Step 4: Commit**

```bash
git add shared/prompts/library/experts.definitions.prompt && git commit -m "feat: define context_expert workflow, rename ingestion packet to rag_ingestion"
```

---

### Task 18: Check for a live `experts.definitions` override

**Files:** none — deployment verification.

The bundled prompt file is the **lowest** priority source. If production overrides it, Task 17's edit has no effect there.

- [ ] **Step 1: Check the resolved source**

Run: `.venv/bin/python -c "
from shared.prompts import PROMPTS
body, source, version = PROMPTS.resolve('experts.definitions')
print(f'source={source} version={version} chars={len(body)}')
print('context_expert present:', 'context_expert' in body)
"`

- [ ] **Step 2: Act on the result**

- `source=PromptSource.BUNDLED` → nothing more to do; Task 17 is live.
- `source=PromptSource.DB` → open `/prompts` in the admin app, find `experts.definitions`, apply the same two edits from Task 17 to the override body, propose and publish a new version.
- `source=PromptSource.GDOC` → apply the same two edits to the Google Doc named by `EXPERT_INSTRUCTIONS_DOC_ID`, then invalidate the cache (`PROMPTS.invalidate_gdoc()` or restart the service).

- [ ] **Step 3: Re-verify**

Re-run Step 1. Expected: `context_expert present: True`.

---

# Phase 4 — Rewire the commands

### Task 19: `/learn` → context, `/learn_rag` → RAG, drop `/ingest`

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/command_registry.py:849-866`
- Modify: `chat_orchestrator/orchestrator/experts/workflow_executor.py:1652, 1969`

- [ ] **Step 1: Replace the command definitions**

Replace the `"ingest"` and `"learn"` entries (lines 849-866) with:

```python
    "learn": CommandDefinition(
        command="learn",
        command_type="expert",
        description="Teach the bot a fact it should always know (context module)",
        packet_type="context_ingestion",
        requires_args=False,
        args_hint="Paste the text, or provide a Google Doc URL or ID",
        staff_only=True,
    ),
    "learn_rag": CommandDefinition(
        command="learn_rag",
        command_type="expert",
        description="Add a source document to the searchable knowledge base",
        packet_type="rag_ingestion",
        requires_args=False,
        args_hint="Optionally provide a Google Doc ID or paste text content",
        staff_only=True,
    ),
```

- [ ] **Step 2: Update the workflow executor's packet-type references**

At `workflow_executor.py:1652`, change:

```python
        if packet_type in ("document_ingestion", "rag_ingestion"):
```

to:

```python
        if packet_type in ("rag_ingestion", "context_ingestion"):
```

At line 1969, inside `INTERACTIVE_PACKET_TYPES`, replace the `"document_ingestion"` entry with both:

```python
        "rag_ingestion",  # RAG ingestion expert has step-level user input flows
        "context_ingestion",  # Context expert has step-level user input flows
```

- [ ] **Step 3: Verify no stale references**

Run: `grep -rn "document_ingestion" --include="*.py" --include="*.prompt" . | grep -v ".venv\|__pycache__\|worktrees"`
Expected: no output

Run: `cd chat_orchestrator && ../.venv/bin/python -c "
from orchestrator.services.command_registry import COMMAND_DEFINITIONS as C
assert 'ingest' not in C, '/ingest should be gone'
assert C['learn'].packet_type == 'context_ingestion'
assert C['learn_rag'].packet_type == 'rag_ingestion'
print('commands OK')
"`
Expected: `commands OK`

(If the registry's dict is named differently, adjust the import — find it with `grep -n "^[A-Z_]* *[:=]" chat_orchestrator/orchestrator/services/command_registry.py | head`.)

- [ ] **Step 4: Run the orchestrator suite**

Run: `cd chat_orchestrator && ../.venv/bin/python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/command_registry.py chat_orchestrator/orchestrator/experts/workflow_executor.py && git commit -m "feat: /learn authors context modules, /learn_rag does RAG ingestion, drop /ingest"
```

---

### Task 20: End-to-end `/learn` smoke test

**Files:** none — manual verification against a running bot.

- [ ] **Step 1: Run `/learn` with inline text**

In Telegram (staff account), send:

```
/learn Inverter fans must have at least 10cm clearance on all sides. Blocked vents are the most common cause of thermal derating on Quattro units.
```

Expected sequence: an improvement/quality prompt → a proposed slug/title/summary → no duplicate found → the prompt-selection list → an approval summary → `✅ Saved …`.

- [ ] **Step 2: Verify it landed**

Run: `.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('chat_orchestrator/.env')
import os; from supabase import create_client
c = create_client(os.getenv('CHAT_DB_URL'), os.getenv('CHAT_DB_SERVICE_KEY'))
rows = c.table('knowledge_modules').select('slug, title, mode, source').order('updated_at', desc=True).limit(1).execute().data
print(rows)
assert rows and rows[0]['source'] == 'manual' and rows[0]['mode'] == 'on_demand'
print('OK')
"`
Expected: the new module, `source=manual`, `mode=on_demand`, then `OK`

- [ ] **Step 3: Verify the duplicate path**

Send the exact same `/learn …` text again. Expected: "This content is already stored as **<slug>**. Nothing to add — cancelling."

- [ ] **Step 4: Verify `/learn_rag` still works**

Send `/learn_rag` with a short pasted document. Expected: the original classify → preprocess → dedupe → entities → approval → embed flow, and a new row in `documents`.

- [ ] **Step 5: Verify `/ingest` is gone**

Send `/ingest test`. Expected: unknown-command handling, not the ingestion flow.

---

# Phase 5 — Rename the UI to Context

### Task 21: Rename nav, headings and copy

**Files:**
- Modify: `anansi_app/nicegui_app/layout.py:30`
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py:97-103, 126`
- Modify: `anansi_app/nicegui_app/pages/documents.py:92`

The route stays `/knowledge-modules` — renaming it would break existing bookmarks and every `layout.frame(user, "/knowledge-modules")` call for no user-visible gain. Only labels change.

- [ ] **Step 1: Rename the nav entry**

In `anansi_app/nicegui_app/layout.py`, change line 30 from:

```python
    ("/knowledge-modules", "🧠 Knowledge Modules"),
```

to:

```python
    ("/knowledge-modules", "🧠 Context"),
```

- [ ] **Step 2: Update the page heading and description**

In `anansi_app/nicegui_app/pages/knowledge_modules.py`, replace lines 97-103 with:

```python
    ui.label("🧠 Context").classes("text-h5")
    ui.label(
        "Curated facts the bot is told directly — the context it works from. Pinned modules "
        "are inlined into a prompt in full; on-demand modules contribute only their summary, "
        "and the model fetches the body with a tool when it decides it's relevant. Attach "
        "modules to prompts here or from the Context tab of any prompt."
    ).classes("text-caption")
```

Change the empty-state at line 126 from `"No knowledge modules yet."` to:

```python
                ui.label("No context modules yet. Use /learn in Telegram to add one.").classes(
                    "text-italic"
                )
```

Change the readiness warning at lines 107-110 from "Knowledge storage not configured" to "Context storage not configured", and the `+ New module` button label at line 122 to `+ New context module`.

- [ ] **Step 3: Update the module docstring**

Replace the docstring at the top of `knowledge_modules.py` (lines 1-7) with:

```python
"""Context admin page: CRUD for curated context modules.

A context module is named, addressable content a prompt can deliberately pin
(inlined in full) or leave on-demand (name + summary only, fetched via the
get_knowledge_module MCP tool when the model decides it's relevant). Selection
is explicit per prompt -- see the Context tab on the Prompts page.
"""
```

- [ ] **Step 4: Fix the RAG page's stale pointer**

In `anansi_app/nicegui_app/pages/documents.py`, change line 92 from:

```python
                    "No documents ingested yet. Use /ingest in Telegram to add documents."
```

to:

```python
                    "No documents ingested yet. Use /learn_rag in Telegram to add source "
                    "documents. For facts the bot should always know, use /learn (Context)."
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/python -m pytest anansi_app/tests/ -v`
Expected: PASS

Open the admin app: the sidebar reads "🧠 Context", the page heading reads "🧠 Context", and the RAG Knowledgebase empty state points at `/learn_rag`.

```bash
git add anansi_app/nicegui_app/layout.py anansi_app/nicegui_app/pages/knowledge_modules.py anansi_app/nicegui_app/pages/documents.py && git commit -m "feat: rename Knowledge Modules to Context across the admin UI"
```

---

### Task 22: Final verification

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/python -m pytest shared/tests/ anansi_app/tests/ mcp_servers/tests/ -v && cd chat_orchestrator && ../.venv/bin/python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 2: Pre-commit across everything**

Run: `pre-commit run --all-files`
Expected: all hooks pass, including `test-wiring`. If it reports untracked files under any `tests/` directory, vet each for operator data, `git add -f` it, and re-run.

- [ ] **Step 3: Confirm every new test file is actually tracked**

Run: `git ls-files shared/tests/test_migrate_rag_docs_to_modules.py chat_orchestrator/tests/test_context_expert.py`
Expected: both paths listed. **If either is missing, it was never committed** — `git add -f` it now. A plain `git add` on these is a silent no-op.

- [ ] **Step 4: Verify final data state**

Run: `.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('chat_orchestrator/.env')
import os; from supabase import create_client
c = create_client(os.getenv('CHAT_DB_URL'), os.getenv('CHAT_DB_SERVICE_KEY'))
docs = c.table('documents').select('id', count='exact').execute().count
mods = c.table('knowledge_modules').select('slug, mode').execute().data
print(f'RAG documents: {docs} (expect 7 + anything added by /learn_rag testing)')
print(f'Context modules: {len(mods)} (expect 14 + anything added by /learn testing)')
print('on_demand:', sum(1 for m in mods if m['mode'] == 'on_demand'))
"`

- [ ] **Step 5: Commit the plan itself**

`docs/superpowers/plans/` is gitignored, so this needs `-f`:

```bash
git add -f docs/superpowers/plans/2026-08-05-context-knowledge-consolidation.md && git commit -m "docs: add context/knowledge consolidation plan"
```

---

## Deferred / Out of Scope

- **`org:<id>` scoping is fixed by Task 6; `site:<name>` scoping is not, and needs a resolver that doesn't exist yet.** Before this plan, `RequestScope` was never populated in production — the only construction was the `scope or RequestScope()` default at `shared/prompts/core.py:168`, so every scoped module silently never rendered. Task 6 fixes the organization half by threading `UserContext.organization_ids[0]` through `get_instructions`. The grid half is still open: `RequestScope.grid` and `knowledge_modules.scope`'s `site:<name>` format are both name-keyed, but the only grid signal reaching this layer is `EntityContext.grid_id` — a numeric id (`grid_id integer` in `auth_db.sql:136`) — and `EntityContext` is only ever constructed from raw MCP-server input, never populated in the primary Telegram chat path. No id→name resolver exists on `auth_service.py` today (every grid helper there takes a name, e.g. `get_dcu_status_by_grid_name`, `get_grid_operational_facts`). Wiring `site:` scoping needs that resolver plus a live grid signal in the chat path — a real feature, not a fix, and out of scope here since none of the 14 migrated modules use `site:` scope. The Context page's `validate_module` still accepts `site:<name>`, so it's worth knowing that value currently has no effect.

- **Generated / derived context as a selectable module kind.** Selection is already agnostic after Phase 2 — `prompt_knowledge_overrides` pins a `module_id`, and nothing in the pin table, the picker UI, `budget_pinned`, or `render_catalog` cares whether the body is stored text or computed per request. What a generated module needs is a different *render* path: a `knowledge_modules` row with no stored `body`, carrying a provider key, whose content is produced at compose time from the `RequestScope` (grid ontology from `auth_db`, a grid profile aggregated over `ticket_correlations`, and so on) — storing the output instead of the query is what makes derived facts go stale. Requires: a migration relaxing `CHECK (mode IN ('pinned','on_demand'))` and `CHECK (source IN ('manual','gdoc','ingested'))`, a provider registry, and a render branch in `_compose_knowledge`. Org-scoped providers can build on Task 6 today; grid-scoped providers additionally need the resolver described above. Deliberately **not** in this plan: the design isn't settled, and adding an unused enum value now would be scaffolding for it. Its natural first target is `context_enrichment.py`, which currently hand-injects grid names / Jira assignees / Jira organizations into every staff and customer context — the "manual injection" this would make selectable.

- **Fixing `RAGProvider.retrieve`'s RPC mismatch.** `chat_orchestrator/orchestrator/services/rag_provider.py:161` calls `search_chunks_with_permissions` with `match_threshold` / `user_role_ids` / `user_org_ids`, but the SQL function (`db/schema/chat_db.sql:742`) takes `similarity_threshold` / `p_organization_id`. The call raises and falls back to `match_rag_documents`, which is not defined in `db/` at all — so permission-filtered RAG retrieval returns nothing. Independent of this plan, but it means the 7 remaining RAG documents are only reachable via the staff-only `summarize_knowledge` tool (which uses the unfiltered `search_chunks`). Worth its own fix.
- **`entities` / `relationships` cleanup.** Deleting the 14 documents cascades their `entity_mentions`, but orphaned `entities` rows (no remaining mentions) are left behind. Harmless — nothing queries `entities` since the 2026-07-11 cleanup dropped its vector index and RPCs.
- **Backfilling `documents.content`** on the remaining rows where it is an empty string. Only matters for re-ingestion change detection on those specific documents.
