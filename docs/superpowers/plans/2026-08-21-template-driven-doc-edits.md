# Template-Driven Doc & Sheet Edits — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `@anansi-chatbot` comment loop the single UX contract for editing both Google Docs and Google Sheets, so any user-supplied file can act as a template — and split LPP's hardwired `populate_lpp_cells` so retiring it later is a config change.

**Architecture:** One shared annotation spine (`scan → locate → apply → reply+resolve`) with file-type dispatch on Drive `mimeType`. Cells are located by searching every tab for the comment's HTML-unescaped `quotedFileContent`; the Drive `anchor` field is unusable (see spec). Values come from a catalogue built by walking `StepContract.outputs` for steps that have run, so free-text comments resolve against real descriptions and the bot replies with the field it chose.

**Tech Stack:** Python 3.12, Google Drive API v3 (`comments`), Google Sheets API v4 (`values`, `spreadsheets.get`), Google Apps Script (`scripts/anansi_helper.gs`), pytest + pytest-asyncio, `StepContract`/`OutputSpec`/`MockSpec` from PR #128.

**Spec:** `docs/superpowers/specs/2026-08-21-template-driven-doc-edits-design.md`

---

## Before you start

**Run tests like CI does.** All commands below assume you are in `chat_orchestrator/`:

```bash
cd chat_orchestrator && uv run pytest tests/ -x -q
```

`shared/` has no venv of its own and is tested from the same place:

```bash
cd chat_orchestrator && uv run pytest ../shared -q
```

**Three repo rules that will bite you:**

1. **New test files under any `tests/` directory need `git add -f`.** `.gitignore` denies `tests/` by default. A plain `git add` is a *silent no-op* — the commit succeeds, the file never reaches the remote, and CI never runs it. Every commit step below that adds a test file uses `-f` explicitly.
2. **Async tests under `shared/tests/` need `@pytest.mark.asyncio`.** Targeted directly, they can resolve the repo-root `pyproject.toml` (no `asyncio_mode`) instead of `chat_orchestrator`'s (`asyncio_mode = "auto"`). Mark them explicitly.
3. **`pre-commit run --all-files` before claiming anything is done.** `ruff check .` skips gitignored files, so a not-yet-force-added test gets zero linting locally.

**Do not copy a `.env` with live credentials into this worktree** except for Phase 0. `shared.prompts.PROMPTS` is a process-wide singleton that silently reads live DB/Doc content whenever real credentials are present, which makes bundled-prompt tests non-hermetic.

---

## File Structure

| file | responsibility |
|---|---|
| `shared/utils/file_annotations.py` | **new** — file-type-agnostic half: scan comments, fold reply threads, reply+resolve, pin revision |
| `shared/utils/doc_editing.py` | **modify** — keeps Docs locate+apply; re-exports the moved helpers for compatibility |
| `shared/utils/sheet_editing.py` | **new** — Sheets locate (cross-tab text search) + apply (cell write, image slot) |
| `chat_orchestrator/orchestrator/experts/output_catalogue.py` | **new** — builds the value catalogue from `StepContract.outputs` |
| `chat_orchestrator/orchestrator/experts/handlers/templates/create_from_template.py` | **new** — generic template copy step |
| `chat_orchestrator/orchestrator/experts/handlers/templates/fill_annotations.py` | **new** — generic comment-driven fill step |
| `chat_orchestrator/orchestrator/experts/handlers/templates/replace_file_image.py` | **new** — generic image replacement step |
| `scripts/anansi_helper.gs` | **modify** — `replaceSheetImage` gains `target` + `fit_range`; new `get_range_pixels` |
| `shared/utils/apps_script_client.py` | **modify** — pass the new params through |
| `mcp_servers/servers/knowledge_server/*` | **modify** — `scan_doc_comments` / `edit_doc_section` accept spreadsheets |
| `chat_orchestrator/orchestrator/experts/handlers/package_generator/populate_cells.py` | **modify** — split into three internal functions, registered name preserved |
| `shared/prompts/library/annotations.resolve_values.prompt` | **new** — the batch value-matching prompt |

---

# Phase 0 — Spike 1: prove the round trip

Spike 0 (in the spec) established that Drive populates `quotedFileContent` for Sheets comments and that the `anchor` is unusable. What it did **not** prove is a full round trip on a *template-shaped* file: a token in a cell, a bot-mention comment on it, located and written.

This phase writes no production code. It exists so Phase 3 is built against verified behaviour.

### Task 0.1: Round-trip a bot comment on a scratch template

**Files:**
- Create: `/tmp/spike1_roundtrip.py` (throwaway — do NOT commit)

- [ ] **Step 1: Create a scratch spreadsheet by hand**

In a browser, create a Google Sheet named `SPIKE1 scratch — safe to delete`. Share it with the service account's email (find it without printing the key: `python3 -c "import json,os;print(json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])['client_email'])"` after sourcing `chat_orchestrator/.env`). Give it **Editor**.

In the sheet, set up two tabs:
- Tab `Main Input`: `A1` = `Total kWp`, `B1` = `{{total_kwp}}`, `A2` = `Site Name`, `B2` = `{{site_name}}`
- Tab `Second`: `A1` = `{{total_kwp}}` (a deliberate duplicate, to exercise the multi-match path)

On `Main Input!B1`, add a comment: `@anansi-chatbot the total peak capacity`.
On `Main Input!B2`, add a comment: `@anansi-chatbot the name of the site`.

- [ ] **Step 2: Write the probe**

```python
"""Spike 1 (READ + ONE WRITE): round-trip a bot comment on a template cell."""
import html, os, sys
sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv("chat_orchestrator/.env")
from googleapiclient.discovery import build
from shared.utils.google_auth import (
    get_drive_credentials, get_sheets_credentials, get_sheets_write_credentials,
)

SHEET_ID = "PASTE_THE_SCRATCH_SHEET_ID_HERE"
BOT_MENTION = "@anansi-chatbot"

drive = build("drive", "v3", credentials=get_drive_credentials())
comments = drive.comments().list(
    fileId=SHEET_ID,
    fields="comments(id,content,resolved,anchor,quotedFileContent)",
    includeDeleted=False,
).execute().get("comments", [])

pending = [c for c in comments
           if not c.get("resolved") and BOT_MENTION in (c.get("content") or "").lower()]
print(f"pending bot comments: {len(pending)}")
assert pending, "no unresolved @anansi-chatbot comments found — check the mention text"

sheets = build("sheets", "v4", credentials=get_sheets_credentials())
meta = sheets.spreadsheets().get(
    spreadsheetId=SHEET_ID, fields="sheets(properties(title))").execute()
tabs = [s["properties"]["title"] for s in meta["sheets"]]

def a1(col_idx, row_idx):
    s, col = "", col_idx + 1
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return f"{s}{row_idx + 1}"

grids = {t: sheets.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"'{t}'").execute().get("values", [])
         for t in tabs}

for c in pending:
    q = (c.get("quotedFileContent") or {}).get("value")
    print(f"\ncomment {c['id']}: quoted={q!r} anchor={c.get('anchor')}")
    if not q:
        print("  EMPTY QUOTE — this cell had no content"); continue
    target = html.unescape(q).strip()
    matches = [f"'{t}'!{a1(ci, ri)}"
               for t, rows in grids.items()
               for ri, row in enumerate(rows)
               for ci, cell in enumerate(row)
               if html.unescape(str(cell)).strip() == target]
    print(f"  matches: {matches}")

# ONE write, to the first pending comment's cell, then reply+resolve.
first = pending[0]
q = html.unescape((first["quotedFileContent"] or {})["value"]).strip()
cell = [f"'{t}'!{a1(ci, ri)}"
        for t, rows in grids.items()
        for ri, row in enumerate(rows)
        for ci, cell_v in enumerate(row)
        if html.unescape(str(cell_v)).strip() == q][0]
w = build("sheets", "v4", credentials=get_sheets_write_credentials())
w.spreadsheets().values().update(
    spreadsheetId=SHEET_ID, range=cell, valueInputOption="RAW",
    body={"values": [["SPIKE1-WROTE-THIS"]]}).execute()
print(f"\nwrote SPIKE1-WROTE-THIS to {cell}")
drive.replies().create(
    fileId=SHEET_ID, commentId=first["id"], fields="id",
    body={"action": "resolve", "content": "Done: SPIKE1-WROTE-THIS"}).execute()
print("replied and resolved")
```

- [ ] **Step 3: Run it**

```bash
.venv/bin/python /tmp/spike1_roundtrip.py
```

Record, in your notes for Phase 3:
- Did `{{total_kwp}}` return **two** matches (Main Input + Second)?
- Did the write land in the right cell?
- Did the comment show as resolved with the reply visible in the Sheets UI?
- Did `anchor` still show `{"type":"workbook-range","uid":0,...}` with an opaque `range`?

- [ ] **Step 4: Record findings in the spec**

Append a short `### Spike 1 — round trip` subsection under *Ground truth* in
`docs/superpowers/specs/2026-08-21-template-driven-doc-edits-design.md` stating what
happened. If anything contradicts the spec, **stop and raise it** — do not continue
building on a refuted assumption.

- [ ] **Step 5: Commit the spec update only**

```bash
git add docs/superpowers/specs/2026-08-21-template-driven-doc-edits-design.md
git commit -m "docs(specs): record Spike 1 round-trip findings"
```

Do **not** commit the probe script — `scripts/*.py` is gitignored anyway, and `/tmp` keeps it out of the tree entirely.

---

# Phase 1 — OutputSpec backfill (gates the catalogue)

`_collect_all_available_values()` in `populate_cells.py:190-303` is the specification. Transcribe it into `OutputSpec`s on the steps that actually produce those values. Nothing in Phase 4 works without this.

### Task 1.1: OutputSpecs for `generate_distribution_map`

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/handlers/package_generator/generate_map.py`
- Test: `chat_orchestrator/tests/experts/test_output_catalogue.py` (created here, extended in Phase 4)

- [ ] **Step 1: Write the failing test**

```python
"""OutputSpec coverage for the steps the value catalogue reads from."""

from orchestrator.experts.step_registry import get_step_contract


def _output_names(step_name: str) -> set[str]:
    contract = get_step_contract(step_name)
    assert contract is not None, f"{step_name} has no StepContract"
    return {spec.name for spec in contract.outputs}


def test_generate_distribution_map_declares_its_statistics():
    names = _output_names("generate_distribution_map")
    assert {
        "meta.pole_count",
        "meta.served_building_count",
        "meta.unserved_building_count",
        "meta.coverage_percentage",
        "meta.backbone_cable_length_m",
        "meta.drop_cable_length_m",
        "computed.total_buildings",
        "location.lat",
        "location.lon",
        "location.gps",
    } <= names


def test_every_catalogue_output_has_a_description():
    """A bare key name is not matchable — the LLM matches on description."""
    for step in (
        "generate_distribution_map",
        "generate_powerplant_design",
        "generate_site_bom",
        "fetch_solar_potential",
        "resolve_sites",
    ):
        contract = get_step_contract(step)
        assert contract is not None, f"{step} has no StepContract"
        for spec in contract.outputs:
            assert spec.description.strip(), f"{step}.{spec.name} has no description"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_output_catalogue.py -q
```

Expected: FAIL — `generate_distribution_map` currently declares zero outputs.

- [ ] **Step 3: Add the OutputSpecs**

In `generate_map.py`, import `OutputSpec` alongside the existing contract imports and add an `outputs=(...)` tuple to the `@register_step("generate_distribution_map", contract=StepContract(...))` call. Every entry uses `where="data"` because these values live under `StepResult.data`, read via `context.get_previous_result("generate_distribution_map")`:

```python
outputs=(
    OutputSpec(name="meta.pole_count", value_type="integer", where="data",
               description="Number of distribution poles in the generated layout."),
    OutputSpec(name="meta.served_building_count", value_type="integer", where="data",
               description="Buildings connected by the distribution network."),
    OutputSpec(name="meta.unserved_building_count", value_type="integer", where="data",
               description="Buildings inside the site boundary left unconnected."),
    OutputSpec(name="meta.coverage_percentage", value_type="number", where="data",
               description="Percentage of buildings served by the network."),
    OutputSpec(name="meta.backbone_cable_length_m", value_type="number", where="data",
               description="Total backbone (trunk) cable length in metres."),
    OutputSpec(name="meta.drop_cable_length_m", value_type="number", where="data",
               description="Total drop (service) cable length in metres."),
    OutputSpec(name="meta.backbone_cable_count", value_type="integer", where="data",
               description="Number of backbone cable spans."),
    OutputSpec(name="meta.drop_cable_count", value_type="integer", where="data",
               description="Number of drop cable runs."),
    OutputSpec(name="meta.average_span_length_m", value_type="number", where="data",
               description="Mean distance between adjacent poles, in metres."),
    OutputSpec(name="meta.max_drop_cable_length_m", value_type="number", where="data",
               description="Longest single drop cable run, in metres."),
    OutputSpec(name="computed.total_buildings", value_type="integer", where="data",
               description="All buildings detected inside the site boundary."),
    OutputSpec(name="computed.cable_length_m", value_type="number", where="data",
               description="Backbone plus drop cable length combined, in metres."),
    OutputSpec(name="location.lat", value_type="string", where="data",
               description="Site centre latitude, 6 decimal places."),
    OutputSpec(name="location.lon", value_type="string", where="data",
               description="Site centre longitude, 6 decimal places."),
    OutputSpec(name="location.gps", value_type="string", where="data",
               description="Site centre as a single 'lat, lon' string."),
),
```

- [ ] **Step 4: Run the test again**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_output_catalogue.py::test_generate_distribution_map_declares_its_statistics -q
```

Expected: PASS. The `test_every_catalogue_output_has_a_description` test still fails on the other four steps — that is expected until Task 1.4.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/package_generator/generate_map.py
git add -f chat_orchestrator/tests/experts/test_output_catalogue.py
git commit -m "feat(experts): declare OutputSpecs for generate_distribution_map"
```

### Task 1.2: OutputSpecs for `generate_powerplant_design` and `generate_site_bom`

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/handlers/package_generator/generate_design.py`
- Modify: `chat_orchestrator/orchestrator/experts/handlers/package_generator/generate_bom.py`
- Test: `chat_orchestrator/tests/experts/test_output_catalogue.py`

- [ ] **Step 1: Write the failing test**

Append to `test_output_catalogue.py`:

```python
def test_design_and_bom_declare_their_energy_and_cost_values():
    design = _output_names("generate_powerplant_design")
    assert {"design.design_id", "design.design_name"} <= design

    bom = _output_names("generate_site_bom")
    assert {
        "bom.total_cost",
        "bom.main_energy_asset_cost",
        "bom.metering_cost",
        "bom.bos_cost",
        "energy.total_kwp",
        "energy.total_kwh",
        "energy.total_kva",
        "energy.Wp_per_conn",
        "energy.num_subsystems",
        "energy.num_inverters",
        "energy.num_batteries",
        "energy.num_panels",
    } <= bom
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_output_catalogue.py::test_design_and_bom_declare_their_energy_and_cost_values -q
```

Expected: FAIL — both steps declare zero outputs.

- [ ] **Step 3: Add the OutputSpecs**

`generate_design.py` — add to its `StepContract`:

```python
outputs=(
    OutputSpec(name="design.design_id", value_type="string", where="data",
               description="Identifier of the generated power plant design."),
    OutputSpec(name="design.design_name", value_type="string", where="data",
               description="Human-readable name of the generated design."),
),
```

`generate_bom.py` — add to its `StepContract`. `energy.*` and `bom.*` are read from
this step's `cost_summary` and `energy_specs` sub-dicts by
`_collect_all_available_values`, so they are `where="data"`:

```python
outputs=(
    OutputSpec(name="bom.total_cost", value_type="number", where="data",
               description="Total bill-of-materials cost for the site."),
    OutputSpec(name="bom.main_energy_asset_cost", value_type="number", where="data",
               description="Cost of generation and storage assets only."),
    OutputSpec(name="bom.metering_cost", value_type="number", where="data",
               description="Cost of meters and metering infrastructure."),
    OutputSpec(name="bom.bos_cost", value_type="number", where="data",
               description="Balance-of-system cost: mounting, cabling, protection."),
    OutputSpec(name="energy.total_kwp", value_type="number", where="data",
               description="Total installed solar peak capacity in kWp."),
    OutputSpec(name="energy.total_kwh", value_type="number", where="data",
               description="Total battery storage capacity in kWh."),
    OutputSpec(name="energy.total_kva", value_type="number", where="data",
               description="Total inverter apparent power rating in kVA."),
    OutputSpec(name="energy.Wp_per_conn", value_type="number", where="data",
               description="Installed peak watts per served connection."),
    OutputSpec(name="energy.num_subsystems", value_type="integer", where="data",
               description="Number of independent generation subsystems."),
    OutputSpec(name="energy.num_inverters", value_type="integer", where="data",
               description="Number of inverters in the design."),
    OutputSpec(name="energy.num_batteries", value_type="integer", where="data",
               description="Number of battery units in the design."),
    OutputSpec(name="energy.num_panels", value_type="integer", where="data",
               description="Number of solar panels in the design."),
),
```

- [ ] **Step 4: Run the test again**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_output_catalogue.py::test_design_and_bom_declare_their_energy_and_cost_values -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/package_generator/generate_design.py
git add chat_orchestrator/orchestrator/experts/handlers/package_generator/generate_bom.py
git commit -m "feat(experts): declare OutputSpecs for design and BOM steps"
```

### Task 1.3: OutputSpecs for `fetch_solar_potential`

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/handlers/package_generator/fetch_solar_potential.py`
- Test: `chat_orchestrator/tests/experts/test_output_catalogue.py`

- [ ] **Step 1: Write the failing test**

```python
def test_solar_potential_declares_its_irradiation_values():
    names = _output_names("fetch_solar_potential")
    assert {
        "energy.gsa_daily_potential_kwhperkwp",
        "energy.gsa_yearly_potential_kwhperkwp",
        "solar.optimal_tilt_deg",
        "solar.ghi_kwh_m2",
        "solar.gti_kwh_m2",
        "solar.dni_kwh_m2",
        "solar.avg_temp_c",
        "solar.elevation_m",
    } <= names
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_output_catalogue.py::test_solar_potential_declares_its_irradiation_values -q
```

Expected: FAIL.

- [ ] **Step 3: Add the OutputSpecs**

```python
outputs=(
    OutputSpec(name="energy.gsa_daily_potential_kwhperkwp", value_type="number", where="data",
               description="Global Solar Atlas daily yield, kWh per installed kWp."),
    OutputSpec(name="energy.gsa_yearly_potential_kwhperkwp", value_type="number", where="data",
               description="Global Solar Atlas annual yield, kWh per installed kWp."),
    OutputSpec(name="solar.optimal_tilt_deg", value_type="number", where="data",
               description="Panel tilt angle that maximises annual yield, in degrees."),
    OutputSpec(name="solar.ghi_kwh_m2", value_type="number", where="data",
               description="Global horizontal irradiation, kWh per square metre."),
    OutputSpec(name="solar.gti_kwh_m2", value_type="number", where="data",
               description="Global tilted irradiation at optimal tilt, kWh per square metre."),
    OutputSpec(name="solar.dni_kwh_m2", value_type="number", where="data",
               description="Direct normal irradiation, kWh per square metre."),
    OutputSpec(name="solar.avg_temp_c", value_type="number", where="data",
               description="Average ambient air temperature at the site, in Celsius."),
    OutputSpec(name="solar.elevation_m", value_type="number", where="data",
               description="Site elevation above sea level, in metres."),
),
```

- [ ] **Step 4: Run the test again**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_output_catalogue.py::test_solar_potential_declares_its_irradiation_values -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/package_generator/fetch_solar_potential.py
git commit -m "feat(experts): declare OutputSpecs for fetch_solar_potential"
```

### Task 1.4: OutputSpecs for `resolve_sites`, and the description guard goes green

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/handlers/package_generator/resolve_sites.py`
- Test: `chat_orchestrator/tests/experts/test_output_catalogue.py`

- [ ] **Step 1: Write the failing test**

```python
def test_resolve_sites_declares_the_site_identity_values():
    names = _output_names("resolve_sites")
    assert {"site.site_name", "site.site_id", "site.state"} <= names
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_output_catalogue.py -q
```

Expected: FAIL on both the new test and `test_every_catalogue_output_has_a_description`.

- [ ] **Step 3: Add the OutputSpecs**

`site_name` and `site_id` are written to `packet_state`, so these use the default
`where="state"`. `site.state` (the administrative state/region) comes from the map
step's `site_state` and stays `where="data"`:

```python
outputs=(
    OutputSpec(name="site.site_name", value_type="string",
               description="Canonical name of the site, as stored in site submissions."),
    OutputSpec(name="site.site_id", value_type="string",
               description="Database identifier of the resolved site submission."),
    OutputSpec(name="site.state", value_type="string", where="data",
               description="Administrative state or region the site sits in."),
),
```

- [ ] **Step 4: Run the whole file**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_output_catalogue.py -q
```

Expected: PASS, including `test_every_catalogue_output_has_a_description`.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/package_generator/resolve_sites.py
git commit -m "feat(experts): declare OutputSpecs for resolve_sites"
```

---

# Phase 2 — The shared annotation spine

Extract the file-type-agnostic half of `doc_editing.py` so Sheets can reuse it verbatim.

### Task 2.1: Create `file_annotations.py` with `scan_annotations`

**Files:**
- Create: `shared/utils/file_annotations.py`
- Test: `shared/tests/test_file_annotations.py`

- [ ] **Step 1: Write the failing test**

```python
"""File-type-agnostic Drive comment scanning."""

import pytest

from shared.utils.file_annotations import Annotation, build_thread_instruction, strip_bot_mention


def test_strip_bot_mention_removes_the_mention_and_trims():
    assert strip_bot_mention("@anansi-chatbot fill this in") == "fill this in"
    assert strip_bot_mention("@anansi-chatbot.iam.gserviceaccount.com do it") == "do it"


def test_build_thread_instruction_concatenates_same_author_replies():
    comment = {
        "content": "@anansi-chatbot make it formal",
        "author": {"emailAddress": "a@x.com", "displayName": "Ada"},
        "replies": [
            {"content": "and shorter", "author": {"emailAddress": "a@x.com", "displayName": "Ada"}},
        ],
    }
    assert build_thread_instruction(comment, "Ada") == "make it formal\nand shorter"


def test_build_thread_instruction_attributes_multiple_authors():
    comment = {
        "content": "@anansi-chatbot make it formal",
        "author": {"emailAddress": "a@x.com", "displayName": "Ada"},
        "replies": [
            {"content": "and shorter", "author": {"emailAddress": "b@x.com", "displayName": "Bob"}},
        ],
    }
    result = build_thread_instruction(comment, "Ada")
    assert "[Ada]: make it formal" in result
    assert "[Bob]: and shorter" in result


def test_annotation_carries_quoted_text_and_comment_id():
    ann = Annotation(
        comment_id="c1", quoted_text="{{total_kwp}}", instruction="the peak capacity",
        author_email="a@x.com", created_time="2026-08-21T00:00:00Z",
    )
    assert ann.comment_id == "c1"
    assert ann.quoted_text == "{{total_kwp}}"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_file_annotations.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'shared.utils.file_annotations'`.

- [ ] **Step 3: Write the implementation**

```python
"""File-type-agnostic Google Drive comment handling.

The Drive `comments` API is the same for Docs and Sheets: `comments.list`
takes a file ID and knows nothing about the file's type. Only *locating*
the thing a comment points at, and *applying* an edit to it, differ --
those live in doc_editing.py and sheet_editing.py respectively.

Everything here was previously private to doc_editing.py and is unchanged
in behaviour; it moved so sheet_editing.py can reuse it rather than
reimplement the reply-thread folding and mention stripping.
"""

import asyncio
import functools
import logging
import re
from dataclasses import dataclass

from googleapiclient.discovery import build

from shared.utils.google_auth import get_drive_write_credentials

LOGGER = logging.getLogger(__name__)

# Partial match for the service account email in comments
BOT_MENTION = "@anansi-chatbot"


@dataclass(frozen=True)
class Annotation:
    """One unresolved @anansi-chatbot comment, independent of file type.

    `quoted_text` is Drive's `quotedFileContent` -- the highlighted run in a
    Doc, or the cell's content in a Sheet. Spike 0 established it is served
    as `text/html`, so callers matching it against live file content must
    HTML-unescape first. It is `""` when the commented range had no content,
    which for a Sheet means an empty cell and makes the comment unlocatable.
    """

    comment_id: str
    quoted_text: str
    instruction: str
    author_email: str
    created_time: str


@functools.lru_cache(maxsize=1)
def _get_drive_service():
    """Cached Drive v3 service (write credentials). Built once per process."""
    creds = get_drive_write_credentials()
    return build("drive", "v3", credentials=creds)


def strip_bot_mention(text: str) -> str:
    """Remove @anansibot mentions from comment text."""
    text = text.replace(BOT_MENTION, "")
    return re.sub(r"@?anansi-chatbot[-\w.@]*", "", text, flags=re.IGNORECASE).strip()


def build_thread_instruction(comment: dict, initial_author: str) -> str:
    """Build a single instruction string from a comment and its reply thread.

    If all messages are from the same author, concatenates plainly.
    If multiple authors, prefixes each reply with the author's name.
    """
    initial_text = strip_bot_mention(comment.get("content", ""))
    replies = comment.get("replies", [])

    if not replies:
        return initial_text

    initial_email = comment.get("author", {}).get("emailAddress", "")
    all_same_author = all(
        r.get("author", {}).get("emailAddress", "") == initial_email for r in replies
    )

    if all_same_author:
        parts = [initial_text]
        for r in replies:
            reply_text = strip_bot_mention(r.get("content", ""))
            if reply_text:
                parts.append(reply_text)
        return "\n".join(parts)

    parts = [f"[{initial_author or 'Author'}]: {initial_text}"]
    for r in replies:
        reply_text = strip_bot_mention(r.get("content", ""))
        if reply_text:
            reply_author = r.get("author", {}).get("displayName", "Someone")
            parts.append(f"[{reply_author}]: {reply_text}")
    return "\n".join(parts)


async def scan_annotations(file_id: str) -> list[Annotation]:
    """Scan any Drive file for pending @anansibot comments.

    Works identically for Docs and Sheets -- `comments.list` is keyed on the
    file, so for a spreadsheet this returns comments across *every* tab in
    one call. There is no per-tab scan and none is needed.
    """
    drive_service = _get_drive_service()

    resp = await asyncio.to_thread(
        lambda: drive_service.comments()
        .list(
            fileId=file_id,
            fields="comments(id,content,resolved,quotedFileContent,createdTime,"
            "author(emailAddress,displayName),"
            "replies(content,author(emailAddress,displayName)))",
            includeDeleted=False,
        )
        .execute()
    )

    pending = [
        c
        for c in resp.get("comments", [])
        if not c.get("resolved") and BOT_MENTION in (c.get("content", "").lower())
    ]

    results = []
    for c in pending:
        results.append(
            Annotation(
                comment_id=c["id"],
                quoted_text=(c.get("quotedFileContent") or {}).get("value", ""),
                instruction=build_thread_instruction(
                    c, c.get("author", {}).get("displayName", "")
                ),
                author_email=c.get("author", {}).get("emailAddress", ""),
                created_time=c.get("createdTime", ""),
            )
        )
    return results


async def reply_and_resolve(file_id: str, comment_id: str, message: str) -> bool:
    """Reply to a comment and resolve it. Returns False on failure.

    Callers MUST NOT treat a write as complete when this returns False --
    an unresolved thread is the only signal a human has that the bot did
    not finish, and edit_section already enforces this ordering for Docs.
    """
    try:
        drive_service = _get_drive_service()
        await asyncio.to_thread(
            lambda: drive_service.replies()
            .create(
                fileId=file_id,
                commentId=comment_id,
                fields="id",
                body={"action": "resolve", "content": message[:200]},
            )
            .execute()
        )
        return True
    except Exception as e:
        LOGGER.warning(f"Could not resolve comment {comment_id} on {file_id}: {e}")
        return False


async def reply_without_resolving(file_id: str, comment_id: str, message: str) -> bool:
    """Reply to a comment but leave the thread open.

    Used for the failure paths -- stale quote, ambiguous match, no catalogue
    match. The human needs the explanation *and* needs the thread to stay
    open so they can see something is outstanding.
    """
    try:
        drive_service = _get_drive_service()
        await asyncio.to_thread(
            lambda: drive_service.replies()
            .create(
                fileId=file_id,
                commentId=comment_id,
                fields="id",
                body={"content": message[:200]},
            )
            .execute()
        )
        return True
    except Exception as e:
        LOGGER.warning(f"Could not reply to comment {comment_id} on {file_id}: {e}")
        return False


async def pin_revision(file_id: str) -> bool:
    """Pin the current revision before editing for rollback safety.

    Non-fatal: returns False rather than raising, because losing rollback
    safety is not a reason to refuse an edit the user asked for.
    """
    try:
        service = _get_drive_service()
        await asyncio.to_thread(
            lambda: service.revisions()
            .update(fileId=file_id, revisionId="head", body={"keepForever": True})
            .execute()
        )
        LOGGER.info(f"Pinned pre-edit revision for {file_id}")
        return True
    except Exception as e:
        LOGGER.warning(f"Could not pin revision for {file_id}: {e}")
        return False


async def get_file_mime_type(file_id: str) -> str:
    """Drive mimeType, used to dispatch between the Doc and Sheet paths."""
    service = _get_drive_service()
    meta = await asyncio.to_thread(
        lambda: service.files()
        .get(fileId=file_id, fields="mimeType", supportsAllDrives=True)
        .execute()
    )
    return str(meta.get("mimeType", ""))


MIME_DOC = "application/vnd.google-apps.document"
MIME_SHEET = "application/vnd.google-apps.spreadsheet"
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_file_annotations.py -q
```

Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add shared/utils/file_annotations.py
git add -f shared/tests/test_file_annotations.py
git commit -m "feat(shared): file-type-agnostic Drive annotation spine"
```

### Task 2.2: Point `doc_editing.py` at the shared spine

**Files:**
- Modify: `shared/utils/doc_editing.py`
- Test: `shared/tests/test_doc_editing_compat.py`

- [ ] **Step 1: Write the failing test**

```python
"""doc_editing keeps its public surface after the spine extraction.

Both mcp_servers/servers/knowledge_server/knowledge_mcp_server.py and
orchestrator/experts/handlers/doc_editor/process_doc_edits.py import these
names directly; moving the implementation must not move the imports.
"""

from shared.utils import doc_editing
from shared.utils.file_annotations import BOT_MENTION as SPINE_MENTION


def test_public_names_are_still_importable():
    for name in ("scan_comments", "edit_section", "pin_revision",
                 "get_comment_by_id", "generate_replacement_markdown"):
        assert hasattr(doc_editing, name), f"doc_editing.{name} disappeared"


def test_bot_mention_is_the_shared_one_not_a_second_copy():
    assert doc_editing.BOT_MENTION is SPINE_MENTION
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_doc_editing_compat.py -q
```

Expected: FAIL on `test_bot_mention_is_the_shared_one_not_a_second_copy` — `doc_editing` still defines its own `BOT_MENTION` string.

- [ ] **Step 3: Rewire `doc_editing.py`**

Delete `_get_drive_service`, `BOT_MENTION`, `_strip_bot_mention`,
`_build_thread_instruction`, `pin_revision` and `_resolve_comment` from
`doc_editing.py`. Replace the top-of-file imports with:

```python
from shared.utils.file_annotations import (
    BOT_MENTION,
    Annotation,
    _get_drive_service,
    build_thread_instruction,
    pin_revision,
    reply_and_resolve,
    scan_annotations,
    strip_bot_mention,
)
```

Rewrite `scan_comments` as a thin adapter that preserves its existing dict
return shape (the MCP server and `process_doc_edits` both index it by key):

```python
async def scan_comments(doc_id: str) -> list[dict]:
    """Docs-shaped view of scan_annotations, kept for existing callers."""
    return [
        {
            "comment_id": a.comment_id,
            "instruction": a.instruction,
            "highlighted_text": a.quoted_text,
            "author_email": a.author_email,
            "created_time": a.created_time,
        }
        for a in await scan_annotations(doc_id)
    ]
```

In `edit_section`, replace the `await _resolve_comment(...)` call with:

```python
    if comment_id and elements_written > 0:
        await reply_and_resolve(doc_id, comment_id, f"Done: {replacement_markdown[:200]}")
    elif comment_id:
        LOGGER.warning(f"Skipping comment resolution — 0 elements written for comment {comment_id}")
```

In `get_comment_by_id`, replace `_build_thread_instruction(...)` with
`build_thread_instruction(...)`.

- [ ] **Step 4: Run the compat test and the existing doc-editing suite**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_doc_editing_compat.py -q
cd chat_orchestrator && uv run pytest tests/ -q -k "doc_edit or process_doc"
```

Expected: PASS on both.

- [ ] **Step 5: Commit**

```bash
git add shared/utils/doc_editing.py
git add -f shared/tests/test_doc_editing_compat.py
git commit -m "refactor(shared): doc_editing delegates to the annotation spine"
```

---

# Phase 3 — Sheets locating and writing

### Task 3.1: The cross-tab cell locator

**Files:**
- Create: `shared/utils/sheet_editing.py`
- Test: `shared/tests/test_sheet_editing.py`

- [ ] **Step 1: Write the failing test**

```python
"""Cross-tab cell location by quoted comment text."""

import pytest

from shared.utils.sheet_editing import CellMatch, find_cells_in_grids, index_to_column_letter


GRIDS = {
    "Main Input": [
        ["Total kWp", "{{total_kwp}}"],
        ["Site Name", "{{site_name}}"],
        ["Note", "It&#39;s fine"],
    ],
    "Second": [
        ["{{total_kwp}}"],
    ],
}


def test_index_to_column_letter_handles_multi_letter_columns():
    assert index_to_column_letter(0) == "A"
    assert index_to_column_letter(25) == "Z"
    assert index_to_column_letter(26) == "AA"
    assert index_to_column_letter(27) == "AB"


def test_finds_a_unique_token_in_one_tab():
    matches = find_cells_in_grids(GRIDS, "{{site_name}}")
    assert matches == [CellMatch(tab="Main Input", a1="A2", row=2, column=1)]


def test_finds_a_repeated_token_across_tabs():
    matches = find_cells_in_grids(GRIDS, "{{total_kwp}}")
    assert len(matches) == 2
    assert {(m.tab, m.a1) for m in matches} == {("Main Input", "B1"), ("Second", "A1")}


def test_html_unescapes_before_matching():
    """Spike 0: quotedFileContent is served as text/html."""
    matches = find_cells_in_grids(GRIDS, "It&#39;s fine")
    assert len(matches) == 1
    assert matches[0].a1 == "B3"


def test_html_unescaped_needle_matches_escaped_cell():
    matches = find_cells_in_grids(GRIDS, "It's fine")
    assert len(matches) == 1
    assert matches[0].a1 == "B3"


def test_returns_empty_for_text_not_present():
    assert find_cells_in_grids(GRIDS, "nothing like this") == []


def test_returns_empty_for_empty_needle():
    """An empty cell quotes nothing — never match everything."""
    assert find_cells_in_grids(GRIDS, "") == []
    assert find_cells_in_grids(GRIDS, "   ") == []
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_sheet_editing.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'shared.utils.sheet_editing'`.

- [ ] **Step 3: Write the implementation**

```python
"""Google Sheets half of the annotation loop.

Locating: Spike 0 established that a Sheets comment's `anchor` field carries
an opaque numeric object ID ({"type":"workbook-range","uid":0,"range":
"361007030"}), NOT A1 notation -- there is no path from a comment to a cell
address through it. The only workable locator is to search every tab for the
comment's `quotedFileContent`, which Drive does populate for cell-anchored
comments, as `text/html`.

That has two consequences the caller must handle, both observed in real
production data during Spike 0:
  - an empty cell quotes nothing, so it is unlocatable (hence the
    "comment on a non-empty cell" contract)
  - a quote can go stale (cell edited after commenting) or match many
    cells at once -- so this module returns *all* matches and never guesses
"""

import asyncio
import html
import logging
from dataclasses import dataclass

from googleapiclient.discovery import build

from shared.utils.google_auth import get_sheets_credentials, get_sheets_write_credentials

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CellMatch:
    """One cell whose content equals a comment's quoted text."""

    tab: str
    a1: str
    row: int  # 1-based
    column: int  # 1-based


def index_to_column_letter(index: int) -> str:
    """0-based column index to spreadsheet column letter (0=A, 26=AA)."""
    result = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _normalise(value: str) -> str:
    """HTML-unescape and trim, so an escaped quote matches a plain cell."""
    return html.unescape(str(value)).strip()


def find_cells_in_grids(grids: dict[str, list[list]], quoted_text: str) -> list[CellMatch]:
    """Every cell across every tab whose normalised content equals quoted_text.

    Pure function over already-fetched grids so it is unit-testable without
    touching the Sheets API. Returns [] for an empty needle rather than
    matching every empty cell in the workbook.
    """
    needle = _normalise(quoted_text)
    if not needle:
        return []

    matches: list[CellMatch] = []
    for tab, rows in grids.items():
        for row_idx, row in enumerate(rows):
            for col_idx, cell in enumerate(row):
                if _normalise(cell) == needle:
                    matches.append(
                        CellMatch(
                            tab=tab,
                            a1=f"{index_to_column_letter(col_idx)}{row_idx + 1}",
                            row=row_idx + 1,
                            column=col_idx + 1,
                        )
                    )
    return matches


async def fetch_all_grids(sheet_id: str) -> dict[str, list[list]]:
    """Every tab's values, fetched once per run and reused for every comment."""
    creds = get_sheets_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    meta = await asyncio.to_thread(
        lambda: service.spreadsheets()
        .get(spreadsheetId=sheet_id, fields="sheets(properties(title))")
        .execute()
    )
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

    grids: dict[str, list[list]] = {}
    for tab in tabs:
        try:
            resp = await asyncio.to_thread(
                lambda t=tab: service.spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range=f"'{t}'")
                .execute()
            )
            grids[tab] = resp.get("values", [])
        except Exception as e:
            LOGGER.warning(f"Could not read tab {tab!r} of {sheet_id}: {e}")
    return grids


async def write_cells(sheet_id: str, writes: list[tuple[str, str, object]]) -> int:
    """Batch-write (tab, a1, value) triples. Returns the number of cells updated."""
    if not writes:
        return 0

    creds = get_sheets_write_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    data = [
        {"range": f"'{tab}'!{a1}", "values": [[value]]}
        for tab, a1, value in writes
    ]
    resp = await asyncio.to_thread(
        lambda: service.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        )
        .execute()
    )
    return int(resp.get("totalUpdatedCells", 0))


def offset_a1(match: CellMatch, columns: int) -> str:
    """A1 address `columns` to the right of a match. Used by the label form,
    which writes beside the matched label rather than into it."""
    return f"{index_to_column_letter(match.column - 1 + columns)}{match.row}"
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_sheet_editing.py -q
```

Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add shared/utils/sheet_editing.py
git add -f shared/tests/test_sheet_editing.py
git commit -m "feat(shared): cross-tab cell locator for Sheets annotations"
```

### Task 3.2: Record Spike 0's failure modes as fixtures

**Files:**
- Modify: `shared/tests/test_sheet_editing.py`

- [ ] **Step 1: Write the failing test**

The two real failure modes Spike 0 found, pinned so a refactor cannot quietly
reintroduce guessing:

```python
# Recorded from Spike 0 against NXT-3235 - Okpokunou Technical Review.
# Comment AAAB0jIG6Kc quoted text that had since been edited (73% similar);
# comment AAABnuBYGB4 quoted a value repeated in 14 cells.
STALE_QUOTE = (
    "HPS Hours were 18.6h, falling short of the 22h target and slightly down "
    "from 20.1h the previous month."
)
CURRENT_CELL = (
    "HPS Hours were 19.2h, falling short of the 22h target and slightly down "
    "from 20.2h the previous month."
)


def test_a_stale_quote_finds_nothing_rather_than_the_closest_cell():
    grids = {"2025 Review": [[CURRENT_CELL]]}
    assert find_cells_in_grids(grids, STALE_QUOTE) == []


def test_a_repeated_value_returns_every_match_not_the_first():
    grids = {"Meter Issues": [["To be checked"] for _ in range(14)]}
    matches = find_cells_in_grids(grids, "To be checked")
    assert len(matches) == 14
```

- [ ] **Step 2: Run it to verify it passes already**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_sheet_editing.py -q
```

Expected: PASS. These pin existing correct behaviour rather than driving new
code — the point is that a future "helpful" fuzzy-match change breaks them loudly.

- [ ] **Step 3: Commit**

```bash
git add -f shared/tests/test_sheet_editing.py
git commit -m "test(shared): pin Spike 0's stale-quote and repeated-value cases"
```

---

# Phase 4 — The value catalogue

### Task 4.1: Build the catalogue from step contracts

**Files:**
- Create: `chat_orchestrator/orchestrator/experts/output_catalogue.py`
- Test: `chat_orchestrator/tests/experts/test_output_catalogue.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
from orchestrator.experts.output_catalogue import CatalogueEntry, build_catalogue_from
from orchestrator.experts.step_contracts import OutputSpec, StepContract


def test_builds_entries_from_contracts_of_steps_that_ran():
    contracts = {
        "step_a": StepContract(
            description="a",
            outputs=(
                OutputSpec(name="energy.total_kwp", value_type="number", where="data",
                           description="Total installed solar peak capacity in kWp."),
            ),
        ),
        "step_b": StepContract(
            description="b",
            outputs=(
                OutputSpec(name="site.site_name", value_type="string", where="state",
                           description="Canonical site name."),
            ),
        ),
    }
    entries = build_catalogue_from(
        contracts=contracts,
        accumulated_results={"step_a": {"energy.total_kwp": 42.5}},
        packet_state={"site.site_name": "ExampleGrid"},
    )
    by_path = {e.path: e for e in entries}
    assert by_path["energy.total_kwp"].value == 42.5
    assert by_path["energy.total_kwp"].produced_by == "step_a"
    assert by_path["site.site_name"].value == "ExampleGrid"


def test_skips_steps_that_have_not_run():
    contracts = {
        "step_a": StepContract(
            description="a",
            outputs=(OutputSpec(name="x", where="data", description="An x."),),
        ),
    }
    assert build_catalogue_from(contracts, accumulated_results={}, packet_state={}) == []


def test_skips_declared_outputs_with_no_value_yet():
    contracts = {
        "step_a": StepContract(
            description="a",
            outputs=(
                OutputSpec(name="present", where="data", description="Here."),
                OutputSpec(name="absent", where="data", description="Not here."),
            ),
        ),
    }
    entries = build_catalogue_from(
        contracts, accumulated_results={"step_a": {"present": 1}}, packet_state={}
    )
    assert [e.path for e in entries] == ["present"]


def test_renders_a_prompt_block_with_descriptions():
    entries = [
        CatalogueEntry(path="energy.total_kwp", value=42.5, value_type="number",
                       description="Total installed solar peak capacity in kWp.",
                       produced_by="generate_site_bom"),
    ]
    from orchestrator.experts.output_catalogue import render_catalogue

    block = render_catalogue(entries)
    assert "energy.total_kwp" in block
    assert "Total installed solar peak capacity in kWp." in block
    assert "42.5" in block
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_output_catalogue.py -q -k catalogue
```

Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.experts.output_catalogue'`.

- [ ] **Step 3: Write the implementation**

```python
"""The value catalogue a comment's free text is matched against.

Replaces populate_cells.py's hand-written _collect_all_available_values()
dict. That function only knew about LPP's four producer steps; this walks
the registry instead, so any expert whose steps declare OutputSpecs gets a
catalogue for free -- which is what makes "supply any Doc or Sheet as a
template" work outside package_generator.

The LLM matches on `description`, not on `path`: a bare key name like
`energy.total_kwp` is not something a template author would write in a
comment, but "the total peak capacity" matches its description.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from orchestrator.experts.step_contracts import StepContract
from orchestrator.experts.step_registry import get_step_contract, get_step_registry

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogueEntry:
    """One resolvable value, with the prose an LLM matches a comment against."""

    path: str
    value: Any
    value_type: str
    description: str
    produced_by: str


def build_catalogue_from(
    contracts: Mapping[str, StepContract],
    accumulated_results: Mapping[str, Any],
    packet_state: Mapping[str, Any],
) -> list[CatalogueEntry]:
    """Pure core: catalogue for a given set of contracts and run state.

    Separated from `build_catalogue` so it is unit-testable without a
    populated global registry.
    """
    entries: list[CatalogueEntry] = []
    for step_name, contract in contracts.items():
        if step_name not in accumulated_results:
            continue  # step has not run; its outputs do not exist yet
        step_data = accumulated_results.get(step_name) or {}
        for spec in contract.outputs:
            if spec.where == "state":
                if spec.name not in packet_state:
                    continue
                value = packet_state[spec.name]
            else:
                if not isinstance(step_data, Mapping) or spec.name not in step_data:
                    continue
                value = step_data[spec.name]
            if value is None:
                continue
            entries.append(
                CatalogueEntry(
                    path=spec.name,
                    value=value,
                    value_type=spec.value_type,
                    description=spec.description,
                    produced_by=step_name,
                )
            )
    return entries


def build_catalogue(context) -> list[CatalogueEntry]:
    """Catalogue for a live StepContext, from the global handler registry."""
    registry = get_step_registry()
    contracts: dict[str, StepContract] = {}
    for name in registry.list_handlers():
        contract = get_step_contract(name)
        if contract is not None and contract.outputs:
            contracts[name] = contract
    return build_catalogue_from(
        contracts=contracts,
        accumulated_results=context.accumulated_results or {},
        packet_state=context.packet_state or {},
    )


def render_catalogue(entries: list[CatalogueEntry]) -> str:
    """The catalogue as a prompt block: path, type, description, current value."""
    lines = []
    for e in entries:
        value_repr = json.dumps(e.value, default=str)
        if len(value_repr) > 120:
            value_repr = value_repr[:117] + "..."
        lines.append(f"- {e.path} ({e.value_type}): {e.description} [current value: {value_repr}]")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_output_catalogue.py -q
```

Expected: PASS (all tests in the file, including Phase 1's).

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/output_catalogue.py
git add -f chat_orchestrator/tests/experts/test_output_catalogue.py
git commit -m "feat(experts): build the value catalogue from StepContract outputs"
```

### Task 4.2: The batch value-matching prompt

**Files:**
- Create: `shared/prompts/library/annotations.resolve_values.prompt`
- Modify: `chat_orchestrator/tests/prompt_checksums.json`
- Modify: `chat_orchestrator/tests/test_prompt_parity.py`

- [ ] **Step 1: Write the failing test**

Append to `chat_orchestrator/tests/test_prompt_misc.py`'s `IDS` list the new id
`annotations.resolve_values`, then add this test to that file:

```python
def test_annotations_prompt_renders_with_a_catalogue_and_requests():
    text = PROMPTS.text(
        "annotations.resolve_values",
        catalogue_block="- energy.total_kwp (number): Peak capacity. [current value: 42.5]",
        requests_block='1. "the total peak capacity"',
    )
    assert "energy.total_kwp" in text
    assert "the total peak capacity" in text
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_prompt_misc.py -q
```

Expected: FAIL — the prompt id does not exist.

- [ ] **Step 3: Write the prompt file**

```
---
id: annotations.resolve_values
description: Matches free-text template comments to entries in the run's value catalogue, in one batch call.
owner: eng
component: shared
overridable: true
model: fast
output: json
sections: []
variables: [catalogue_block, requests_block]
access:
  view: [ops, eng]
  edit: []
  publish: []
---
You are filling slots in a document template. Each numbered request below is a
comment someone left on a specific cell or section, saying what value belongs
there. Match each request to exactly one entry in the value catalogue.

VALUE CATALOGUE — the only values available. Match on the description.
{catalogue_block}

REQUESTS
{requests_block}

Rules:
- Match on meaning, not on wording. "the total peak capacity" matches an entry
  described as "Total installed solar peak capacity in kWp".
- If no catalogue entry plausibly answers a request, return null for its path.
  Do NOT invent a path, and do NOT pick a loosely related entry — a wrong value
  written into a document is far worse than an unfilled slot.
- Never invent a value. Values come only from the catalogue.

Return ONLY a JSON array, one object per request, in the same order:
[{{"request": 1, "path": "energy.total_kwp", "confidence": 0.95}},
 {{"request": 2, "path": null, "confidence": 0.0}}]
```

- [ ] **Step 4: Register it in the parity snapshot**

`test_prompt_parity.py`'s `test_snapshot_covers_every_prompt` fails on any prompt
missing from `prompt_checksums.json`, and `test_every_declared_variable_has_a_sample_value`
fails without sample vars. Add to `SAMPLE_VARS` in `test_prompt_parity.py`:

```python
    "annotations.resolve_values": {
        "catalogue_block": "- energy.total_kwp (number): Peak capacity. [current value: 42.5]",
        "requests_block": '1. "the total peak capacity"',
    },
```

Then regenerate the checksum entry — the file's own docstring says to review the
diff before committing:

```bash
cd chat_orchestrator && uv run pytest tests/test_prompt_parity.py -q
```

If it reports a missing snapshot entry, add the printed checksum for
`annotations.resolve_values` to `tests/prompt_checksums.json`. Do **not** delete and
regenerate the whole file — that would silently re-baseline every other prompt.

- [ ] **Step 5: Run both prompt suites**

```bash
cd chat_orchestrator && uv run pytest tests/test_prompt_parity.py ../shared/tests/test_prompt_misc.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add shared/prompts/library/annotations.resolve_values.prompt
git add chat_orchestrator/tests/prompt_checksums.json chat_orchestrator/tests/test_prompt_parity.py
git add -f shared/tests/test_prompt_misc.py
git commit -m "feat(prompts): batch value-matching prompt for template annotations"
```

---

# Phase 5 — Apps Script image targeting and sizing

### Task 5.1: `get_range_pixels` and targeted `replace_sheet_image`

**Files:**
- Modify: `scripts/anansi_helper.gs`

- [ ] **Step 1: Add the range-to-pixels helper**

`getColumnWidth(col)` and `getRowHeight(row)` are both documented and return
pixels, so a range converts to a box by summation. Add to `anansi_helper.gs`:

```javascript
/**
 * Pixel dimensions of an A1 range, for sizing an image to fit inside it.
 * Params: sheet_id, worksheet_name, range (A1, e.g. "B6:F20")
 */
function getRangePixels(params) {
  if (!params.sheet_id) { return { error: 'Missing sheet_id' }; }
  if (!params.worksheet_name) { return { error: 'Missing worksheet_name' }; }
  if (!params.range) { return { error: 'Missing range' }; }

  var sheet = SpreadsheetApp.openById(params.sheet_id)
                            .getSheetByName(params.worksheet_name);
  if (!sheet) { return { error: 'Worksheet not found: ' + params.worksheet_name }; }

  var r = sheet.getRange(params.range);
  var width = 0, height = 0;
  for (var c = r.getColumn(); c < r.getColumn() + r.getNumColumns(); c++) {
    width += sheet.getColumnWidth(c);
  }
  for (var w = r.getRow(); w < r.getRow() + r.getNumRows(); w++) {
    height += sheet.getRowHeight(w);
  }
  return {
    range: params.range, width: width, height: height,
    anchor_row: r.getRow(), anchor_column: r.getColumn()
  };
}

/**
 * Pixel dimensions of the merged range containing a cell, or null if the
 * cell is not merged. Layer 1 of the image sizing precedence.
 */
function getMergedRangeAt(params) {
  if (!params.sheet_id || !params.worksheet_name || !params.cell) {
    return { error: 'Missing sheet_id, worksheet_name or cell' };
  }
  var sheet = SpreadsheetApp.openById(params.sheet_id)
                            .getSheetByName(params.worksheet_name);
  if (!sheet) { return { error: 'Worksheet not found: ' + params.worksheet_name }; }

  var merges = sheet.getRange(params.cell).getMergedRanges();
  if (!merges || merges.length === 0) { return { merged: false }; }
  var m = merges[0];
  return { merged: true, range: m.getA1Notation() };
}
```

- [ ] **Step 2: Give `replaceSheetImage` an explicit target**

Inside `replaceSheetImage`, replace the height-heuristic target selection
block (currently `for (var i = 0; ...) if (images[i].getHeight() >= minHeight)`)
with target-aware selection that **keeps the heuristic as the default**, so the
existing LPP call site is unaffected:

```javascript
  // Find the image to replace.
  // Precedence: explicit anchor cell > alt text > height heuristic (legacy).
  var targetImage = null;
  var target = params.target || null;

  if (target) {
    for (var i = 0; i < images.length; i++) {
      var anchorA1 = images[i].getAnchorCell().getA1Notation();
      var alt = images[i].getAltTextTitle() || images[i].getAltTextDescription() || '';
      if (anchorA1 === target || alt === target) { targetImage = images[i]; break; }
    }
    if (!targetImage) {
      return { error: 'No image matched target: ' + target };
    }
  } else {
    for (var j = 0; j < images.length; j++) {
      if (images[j].getHeight() >= minHeight) { targetImage = images[j]; break; }
    }
  }
```

- [ ] **Step 3: Honour `fit_range` when sizing**

After the existing `oldHeight` / `oldWidth` capture, add:

```javascript
  // fit_range overrides the replaced image's own dimensions (sizing layers 1-2).
  if (params.fit_range) {
    var box = getRangePixels({
      sheet_id: sheetId, worksheet_name: worksheetName, range: params.fit_range
    });
    if (!box.error) {
      oldWidth = box.width;
      oldHeight = box.height;
      anchorCol = box.anchor_column;
      anchorRow = box.anchor_row;
      offsetX = 0;
      offsetY = 0;
    }
  }
```

Then change the scale calculation so the image fits *inside* the box on both
axes rather than matching height alone:

```javascript
    // Fit inside the box preserving aspect ratio (was: match height only).
    var scale = oldHeight / insertedHeight;
    if (oldWidth) {
      scale = Math.min(scale, oldWidth / insertedWidth);
    }
    var scaledWidth = Math.round(insertedWidth * scale);
    var scaledHeight = Math.round(insertedHeight * scale);
    newImage.setWidth(scaledWidth);
    newImage.setHeight(scaledHeight);
```

- [ ] **Step 4: Register the new actions**

```javascript
var ACTIONS = {
  'ping': pingAction,
  'replace_sheet_image': replaceSheetImage,
  'get_sheet_images': getSheetImages,
  'list_worksheets': listWorksheets,
  'write_doc_markdown': writeDocMarkdown,
  'get_range_pixels': getRangePixels,
  'get_merged_range_at': getMergedRangeAt
};
```

- [ ] **Step 5: Add a manual test function**

Following the file's existing `testPing` / `testReplaceSheetImage` pattern:

```javascript
function testGetRangePixels() {
  var result = getRangePixels({
    sheet_id: 'PASTE_A_TEST_SHEET_ID',
    worksheet_name: 'Sheet1',
    range: 'B6:F20'
  });
  Logger.log(JSON.stringify(result, null, 2));
}
```

- [ ] **Step 6: Commit**

```bash
git add scripts/anansi_helper.gs
git commit -m "feat(apps-script): targeted image replacement and range-to-pixel sizing"
```

**Note:** `scripts/*.py` and `scripts/*.sh` are gitignored but `*.gs` is not — verify with `git status` that the file was actually staged before committing.

### Task 5.2: Pass the new parameters through the Python client

**Files:**
- Modify: `shared/utils/apps_script_client.py`
- Test: `shared/tests/test_apps_script_image_params.py`

- [ ] **Step 1: Write the failing test**

The Apps Script side cannot be exercised from Python, so the contract under test
is the request payload the client builds:

```python
"""replace_sheet_image sends the target/fit_range params the .gs now reads."""

import pytest

from shared.utils import apps_script_client


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, params=None):
        self.calls.append((action, params or {}))
        return apps_script_client.AppsScriptResult(success=True, action=action, data={})


@pytest.mark.asyncio
async def test_target_and_fit_range_reach_the_action_payload(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(apps_script_client, "AnansiAppsScriptClient", lambda *a, **k: fake)

    await apps_script_client.replace_sheet_image(
        sheet_id="s1", worksheet_name="Main Input", image_base64="AAAA",
        target="{{site_map}}", fit_range="B6:F20",
    )

    action, params = fake.calls[0]
    assert action == "replace_sheet_image"
    assert params["target"] == "{{site_map}}"
    assert params["fit_range"] == "B6:F20"


@pytest.mark.asyncio
async def test_omitting_them_keeps_the_legacy_payload_shape(monkeypatch):
    """The LPP call site passes neither — its payload must not change."""
    fake = _FakeClient()
    monkeypatch.setattr(apps_script_client, "AnansiAppsScriptClient", lambda *a, **k: fake)

    await apps_script_client.replace_sheet_image(
        sheet_id="s1", worksheet_name="Proposed Budget",
        image_base64="AAAA", min_height_px=100,
    )

    _, params = fake.calls[0]
    assert "target" not in params
    assert "fit_range" not in params
    assert params["min_height"] == 100
```

Note the explicit `@pytest.mark.asyncio` — required for async tests under
`shared/tests/`, which can resolve the repo-root `pyproject.toml` (no
`asyncio_mode`) when targeted directly.

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_apps_script_image_params.py -q
```

Expected: FAIL — `replace_sheet_image()` takes no `target` argument.

- [ ] **Step 3: Extend the convenience function**

In `apps_script_client.py`, change `replace_sheet_image`'s signature and body so
the two new params are only included when supplied:

```python
async def replace_sheet_image(
    sheet_id: str,
    worksheet_name: str,
    image_base64: str,
    min_height_px: int = 100,
    target: Optional[str] = None,
    fit_range: Optional[str] = None,
) -> AppsScriptResult:
    """Replace an image in a worksheet.

    `target` selects the image by anchor cell (A1) or alt text; without it the
    legacy first-image-taller-than-min_height heuristic applies, which is what
    the LPP call site relies on. `fit_range` sizes the replacement to an A1
    range's pixel box instead of the replaced image's own dimensions.
    """
    client = AnansiAppsScriptClient()
    params: dict[str, Any] = {
        "sheet_id": sheet_id,
        "worksheet_name": worksheet_name,
        "image_base64": resize_image_for_sheets(image_base64),
        "min_height": min_height_px,
    }
    if target:
        params["target"] = target
    if fit_range:
        params["fit_range"] = fit_range
    return await client.call_action("replace_sheet_image", params)


async def get_range_pixels(sheet_id: str, worksheet_name: str, range_a1: str) -> AppsScriptResult:
    """Pixel width/height of an A1 range, for image sizing."""
    client = AnansiAppsScriptClient()
    return await client.call_action(
        "get_range_pixels",
        {"sheet_id": sheet_id, "worksheet_name": worksheet_name, "range": range_a1},
    )


async def get_merged_range_at(sheet_id: str, worksheet_name: str, cell: str) -> AppsScriptResult:
    """The merged range containing a cell, or {"merged": False}."""
    client = AnansiAppsScriptClient()
    return await client.call_action(
        "get_merged_range_at",
        {"sheet_id": sheet_id, "worksheet_name": worksheet_name, "cell": cell},
    )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_apps_script_image_params.py -q
```

Expected: PASS (2 tests).

- [ ] **Step 5: Confirm the LPP call site still type-checks**

```bash
cd chat_orchestrator && uv run pytest tests/experts/ -q -k "populate or lpp"
```

Expected: PASS — `populate_cells.py` calls `replace_sheet_image(sheet_id=..., worksheet_name=..., image_base64=..., min_height_px=100)`, all still valid.

- [ ] **Step 6: Commit**

```bash
git add shared/utils/apps_script_client.py
git add -f shared/tests/test_apps_script_image_params.py
git commit -m "feat(shared): target and fit_range params for replace_sheet_image"
```

---

# Phase 6 — The three generic steps

### Task 6.1: `create_from_template`

**Files:**
- Create: `chat_orchestrator/orchestrator/experts/handlers/templates/__init__.py`
- Create: `chat_orchestrator/orchestrator/experts/handlers/templates/create_from_template.py`
- Modify: `chat_orchestrator/orchestrator/experts/handlers/__init__.py`
- Test: `chat_orchestrator/tests/experts/test_create_from_template.py`

- [ ] **Step 1: Write the failing test**

```python
"""Generic template copy: no site validation, naming from the template's filename."""

import pytest

from orchestrator.experts.step_registry import get_step_contract


def test_contract_declares_template_and_folder_as_parameters():
    contract = get_step_contract("create_from_template")
    assert contract is not None
    names = {p.name for p in contract.params}
    assert {"template_id", "output_folder_id"} <= names


def test_contract_declares_the_document_id_it_produces():
    contract = get_step_contract("create_from_template")
    assert "document_id" in {o.name for o in contract.outputs}
    assert contract.mutates is True
    assert contract.mutation_kind == "external_write"


def test_mock_populates_document_id_so_downstream_steps_survive():
    """A mock returning nothing collapses the next step's precondition."""
    contract = get_step_contract("create_from_template")
    assert contract.mock is not None
    assert contract.mock.state_updates.get("document_id")


@pytest.mark.asyncio
async def test_accepts_a_pasted_url_as_well_as_a_bare_id(monkeypatch):
    from orchestrator.experts.handlers.templates import create_from_template as mod

    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)

        class R:
            success = True
            document_id = "new-doc"
            document_url = "https://docs.google.com/spreadsheets/d/new-doc"
            final_title = "ExampleGrid Site Package"
            template_type = "spreadsheet"
            error_message = None

        return R()

    monkeypatch.setattr(mod, "create_from_template_file", fake_create)

    class Ctx:
        packet_state = {}
        accumulated_results = {}

        def get_parameter_value(self, name, default=None):
            return {
                "template_id": "https://docs.google.com/spreadsheets/d/TPL123/edit#gid=0",
                "output_folder_id": "FOLDER1",
            }.get(name, default)

        def get_input(self, key, default=None):
            return default

        def get_state(self, key, default=None):
            return default

        async def send_progress_to_user(self, *_a, **_k):
            return None

    result = await mod.create_from_template(Ctx())
    assert captured["template_id"] == "TPL123"
    assert result.state_updates["document_id"] == "new-doc"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_create_from_template.py -q
```

Expected: FAIL — the step is not registered.

- [ ] **Step 3: Write the implementation**

`chat_orchestrator/orchestrator/experts/handlers/templates/__init__.py`:

```python
"""Generic, expert-agnostic document and template step handlers."""

from orchestrator.experts.handlers.templates.create_from_template import create_from_template

__all__ = ["create_from_template"]
```

`create_from_template.py`:

```python
"""Create a new Drive file from any Doc or Sheet template.

The generic half of copy_lpp_template. What it deliberately does NOT do is
copy_lpp_template's site_submissions validation -- that check is what makes
that step LPP-only, and it stays there.

Naming comes from the template's own filename: GoogleTemplateCreator
substitutes [placeholder] tokens in the copied title, so a template named
"[site_name] Site Package - [date]" names every copy of itself. That is the
whole naming convention -- there is no separate pattern to configure.
"""

import re
from datetime import datetime

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import MockSpec, OutputSpec, ParamSpec, StepContract
from orchestrator.experts.step_registry import register_step
from shared.utils.gdrive_template_creator import create_from_template as create_from_template_file
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Same two URL shapes copy_template.py normalises -- a "/d/<id>" path segment
# or an "id=<id>" query parameter. Duplicated rather than imported because
# this module must not depend on package_generator, which is the LPP-specific
# package this step exists to be independent of.
_DRIVE_URL_ID_PATTERNS = (
    re.compile(r"/d/([a-zA-Z0-9_-]{10,})"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]{10,})"),
)


def extract_drive_file_id(value: str) -> str:
    """Normalize a full Drive URL or a bare file ID to a bare file ID."""
    value = (value or "").strip()
    for pattern in _DRIVE_URL_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return value


@register_step(
    "create_from_template",
    contract=StepContract(
        description=(
            "Copies any Google Docs or Sheets template into a target folder, naming "
            "the copy from the template's own filename placeholders."
        ),
        params=(
            ParamSpec(
                name="template_id",
                param_type="string",
                description=(
                    "The template to copy -- a full doc link (paste the URL from the "
                    "browser address bar) or a bare Drive file ID both work."
                ),
                synonyms=("template", "template_url", "template_link"),
                required=True,
            ),
            ParamSpec(
                name="output_folder_id",
                param_type="string",
                description=(
                    "Drive folder the copy is created in -- a folder link or a bare "
                    "folder ID. Defaults to the folder the template lives in."
                ),
                synonyms=("folder", "output_folder", "destination_folder"),
                required=False,
            ),
            ParamSpec(
                name="name_variables",
                param_type="object",
                description=(
                    "Values for [placeholder] tokens in the template's filename, e.g. "
                    '{"site_name": "ExampleGrid"}. A "date" token is filled automatically.'
                ),
                required=False,
            ),
        ),
        optional_consumes_state=("site_name", "site_folder_id", "document_id"),
        produces_state=("document_id", "document_url", "document_title"),
        outputs=(
            OutputSpec(
                name="document_id",
                value_type="string",
                description="Drive file ID of the newly created document.",
            ),
            OutputSpec(
                name="document_url",
                value_type="string",
                description="Shareable URL of the newly created document.",
            ),
            OutputSpec(
                name="document_title",
                value_type="string",
                description="Final title of the copy, after placeholder substitution.",
            ),
        ),
        side_effects="Copies a Google Drive template file into a target folder.",
        mutates=True,
        mutation_kind="external_write",
        mock=MockSpec(
            state_updates={
                "document_id": "MOCK-document-id",
                "document_url": "https://docs.google.com/document/d/MOCK-document-id",
                "document_title": "MOCK Document From Template",
            },
            data={
                "document_id": "MOCK-document-id",
                "document_url": "https://docs.google.com/document/d/MOCK-document-id",
                "document_title": "MOCK Document From Template",
            },
            message="Would have copied the template into a new document.",
        ),
    ),
)
async def create_from_template(context: StepContext) -> StepResult:
    """Copy a template file and return the new document's id, url and title."""
    template_ref = context.get_parameter_value("template_id")
    if not template_ref:
        return StepResult.soft_failure(
            code="missing_parameter",
            message="No template was given, so there is nothing to copy.",
            remediation=(
                "Call this step again with template_id set to the template's "
                "Google Docs/Sheets link, or its Drive file ID."
            ),
        )
    template_id = extract_drive_file_id(str(template_ref))

    folder_ref = context.get_parameter_value("output_folder_id") or context.get_state(
        "site_folder_id"
    )
    output_folder_id = extract_drive_file_id(str(folder_ref)) if folder_ref else ""

    variables = dict(context.get_parameter_value("name_variables") or {})
    variables.setdefault("date", datetime.now().strftime("%Y%m%d-%H%M"))
    site_name = context.get_state("site_name")
    if site_name:
        variables.setdefault("site_name", site_name)

    await context.send_progress_to_user("Creating a new document from the template...")

    try:
        result = await create_from_template_file(
            template_id=template_id,
            output_folder_id=output_folder_id,
            variables=variables,
            register_with_doc_tracker=True,
        )
    except Exception as e:
        LOGGER.exception(f"Error creating document from template {template_id}: {e}")
        return StepResult.failure(f"Error creating document: {str(e)}")

    if not result.success:
        return StepResult.failure(f"Failed to create document: {result.error_message}")

    LOGGER.info(f"Created {result.final_title} ({result.document_id}) from {template_id}")
    return StepResult(
        data={
            "document_id": result.document_id,
            "document_url": result.document_url,
            "document_title": result.final_title,
            "template_type": result.template_type,
        },
        state_updates={
            "document_id": result.document_id,
            "document_url": result.document_url,
            "document_title": result.final_title,
        },
        progress_message=f"Created: {result.final_title}",
    )
```

Register the package in `chat_orchestrator/orchestrator/experts/handlers/__init__.py`
by adding `templates,` to its import list and `"templates",` to its `__all__`,
following the existing `doc_editor` entries at lines 20 and 32.

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_create_from_template.py -q
```

Expected: PASS (4 tests).

- [ ] **Step 5: Check the contract lint suite still passes**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_contract_lint.py -q
```

Expected: PASS — this suite enforces repo-wide contract rules on every registered step.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/templates/
git add chat_orchestrator/orchestrator/experts/handlers/__init__.py
git add -f chat_orchestrator/tests/experts/test_create_from_template.py
git commit -m "feat(experts): generic create_from_template step"
```

### Task 6.2: `fill_annotations`

**Files:**
- Create: `chat_orchestrator/orchestrator/experts/handlers/templates/fill_annotations.py`
- Test: `chat_orchestrator/tests/experts/test_fill_annotations.py`

- [ ] **Step 1: Write the failing test**

```python
"""Comment-driven filling: resolution, failure paths, and the audit reply."""

import pytest

from orchestrator.experts.output_catalogue import CatalogueEntry
from orchestrator.experts.step_registry import get_step_contract


def test_contract_marks_it_mutating_with_a_mock():
    contract = get_step_contract("fill_annotations")
    assert contract is not None
    assert contract.mutates is True
    assert contract.mock is not None


def test_contract_exposes_file_id_and_dry_run():
    contract = get_step_contract("fill_annotations")
    names = {p.name for p in contract.params}
    assert {"file_id", "dry_run"} <= names


CATALOGUE = [
    CatalogueEntry(path="energy.total_kwp", value=42.5, value_type="number",
                   description="Total installed solar peak capacity in kWp.",
                   produced_by="generate_site_bom"),
    CatalogueEntry(path="site.site_name", value="ExampleGrid", value_type="string",
                   description="Canonical site name.", produced_by="resolve_sites"),
]


def test_plan_writes_pairs_each_match_to_its_cell():
    from orchestrator.experts.handlers.templates.fill_annotations import plan_writes
    from shared.utils.sheet_editing import CellMatch

    plan = plan_writes(
        resolutions=[{"request": 1, "path": "energy.total_kwp", "confidence": 0.95}],
        matches_by_request={1: [CellMatch(tab="Main Input", a1="B1", row=1, column=2)]},
        catalogue=CATALOGUE,
    )
    assert plan.writes == [("Main Input", "B1", 42.5)]
    assert plan.replies[0].startswith("Done: energy.total_kwp = 42.5")


def test_a_null_path_produces_a_question_not_a_write():
    from orchestrator.experts.handlers.templates.fill_annotations import plan_writes
    from shared.utils.sheet_editing import CellMatch

    plan = plan_writes(
        resolutions=[{"request": 1, "path": None, "confidence": 0.0}],
        matches_by_request={1: [CellMatch(tab="Main Input", a1="B1", row=1, column=2)]},
        catalogue=CATALOGUE,
    )
    assert plan.writes == []
    assert plan.unresolved and "no value" in plan.unresolved[0][1].lower()


def test_a_token_matching_many_cells_fills_all_of_them():
    from orchestrator.experts.handlers.templates.fill_annotations import plan_writes
    from shared.utils.sheet_editing import CellMatch

    plan = plan_writes(
        resolutions=[{"request": 1, "path": "energy.total_kwp", "confidence": 0.9}],
        matches_by_request={1: [
            CellMatch(tab="Main Input", a1="B1", row=1, column=2),
            CellMatch(tab="Second", a1="A1", row=1, column=1),
        ]},
        catalogue=CATALOGUE,
    )
    assert len(plan.writes) == 2


def test_no_cell_match_leaves_the_thread_open_with_an_explanation():
    from orchestrator.experts.handlers.templates.fill_annotations import plan_writes

    plan = plan_writes(
        resolutions=[{"request": 1, "path": "energy.total_kwp", "confidence": 0.9}],
        matches_by_request={1: []},
        catalogue=CATALOGUE,
    )
    assert plan.writes == []
    assert "no longer appears" in plan.unresolved[0][1]
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_fill_annotations.py -q
```

Expected: FAIL — the step is not registered and `plan_writes` does not exist.

- [ ] **Step 3: Write the implementation**

```python
"""Fill a Doc or Sheet from its @anansi-chatbot comments.

The generic replacement for populate_lpp_cells' cell-filling half. Where that
step reads a ## Cell Mapping table out of an expert's Google Doc, this reads
the target file's own comments and matches them against the run's value
catalogue.

All comments resolve in ONE LLM call (see annotations.resolve_values). That is
what lets a forty-slot template fill cheaply, and it is why MAX_EDITS_PER_RUN
-- a cap on per-comment *generative* rewriting in process_doc_edits -- does
not apply here.

Nothing is ever guessed. A comment that cannot be matched to a catalogue entry,
or whose quoted text no longer appears in the file, gets a reply explaining why
and its thread is left OPEN, so the human can see there is something
outstanding.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from orchestrator.experts.output_catalogue import (
    CatalogueEntry,
    build_catalogue,
    render_catalogue,
)
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import MockSpec, OutputSpec, ParamSpec, StepContract
from orchestrator.experts.step_registry import register_step
from shared.prompts import PROMPTS
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@dataclass
class FillPlan:
    """What a run intends to do, before it does any of it.

    Separated from execution so `dry_run` can return exactly what a real run
    would write, and so the resolution logic is unit-testable without Drive.
    """

    writes: list[tuple[str, str, Any]] = field(default_factory=list)  # (tab, a1, value)
    replies: list[str] = field(default_factory=list)  # audit message per write
    reply_comment_ids: list[str] = field(default_factory=list)
    unresolved: list[tuple[str, str]] = field(default_factory=list)  # (comment_id, why)


def plan_writes(
    resolutions: list[dict],
    matches_by_request: dict[int, list],
    catalogue: list[CatalogueEntry],
    comment_ids_by_request: Optional[dict[int, str]] = None,
) -> FillPlan:
    """Turn LLM resolutions plus cell matches into a concrete write plan."""
    comment_ids_by_request = comment_ids_by_request or {}
    by_path = {e.path: e for e in catalogue}
    plan = FillPlan()

    for resolution in resolutions:
        request_no = int(resolution.get("request", 0))
        comment_id = comment_ids_by_request.get(request_no, str(request_no))
        path = resolution.get("path")
        matches = matches_by_request.get(request_no, [])

        if not path or path not in by_path:
            plan.unresolved.append(
                (
                    comment_id,
                    "I could not find a value for this in the data available from "
                    "this run. Could you say which figure you mean?",
                )
            )
            continue

        if not matches:
            plan.unresolved.append(
                (
                    comment_id,
                    "The text this comment quotes no longer appears in the file — "
                    "it may have been edited since the comment was made.",
                )
            )
            continue

        entry = by_path[path]
        for match in matches:
            plan.writes.append((match.tab, match.a1, entry.value))
        plan.replies.append(f"Done: {entry.path} = {entry.value}")
        plan.reply_comment_ids.append(comment_id)

    return plan


async def _resolve_against_catalogue(
    instructions: list[str], catalogue: list[CatalogueEntry]
) -> list[dict]:
    """One LLM call for every comment in the file."""
    from orchestrator.config.settings import get_settings
    from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway

    settings = get_settings()
    gateway = get_default_generation_gateway(default_model=settings.gemini.model)

    requests_block = "\n".join(
        f'{i + 1}. "{text}"' for i, text in enumerate(instructions)
    )
    prompt = PROMPTS.text(
        "annotations.resolve_values",
        catalogue_block=render_catalogue(catalogue),
        requests_block=requests_block,
    )

    response = await gateway.generate(
        [LLMMessage(role="user", text=prompt)],
        GenerationOptions(model=settings.gemini.model, temperature=0.1, max_output_tokens=2000),
    )

    text = str(response.text).strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, ValueError) as e:
        LOGGER.warning(f"Could not parse value resolutions: {e}")
        return []


@register_step(
    "fill_annotations",
    contract=StepContract(
        description=(
            "Fills a Google Doc or Sheet from its own @anansi-chatbot comments, "
            "matching each comment against the values this run has produced."
        ),
        params=(
            ParamSpec(
                name="file_id",
                param_type="string",
                description=(
                    "The document or spreadsheet to fill -- a link or a bare Drive "
                    "file ID. Defaults to the document this run just created."
                ),
                synonyms=("document_id", "sheet_id", "spreadsheet_id", "file"),
                required=False,
            ),
            ParamSpec(
                name="dry_run",
                param_type="boolean",
                description=(
                    "Report what would be filled in, without writing anything or "
                    "resolving any comment. Use this to confirm before writing."
                ),
                required=False,
                default=False,
            ),
        ),
        optional_consumes_state=("document_id", "annotations_filled"),
        produces_state=("annotations_filled",),
        outputs=(
            OutputSpec(
                name="cells_filled",
                value_type="integer",
                where="data",
                description="Number of cells or sections written.",
            ),
        ),
        guard_keys=("annotations_filled",),
        side_effects=(
            "Writes values into a Google Doc or Sheet and resolves the comments "
            "that requested them."
        ),
        mutates=True,
        mutation_kind="external_write",
        mock=MockSpec(
            state_updates={"annotations_filled": True},
            data={"cells_filled": 0},
            message="Would have filled the template's annotated slots.",
        ),
    ),
)
async def fill_annotations(context: StepContext) -> StepResult:
    """Scan a file's bot comments, resolve them, write the values, resolve threads."""
    from shared.utils.file_annotations import (
        MIME_SHEET,
        get_file_mime_type,
        pin_revision,
        reply_and_resolve,
        reply_without_resolving,
        scan_annotations,
    )
    from shared.utils.sheet_editing import fetch_all_grids, find_cells_in_grids, write_cells

    file_ref = context.get_parameter_value("file_id") or context.get_state("document_id")
    if not file_ref:
        return StepResult.soft_failure(
            code="unmet_prerequisite",
            message="No file was given and this run has not created one yet.",
            remediation="Call create_from_template first, or pass file_id explicitly.",
        )

    from orchestrator.experts.handlers.templates.create_from_template import (
        extract_drive_file_id,
    )

    file_id = extract_drive_file_id(str(file_ref))
    dry_run = bool(context.get_parameter_value("dry_run") or False)

    mime = await get_file_mime_type(file_id)
    if mime != MIME_SHEET:
        return StepResult.soft_failure(
            code="invalid_parameter",
            message=(
                "fill_annotations currently fills spreadsheets only; this file is "
                f"a {mime or 'unknown type'}."
            ),
            remediation="Use process_doc_edits for Google Docs.",
        )

    await context.send_progress_to_user("Checking the template for @anansi-chatbot comments...")

    annotations = await scan_annotations(file_id)
    if not annotations:
        return StepResult(
            data={"cells_filled": 0},
            state_updates={"annotations_filled": True},
            progress_message="No pending @anansi-chatbot comments in that file.",
        )

    catalogue = build_catalogue(context)
    if not catalogue:
        return StepResult.soft_failure(
            code="unmet_prerequisite",
            message="No values are available from this run yet, so nothing can be filled in.",
            remediation=(
                "Run the steps that produce the data first — the catalogue is built "
                "from the outputs steps have already returned."
            ),
        )

    grids = await fetch_all_grids(file_id)

    matches_by_request: dict[int, list] = {}
    comment_ids_by_request: dict[int, str] = {}
    instructions: list[str] = []
    for i, ann in enumerate(annotations, start=1):
        instructions.append(ann.instruction)
        comment_ids_by_request[i] = ann.comment_id
        matches_by_request[i] = find_cells_in_grids(grids, ann.quoted_text)

    resolutions = await _resolve_against_catalogue(instructions, catalogue)
    plan = plan_writes(resolutions, matches_by_request, catalogue, comment_ids_by_request)

    if dry_run:
        return StepResult(
            data={
                "cells_filled": 0,
                "dry_run": True,
                "planned_writes": [
                    {"tab": t, "cell": a, "value": v} for t, a, v in plan.writes
                ],
                "unresolved": [{"comment_id": c, "reason": r} for c, r in plan.unresolved],
            },
            progress_message=(
                f"Would fill {len(plan.writes)} cell(s); "
                f"{len(plan.unresolved)} comment(s) need a human."
            ),
        )

    await pin_revision(file_id)
    cells_written = await write_cells(file_id, plan.writes)

    # Resolve threads only AFTER confirming the write landed -- an unresolved
    # thread is the only signal a human gets that the bot did not finish.
    if cells_written:
        for comment_id, message in zip(plan.reply_comment_ids, plan.replies):
            await reply_and_resolve(file_id, comment_id, message)

    for comment_id, reason in plan.unresolved:
        await reply_without_resolving(file_id, comment_id, reason)

    return StepResult(
        data={
            "cells_filled": cells_written,
            "unresolved": [{"comment_id": c, "reason": r} for c, r in plan.unresolved],
        },
        state_updates={"annotations_filled": True},
        progress_message=(
            f"Filled {cells_written} cell(s)"
            + (f", {len(plan.unresolved)} comment(s) need a human" if plan.unresolved else "")
        ),
    )
```

Remove the stray duplicate import (`MockSpec as _M`) if your editor did not — it
is not used.

Add `from orchestrator.experts.handlers.templates.fill_annotations import fill_annotations`
and `"fill_annotations"` to the `templates/__init__.py` exports.

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_fill_annotations.py -q
```

Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/templates/
git add -f chat_orchestrator/tests/experts/test_fill_annotations.py
git commit -m "feat(experts): comment-driven fill_annotations step"
```

### Task 6.2b: The label fallback writes beside the label, not into it

The spec's degraded path: a comment on a label cell (`Total kWp` in column A)
fills the cell *beside* it, not the label itself. A comment on a `{{token}}`
fills the token's own cell. Without this, `offset_a1` is dead code and
commenting on a label would overwrite the label.

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/handlers/templates/fill_annotations.py`
- Test: `chat_orchestrator/tests/experts/test_fill_annotations.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_a_token_fills_its_own_cell():
    from orchestrator.experts.handlers.templates.fill_annotations import is_placeholder_token

    assert is_placeholder_token("{{total_kwp}}") is True
    assert is_placeholder_token("  {{site_map}}  ") is True
    assert is_placeholder_token("Total kWp") is False
    assert is_placeholder_token("") is False


def test_a_label_fills_the_cell_to_its_right():
    from orchestrator.experts.handlers.templates.fill_annotations import plan_writes
    from shared.utils.sheet_editing import CellMatch

    plan = plan_writes(
        resolutions=[{"request": 1, "path": "energy.total_kwp", "confidence": 0.9}],
        matches_by_request={1: [CellMatch(tab="Main Input", a1="A1", row=1, column=1)]},
        catalogue=CATALOGUE,
        quoted_by_request={1: "Total kWp"},
    )
    assert plan.writes == [("Main Input", "B1", 42.5)]


def test_a_token_match_is_not_offset():
    from orchestrator.experts.handlers.templates.fill_annotations import plan_writes
    from shared.utils.sheet_editing import CellMatch

    plan = plan_writes(
        resolutions=[{"request": 1, "path": "energy.total_kwp", "confidence": 0.9}],
        matches_by_request={1: [CellMatch(tab="Main Input", a1="B1", row=1, column=2)]},
        catalogue=CATALOGUE,
        quoted_by_request={1: "{{total_kwp}}"},
    )
    assert plan.writes == [("Main Input", "B1", 42.5)]
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_fill_annotations.py -q -k "label or token"
```

Expected: FAIL — `is_placeholder_token` does not exist and `plan_writes` takes no `quoted_by_request`.

- [ ] **Step 3: Add token detection and the offset**

Add to `fill_annotations.py`:

```python
import re

_TOKEN_RE = re.compile(r"^\{\{[^{}]+\}\}$")


def is_placeholder_token(quoted_text: str) -> bool:
    """Whether a comment's quoted text is a {{token}} rather than a label.

    A token is filled in place; a label is filled in the cell beside it. This
    is the whole difference between the primary and degraded paths, and it is
    decided by what the template author typed, not by configuration.
    """
    return bool(_TOKEN_RE.match((quoted_text or "").strip()))
```

Change `plan_writes`' signature to accept `quoted_by_request` and offset when
the quote is not a token:

```python
def plan_writes(
    resolutions: list[dict],
    matches_by_request: dict[int, list],
    catalogue: list[CatalogueEntry],
    comment_ids_by_request: Optional[dict[int, str]] = None,
    quoted_by_request: Optional[dict[int, str]] = None,
) -> FillPlan:
```

and inside the per-match loop, replace `plan.writes.append((match.tab, match.a1, entry.value))` with:

```python
        quoted = (quoted_by_request or {}).get(request_no, "")
        # A token is the slot itself; a label names the slot beside it.
        fill_in_place = is_placeholder_token(quoted) if quoted else True
        for match in matches:
            target_a1 = match.a1 if fill_in_place else offset_a1(match, 1)
            plan.writes.append((match.tab, target_a1, entry.value))
```

Import `offset_a1` at the top:

```python
from shared.utils.sheet_editing import offset_a1
```

In the `fill_annotations` handler, build and pass the new map alongside the
existing ones:

```python
    quoted_by_request: dict[int, str] = {}
    for i, ann in enumerate(annotations, start=1):
        ...
        quoted_by_request[i] = ann.quoted_text

    plan = plan_writes(
        resolutions, matches_by_request, catalogue, comment_ids_by_request, quoted_by_request
    )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_fill_annotations.py -q
```

Expected: PASS (9 tests — the six from Task 6.2 plus these three).

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/templates/fill_annotations.py
git add -f chat_orchestrator/tests/experts/test_fill_annotations.py
git commit -m "feat(experts): label comments fill the cell beside the label"
```

### Task 6.3: `replace_file_image`

**Files:**
- Create: `chat_orchestrator/orchestrator/experts/handlers/templates/replace_file_image.py`
- Test: `chat_orchestrator/tests/experts/test_replace_file_image.py`

- [ ] **Step 1: Write the failing test**

```python
"""Generic image replacement, targeted by token or alt text."""

import pytest

from orchestrator.experts.step_registry import get_step_contract


def test_contract_exposes_file_target_and_image_source():
    contract = get_step_contract("replace_file_image")
    assert contract is not None
    names = {p.name for p in contract.params}
    assert {"file_id", "target", "worksheet_name"} <= names
    assert contract.mutates is True


def test_sizing_precedence_prefers_a_merged_range_over_a_fit_range():
    from orchestrator.experts.handlers.templates.replace_file_image import choose_fit_range

    assert choose_fit_range(merged_range="B6:F20", comment_fit_range="C1:D2") == "B6:F20"


def test_sizing_precedence_falls_back_to_the_comment_range():
    from orchestrator.experts.handlers.templates.replace_file_image import choose_fit_range

    assert choose_fit_range(merged_range=None, comment_fit_range="C1:D2") == "C1:D2"


def test_sizing_precedence_returns_none_when_neither_is_given():
    """None means: let Apps Script use the replaced image's own dimensions."""
    from orchestrator.experts.handlers.templates.replace_file_image import choose_fit_range

    assert choose_fit_range(merged_range=None, comment_fit_range=None) is None


def test_parses_a_fit_range_out_of_comment_text():
    from orchestrator.experts.handlers.templates.replace_file_image import parse_fit_range

    assert parse_fit_range("@anansi-chatbot {{site_map}} fit B6:F20") == "B6:F20"
    assert parse_fit_range("@anansi-chatbot {{site_map}}") is None
    assert parse_fit_range("fit A1:A1 please") == "A1:A1"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_replace_file_image.py -q
```

Expected: FAIL — the module does not exist.

- [ ] **Step 3: Write the implementation**

```python
"""Replace an image in a Sheet, targeted by token or alt text.

The generic form of populate_lpp_cells' map-image swap. Two things make it
reusable where that one was not: the image is named rather than found by a
height heuristic, and its size comes from the sheet's own geometry.

Sizing precedence (spec section "Image slots and sizing"):
  1. merged range containing the token cell
  2. an explicit "fit B6:F20" in the comment
  3. the replaced image's own dimensions   <- Apps Script default when fit_range is None
  4. the min_height heuristic              <- Apps Script default when target is None too
"""

import re
from typing import Optional

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import MockSpec, ParamSpec, StepContract
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

_FIT_RANGE_RE = re.compile(r"\bfit\s+([A-Z]{1,3}\d+:[A-Z]{1,3}\d+)\b", re.IGNORECASE)


def parse_fit_range(comment_text: str) -> Optional[str]:
    """Pull an explicit 'fit B6:F20' out of a comment, if present."""
    match = _FIT_RANGE_RE.search(comment_text or "")
    return match.group(1).upper() if match else None


def choose_fit_range(
    merged_range: Optional[str], comment_fit_range: Optional[str]
) -> Optional[str]:
    """Sizing layers 1 and 2. None means 'let the replaced image's size stand'."""
    return merged_range or comment_fit_range


@register_step(
    "replace_file_image",
    contract=StepContract(
        description=(
            "Replaces an image in a Google Sheet, selected by a placeholder token in "
            "its anchor cell or by its alt text, and sized to fit its slot."
        ),
        params=(
            ParamSpec(
                name="file_id",
                param_type="string",
                description="The spreadsheet holding the image -- a link or a bare Drive file ID.",
                synonyms=("sheet_id", "spreadsheet_id", "document_id"),
                required=False,
            ),
            ParamSpec(
                name="worksheet_name",
                param_type="string",
                description="Name of the tab holding the image.",
                synonyms=("tab", "sheet_name", "worksheet"),
                required=True,
            ),
            ParamSpec(
                name="target",
                param_type="string",
                description=(
                    "Which image to replace: the placeholder token in its anchor cell "
                    "(e.g. {{site_map}}), the anchor cell's A1 address, or its alt text. "
                    "Omit to replace the first large image, as the LPP flow does."
                ),
                synonyms=("image", "slot", "placeholder"),
                required=False,
            ),
            ParamSpec(
                name="fit_range",
                param_type="string",
                description="A1 range the image should be sized to fit inside, e.g. B6:F20.",
                required=False,
            ),
            ParamSpec(
                name="image_base64",
                param_type="string",
                description=(
                    "The replacement image, base64-encoded. Omit to use the map "
                    "produced earlier in this run."
                ),
                required=False,
            ),
        ),
        optional_consumes_state=("document_id",),
        consumes_results=("generate_distribution_map",),
        produces_state=("image_replaced",),
        side_effects="Replaces an image in a Google Sheet via the Apps Script bridge.",
        mutates=True,
        mutation_kind="external_write",
        mock=MockSpec(
            state_updates={"image_replaced": True},
            data={"image_replaced": True},
            message="Would have replaced the image in the target sheet.",
        ),
    ),
)
async def replace_file_image(context: StepContext) -> StepResult:
    """Replace a named image slot with an image produced earlier in the run."""
    from orchestrator.experts.handlers.templates.create_from_template import (
        extract_drive_file_id,
    )
    from shared.utils.apps_script_client import get_merged_range_at, replace_sheet_image

    file_ref = context.get_parameter_value("file_id") or context.get_state("document_id")
    if not file_ref:
        return StepResult.soft_failure(
            code="unmet_prerequisite",
            message="No spreadsheet was given and this run has not created one yet.",
            remediation="Call create_from_template first, or pass file_id explicitly.",
        )
    file_id = extract_drive_file_id(str(file_ref))

    worksheet_name = context.get_parameter_value("worksheet_name")
    if not worksheet_name:
        return StepResult.soft_failure(
            code="missing_parameter",
            message="No worksheet name was given, so the image cannot be located.",
            remediation="Pass worksheet_name — the name of the tab holding the image.",
        )

    target = context.get_parameter_value("target")
    fit_range = context.get_parameter_value("fit_range")

    image_b64 = context.get_parameter_value("image_base64")
    if not image_b64:
        map_result = context.get_previous_result("generate_distribution_map") or {}
        image_b64 = map_result.get("map_image_b64")
    if not image_b64:
        return StepResult.soft_failure(
            code="missing_parameter",
            message="There is no image to insert.",
            remediation="Run the step that produces the image first, or pass image_base64.",
        )

    # Sizing layer 1: a merged range containing the target cell wins over
    # anything the comment said.
    merged_range = None
    if target and re.fullmatch(r"[A-Z]{1,3}\d+", str(target).upper()):
        merged = await get_merged_range_at(file_id, str(worksheet_name), str(target).upper())
        if merged.success and (merged.data or {}).get("merged"):
            merged_range = (merged.data or {}).get("range")

    chosen_fit = choose_fit_range(merged_range, fit_range)

    await context.send_progress_to_user("Replacing the image in the sheet...")

    result = await replace_sheet_image(
        sheet_id=file_id,
        worksheet_name=str(worksheet_name),
        image_base64=image_b64,
        target=str(target) if target else None,
        fit_range=chosen_fit,
    )

    if not result.success:
        return StepResult.soft_failure(
            code="invalid_parameter",
            message=f"Could not replace the image: {result.error_message}",
            remediation=(
                "Check that the target token or alt text matches an image in that tab. "
                "Omit target to fall back to the first large image."
            ),
        )

    LOGGER.info(f"Replaced image in {file_id}/{worksheet_name} (target={target})")
    return StepResult(
        data={"image_replaced": True, **(result.data or {})},
        state_updates={"image_replaced": True},
        progress_message="Image updated.",
    )
```

Add it to `templates/__init__.py`'s imports and `__all__`.

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_replace_file_image.py -q
```

Expected: PASS (5 tests).

- [ ] **Step 5: Run the whole experts suite**

```bash
cd chat_orchestrator && uv run pytest tests/experts/ -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/templates/
git add -f chat_orchestrator/tests/experts/test_replace_file_image.py
git commit -m "feat(experts): generic replace_file_image step with slot sizing"
```

### Task 6.4: Give `process_doc_edits` a StepContract

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/handlers/doc_editor/process_doc_edits.py`
- Test: `chat_orchestrator/tests/experts/test_process_doc_edits_contract.py`

- [ ] **Step 1: Write the failing test**

```python
"""process_doc_edits is the one handler already doing comment-driven editing,
and until it has a contract it is unreachable as a skill step tool."""

from orchestrator.experts.step_registry import get_step_contract


def test_it_has_a_contract_at_all():
    assert get_step_contract("process_doc_edits") is not None


def test_it_declares_document_id_and_instruction_as_parameters():
    contract = get_step_contract("process_doc_edits")
    names = {p.name for p in contract.params}
    assert {"document_id", "instruction"} <= names


def test_it_is_marked_mutating_with_a_mock():
    contract = get_step_contract("process_doc_edits")
    assert contract.mutates is True
    assert contract.mutation_kind == "external_write"
    assert contract.mock is not None
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_process_doc_edits_contract.py -q
```

Expected: FAIL — `@register_step("process_doc_edits")` currently passes no contract.

- [ ] **Step 3: Add the contract**

Replace the bare `@register_step("process_doc_edits")` decorator with:

```python
@register_step(
    "process_doc_edits",
    contract=StepContract(
        description=(
            "Applies @anansi-chatbot comment requests to a Google Doc, or edits one "
            "section identified from a chat instruction."
        ),
        params=(
            ParamSpec(
                name="document_id",
                param_type="string",
                description="The Google Doc to edit -- a doc link or a bare Drive file ID.",
                synonyms=("doc_id", "document", "file_id"),
                required=True,
            ),
            ParamSpec(
                name="instruction",
                param_type="string",
                description=(
                    "What to change. Omit to process every unresolved "
                    "@anansi-chatbot comment in the document instead."
                ),
                required=False,
            ),
        ),
        produces_state=("doc_edits_processed",),
        outputs=(
            OutputSpec(
                name="succeeded",
                value_type="integer",
                where="data",
                description="Number of sections successfully edited.",
            ),
        ),
        side_effects="Rewrites sections of a Google Doc and resolves the comments requesting them.",
        mutates=True,
        mutation_kind="external_write",
        mock=MockSpec(
            state_updates={"doc_edits_processed": True},
            data={"edits": 0, "succeeded": 0, "failed": 0},
            message="Would have applied the document's pending comment edits.",
        ),
    ),
)
```

Add the imports at the top of the file:

```python
from orchestrator.experts.step_contracts import MockSpec, OutputSpec, ParamSpec, StepContract
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_process_doc_edits_contract.py tests/experts/test_contract_lint.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/doc_editor/process_doc_edits.py
git add -f chat_orchestrator/tests/experts/test_process_doc_edits_contract.py
git commit -m "feat(experts): StepContract for process_doc_edits"
```

---

# Phase 7 — MCP tools accept spreadsheets

### Task 7.1: `scan_doc_comments` and `edit_doc_section` dispatch on mimeType

**Files:**
- Modify: `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py:788-910`
- Modify: `mcp_servers/servers/knowledge_server/tool_schemas.py`
- Modify: `mcp_servers/tool_definitions.json`
- Test: `mcp_servers/tests/servers/knowledge_server/test_doc_tools_accept_sheets.py`

- [ ] **Step 1: Write the failing test**

```python
"""The doc comment tools work on spreadsheets too, not just Docs."""

import json
import pathlib


def _schema(name):
    import sys

    root = pathlib.Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root))
    from mcp_servers.servers.knowledge_server.tool_schemas import TOOL_SCHEMAS

    return next(t for t in TOOL_SCHEMAS if t["name"] == name)


def test_scan_doc_comments_no_longer_says_google_doc_only():
    desc = _schema("scan_doc_comments")["description"]
    assert "spreadsheet" in desc.lower() or "sheet" in desc.lower()


def test_edit_doc_section_no_longer_says_google_doc_only():
    desc = _schema("edit_doc_section")["description"]
    assert "spreadsheet" in desc.lower() or "sheet" in desc.lower()


def test_exported_definitions_match_the_source_schemas():
    """tool_definitions.json is what production actually serves — it must be
    regenerated after any tool_schemas.py edit."""
    root = pathlib.Path(__file__).resolve().parents[4]
    exported = json.loads((root / "mcp_servers" / "tool_definitions.json").read_text())
    knowledge = {t["name"]: t for t in exported["tools"]["knowledge"]}
    assert knowledge["scan_doc_comments"]["description"] == _schema("scan_doc_comments")["description"]
```

- [ ] **Step 2: Run it to verify it fails**

```bash
PYTHONPATH="$PWD:$PWD/mcp_servers" pytest mcp_servers/tests/servers/knowledge_server/test_doc_tools_accept_sheets.py -q
```

Expected: FAIL — both descriptions currently say "Google Doc".

- [ ] **Step 3: Update the descriptions and the handler**

In `tool_schemas.py`, rewrite the two descriptions to the 5-slot standard from
PR #99 and rename the parameter description away from "Google Doc file ID":

```python
{
    "name": "scan_doc_comments",
    "description": (
        "[READ-ONLY] Scan a Google Doc or Google Sheet for pending @anansibot "
        "comments. Returns each comment's highlighted text (for a Sheet, the "
        "commented cell's content), instruction, and comment ID. Use before "
        "edit_doc_section to see what edits a file is asking for. A comment on "
        "an empty cell cannot be located and is returned with empty text."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "description": (
                    "Google Doc or Sheet file ID (required — not a name). If the "
                    "user gives a name, use find_document first to resolve the ID."
                ),
            }
        },
        "required": ["document_id"],
    },
},
```

In `knowledge_mcp_server.py`, `scan_doc_comments`'s handler already calls
`shared.utils.doc_editing.scan_comments`, which after Phase 2 delegates to the
file-type-agnostic `scan_annotations` — so it works on Sheets with no code change.
Verify that by reading lines 788-800 and confirming no Docs-only call remains.

For `edit_doc_section`, add mimeType dispatch before the existing `edit_section`
call at line 898:

```python
        from shared.utils.file_annotations import MIME_SHEET, get_file_mime_type

        mime = await get_file_mime_type(doc_id)
        if mime == MIME_SHEET:
            from shared.utils.sheet_editing import (
                fetch_all_grids,
                find_cells_in_grids,
                write_cells,
            )

            grids = await fetch_all_grids(doc_id)
            matches = find_cells_in_grids(grids, section_text or "")
            if not matches:
                return {
                    "success": False,
                    "error": (
                        "That text does not appear in the spreadsheet. It may have "
                        "been edited since the comment was made."
                    ),
                }
            written = await write_cells(
                doc_id, [(m.tab, m.a1, replacement_markdown) for m in matches]
            )
            return {"success": True, "cells_written": written}
```

- [ ] **Step 4: Regenerate `tool_definitions.json`**

This file is what production actually serves — it is exported wholesale per
server, not merged with the code at runtime, so a `tool_schemas.py` edit that
skips this step changes nothing in production:

```bash
python3 scripts/export_tools.py
```

If the repo-root `.venv` is missing a server's dependencies, that server can be
silently dropped from the export. Verify the file still has all 12 servers:

```bash
python3 -c "import json; d=json.load(open('mcp_servers/tool_definitions.json')); print(len(d['tools']), 'servers'); print(sorted(d['tools']))"
```

Expected: `12 servers`.

- [ ] **Step 5: Run the test to verify it passes**

```bash
PYTHONPATH="$PWD:$PWD/mcp_servers" pytest mcp_servers/tests/servers/knowledge_server/ -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mcp_servers/servers/knowledge_server/ mcp_servers/tool_definitions.json
git add -f mcp_servers/tests/servers/knowledge_server/test_doc_tools_accept_sheets.py
git commit -m "feat(mcp): doc comment tools accept spreadsheets"
```

---

# Phase 8 — Split the LPP monolith

The registered step name and the Google Doc workflow definition do **not**
change. This is an internal refactor that creates the seams for a later cutover.

### Task 8.1: Extract the three jobs into named functions

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/handlers/package_generator/populate_cells.py:600-853`
- Test: `chat_orchestrator/tests/experts/test_populate_cells_split.py`

- [ ] **Step 1: Write the failing test**

```python
"""populate_lpp_cells splits into three callable jobs behind one step name."""

import inspect

from orchestrator.experts.handlers.package_generator import populate_cells
from orchestrator.experts.step_registry import get_step_handler


def test_the_registered_step_name_is_unchanged():
    """The Google Doc workflow definition names this step — it must not move."""
    assert get_step_handler("populate_lpp_cells") is not None


def test_the_three_jobs_are_separately_callable():
    for name in ("fill_main_input_cells", "replace_map_image", "build_bom_tab"):
        assert hasattr(populate_cells, name), f"populate_cells.{name} missing"
        assert inspect.iscoroutinefunction(getattr(populate_cells, name))


def test_the_handler_body_is_now_short():
    """If the orchestrator grew back past ~80 lines, the split leaked."""
    source = inspect.getsource(populate_cells.populate_lpp_cells)
    assert len(source.splitlines()) < 80
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_populate_cells_split.py -q
```

Expected: FAIL — the three functions do not exist and the handler is ~280 lines.

- [ ] **Step 3: Extract the functions**

This is a **pure move**. Change no logic, no variable names, no ordering — only
where the code lives. The exact source ranges in the current file (verify with
`grep -n "    # [0-9]\+\." populate_cells.py` before cutting, since earlier tasks
in this plan do not touch this file but a rebase might):

| new function | move lines | current markers |
|---|---|---|
| `fill_main_input_cells` | **643–747** | `# 1. Collect all available values` through the `cells_written` log line |
| `replace_map_image` | **749–798** | `# 9. Replace map image in "Proposed Budget" sheet` |
| `build_bom_tab` | **800–829** | `# 10. Create Full BOM tab with items grouped by Component Type` |

Each becomes a module-level coroutine taking `(context, document_id)` and
returning the dict of values the final `StepResult` at line 831 already reads:

```python
async def fill_main_input_cells(context: StepContext, document_id: str) -> Dict[str, Any]:
    """Populate the Main Input sheet from the expert's ## Cell Mapping table.

    The LPP-specific path fill_annotations generalises. It stays as-is on
    purpose: cutting LPP over to the comment-driven mechanism also needs ~40
    comments added to a live production template, which is a separate,
    scheduled change.

    Returns the keys the caller's StepResult already publishes:
    cells_populated, matched_keys, unmatched_keys, mapping, key_columns,
    debug_writes. Returns {"error": "..."} instead when the sheet read or
    write fails, so the caller can turn that into StepResult.failure.
    """


async def replace_map_image(context: StepContext, document_id: str) -> Dict[str, Any]:
    """Swap the distribution map into the 'Proposed Budget' sheet.

    Still calls replace_sheet_image with neither target nor fit_range, so it
    resolves through the legacy min_height heuristic and its behaviour is
    unchanged. replace_file_image is the generic version for new flows.

    Returns {"map_image_replaced": bool, "map_image_error": str | None}.
    """


async def build_bom_tab(context: StepContext, document_id: str) -> Dict[str, Any]:
    """Create the Full BOM tab from the run's BOM items.

    Returns {"bom_tab_populated": bool, "bom_tab_error": str | None,
    "bom_item_count": int}.
    """
```

Then reduce `populate_lpp_cells` to the guard (lines 604–612), the
`document_id` precondition (614–618), and three awaits whose dicts merge into
the existing `StepResult`:

```python
    fill = await fill_main_input_cells(context, document_id)
    if fill.get("error"):
        return StepResult.failure(fill["error"])
    image = await replace_map_image(context, document_id)
    bom = await build_bom_tab(context, document_id)

    return StepResult(
        data={**fill, **image, **bom},
        state_updates={
            "cells_populated": True,
            "map_image_replaced": image["map_image_replaced"],
            "bom_tab_populated": bom["bom_tab_populated"],
        },
        progress_message=f"Populated {fill['cells_populated']} cells in {fill['sheet_name']}"
        + (", map image updated" if image["map_image_replaced"] else "")
        + (f", BOM tab with {bom['bom_item_count']} items" if bom["bom_tab_populated"] else ""),
    )
```

The `data` and `state_updates` keys must stay **byte-for-byte identical** —
`test_package_generator_mocks.py` and downstream steps read them by name. Note
`fill` must therefore also return `sheet_name`, which the current code holds in
a local at line 625.

- [ ] **Step 4: Run the split test and the full LPP suite**

```bash
cd chat_orchestrator && uv run pytest tests/experts/test_populate_cells_split.py -q
cd chat_orchestrator && uv run pytest tests/experts/ -q -k "package_generator or populate or lpp"
```

Expected: PASS on both. If any LPP test fails, the extraction changed behaviour — revert and redo it mechanically.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/package_generator/populate_cells.py
git add -f chat_orchestrator/tests/experts/test_populate_cells_split.py
git commit -m "refactor(experts): split populate_lpp_cells into three jobs"
```

---

# Phase 9 — Final verification

### Task 9.1: Full suite and pre-commit

- [ ] **Step 1: Run every suite CI runs**

```bash
cd chat_orchestrator && uv run pytest tests/ -x -q
```

```bash
cd chat_orchestrator && uv run pytest ../shared -q
```

```bash
PYTHONPATH="$PWD:$PWD/mcp_servers" pytest mcp_servers/tests -q
```

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests -q
```

- [ ] **Step 2: Run pre-commit across the whole repo**

This is the only check that catches a test file that was never actually
committed — `git status` and `pytest` both pass while the file sits untracked:

```bash
pre-commit run --all-files
```

- [ ] **Step 3: If `test-wiring` reports untracked test files, force-add them**

Vet each for operator data first, then:

```bash
git add -f <path>
```

Re-run `pre-commit run --all-files` until clean.

- [ ] **Step 4: Confirm what actually got committed**

```bash
git log --oneline origin/main..HEAD
git diff --stat origin/main..HEAD
```

Check every test file listed in this plan appears in the diffstat. A missing one
means the `git add -f` was skipped and CI will never run it.

- [ ] **Step 5: Commit any stragglers**

```bash
git commit -m "test: force-add test files denied by the tests/ gitignore rule"
```

---

## Verification checklist before calling this done

- [ ] `pre-commit run --all-files` is green
- [ ] Every test file in this plan appears in `git diff --stat origin/main..HEAD`
- [ ] `mcp_servers/tool_definitions.json` still lists 12 servers
- [ ] `populate_lpp_cells` is still the registered step name, and the LPP suite passes
- [ ] `replace_sheet_image`'s payload is unchanged when `target`/`fit_range` are omitted
- [ ] Spike 1's findings are recorded in the spec, and nothing contradicts it
