# Prompt Library & Composable Knowledge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Anansi's five ad-hoc prompt mechanisms with one library — bundled `.prompt` files as the versioned default, DB overrides editable live from the admin app, per-prompt group ACLs, and tagged knowledge modules composed into prompt context.

**Architecture:** `shared/prompts/` holds a loader that resolves each prompt id through DB override → attached Google Doc → bundled file, always returning provenance. Frontmatter comes only from the bundled file, so a UI edit can never change a prompt's schema, overridability or access lists. Knowledge modules attach by tag intersection plus per-prompt overrides, split into a pinned tier (inlined, budgeted) and an on-demand tier (catalog line only).

**Tech Stack:** Python 3.11, PyYAML, pydantic v2, supabase-py, NiceGUI, pytest, Postgres (Supabase `chat_db`).

**Spec:** `docs/superpowers/specs/2026-07-30-prompt-library-design.md`

---

## Working constraints (read before Task 1)

- **Branch:** `feature/prompt-library`. Already created; the spec is committed on it.
- **Commit after every task.** Each task ends with a commit step. Never batch tasks into one commit.
- **Do not push.** No `git push`, no PR, until the entire plan is complete and Task 26 (final verification) passes. The user pushes.
- **SQL is delivered as one file**, `db/migrations/0006_prompt_library.sql`, written in Task 12 and never split. It is applied by hand in the Supabase query editor — no migration runner executes it. Nothing in the code may assume the tables exist before that file is applied; every DB path degrades to bundled resolution when a table is missing.
- **New test files need `git add -f`.** The repo `.gitignore` denies `tests/`. A plain `git add` on a new test file is a silent no-op. Every commit step below force-adds test paths explicitly. See `CLAUDE.md`.
- **Before the final commit, run `pre-commit run --all-files`** — not just `ruff check .` or `pytest`.

**Running tests.** `shared/` has no venv; it is tested through `chat_orchestrator`'s:

```bash
cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests -q
```

Orchestrator tests:

```bash
cd chat_orchestrator && source .venv/bin/activate && pytest tests/ -q
```

---

## File structure

**New — the library (Phase 1):**

| File | Responsibility |
|---|---|
| `shared/prompts/__init__.py` | Public surface: `PROMPTS`, `RenderedPrompt`, `PromptNotFound` |
| `shared/prompts/types.py` | `RenderedPrompt`, `PromptSource`, `RequestScope`, exceptions |
| `shared/prompts/spec.py` | `PromptSpec`, `AccessSpec`, `parse_prompt_file` |
| `shared/prompts/render.py` | `{{var}}` substitution, `{{> partial}}` inlining, section splitting |
| `shared/prompts/bundled.py` | `BundledStore` — discovers and caches `library/*.prompt` |
| `shared/prompts/gdoc.py` | `GDocStore` — the single Google Doc adapter |
| `shared/prompts/library.py` | `PromptLibrary` — resolution order, cache, provenance |
| `shared/prompts/library/*.prompt` | The prompt content itself (~26 files) |

**New — persistence and access (Phase 2):**

| File | Responsibility |
|---|---|
| `shared/prompts/access.py` | Group parsing, three verbs. Pure, env-based, no UI deps |
| `shared/prompts/overrides.py` | `OverrideStore` — versions, labels, propose/publish |
| `db/migrations/0006_prompt_library.sql` | **The one SQL file.** All five tables |
| `anansi_app/nicegui_app/pages/prompts.py` | Prompts admin page |

**New — knowledge (Phase 3):**

| File | Responsibility |
|---|---|
| `shared/prompts/knowledge.py` | `KnowledgeModule`, `KnowledgeStore`, selection + budgeting |
| `anansi_app/nicegui_app/pages/knowledge_modules.py` | Knowledge Modules admin page |

**Modified:**

| File | Change |
|---|---|
| `chat_orchestrator/orchestrator/services/instructions_provider.py` | Delete `_load_fallback_instructions`, both duplicated composition blocks; delegate to `PROMPTS` |
| `chat_orchestrator/orchestrator/services/expert_instructions_provider.py` | Delete `_load_fallback_expert_instructions`; source sections from `PROMPTS` |
| `chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py` | `get_correlation_instructions` reads `PROMPTS` |
| ~20 call sites listed in Tasks 9–11 | Replace literals with `PROMPTS.render(...)` |
| `anansi_app/grid_app/lib/perms.py` | Delegate prompt verbs to `shared.prompts.access` |
| `anansi_app/nicegui_app/main.py`, `layout.py` | Register + link the two new pages |
| `shared/config/flag_registry.py` | Register the three group whitelists; retire `GRAFANA_PANEL_DESCRIPTION_PROMPT` |
| `db/schema/chat_db.sql` | Append the five tables (documentation copy) |

---

# Phase 1 — The library (no behavior change)

Phase 1 is a pure refactor. Nothing a user can see changes. It must ship complete: stopping halfway leaves a sixth prompt mechanism beside the five that exist.

---

### Task 1: Package skeleton and types

**Files:**
- Create: `shared/prompts/__init__.py`
- Create: `shared/prompts/types.py`
- Test: `shared/tests/test_prompt_types.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for prompt library value types."""

import pytest

from shared.prompts.types import (
    PromptNotFound,
    PromptSource,
    RenderedPrompt,
    RequestScope,
)


def test_rendered_prompt_carries_provenance():
    rendered = RenderedPrompt(
        prompt_id="customer.system",
        system_text="You are Anansi.",
        context_text=None,
        source=PromptSource.BUNDLED,
        version=None,
        checksum="abc123",
    )
    assert rendered.prompt_id == "customer.system"
    assert rendered.source is PromptSource.BUNDLED
    assert rendered.knowledge_used == []


def test_rendered_prompt_provenance_string_is_log_friendly():
    rendered = RenderedPrompt(
        prompt_id="customer.system",
        system_text="x",
        context_text=None,
        source=PromptSource.DB,
        version=7,
        checksum="deadbeefcafe",
    )
    assert rendered.provenance() == "customer.system@db:v7:deadbeef"


def test_bundled_provenance_has_no_version():
    rendered = RenderedPrompt(
        prompt_id="a.b",
        system_text="x",
        context_text=None,
        source=PromptSource.BUNDLED,
        version=None,
        checksum="0123456789ab",
    )
    assert rendered.provenance() == "a.b@bundled:default:01234567"


def test_request_scope_matches_sector_always():
    scope = RequestScope(grid="ABC")
    assert scope.matches("sector") is True
    assert scope.matches("site:ABC") is True
    assert scope.matches("site:XYZ") is False


def test_request_scope_without_grid_matches_only_sector():
    scope = RequestScope()
    assert scope.matches("sector") is True
    assert scope.matches("site:ABC") is False


def test_request_scope_site_match_is_case_insensitive():
    assert RequestScope(grid="abc").matches("site:ABC") is True


def test_request_scope_matches_org():
    assert RequestScope(organization_id="2").matches("org:2") is True
    assert RequestScope(organization_id="3").matches("org:2") is False


def test_prompt_not_found_is_an_exception():
    with pytest.raises(PromptNotFound):
        raise PromptNotFound("nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_types.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.prompts'`

- [ ] **Step 3: Write the implementation**

`shared/prompts/types.py`:

```python
"""Value types for the prompt library."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class PromptSource(str, Enum):
    """Where a rendered prompt's body came from."""

    DB = "db"
    GDOC = "gdoc"
    BUNDLED = "bundled"


class PromptError(Exception):
    """Base class for prompt library errors."""


class PromptNotFound(PromptError):
    """No prompt with this id exists in the bundled library."""


class PromptRenderError(PromptError):
    """A prompt could not be rendered (bad variable, bad partial)."""


@dataclass(frozen=True)
class RequestScope:
    """The entity context a prompt is being rendered for.

    Used to decide which scoped knowledge modules apply.
    """

    grid: Optional[str] = None
    organization_id: Optional[str] = None

    def matches(self, scope: str) -> bool:
        """Whether a module declaring ``scope`` applies to this request."""
        if scope == "sector":
            return True
        if scope.startswith("site:"):
            return bool(self.grid) and scope[5:].lower() == (self.grid or "").lower()
        if scope.startswith("org:"):
            return bool(self.organization_id) and scope[4:] == self.organization_id
        return False


@dataclass
class RenderedPrompt:
    """A prompt resolved, rendered, and ready to send, with provenance."""

    prompt_id: str
    system_text: str
    context_text: Optional[str]
    source: PromptSource
    version: Optional[int]
    checksum: str
    knowledge_used: List[str] = field(default_factory=list)

    def provenance(self) -> str:
        """Compact identity for logs and traces."""
        version = f"v{self.version}" if self.version is not None else "default"
        return f"{self.prompt_id}@{self.source.value}:{version}:{self.checksum[:8]}"
```

`shared/prompts/__init__.py`:

```python
"""Anansi prompt library — one home for every prompt in the product."""

from shared.prompts.types import (
    PromptError,
    PromptNotFound,
    PromptRenderError,
    PromptSource,
    RenderedPrompt,
    RequestScope,
)

__all__ = [
    "PromptError",
    "PromptNotFound",
    "PromptRenderError",
    "PromptSource",
    "RenderedPrompt",
    "RequestScope",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_types.py -q`
Expected: PASS, 8 passed

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/__init__.py shared/prompts/types.py
git add -f shared/tests/test_prompt_types.py
git commit -m "feat(prompts): add prompt library value types"
```

---

### Task 2: Prompt file parsing

**Files:**
- Create: `shared/prompts/spec.py`
- Test: `shared/tests/test_prompt_spec.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for .prompt file parsing."""

import pytest

from shared.prompts.spec import AccessSpec, parse_prompt_file

VALID = """---
id: customer.system
description: Customer-mode instructions.
owner: ops
overridable: true
output: text
variables: [user_name]
sections: [system_instructions, examples]
knowledge_tags: [grid_ops]
access:
  view: [ops, eng]
  edit: [ops]
  publish: [eng]
---
# System Instructions

Hello {{user_name}}.
"""


def test_parses_frontmatter_fields():
    spec = parse_prompt_file(VALID, path="customer.system.prompt")
    assert spec.id == "customer.system"
    assert spec.owner == "ops"
    assert spec.overridable is True
    assert spec.variables == ["user_name"]
    assert spec.sections == ["system_instructions", "examples"]
    assert spec.knowledge_tags == ["grid_ops"]


def test_body_excludes_frontmatter():
    spec = parse_prompt_file(VALID, path="x.prompt")
    assert spec.body.startswith("# System Instructions")
    assert "owner: ops" not in spec.body


def test_access_defaults_to_empty_lists():
    text = "---\nid: a.b\ndescription: d\n---\nbody"
    spec = parse_prompt_file(text, path="a.b.prompt")
    assert spec.access == AccessSpec(view=[], edit=[], publish=[])


def test_defaults_are_conservative():
    text = "---\nid: a.b\ndescription: d\n---\nbody"
    spec = parse_prompt_file(text, path="a.b.prompt")
    assert spec.overridable is False
    assert spec.output == "text"
    assert spec.owner == "eng"


def test_checksum_is_stable_and_body_only():
    a = parse_prompt_file(VALID, path="x.prompt")
    b = parse_prompt_file(VALID.replace("owner: ops", "owner: eng"), path="x.prompt")
    assert a.checksum == b.checksum


def test_missing_frontmatter_raises():
    with pytest.raises(ValueError, match="frontmatter"):
        parse_prompt_file("no frontmatter here", path="x.prompt")


def test_missing_id_raises():
    with pytest.raises(ValueError, match="id"):
        parse_prompt_file("---\ndescription: d\n---\nbody", path="x.prompt")


def test_missing_description_raises():
    with pytest.raises(ValueError, match="description"):
        parse_prompt_file("---\nid: a.b\n---\nbody", path="x.prompt")


def test_json_output_requires_schema():
    text = "---\nid: a.b\ndescription: d\noutput: json\n---\nbody"
    with pytest.raises(ValueError, match="schema"):
        parse_prompt_file(text, path="a.b.prompt")


def test_json_output_with_schema_is_accepted():
    text = (
        "---\nid: a.b\ndescription: d\noutput: json\n"
        "schema:\n  type: object\n---\nbody"
    )
    spec = parse_prompt_file(text, path="a.b.prompt")
    assert spec.output == "json"
    assert spec.schema == {"type": "object"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_spec.py -q`
Expected: FAIL — `No module named 'shared.prompts.spec'`

- [ ] **Step 3: Write the implementation**

`shared/prompts/spec.py`:

```python
"""Parsing for ``.prompt`` files — YAML frontmatter plus a markdown body."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class AccessSpec:
    """Default group bindings for the three verbs."""

    view: List[str] = field(default_factory=list)
    edit: List[str] = field(default_factory=list)
    publish: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PromptSpec:
    """A parsed ``.prompt`` file. Frontmatter here is authoritative.

    Overrides supply body text only, so a UI edit can never change
    ``overridable``, ``output``, ``schema`` or ``access``.
    """

    id: str
    description: str
    body: str
    checksum: str
    owner: str = "eng"
    overridable: bool = False
    output: str = "text"
    schema: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    variables: List[str] = field(default_factory=list)
    sections: List[str] = field(default_factory=list)
    knowledge_tags: List[str] = field(default_factory=list)
    access: AccessSpec = field(default_factory=AccessSpec)


def body_checksum(body: str) -> str:
    """Content address for a prompt body. Body only — frontmatter excluded."""
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def parse_prompt_file(text: str, *, path: str) -> PromptSpec:
    """Parse ``.prompt`` file contents. ``path`` is used only in error messages."""
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError(f"{path}: missing YAML frontmatter (expected a leading '---' block)")

    raw = yaml.safe_load(match.group(1)) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")

    body = match.group(2)

    prompt_id = raw.get("id")
    if not prompt_id:
        raise ValueError(f"{path}: frontmatter is missing required field 'id'")
    description = raw.get("description")
    if not description:
        raise ValueError(f"{path}: frontmatter is missing required field 'description'")

    output = raw.get("output", "text")
    schema = raw.get("schema")
    if output == "json" and not schema:
        raise ValueError(f"{path}: output 'json' requires a 'schema' field")

    access_raw = raw.get("access") or {}
    access = AccessSpec(
        view=list(access_raw.get("view") or []),
        edit=list(access_raw.get("edit") or []),
        publish=list(access_raw.get("publish") or []),
    )

    return PromptSpec(
        id=str(prompt_id),
        description=str(description),
        body=body,
        checksum=body_checksum(body),
        owner=str(raw.get("owner", "eng")),
        overridable=bool(raw.get("overridable", False)),
        output=str(output),
        schema=schema,
        model=raw.get("model"),
        variables=list(raw.get("variables") or []),
        sections=list(raw.get("sections") or []),
        knowledge_tags=list(raw.get("knowledge_tags") or []),
        access=access,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_spec.py -q`
Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/spec.py
git add -f shared/tests/test_prompt_spec.py
git commit -m "feat(prompts): parse .prompt frontmatter and body"
```

---

### Task 3: Body rendering — variables and partials

**Files:**
- Create: `shared/prompts/render.py`
- Test: `shared/tests/test_prompt_render.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for prompt body rendering."""

import pytest

from shared.prompts.render import render_body, split_sections
from shared.prompts.types import PromptRenderError


def _no_partials(prompt_id: str) -> str:
    raise AssertionError(f"unexpected partial lookup: {prompt_id}")


def test_substitutes_declared_variable():
    out = render_body("Hi {{name}}.", {"name": "Ada"}, ["name"], _no_partials)
    assert out == "Hi Ada."


def test_substitutes_repeated_variable():
    out = render_body("{{n}}/{{n}}", {"n": "x"}, ["n"], _no_partials)
    assert out == "x/x"


def test_undeclared_placeholder_raises():
    with pytest.raises(PromptRenderError, match="not declared"):
        render_body("Hi {{name}}.", {"name": "Ada"}, [], _no_partials)


def test_missing_value_raises():
    with pytest.raises(PromptRenderError, match="no value"):
        render_body("Hi {{name}}.", {}, ["name"], _no_partials)


def test_none_value_raises():
    with pytest.raises(PromptRenderError, match="no value"):
        render_body("Hi {{name}}.", {"name": None}, ["name"], _no_partials)


def test_inlines_partial():
    def resolve(prompt_id):
        assert prompt_id == "partials.tone"
        return "Be brief."

    out = render_body("A. {{> partials.tone}} B.", {}, [], resolve)
    assert out == "A. Be brief. B."


def test_partial_may_contain_variables_of_the_host():
    def resolve(prompt_id):
        return "Grid {{grid}}."

    out = render_body("{{> partials.grid}}", {"grid": "ABC"}, ["grid"], resolve)
    assert out == "Grid ABC."


def test_partial_must_be_namespaced():
    with pytest.raises(PromptRenderError, match="partials\\."):
        render_body("{{> customer.system}}", {}, [], _no_partials)


def test_partial_cycle_raises():
    def resolve(prompt_id):
        return "{{> partials.a}}"

    with pytest.raises(PromptRenderError, match="cycle"):
        render_body("{{> partials.a}}", {}, [], resolve)


def test_partial_depth_cap():
    def resolve(prompt_id):
        depth = int(prompt_id.rsplit(".", 1)[-1])
        return f"{{{{> partials.p{depth + 1}}}}}"

    with pytest.raises(PromptRenderError, match="depth"):
        render_body("{{> partials.p0}}", {}, [], resolve)


def test_split_sections_routes_named_heading_to_system():
    body = "# System Instructions\n\nBe kind.\n\n# Examples\n\nQ then A.\n"
    system, context = split_sections(body, ["system_instructions"])
    assert system == "Be kind."
    assert context == "# Examples\n\nQ then A."


def test_split_sections_with_no_declared_sections_is_all_system():
    system, context = split_sections("Just text.", [])
    assert system == "Just text."
    assert context is None


def test_split_sections_missing_named_section_raises():
    with pytest.raises(PromptRenderError, match="System Instructions"):
        split_sections("# Examples\n\nx", ["system_instructions"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_render.py -q`
Expected: FAIL — `No module named 'shared.prompts.render'`

- [ ] **Step 3: Write the implementation**

`shared/prompts/render.py`:

```python
"""Rendering for prompt bodies: variables, partials, and section splitting."""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

from shared.prompts.types import PromptRenderError

_PARTIAL = re.compile(r"\{\{>\s*([A-Za-z0-9_.]+)\s*\}\}")
_VARIABLE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_HEADING = re.compile(r"^#\s+(.+)$", re.MULTILINE)

MAX_PARTIAL_DEPTH = 3

PartialResolver = Callable[[str], str]


def _inline_partials(body: str, resolve: PartialResolver, seen: List[str], depth: int) -> str:
    if depth > MAX_PARTIAL_DEPTH:
        raise PromptRenderError(
            f"partial include depth exceeded {MAX_PARTIAL_DEPTH} (chain: {' -> '.join(seen)})"
        )

    def replace(match: re.Match) -> str:
        target = match.group(1)
        if not target.startswith("partials."):
            raise PromptRenderError(
                f"'{target}' is not includable: only ids under 'partials.' may be included"
            )
        if target in seen:
            raise PromptRenderError(f"partial cycle detected: {' -> '.join(seen + [target])}")
        return _inline_partials(resolve(target), resolve, seen + [target], depth + 1)

    return _PARTIAL.sub(replace, body)


def render_body(
    body: str,
    variables: Dict[str, object],
    declared: List[str],
    resolve_partial: PartialResolver,
) -> str:
    """Inline partials, then substitute variables.

    Every placeholder must appear in ``declared`` and have a non-None value.
    Silent empty substitution is never correct for a prompt.
    """
    expanded = _inline_partials(body, resolve_partial, [], 1)

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in declared:
            raise PromptRenderError(f"'{{{{{name}}}}}' is used but not declared in 'variables'")
        if variables.get(name) is None:
            raise PromptRenderError(f"'{{{{{name}}}}}' is declared but no value was supplied")
        return str(variables[name])

    return _VARIABLE.sub(replace, expanded)


def _heading_key(title: str) -> str:
    return title.strip().lower().replace(" ", "_")


def split_sections(body: str, sections: List[str]) -> Tuple[str, Optional[str]]:
    """Split a body into (system channel, context channel).

    ``sections[0]`` names the ``# `` heading whose content is the system
    instruction. Everything else becomes the context message. With no declared
    sections the whole body is the system instruction.
    """
    body = body.strip()
    if not sections:
        return body, None

    system_key = sections[0]
    matches = list(_HEADING.finditer(body))
    if not matches:
        raise PromptRenderError(f"body has no '# ' headings but declares sections {sections}")

    chunks: Dict[str, str] = {}
    order: List[str] = []
    for index, match in enumerate(matches):
        key = _heading_key(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        chunks[key] = body[match.end() : end].strip()
        order.append(key)

    if system_key not in chunks:
        wanted = system_key.replace("_", " ").title()
        raise PromptRenderError(f"body is missing the '{wanted}' section")

    system_text = chunks[system_key]
    context_parts = [
        f"# {key.replace('_', ' ').title()}\n\n{chunks[key]}"
        for key in order
        if key != system_key and chunks[key]
    ]
    return system_text, "\n\n".join(context_parts) or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_render.py -q`
Expected: PASS, 13 passed

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/render.py
git add -f shared/tests/test_prompt_render.py
git commit -m "feat(prompts): render variables, partials and section splits"
```

---

### Task 4: Bundled store

**Files:**
- Create: `shared/prompts/bundled.py`
- Create: `shared/prompts/library/.gitkeep`
- Test: `shared/tests/test_prompt_bundled.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the bundled .prompt file store."""

import pytest

from shared.prompts.bundled import BundledStore
from shared.prompts.types import PromptNotFound


@pytest.fixture
def store(tmp_path):
    (tmp_path / "a.b.prompt").write_text(
        "---\nid: a.b\ndescription: First.\n---\nAlpha body\n"
    )
    (tmp_path / "partials.tone.prompt").write_text(
        "---\nid: partials.tone\ndescription: Tone.\n---\nBe brief.\n"
    )
    return BundledStore(directory=tmp_path)


def test_get_returns_spec(store):
    spec = store.get("a.b")
    assert spec.description == "First."
    assert spec.body.strip() == "Alpha body"


def test_ids_lists_every_prompt(store):
    assert sorted(store.ids()) == ["a.b", "partials.tone"]


def test_unknown_id_raises(store):
    with pytest.raises(PromptNotFound, match="a.missing"):
        store.get("a.missing")


def test_filename_must_match_declared_id(tmp_path):
    (tmp_path / "wrong.prompt").write_text("---\nid: a.b\ndescription: d\n---\nx")
    with pytest.raises(ValueError, match="does not match"):
        BundledStore(directory=tmp_path).ids()


def test_specs_are_parsed_once(store):
    assert store.get("a.b") is store.get("a.b")


def test_reload_picks_up_new_files(store, tmp_path):
    (tmp_path / "c.d.prompt").write_text("---\nid: c.d\ndescription: d\n---\nx")
    store.reload()
    assert "c.d" in store.ids()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_bundled.py -q`
Expected: FAIL — `No module named 'shared.prompts.bundled'`

- [ ] **Step 3: Write the implementation**

`shared/prompts/bundled.py`:

```python
"""Loads the bundled ``.prompt`` files that ship with the application.

This store is the floor of every resolution: it is always available, needs no
network or database, and is the authority for prompt frontmatter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from shared.prompts.spec import PromptSpec, parse_prompt_file
from shared.prompts.types import PromptNotFound

DEFAULT_DIRECTORY = Path(__file__).parent / "library"


class BundledStore:
    """Parses and caches ``<id>.prompt`` files from a directory."""

    def __init__(self, directory: Optional[Path] = None) -> None:
        self._directory = Path(directory) if directory else DEFAULT_DIRECTORY
        self._specs: Optional[Dict[str, PromptSpec]] = None

    def _load(self) -> Dict[str, PromptSpec]:
        if self._specs is not None:
            return self._specs
        specs: Dict[str, PromptSpec] = {}
        for path in sorted(self._directory.glob("*.prompt")):
            spec = parse_prompt_file(path.read_text(), path=str(path))
            expected = path.name[: -len(".prompt")]
            if spec.id != expected:
                raise ValueError(
                    f"{path}: declared id '{spec.id}' does not match filename '{expected}'"
                )
            specs[spec.id] = spec
        self._specs = specs
        return specs

    def reload(self) -> None:
        """Drop the parse cache. Used by tests and the admin 'reload' action."""
        self._specs = None

    def ids(self) -> List[str]:
        return list(self._load().keys())

    def get(self, prompt_id: str) -> PromptSpec:
        specs = self._load()
        if prompt_id not in specs:
            raise PromptNotFound(f"no bundled prompt with id '{prompt_id}'")
        return specs[prompt_id]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_bundled.py -q`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/bundled.py shared/prompts/library/.gitkeep
git add -f shared/tests/test_prompt_bundled.py
git commit -m "feat(prompts): add bundled .prompt file store"
```

---

### Task 5: Google Doc adapter

**Files:**
- Create: `shared/prompts/gdoc.py`
- Test: `shared/tests/test_prompt_gdoc.py`

Replaces five separate fetch-and-parse paths with one. It returns a *body*, not sections — section splitting is `render.py`'s job now.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the single Google Doc prompt adapter."""

from shared.prompts.gdoc import GDocStore


def test_returns_none_when_no_doc_configured():
    store = GDocStore(doc_id_for=lambda pid: None, fetch=lambda doc: "x")
    assert store.body_for("a.b") is None


def test_fetches_configured_doc():
    calls = []

    def fetch(doc_id):
        calls.append(doc_id)
        return "# System Instructions\n\nFrom the doc."

    store = GDocStore(doc_id_for=lambda pid: "DOC1", fetch=fetch)
    assert store.body_for("a.b") == "# System Instructions\n\nFrom the doc."
    assert calls == ["DOC1"]


def test_caches_within_ttl():
    calls = []

    def fetch(doc_id):
        calls.append(doc_id)
        return "body"

    store = GDocStore(doc_id_for=lambda pid: "DOC1", fetch=fetch, ttl_seconds=1000)
    store.body_for("a.b")
    store.body_for("a.b")
    assert calls == ["DOC1"]


def test_fetch_failure_returns_none_and_does_not_raise():
    def fetch(doc_id):
        raise RuntimeError("drive is down")

    store = GDocStore(doc_id_for=lambda pid: "DOC1", fetch=fetch)
    assert store.body_for("a.b") is None


def test_empty_document_is_treated_as_absent():
    store = GDocStore(doc_id_for=lambda pid: "DOC1", fetch=lambda doc: "   ")
    assert store.body_for("a.b") is None


def test_invalidate_clears_cache():
    calls = []

    def fetch(doc_id):
        calls.append(doc_id)
        return "body"

    store = GDocStore(doc_id_for=lambda pid: "DOC1", fetch=fetch, ttl_seconds=1000)
    store.body_for("a.b")
    store.invalidate()
    store.body_for("a.b")
    assert calls == ["DOC1", "DOC1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_gdoc.py -q`
Expected: FAIL — `No module named 'shared.prompts.gdoc'`

- [ ] **Step 3: Write the implementation**

`shared/prompts/gdoc.py`:

```python
"""The single Google Doc adapter for prompt overrides.

Replaces the five separate fetch-and-parse paths that previously existed in
instructions_provider, expert_instructions_provider, artifacts_provider,
procedure_provider and customer_mcp_server. This returns raw markdown; section
splitting belongs to render.py.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, Optional, Tuple

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DEFAULT_TTL_SECONDS = 3600

# Prompt id -> the env var that historically carried its doc id. Kept so
# existing deployments keep working unchanged after the refactor.
LEGACY_DOC_ENV_VARS: Dict[str, str] = {
    "customer.system": "CUSTOMER_SUPPORT_DOC_ID",
    "staff.system": "STAFF_SUPPORT_DOC_ID",
    "experts.definitions": "EXPERT_INSTRUCTIONS_DOC_ID",
    "troubleshooting.procedures": "TROUBLESHOOTING_PROCEDURES_DOC_ID",
    "verification.criteria": "VERIFICATION_DOC_ID",
}


def legacy_doc_id_for(prompt_id: str) -> Optional[str]:
    """Doc id from the historical env var for this prompt, if any.

    Task 15 layers a ``prompt_doc_bindings`` lookup in front of this, so a doc
    attached from the admin page wins over the legacy env var. Keeping the env
    var as the floor means existing deployments keep working untouched.
    """
    env_var = LEGACY_DOC_ENV_VARS.get(prompt_id)
    if not env_var:
        return None
    return os.getenv(env_var, "").strip() or None


def _default_fetch(doc_id: str) -> str:
    from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown

    return fetch_google_doc_markdown(doc_id) or ""


class GDocStore:
    """Fetches prompt bodies from Google Docs, with a TTL cache.

    A fetch failure is never fatal: it returns None so the caller falls through
    to the bundled default, and logs the failure with the doc id.
    """

    def __init__(
        self,
        doc_id_for: Callable[[str], Optional[str]] = legacy_doc_id_for,
        fetch: Callable[[str], str] = _default_fetch,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._doc_id_for = doc_id_for
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._cache: Dict[str, Tuple[str, float]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    def body_for(self, prompt_id: str) -> Optional[str]:
        doc_id = self._doc_id_for(prompt_id)
        if not doc_id:
            return None

        cached = self._cache.get(doc_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        try:
            body = self._fetch(doc_id)
        except Exception:
            LOGGER.warning(
                f"Google Doc fetch failed for prompt '{prompt_id}' (doc {doc_id}); "
                f"falling back to the bundled default",
                exc_info=True,
            )
            return None

        if not body or not body.strip():
            LOGGER.warning(f"Google Doc {doc_id} for prompt '{prompt_id}' is empty")
            return None

        self._cache[doc_id] = (body, time.time() + self._ttl)
        return body
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_gdoc.py -q`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/gdoc.py
git add -f shared/tests/test_prompt_gdoc.py
git commit -m "feat(prompts): add single Google Doc adapter with legacy env mapping"
```

---

### Task 6: The library facade

**Files:**
- Create: `shared/prompts/library.py`
- Modify: `shared/prompts/__init__.py`
- Test: `shared/tests/test_prompt_library.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the PromptLibrary resolution order."""

import pytest

from shared.prompts.bundled import BundledStore
from shared.prompts.library import PromptLibrary
from shared.prompts.types import PromptSource


@pytest.fixture
def bundled(tmp_path):
    (tmp_path / "a.b.prompt").write_text(
        "---\nid: a.b\ndescription: d\noverridable: true\n"
        "sections: [system_instructions]\n---\n"
        "# System Instructions\n\nBundled text.\n"
    )
    (tmp_path / "locked.prompt").write_text(
        "---\nid: locked\ndescription: d\noverridable: false\n---\nLocked text.\n"
    )
    (tmp_path / "partials.tone.prompt").write_text(
        "---\nid: partials.tone\ndescription: d\n---\nBe brief.\n"
    )
    return BundledStore(directory=tmp_path)


def test_falls_back_to_bundled(bundled):
    lib = PromptLibrary(bundled=bundled)
    out = lib.render("a.b")
    assert out.system_text == "Bundled text."
    assert out.source is PromptSource.BUNDLED
    assert out.version is None


def test_gdoc_beats_bundled(bundled):
    lib = PromptLibrary(
        bundled=bundled,
        gdoc_body_for=lambda pid: "# System Instructions\n\nDoc text.",
    )
    out = lib.render("a.b")
    assert out.system_text == "Doc text."
    assert out.source is PromptSource.GDOC


def test_db_beats_gdoc(bundled):
    lib = PromptLibrary(
        bundled=bundled,
        gdoc_body_for=lambda pid: "# System Instructions\n\nDoc text.",
        db_body_for=lambda pid: ("# System Instructions\n\nDb text.", 4),
    )
    out = lib.render("a.b")
    assert out.system_text == "Db text."
    assert out.source is PromptSource.DB
    assert out.version == 4


def test_non_overridable_ignores_db_and_gdoc(bundled):
    lib = PromptLibrary(
        bundled=bundled,
        gdoc_body_for=lambda pid: "Doc text.",
        db_body_for=lambda pid: ("Db text.", 4),
    )
    out = lib.render("locked")
    assert out.system_text == "Locked text."
    assert out.source is PromptSource.BUNDLED


def test_db_failure_falls_through_to_bundled(bundled):
    def boom(prompt_id):
        raise RuntimeError("db down")

    lib = PromptLibrary(bundled=bundled, db_body_for=boom)
    out = lib.render("a.b")
    assert out.system_text == "Bundled text."
    assert out.source is PromptSource.BUNDLED


def test_partials_resolve_from_the_bundled_store(bundled, tmp_path):
    (tmp_path / "c.d.prompt").write_text(
        "---\nid: c.d\ndescription: d\n---\n{{> partials.tone}}\n"
    )
    bundled.reload()
    lib = PromptLibrary(bundled=bundled)
    assert lib.render("c.d").system_text == "Be brief."


def test_partials_always_come_from_bundled_even_when_host_is_overridden(bundled, tmp_path):
    (tmp_path / "c.d.prompt").write_text(
        "---\nid: c.d\ndescription: d\noverridable: true\n---\nx\n"
    )
    bundled.reload()
    lib = PromptLibrary(
        bundled=bundled,
        db_body_for=lambda pid: ("{{> partials.tone}}", 1) if pid == "c.d" else None,
    )
    assert lib.render("c.d").system_text == "Be brief."


def test_checksum_reflects_the_body_actually_used(bundled):
    lib = PromptLibrary(bundled=bundled, db_body_for=lambda pid: ("Db text.", 2))
    a = lib.render("a.b")
    b = PromptLibrary(bundled=bundled).render("a.b")
    assert a.checksum != b.checksum


def test_ids_come_from_the_bundled_store(bundled):
    lib = PromptLibrary(bundled=bundled)
    assert "a.b" in lib.ids()


def test_spec_exposes_frontmatter(bundled):
    lib = PromptLibrary(bundled=bundled)
    assert lib.spec("locked").overridable is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_library.py -q`
Expected: FAIL — `No module named 'shared.prompts.library'`

- [ ] **Step 3: Write the implementation**

`shared/prompts/library.py`:

```python
"""The prompt library facade — the one entry point for rendering a prompt."""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from shared.prompts.bundled import BundledStore
from shared.prompts.render import render_body, split_sections
from shared.prompts.spec import PromptSpec, body_checksum
from shared.prompts.types import PromptSource, RenderedPrompt, RequestScope
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DbBodyFor = Callable[[str], Optional[Tuple[str, int]]]
GDocBodyFor = Callable[[str], Optional[str]]


class PromptLibrary:
    """Resolves, renders and reports provenance for every prompt in Anansi.

    Resolution order per id: DB override, then attached Google Doc, then the
    bundled file. Frontmatter always comes from the bundled file, so an override
    can supply body text but can never change a prompt's overridability, output
    schema or access lists.
    """

    def __init__(
        self,
        bundled: Optional[BundledStore] = None,
        db_body_for: Optional[DbBodyFor] = None,
        gdoc_body_for: Optional[GDocBodyFor] = None,
    ) -> None:
        self._bundled = bundled or BundledStore()
        self._db_body_for = db_body_for
        self._gdoc_body_for = gdoc_body_for

    # ── introspection ────────────────────────────────────────────────────────
    def ids(self) -> List[str]:
        return self._bundled.ids()

    def spec(self, prompt_id: str) -> PromptSpec:
        return self._bundled.get(prompt_id)

    def reload(self) -> None:
        self._bundled.reload()

    # ── resolution ───────────────────────────────────────────────────────────
    def _resolve_body(self, spec: PromptSpec) -> Tuple[str, PromptSource, Optional[int]]:
        if not spec.overridable:
            return spec.body, PromptSource.BUNDLED, None

        if self._db_body_for is not None:
            try:
                found = self._db_body_for(spec.id)
                if found:
                    return found[0], PromptSource.DB, found[1]
            except Exception:
                LOGGER.warning(
                    f"Prompt override lookup failed for '{spec.id}'; "
                    f"falling through to doc/bundled",
                    exc_info=True,
                )

        if self._gdoc_body_for is not None:
            try:
                body = self._gdoc_body_for(spec.id)
                if body:
                    return body, PromptSource.GDOC, None
            except Exception:
                LOGGER.warning(
                    f"Prompt Google Doc lookup failed for '{spec.id}'; using bundled",
                    exc_info=True,
                )

        return spec.body, PromptSource.BUNDLED, None

    def _partial(self, prompt_id: str) -> str:
        """Partials always come from the bundled store.

        A partial is shared infrastructure; letting it be overridden would make
        one operator's edit silently change every prompt that includes it.
        """
        return self._bundled.get(prompt_id).body

    def render(
        self,
        prompt_id: str,
        vars: Optional[Dict[str, object]] = None,
        scope: Optional[RequestScope] = None,
    ) -> RenderedPrompt:
        """Resolve, render and return a prompt with full provenance."""
        spec = self._bundled.get(prompt_id)
        body, source, version = self._resolve_body(spec)

        rendered = render_body(body, vars or {}, spec.variables, self._partial)
        system_text, context_text = split_sections(rendered, spec.sections)

        result = RenderedPrompt(
            prompt_id=prompt_id,
            system_text=system_text,
            context_text=context_text,
            source=source,
            version=version,
            checksum=body_checksum(body),
        )
        LOGGER.debug(f"Rendered prompt {result.provenance()}")
        return result

    def text(self, prompt_id: str, **vars: object) -> str:
        """Convenience for single-channel prompts: the full rendered body."""
        rendered = self.render(prompt_id, vars=vars)
        if rendered.context_text:
            return f"{rendered.system_text}\n\n{rendered.context_text}"
        return rendered.system_text


def _build_default_library() -> PromptLibrary:
    from shared.prompts.gdoc import GDocStore

    return PromptLibrary(gdoc_body_for=GDocStore().body_for)


PROMPTS = _build_default_library()
```

Append to `shared/prompts/__init__.py`:

```python
from shared.prompts.library import PROMPTS, PromptLibrary

__all__ += ["PROMPTS", "PromptLibrary"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_library.py -q`
Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/library.py shared/prompts/__init__.py
git add -f shared/tests/test_prompt_library.py
git commit -m "feat(prompts): add PromptLibrary with layered resolution"
```

---

### Task 7: Extract the four instruction documents

**Files:**
- Create: `shared/prompts/library/customer.system.prompt`
- Create: `shared/prompts/library/staff.system.prompt`
- Create: `shared/prompts/library/experts.definitions.prompt`
- Create: `shared/prompts/library/troubleshooting.procedures.prompt`
- Create: `shared/prompts/library/verification.criteria.prompt`
- Create: `shared/prompts/library/ticketing.correlation.prompt`
- Test: `shared/tests/test_prompt_library_contents.py`

The four files under `chat_orchestrator/instructions/` move verbatim. Do not reword, reformat or "improve" any prompt text in this task — a parity harness in Task 12 compares byte-for-byte.

- [ ] **Step 1: Move the bodies verbatim**

For each, the body is the existing file's content with its leading HTML comment header stripped (the old parser stripped `<!-- ... -->` before splitting, so keeping it would change the rendered output).

```bash
git mv chat_orchestrator/instructions/customer_instructions.md shared/prompts/library/customer.system.prompt
git mv chat_orchestrator/instructions/staff_instructions.md shared/prompts/library/staff.system.prompt
git mv chat_orchestrator/instructions/expert_instructions.md shared/prompts/library/experts.definitions.prompt
git mv chat_orchestrator/instructions/alert_correlation_instructions.md shared/prompts/library/ticketing.correlation.prompt
```

Then prepend frontmatter to each and delete any leading `<!-- ... -->` block from the body.

`customer.system.prompt`:

```yaml
---
id: customer.system
description: Customer-mode system instructions and context for Telegram support.
owner: ops
overridable: true
output: text
sections: [system_instructions]
knowledge_tags: [customer_support, troubleshooting]
access:
  view: [ops, eng]
  edit: [ops]
  publish: [ops]
---
```

`staff.system.prompt` — identical frontmatter except:

```yaml
id: staff.system
description: Staff-mode system instructions and context for internal users.
knowledge_tags: [grid_ops, customer_support, troubleshooting]
```

`experts.definitions.prompt`:

```yaml
---
id: experts.definitions
description: Expert, workflow and packet-type definitions parsed into ExpertConfig objects.
owner: eng
overridable: true
output: text
sections: []
knowledge_tags: []
access:
  view: [ops, eng]
  edit: [eng]
  publish: [eng]
---
```

`ticketing.correlation.prompt` — **not overridable**; this is the policy `correlation_rules.py` protects:

```yaml
---
id: ticketing.correlation
description: Alert-correlation grouping policy. Versioned with the application; not editable at runtime.
owner: eng
overridable: false
output: text
sections: []
knowledge_tags: []
access:
  view: [ops, eng]
  edit: []
  publish: []
---
```

- [ ] **Step 2: Create the two prompts that had no bundled fallback**

`troubleshooting.procedures.prompt` and `verification.criteria.prompt` currently exist only as Google Docs — there is no bundled file, and when the doc is absent the feature silently turns off. Create minimal, honest defaults that preserve that behavior by being explicitly empty-ish rather than inventing content.

`shared/prompts/library/troubleshooting.procedures.prompt`:

```yaml
---
id: troubleshooting.procedures
description: Shared troubleshooting procedures appended to customer and staff system instructions.
owner: ops
overridable: true
output: text
sections: []
knowledge_tags: [troubleshooting]
access:
  view: [ops, eng]
  edit: [ops]
  publish: [ops]
---
```

with body:

```markdown
# Troubleshooting Steps For Common Issues

No troubleshooting procedures have been configured yet. Add them here, or
attach a Google Doc to this prompt, and they will be appended to both the
customer and staff system instructions.
```

`shared/prompts/library/verification.criteria.prompt`:

```yaml
---
id: verification.criteria
description: LLM-as-judge criteria for verifying customer-facing responses before sending.
owner: eng
overridable: false
output: text
sections: []
knowledge_tags: []
access:
  view: [ops, eng]
  edit: []
  publish: []
---
```

with body:

```markdown
# Verification Criteria

Check the drafted response against these criteria and reply with a verdict.

- It answers the question actually asked.
- Every factual claim is supported by the tool output or context provided.
- It exposes no internal identifiers, stack traces, table names or tool names.
- It states clearly when information is unavailable rather than guessing.
- Its tone is respectful and appropriate for a customer of a utility service.
```

- [ ] **Step 3: Write the test**

```python
"""Every prompt in the library parses, and the protected set stays protected."""

import pytest

from shared.prompts import PROMPTS

NON_OVERRIDABLE = {
    "ticketing.correlation",
    "verification.criteria",
}


def test_library_is_not_empty():
    assert len(PROMPTS.ids()) >= 6


def test_every_prompt_parses_and_has_a_description():
    for prompt_id in PROMPTS.ids():
        spec = PROMPTS.spec(prompt_id)
        assert spec.description.strip(), f"{prompt_id} has an empty description"


def test_every_prompt_declares_an_owner_we_recognise():
    for prompt_id in PROMPTS.ids():
        assert PROMPTS.spec(prompt_id).owner in {"ops", "eng"}


@pytest.mark.parametrize("prompt_id", sorted(NON_OVERRIDABLE))
def test_protected_prompts_are_not_overridable(prompt_id):
    assert PROMPTS.spec(prompt_id).overridable is False


def test_customer_system_still_has_a_system_instructions_section():
    assert PROMPTS.render("customer.system").system_text.strip()
```

- [ ] **Step 4: Run the test**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_library_contents.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/library/ chat_orchestrator/instructions/
git add -f shared/tests/test_prompt_library_contents.py
git commit -m "refactor(prompts): move bundled instruction docs into the library"
```

---

### Task 8: Extract the ingestion-expert prompts

**Files:**
- Create: `shared/prompts/library/ingestion.classify_document.prompt`
- Create: `shared/prompts/library/ingestion.detect_contradictions.prompt`
- Create: `shared/prompts/library/ingestion.extract_entities.prompt`
- Create: `shared/prompts/library/ingestion.improve_content.quality_eval.prompt`
- Create: `shared/prompts/library/ingestion.improve_content.modification.prompt`
- Create: `shared/prompts/library/ingestion.improve_content.naming.prompt`
- Create: `shared/prompts/library/ingestion.fetch_document.type_selection.prompt`
- Modify: `chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/classify_document.py:24`
- Modify: `chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/detect_contradictions.py:33`
- Modify: `chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/extract_entities.py:24`
- Modify: `chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/improve_content.py:28,57,74`
- Modify: `chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/fetch_document.py:48`

All seven are JSON-emitting machinery: `overridable: false`.

- [ ] **Step 1: Copy each existing literal verbatim into a `.prompt` file**

Each body is the existing constant's text, unchanged. Read it from the exact location below — do not retype or reword it, and do not "improve" it; Task 12 pins the result:

| New file (`shared/prompts/library/…`) | Copy the body from |
|---|---|
| `ingestion.classify_document.prompt` | `CLASSIFICATION_PROMPT`, `classify_document.py:24` |
| `ingestion.detect_contradictions.prompt` | `CONTRADICTION_PROMPT`, `detect_contradictions.py:33` |
| `ingestion.extract_entities.prompt` | `EXTRACTION_PROMPT`, `extract_entities.py:24` |
| `ingestion.improve_content.quality_eval.prompt` | `QUALITY_EVAL_PROMPT`, `improve_content.py:28` |
| `ingestion.improve_content.modification.prompt` | `MODIFICATION_PROMPT`, `improve_content.py:57` |
| `ingestion.improve_content.naming.prompt` | `NAMING_PROMPT`, `improve_content.py:74` |
| `ingestion.fetch_document.type_selection.prompt` | `TYPE_SELECTION_PROMPT`, `fetch_document.py:48` |

Existing literals interpolate with `{name}` (via `.format(...)`). Convert every placeholder to `{{name}}` and list each one in `variables` — an undeclared placeholder now raises at render time rather than substituting silently.

The frontmatter for each follows this shape; substitute the id, the description, the variables, and for `output: json` a schema matching what the call site's parser actually reads.

```yaml
---
id: ingestion.classify_document
description: Classifies an ingested document into exactly one knowledge-base category.
owner: eng
overridable: false
output: json
schema:
  type: object
  required: [category]
  properties:
    category: {type: string}
sections: []
variables: [content]
access:
  view: [ops, eng]
  edit: []
  publish: []
---
```

Repeat for the other six rows of the table above, copying each literal's text unchanged into the body and deriving `variables` from that literal's own placeholders.

- [ ] **Step 2: Write the parity test**

```python
"""Extracted ingestion prompts still render exactly what the literals produced."""

from shared.prompts import PROMPTS

IDS = [
    "ingestion.classify_document",
    "ingestion.detect_contradictions",
    "ingestion.extract_entities",
    "ingestion.improve_content.quality_eval",
    "ingestion.improve_content.modification",
    "ingestion.improve_content.naming",
    "ingestion.fetch_document.type_selection",
]


def test_all_ingestion_prompts_exist():
    for prompt_id in IDS:
        assert prompt_id in PROMPTS.ids()


def test_all_ingestion_prompts_are_locked():
    for prompt_id in IDS:
        assert PROMPTS.spec(prompt_id).overridable is False


def test_json_prompts_declare_a_schema():
    for prompt_id in IDS:
        spec = PROMPTS.spec(prompt_id)
        if spec.output == "json":
            assert spec.schema, f"{prompt_id} declares json output but no schema"
```

- [ ] **Step 3: Replace each literal with a library call**

In `classify_document.py`, delete `CLASSIFICATION_PROMPT` and its `.format(...)` use:

```python
from shared.prompts import PROMPTS

prompt = PROMPTS.text("ingestion.classify_document", content=content)
```

Apply the same shape to the other six. Delete every extracted module-level constant.

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_ingestion.py tests/experts -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/library/ chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/
git add -f shared/tests/test_prompt_ingestion.py
git commit -m "refactor(prompts): move ingestion expert prompts into the library"
```

---

### Task 9: Extract the service prompts

**Files:**
- Create: `shared/prompts/library/context_filter.relevance.prompt` (from `services/context_filter.py:30`)
- Create: `shared/prompts/library/conversation.summarize.prompt` (from `services/conversation_summarizer.py:40`)
- Create: `shared/prompts/library/thread_assignment.classify.prompt` (from `services/thread_assignment.py:283`)
- Create: `shared/prompts/library/intent_router.route.prompt` (from `services/intent_router.py`)
- Create: `shared/prompts/library/procedure.suggest.prompt` (from `services/procedure_provider.py:195`)
- Create: `shared/prompts/library/procedure.match.prompt` (from `services/procedure_provider.py:270`)
- Create: `shared/prompts/library/verification.sanitize.prompt` (from `services/verification_service.py:385`)
- Create: `shared/prompts/library/verification.sanitize_system.prompt` (from `services/verification_service.py:417`)
- Modify: each source file above
- Test: `shared/tests/test_prompt_services.py`

- [ ] **Step 1: Extract each literal verbatim into a `.prompt` file**

Each new file's body is the literal at the source location named in the **Files** list above — copy it unchanged, convert `{name}` placeholders to `{{name}}`, and declare each in `variables`. Frontmatter follows the shape in Task 8 Step 1.

Override policy per prompt, following the spec's starting list:

| Prompt id | `overridable` | Rationale |
|---|---|---|
| `context_filter.relevance` | `false` | Structured output consumed by a parser |
| `conversation.summarize` | `true` | Free text; safe for ops to tune |
| `thread_assignment.classify` | `false` | Structured routing output |
| `intent_router.route` | `false` | Structured routing output |
| `procedure.suggest` | `true` | Free text |
| `procedure.match` | `false` | Structured output |
| `verification.sanitize` | `false` | Gates customer-facing output |
| `verification.sanitize_system` | `false` | Gates customer-facing output |

- [ ] **Step 2: Write the test**

```python
"""Service prompts are in the library with the intended override policy."""

import pytest

from shared.prompts import PROMPTS

OVERRIDABLE = {"conversation.summarize", "procedure.suggest"}
LOCKED = {
    "context_filter.relevance",
    "thread_assignment.classify",
    "intent_router.route",
    "procedure.match",
    "verification.sanitize",
    "verification.sanitize_system",
}


@pytest.mark.parametrize("prompt_id", sorted(OVERRIDABLE | LOCKED))
def test_service_prompt_exists(prompt_id):
    assert prompt_id in PROMPTS.ids()


@pytest.mark.parametrize("prompt_id", sorted(LOCKED))
def test_locked_service_prompts_are_locked(prompt_id):
    assert PROMPTS.spec(prompt_id).overridable is False


@pytest.mark.parametrize("prompt_id", sorted(OVERRIDABLE))
def test_ops_editable_service_prompts_are_overridable(prompt_id):
    assert PROMPTS.spec(prompt_id).overridable is True
```

- [ ] **Step 3: Replace each literal with `PROMPTS.text(...)`**

Example, `context_filter.py` — delete `CONTEXT_FILTER_PROMPT` and call:

```python
from shared.prompts import PROMPTS

prompt = PROMPTS.text(
    "context_filter.relevance",
    incoming_message=incoming_message,
    numbered_messages=numbered_messages,
)
```

Match each call's existing `.format(...)` / f-string arguments to the `variables` declared in the corresponding `.prompt` file.

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_services.py tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/library/ chat_orchestrator/orchestrator/services/
git add -f shared/tests/test_prompt_services.py
git commit -m "refactor(prompts): move orchestrator service prompts into the library"
```

---

### Task 10: Extract the remaining prompts, including the env-var one

**Files:**
- Create: `shared/prompts/library/doc_editing.edit_highlighted.prompt` (from `shared/utils/doc_editing.py:375`)
- Create: `shared/prompts/library/doc_editor.locate_edits.prompt` (from `experts/handlers/doc_editor/process_doc_edits.py:38`)
- Create: `shared/prompts/library/knowledge.summarize_topic.prompt` (from `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py:180`)
- Create: `shared/prompts/library/ticketing.jira_issue_types.prompt` (from `services/ticketing/jira_issue_types.py`)
- Create: `shared/prompts/library/gtr.analysis_conversation.prompt` (from `experts/handlers/grids_technical_reviewer/gtr_analysis_conversation.py`)
- Create: `shared/prompts/library/grafana.panel_description.prompt` (from the `GRAFANA_PANEL_DESCRIPTION_PROMPT` env var default)
- Modify: `anansi_app/scripts/grafana_indexer_v2.py:765`, `anansi_app/scripts/grafana_indexer_incremental.py:62`
- Modify: `shared/config/flag_registry.py:830`
- Modify: `anansi_app/nicegui_app/pages/settings.py:604`
- Test: `shared/tests/test_prompt_misc.py`

- [ ] **Step 1: Extract the five in-code literals as in Task 9**

`gtr.analysis_conversation` and `knowledge.summarize_topic` are free text → `overridable: true`, owner `ops`. The rest → `overridable: false`.

- [ ] **Step 2: Retire the env-var prompt**

The body of `grafana.panel_description.prompt` is the current default value of `GRAFANA_PANEL_DESCRIPTION_PROMPT` from `shared/config/flags.env.example:294`, copied verbatim:

```yaml
---
id: grafana.panel_description
description: Generates an LLM tool description for a Grafana dashboard panel.
owner: ops
overridable: true
output: text
sections: []
variables: []
access:
  view: [ops, eng]
  edit: [ops]
  publish: [ops]
---
You are a system that generates tool descriptions for Grafana dashboard panels. Given a panel with title, description, query, and dashboard variables, create a concise tool description that explains what data this panel shows and what variables it requires. Format: A tool description suitable for an LLM to understand when to use this panel.
```

In both indexer scripts, replace the `os.getenv("GRAFANA_PANEL_DESCRIPTION_PROMPT", ...)` read with:

```python
from shared.prompts import PROMPTS

system_prompt = PROMPTS.text("grafana.panel_description")
```

Remove the flag from `flag_registry.py` and its special case from `settings.py:604`.

Regenerate the env example — the registry test enforces that it stays in sync:

```bash
python -m shared.config.flag_registry > shared/config/flags.env.example
```

- [ ] **Step 3: Write the test**

```python
"""The last prompts to leave code and env vars are in the library."""

import os

from shared.config import flag_registry
from shared.prompts import PROMPTS

IDS = [
    "doc_editing.edit_highlighted",
    "doc_editor.locate_edits",
    "knowledge.summarize_topic",
    "ticketing.jira_issue_types",
    "gtr.analysis_conversation",
    "grafana.panel_description",
]


def test_all_remaining_prompts_exist():
    for prompt_id in IDS:
        assert prompt_id in PROMPTS.ids()


def test_grafana_prompt_flag_is_retired():
    assert "GRAFANA_PANEL_DESCRIPTION_PROMPT" not in flag_registry.FLAGS


def test_grafana_prompt_renders_without_the_env_var(monkeypatch):
    monkeypatch.delenv("GRAFANA_PANEL_DESCRIPTION_PROMPT", raising=False)
    assert "Grafana dashboard panels" in PROMPTS.text("grafana.panel_description")


def test_no_prompt_text_remains_in_the_registry():
    for name, flag in flag_registry.FLAGS.items():
        assert not name.endswith("_PROMPT"), f"{name} still holds prompt text"
```

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests -q && pytest tests/test_flag_registry.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/library/ shared/utils/doc_editing.py shared/config/ anansi_app/ mcp_servers/ chat_orchestrator/orchestrator/
git add -f shared/tests/test_prompt_misc.py
git commit -m "refactor(prompts): retire the env-var prompt and extract the last literals"
```

---

### Task 11: Convert the three provider services and delete the duplicated parsers

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/instructions_provider.py`
- Modify: `chat_orchestrator/orchestrator/services/expert_instructions_provider.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py`
- Modify: `chat_orchestrator/orchestrator/services/procedure_provider.py`
- Test: `chat_orchestrator/tests/test_instructions_provider_library.py`

This is the task that deletes duplication: `_load_fallback_instructions`, `_load_fallback_expert_instructions`, both ~55-line composition blocks, and the two copies of `MAX_EXAMPLES_WORDS`.

- [ ] **Step 1: Write the failing test**

```python
"""InstructionsProvider now sources everything through the prompt library."""

import pytest

from orchestrator.services import instructions_provider as ip


def test_fallback_loader_is_gone():
    assert not hasattr(ip, "_load_fallback_instructions")


def test_examples_word_cap_lives_in_one_place():
    import inspect

    source = inspect.getsource(ip)
    assert source.count("MAX_EXAMPLES_WORDS") <= 2, "the cap is defined more than once"


@pytest.mark.asyncio
async def test_customer_instructions_come_from_the_library():
    system, _context = await ip.InstructionsProvider().get_customer_instructions()
    assert system.strip()


@pytest.mark.asyncio
async def test_staff_missing_section_no_longer_silently_degrades(monkeypatch):
    """The staff path used to substitute a generic assistant prompt on error.

    It must now raise, exactly as the customer path already did.
    """
    from shared.prompts.types import PromptRenderError

    def boom(*args, **kwargs):
        raise PromptRenderError("missing section")

    monkeypatch.setattr(ip.PROMPTS, "render", boom)
    with pytest.raises(PromptRenderError):
        await ip.InstructionsProvider()._get_staff_instructions_from_doc()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest tests/test_instructions_provider_library.py -q`
Expected: FAIL — `_load_fallback_instructions` still exists

- [ ] **Step 3: Rewrite the three providers**

In `instructions_provider.py`, delete `_load_fallback_instructions`, `_INSTRUCTIONS_DIR`, and the duplicated composition in both methods. Replace the two loaders with:

```python
from shared.prompts import PROMPTS

MAX_EXAMPLES_WORDS = 5000


async def get_customer_instructions(self) -> tuple[str, Optional[str]]:
    rendered = PROMPTS.render("customer.system")
    return rendered.system_text, self._cap_context(rendered.context_text)


async def _get_staff_instructions_from_doc(self) -> tuple[str, Optional[str]]:
    rendered = PROMPTS.render("staff.system")
    self._load_staff_groups(rendered.context_text)
    return rendered.system_text, self._cap_context(rendered.context_text)
```

with one shared budgeting helper replacing the two copies:

```python
def _cap_context(self, context: Optional[str]) -> Optional[str]:
    """Apply the context budget once, for both modes."""
    if not context:
        return None
    if len(context) <= MAX_CONTEXT_CHARS:
        return context
    LOGGER.warning(
        f"Context message exceeds budget: {len(context)} > {MAX_CONTEXT_CHARS} chars; "
        f"truncating at the last paragraph boundary"
    )
    clipped = context[:MAX_CONTEXT_CHARS]
    boundary = clipped.rfind("\n\n")
    if boundary > MAX_CONTEXT_CHARS * 0.8:
        clipped = clipped[:boundary]
    return clipped + "\n\n[Context truncated due to size limits]"
```

Keep `get_troubleshooting_procedures` and `get_verification_instructions` but source them from `PROMPTS.text("troubleshooting.procedures")` and `PROMPTS.text("verification.criteria")`.

In `expert_instructions_provider.py`, delete `_load_fallback_expert_instructions` and `_INSTRUCTIONS_DIR`; feed `_parse_expert_sections` from the library body split on `# ` headings. In `correlation_rules.py`, `get_correlation_instructions` becomes:

```python
def get_correlation_instructions() -> Dict[str, str]:
    """Bundled correlation policy. Deliberately not overridable at runtime."""
    return {"system_instructions": PROMPTS.text("ticketing.correlation")}
```

Delete `_MINIMAL_FALLBACK_INSTRUCTIONS` — the bundled file is now the guaranteed floor, so the fallback-to-a-fallback is dead code.

In `procedure_provider.py`, replace the direct `fetch_google_doc_markdown(doc_id)` with `PROMPTS.text("customer.system")` and delete the never-expiring `_cached_procedures` field (the library caches).

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest tests/ -q && pytest ../shared/tests -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/
git add -f chat_orchestrator/tests/test_instructions_provider_library.py
git commit -m "refactor(prompts): route providers through the library, delete duplicate parsers"
```

---

### Task 12: Parity harness

**Files:**
- Test: `chat_orchestrator/tests/test_prompt_parity.py`

The defense for a refactor of this shape. It pins every prompt's rendered output so a later change that alters text fails loudly.

- [ ] **Step 1: Write the snapshot test**

```python
"""Snapshot every prompt so accidental text drift fails CI.

Regenerate deliberately with:
    pytest tests/test_prompt_parity.py --snapshot-update
and review the diff — a change here changes what the model sees.
"""

import hashlib
import json
import pathlib

import pytest

from shared.prompts import PROMPTS

SNAPSHOT = pathlib.Path(__file__).parent / "prompt_checksums.json"

# Prompts whose bodies need variables to render; supply representative values.
SAMPLE_VARS = {
    "ingestion.classify_document": {"content": "sample"},
    "context_filter.relevance": {"incoming_message": "m", "numbered_messages": "1. a"},
}


def _checksum(prompt_id: str) -> str:
    spec = PROMPTS.spec(prompt_id)
    variables = SAMPLE_VARS.get(prompt_id, {name: "x" for name in spec.variables})
    rendered = PROMPTS.render(prompt_id, vars=variables)
    payload = f"{rendered.system_text}\x00{rendered.context_text or ''}"
    return hashlib.sha256(payload.encode()).hexdigest()


def test_every_prompt_renders_without_error():
    for prompt_id in PROMPTS.ids():
        _checksum(prompt_id)


def test_prompt_text_has_not_drifted():
    current = {prompt_id: _checksum(prompt_id) for prompt_id in sorted(PROMPTS.ids())}
    if not SNAPSHOT.exists():
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        pytest.skip("snapshot created; re-run to verify")
    expected = json.loads(SNAPSHOT.read_text())
    assert current == expected, (
        "Prompt text changed. If deliberate, delete "
        f"{SNAPSHOT.name}, re-run, and review the diff in the commit."
    )


def test_snapshot_covers_every_prompt():
    if SNAPSHOT.exists():
        assert set(json.loads(SNAPSHOT.read_text())) == set(PROMPTS.ids())
```

- [ ] **Step 2: Generate the snapshot**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest tests/test_prompt_parity.py -q`
Expected: first run skips (snapshot created), second run passes

- [ ] **Step 3: Verify no literals remain**

```bash
grep -rnE '^[A-Z_]*PROMPT[A-Z_]* *=' --include="*.py" chat_orchestrator/ shared/ mcp_servers/ anansi_app/ | grep -v ".venv"
```

Expected: no output. Any hit is an unextracted prompt — extract it before continuing.

- [ ] **Step 4: Run the whole suite**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest tests/ -q && pytest ../shared/tests -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -f chat_orchestrator/tests/test_prompt_parity.py chat_orchestrator/tests/prompt_checksums.json
git commit -m "test(prompts): pin every prompt's rendered output against drift"
```

**Phase 1 is now complete.** Every prompt in Anansi resolves through one loader. Nothing user-visible has changed.

---

# Phase 2 — Live editing, versioning, access control

---

### Task 13: The SQL file

**Files:**
- Create: `db/migrations/0006_prompt_library.sql`
- Modify: `db/schema/chat_db.sql`

This is **the one SQL file** the user applies by hand in the Supabase query editor. It contains every table for Phases 2 and 3. Nothing is added to it later.

- [ ] **Step 1: Write the migration**

```sql
-- 0006_prompt_library.sql
-- Prompt library: versioned overrides, labels, doc bindings, knowledge modules.
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent: safe to
-- re-run. No application code requires these tables to exist — every DB path
-- degrades to bundled prompt resolution when they are absent.

BEGIN;

-- ── Prompt versions (append-only) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_versions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_id    text NOT NULL,
    version      integer NOT NULL,
    body         text NOT NULL,
    checksum     text NOT NULL,
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text NOT NULL,
    created_via  text NOT NULL DEFAULT 'ui',
    CONSTRAINT prompt_versions_via_chk CHECK (created_via IN ('ui', 'api', 'import')),
    CONSTRAINT prompt_versions_unique UNIQUE (prompt_id, version)
);

CREATE INDEX IF NOT EXISTS prompt_versions_prompt_idx
    ON prompt_versions (prompt_id, version DESC);

-- ── Labels (which version is live) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_labels (
    prompt_id    text NOT NULL,
    label        text NOT NULL,
    version      integer NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    PRIMARY KEY (prompt_id, label)
);

-- ── Google Doc bindings (one adapter, configured per prompt) ─────────────────
CREATE TABLE IF NOT EXISTS prompt_doc_bindings (
    prompt_id      text PRIMARY KEY,
    doc_id         text NOT NULL,
    last_synced_at timestamptz
);

-- ── Knowledge modules ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_modules (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug         text NOT NULL UNIQUE,
    title        text NOT NULL,
    summary      text NOT NULL,
    body         text NOT NULL,
    tags         text[] NOT NULL DEFAULT '{}',
    scope        text NOT NULL DEFAULT 'sector',
    mode         text NOT NULL DEFAULT 'pinned',
    source       text NOT NULL DEFAULT 'manual',
    source_ref   text,
    edit_groups  text[] NOT NULL DEFAULT '{}',
    version      integer NOT NULL DEFAULT 1,
    is_active    boolean NOT NULL DEFAULT true,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text,
    CONSTRAINT knowledge_modules_mode_chk CHECK (mode IN ('pinned', 'on_demand')),
    CONSTRAINT knowledge_modules_source_chk
        CHECK (source IN ('manual', 'gdoc', 'ingested'))
);

CREATE INDEX IF NOT EXISTS knowledge_modules_tags_idx
    ON knowledge_modules USING gin (tags);
CREATE INDEX IF NOT EXISTS knowledge_modules_active_idx
    ON knowledge_modules (is_active) WHERE is_active = true;

-- ── Per-prompt knowledge overrides ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_knowledge_overrides (
    prompt_id    text NOT NULL,
    module_id    uuid NOT NULL REFERENCES knowledge_modules (id) ON DELETE CASCADE,
    pinned       boolean NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text,
    PRIMARY KEY (prompt_id, module_id)
);

COMMIT;
```

- [ ] **Step 2: Mirror the tables into the schema reference**

Append the same five `CREATE TABLE` blocks to `db/schema/chat_db.sql` under a new `-- ── Prompt Library ──` heading, matching the file's existing style.

- [ ] **Step 3: Verify the SQL parses**

Run: `cd chat_orchestrator && source .venv/bin/activate && python -c "import pathlib; s=pathlib.Path('../db/migrations/0006_prompt_library.sql').read_text(); assert s.count('BEGIN;')==1 and s.count('COMMIT;')==1; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add db/migrations/0006_prompt_library.sql db/schema/chat_db.sql
git commit -m "feat(prompts): add the prompt library schema migration"
```

---

### Task 14: Access control

**Files:**
- Create: `shared/prompts/access.py`
- Modify: `anansi_app/grid_app/lib/perms.py`
- Modify: `shared/config/flag_registry.py`
- Test: `shared/tests/test_prompt_access.py`

The logic lives in `shared/` because the write API needs it and `shared` must not import from `anansi_app`. `perms.py` delegates, so the admin app keeps one RBAC entry point.

- [ ] **Step 1: Write the failing test**

```python
"""Per-prompt group access control."""

import pytest

from shared.prompts.access import can_edit_prompt, can_publish_prompt, can_view_prompt
from shared.prompts.spec import AccessSpec, PromptSpec


def _spec(prompt_id="a.b", overridable=True, **access):
    return PromptSpec(
        id=prompt_id,
        description="d",
        body="x",
        checksum="c",
        overridable=overridable,
        access=AccessSpec(**access),
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("PROMPT_EDITORS_OPS", "PROMPT_EDITORS_ENG", "PROMPT_ADMINS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GRID_DESIGN_DEV_NO_AUTH", raising=False)


def test_group_member_gets_the_verb(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    assert can_edit_prompt(_spec(edit=["ops"]), "ada@x.com") is True


def test_non_member_is_denied(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    assert can_edit_prompt(_spec(edit=["ops"]), "bob@x.com") is False


def test_edit_does_not_imply_publish(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    spec = _spec(edit=["ops"], publish=["eng"])
    assert can_edit_prompt(spec, "ada@x.com") is True
    assert can_publish_prompt(spec, "ada@x.com") is False


def test_admin_passes_every_verb_without_being_listed(monkeypatch):
    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    spec = _spec(view=[], edit=[], publish=[])
    assert can_view_prompt(spec, "root@x.com") is True
    assert can_edit_prompt(spec, "root@x.com") is True
    assert can_publish_prompt(spec, "root@x.com") is True


def test_non_overridable_beats_admin(monkeypatch):
    """A PR-only prompt is PR-only. Admin gets view, never edit."""
    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    spec = _spec(overridable=False)
    assert can_view_prompt(spec, "root@x.com") is True
    assert can_edit_prompt(spec, "root@x.com") is False
    assert can_publish_prompt(spec, "root@x.com") is False


def test_non_overridable_beats_an_explicit_grant(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    assert can_edit_prompt(_spec(overridable=False, edit=["ops"]), "ada@x.com") is False


def test_empty_whitelists_grant_nothing(monkeypatch):
    assert can_edit_prompt(_spec(edit=["ops"]), "ada@x.com") is False


def test_blank_email_is_denied(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    assert can_edit_prompt(_spec(edit=["ops"]), "") is False


def test_membership_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "Ada@X.com")
    assert can_edit_prompt(_spec(edit=["ops"]), "ada@x.com") is True


def test_dev_bypass_grants_everything_except_non_overridable(monkeypatch):
    monkeypatch.setenv("GRID_DESIGN_DEV_NO_AUTH", "1")
    assert can_edit_prompt(_spec(), "anyone@x.com") is True
    assert can_edit_prompt(_spec(overridable=False), "anyone@x.com") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_access.py -q`
Expected: FAIL — `No module named 'shared.prompts.access'`

- [ ] **Step 3: Write the implementation**

`shared/prompts/access.py`:

```python
"""Per-prompt, per-group access control for the prompt library.

Membership lives in env-var whitelists, parsed exactly like the four lists in
``anansi_app/grid_app/lib/perms.py`` so the app has one RBAC story. Bindings
live per prompt.

This module governs the admin UI and the write API only. It is never consulted
on the render path: ``PROMPTS.render`` serves users who are not logged into the
admin app at all, and these whitelists fail closed.
"""

from __future__ import annotations

import os
import re

from shared.prompts.spec import PromptSpec
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

GROUP_ENV_VARS = {
    "ops": "PROMPT_EDITORS_OPS",
    "eng": "PROMPT_EDITORS_ENG",
    "admin": "PROMPT_ADMINS",
}


def _parse(env_name: str) -> set[str]:
    raw = os.getenv(env_name, "")
    if not raw:
        return set()
    return {e.strip().lower() for e in re.split(r"[,;\n]+", raw) if e.strip()}


def _dev_bypass() -> bool:
    return os.getenv("GRID_DESIGN_DEV_NO_AUTH", "").lower() in ("1", "true", "yes")


def groups_for(email: str) -> set[str]:
    """Every group this email belongs to."""
    email = (email or "").lower()
    if not email:
        return set()
    return {name for name, var in GROUP_ENV_VARS.items() if email in _parse(var)}


def is_prompt_admin(email: str) -> bool:
    return "admin" in groups_for(email)


def _allows(spec: PromptSpec, verb: str, email: str) -> bool:
    # Rule 2 of the design: `overridable: false` beats every grant, admin
    # included. A PR-only prompt is PR-only.
    if verb in ("edit", "publish") and not spec.overridable:
        return False
    if _dev_bypass():
        return True
    # Rule 1: `admin` is implicit and never listed in frontmatter.
    if is_prompt_admin(email):
        return True
    bound = set(getattr(spec.access, verb))
    return bool(bound & groups_for(email))


def can_view_prompt(spec: PromptSpec, email: str) -> bool:
    if _dev_bypass() or is_prompt_admin(email):
        return True
    return bool(set(spec.access.view) & groups_for(email))


def can_edit_prompt(spec: PromptSpec, email: str) -> bool:
    allowed = _allows(spec, "edit", email)
    if not allowed:
        LOGGER.info(f"Prompt edit denied: {email or '<anonymous>'} on '{spec.id}'")
    return allowed


def can_publish_prompt(spec: PromptSpec, email: str) -> bool:
    allowed = _allows(spec, "publish", email)
    if not allowed:
        LOGGER.info(f"Prompt publish denied: {email or '<anonymous>'} on '{spec.id}'")
    return allowed
```

Append delegating wrappers to `anansi_app/grid_app/lib/perms.py`:

```python
# ── prompt library (logic lives in shared.prompts.access; delegated so this
# module stays the app's single RBAC entry point) ─────────────────────────────
def can_view_prompt(prompt_id: str, email: str | None = None) -> bool:
    from shared.prompts import PROMPTS
    from shared.prompts.access import can_view_prompt as _check

    return _check(PROMPTS.spec(prompt_id), email or current_email())


def can_edit_prompt(prompt_id: str, email: str | None = None) -> bool:
    from shared.prompts import PROMPTS
    from shared.prompts.access import can_edit_prompt as _check

    return _check(PROMPTS.spec(prompt_id), email or current_email())


def can_publish_prompt(prompt_id: str, email: str | None = None) -> bool:
    from shared.prompts import PROMPTS
    from shared.prompts.access import can_publish_prompt as _check

    return _check(PROMPTS.spec(prompt_id), email or current_email())
```

Register the three whitelists in `flag_registry.py` under the existing `documents` group, `editable=False` (they are host-owned access lists, like the other four):

```python
_s(
    "PROMPT_EDITORS_OPS",
    "",
    "Comma-separated emails in the ops prompt-editor group.",
    editable=False,
    group="documents",
    set_via="Set in the deployment environment alongside the other access whitelists.",
),
_s(
    "PROMPT_EDITORS_ENG",
    "",
    "Comma-separated emails in the engineering prompt-editor group.",
    editable=False,
    group="documents",
    set_via="Set in the deployment environment alongside the other access whitelists.",
),
_s(
    "PROMPT_ADMINS",
    "",
    "Comma-separated emails with full access to every prompt.",
    editable=False,
    group="documents",
    set_via="Set in the deployment environment alongside the other access whitelists.",
),
```

Regenerate the env example:

```bash
python -m shared.config.flag_registry > shared/config/flags.env.example
```

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_access.py tests/test_flag_registry.py -q`
Expected: PASS, 10 passed in the access module

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/access.py anansi_app/grid_app/lib/perms.py shared/config/
git add -f shared/tests/test_prompt_access.py
git commit -m "feat(prompts): add per-prompt group access control"
```

---

### Task 15: Override store

**Files:**
- Create: `shared/prompts/overrides.py`
- Test: `shared/tests/test_prompt_overrides.py`

- [ ] **Step 1: Write the failing test**

```python
"""Versioned prompt overrides with label-based publishing."""

import pytest

from shared.prompts.overrides import OverrideStore


class FakeTable:
    """Minimal in-memory stand-in for the supabase table API used here."""

    def __init__(self, rows):
        self.rows = rows


@pytest.fixture
def store():
    return OverrideStore(client=None)  # unconfigured: every read returns None


def test_unconfigured_store_returns_no_override(store):
    assert store.body_for("a.b") is None


def test_unconfigured_store_reports_not_configured(store):
    assert store.is_configured() is False


def test_propose_on_unconfigured_store_raises(store):
    with pytest.raises(RuntimeError, match="not configured"):
        store.propose("a.b", "body", note="n", actor="ada@x.com")


def test_next_version_starts_at_one():
    assert OverrideStore._next_version([]) == 1


def test_next_version_increments_past_the_highest():
    assert OverrideStore._next_version([{"version": 2}, {"version": 7}]) == 8


def test_label_map_indexes_by_prompt():
    rows = [
        {"prompt_id": "a.b", "label": "production", "version": 3},
        {"prompt_id": "c.d", "label": "production", "version": 1},
    ]
    assert OverrideStore._label_map(rows) == {"a.b": 3, "c.d": 1}


def test_label_map_ignores_non_production_labels():
    rows = [{"prompt_id": "a.b", "label": "staging", "version": 9}]
    assert OverrideStore._label_map(rows) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_overrides.py -q`
Expected: FAIL — `No module named 'shared.prompts.overrides'`

- [ ] **Step 3: Write the implementation**

`shared/prompts/overrides.py`:

```python
"""DB-backed prompt overrides: append-only versions, label-based publishing.

Two-level cache so a save in the admin app becomes visible to the bot without a
restart and without a query per render:

* the label map (prompt_id -> live version) is small and refreshed on a short
  TTL;
* bodies are content-addressed by checksum and never expire.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from shared.config.db_credentials import chat_db_service_key, chat_db_url
from shared.prompts.spec import body_checksum
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

LABEL_TTL_SECONDS = 60
PRODUCTION = "production"


class OverrideStore:
    """Reads and writes prompt_versions / prompt_labels."""

    def __init__(self, client: Any = None) -> None:
        self._client = client
        self._label_cache: Optional[Dict[str, int]] = None
        self._label_expires: float = 0.0
        self._body_cache: Dict[Tuple[str, int], str] = {}

    # ── construction ─────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls) -> "OverrideStore":
        url, key = chat_db_url(), chat_db_service_key()
        if not (url and key):
            LOGGER.info("Prompt override store not configured; bundled prompts only")
            return cls(client=None)
        try:
            from supabase import create_client

            return cls(client=create_client(url, key))
        except Exception:
            LOGGER.warning("Could not build the prompt override client", exc_info=True)
            return cls(client=None)

    def is_configured(self) -> bool:
        return self._client is not None

    # ── pure helpers (unit-tested directly) ──────────────────────────────────
    @staticmethod
    def _next_version(rows: List[Dict[str, Any]]) -> int:
        return max((int(r["version"]) for r in rows), default=0) + 1

    @staticmethod
    def _label_map(rows: List[Dict[str, Any]]) -> Dict[str, int]:
        return {
            r["prompt_id"]: int(r["version"])
            for r in rows
            if r.get("label") == PRODUCTION
        }

    # ── reads ────────────────────────────────────────────────────────────────
    def invalidate(self) -> None:
        self._label_cache = None
        self._label_expires = 0.0

    def _labels(self) -> Dict[str, int]:
        if self._label_cache is not None and time.time() < self._label_expires:
            return self._label_cache
        if not self._client:
            return {}
        try:
            result = (
                self._client.table("prompt_labels")
                .select("prompt_id, label, version")
                .eq("label", PRODUCTION)
                .execute()
            )
            self._label_cache = self._label_map(result.data or [])
        except Exception:
            LOGGER.warning("Prompt label fetch failed; using bundled prompts", exc_info=True)
            self._label_cache = {}
        self._label_expires = time.time() + LABEL_TTL_SECONDS
        return self._label_cache

    def body_for(self, prompt_id: str) -> Optional[Tuple[str, int]]:
        """The live body and version for this prompt, or None."""
        version = self._labels().get(prompt_id)
        if version is None:
            return None

        cached = self._body_cache.get((prompt_id, version))
        if cached is not None:
            return cached, version

        try:
            result = (
                self._client.table("prompt_versions")
                .select("body")
                .eq("prompt_id", prompt_id)
                .eq("version", version)
                .single()
                .execute()
            )
        except Exception:
            LOGGER.warning(
                f"Prompt body fetch failed for '{prompt_id}' v{version}; using bundled",
                exc_info=True,
            )
            return None

        if not result.data:
            return None
        body = result.data["body"]
        self._body_cache[(prompt_id, version)] = body
        return body, version

    def versions(self, prompt_id: str) -> List[Dict[str, Any]]:
        if not self._client:
            return []
        result = (
            self._client.table("prompt_versions")
            .select("version, note, created_at, created_by, created_via, checksum")
            .eq("prompt_id", prompt_id)
            .order("version", desc=True)
            .execute()
        )
        return result.data or []

    # ── writes ───────────────────────────────────────────────────────────────
    def propose(self, prompt_id: str, body: str, note: str, actor: str, via: str = "ui") -> int:
        """Append a new version. Does NOT make it live."""
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        existing = (
            self._client.table("prompt_versions")
            .select("version")
            .eq("prompt_id", prompt_id)
            .execute()
        )
        version = self._next_version(existing.data or [])
        self._client.table("prompt_versions").insert(
            {
                "prompt_id": prompt_id,
                "version": version,
                "body": body,
                "checksum": body_checksum(body),
                "note": note,
                "created_by": actor,
                "created_via": via,
            }
        ).execute()
        LOGGER.info(f"Proposed {prompt_id} v{version} by {actor} via {via}")
        return version

    def publish(self, prompt_id: str, version: int, actor: str) -> None:
        """Point the production label at a version, making it live."""
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        self._client.table("prompt_labels").upsert(
            {
                "prompt_id": prompt_id,
                "label": PRODUCTION,
                "version": version,
                "updated_by": actor,
            }
        ).execute()
        self.invalidate()
        LOGGER.info(f"Published {prompt_id} v{version} by {actor}")

    def revert_to_default(self, prompt_id: str, actor: str) -> None:
        """Drop the label so resolution falls back to the bundled file."""
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        self._client.table("prompt_labels").delete().eq("prompt_id", prompt_id).eq(
            "label", PRODUCTION
        ).execute()
        self.invalidate()
        LOGGER.info(f"Reverted {prompt_id} to the bundled default by {actor}")
```

Add the doc-binding lookup that makes `prompt_doc_bindings` live — a doc attached from the admin page wins over the legacy env var:

```python
    def doc_id_for(self, prompt_id: str) -> Optional[str]:
        """Attached doc id for this prompt, falling back to the legacy env var."""
        from shared.prompts.gdoc import legacy_doc_id_for

        if self._client:
            try:
                result = (
                    self._client.table("prompt_doc_bindings")
                    .select("doc_id")
                    .eq("prompt_id", prompt_id)
                    .execute()
                )
                if result.data:
                    return str(result.data[0]["doc_id"])
            except Exception:
                LOGGER.warning(
                    f"Doc binding lookup failed for '{prompt_id}'; using the legacy env var",
                    exc_info=True,
                )
        return legacy_doc_id_for(prompt_id)
```

Wire both into the default library in `shared/prompts/library.py`:

```python
def _build_default_library() -> PromptLibrary:
    from shared.prompts.gdoc import GDocStore
    from shared.prompts.overrides import OverrideStore

    overrides = OverrideStore.from_env()
    return PromptLibrary(
        db_body_for=overrides.body_for,
        gdoc_body_for=GDocStore(doc_id_for=overrides.doc_id_for).body_for,
    )
```

(Task 16 replaces `db_body_for=overrides.body_for` with `overrides=overrides` once the write API needs the full store.)

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_overrides.py ../shared/tests/test_prompt_library.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/overrides.py shared/prompts/library.py
git add -f shared/tests/test_prompt_overrides.py
git commit -m "feat(prompts): add versioned DB overrides with label publishing"
```

---

### Task 16: Guarded write API

**Files:**
- Modify: `shared/prompts/library.py`
- Test: `shared/tests/test_prompt_write_api.py`

Enforces the rule from the spec: automated callers may propose, never publish.

- [ ] **Step 1: Write the failing test**

```python
"""The write API enforces access and the propose-but-never-publish rule."""

import pytest

from shared.prompts.bundled import BundledStore
from shared.prompts.library import PromptLibrary


class RecordingStore:
    def __init__(self):
        self.proposed = []
        self.published = []

    def is_configured(self):
        return True

    def propose(self, prompt_id, body, note, actor, via="ui"):
        self.proposed.append((prompt_id, body, actor, via))
        return len(self.proposed)

    def publish(self, prompt_id, version, actor):
        self.published.append((prompt_id, version, actor))

    def body_for(self, prompt_id):
        return None


@pytest.fixture
def lib(tmp_path, monkeypatch):
    (tmp_path / "a.b.prompt").write_text(
        "---\nid: a.b\ndescription: d\noverridable: true\n"
        "access:\n  edit: [ops]\n  publish: [eng]\n---\nbody\n"
    )
    (tmp_path / "locked.prompt").write_text(
        "---\nid: locked\ndescription: d\noverridable: false\n---\nbody\n"
    )
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    monkeypatch.setenv("PROMPT_EDITORS_ENG", "eve@x.com")
    monkeypatch.delenv("GRID_DESIGN_DEV_NO_AUTH", raising=False)
    store = RecordingStore()
    library = PromptLibrary(bundled=BundledStore(directory=tmp_path), overrides=store)
    return library, store


def test_editor_can_propose(lib):
    library, store = lib
    library.propose("a.b", "new body", note="n", actor="ada@x.com")
    assert store.proposed[0][0] == "a.b"


def test_non_editor_cannot_propose(lib):
    library, _ = lib
    with pytest.raises(PermissionError, match="edit"):
        library.propose("a.b", "new", note="n", actor="bob@x.com")


def test_editor_without_publish_cannot_publish(lib):
    library, _ = lib
    with pytest.raises(PermissionError, match="publish"):
        library.publish("a.b", 1, actor="ada@x.com")


def test_publisher_can_publish(lib):
    library, store = lib
    library.publish("a.b", 1, actor="eve@x.com")
    assert store.published == [("a.b", 1, "eve@x.com")]


def test_non_overridable_prompt_rejects_propose(lib):
    library, _ = lib
    with pytest.raises(PermissionError):
        library.propose("locked", "new", note="n", actor="ada@x.com")


def test_api_actor_may_propose_but_never_publish(lib):
    library, store = lib
    library.propose("a.b", "new", note="n", actor="agent@system", via="api", enforce_access=False)
    assert store.proposed[-1][3] == "api"
    with pytest.raises(PermissionError, match="Automated"):
        library.publish("a.b", 1, actor="agent@system", via="api")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_write_api.py -q`
Expected: FAIL — `PromptLibrary() got an unexpected keyword argument 'overrides'`

- [ ] **Step 3: Extend `PromptLibrary`**

Add an `overrides` parameter (defaulting to `None`) that supersedes `db_body_for` — when present, `_resolve_body` calls `self._overrides.body_for`. Update `_build_default_library` to pass `overrides=overrides` instead of `db_body_for=overrides.body_for`. Then add these methods:

```python
def propose(
    self,
    prompt_id: str,
    body: str,
    note: str,
    actor: str,
    via: str = "ui",
    enforce_access: bool = True,
) -> int:
    """Append a new version. Never makes it live.

    ``enforce_access=False`` is for trusted backend callers, which have no
    email identity. They may propose; ``publish`` still refuses them.
    """
    from shared.prompts.access import can_edit_prompt

    spec = self._bundled.get(prompt_id)
    if enforce_access and not can_edit_prompt(spec, actor):
        raise PermissionError(f"{actor} may not edit prompt '{prompt_id}'")
    if not enforce_access and not spec.overridable:
        raise PermissionError(f"prompt '{prompt_id}' is not overridable")
    if self._overrides is None:
        raise RuntimeError("prompt override store is not configured")
    return self._overrides.propose(prompt_id, body, note=note, actor=actor, via=via)


def publish(self, prompt_id: str, version: int, actor: str, via: str = "ui") -> None:
    """Make a version live. Humans only."""
    from shared.prompts.access import can_publish_prompt

    if via != "ui":
        raise PermissionError(
            "Automated callers may propose a prompt version but never publish one; "
            "a human with the publish verb must promote it"
        )
    spec = self._bundled.get(prompt_id)
    if not can_publish_prompt(spec, actor):
        raise PermissionError(f"{actor} may not publish prompt '{prompt_id}'")
    if self._overrides is None:
        raise RuntimeError("prompt override store is not configured")
    self._overrides.publish(prompt_id, version, actor=actor)
```

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/library.py
git add -f shared/tests/test_prompt_write_api.py
git commit -m "feat(prompts): add guarded propose/publish write API"
```

---

### Task 17: Provenance in logs and Langfuse

**Files:**
- Modify: `shared/utils/langfuse_utils.py`
- Modify: `chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py`
- Test: `shared/tests/test_prompt_provenance.py`

- [ ] **Step 1: Write the failing test**

```python
"""Prompt provenance reaches logs and the Langfuse trace."""

from shared.prompts.types import PromptSource, RenderedPrompt
from shared.utils.langfuse_utils import prompt_metadata


def test_prompt_metadata_shape():
    rendered = RenderedPrompt(
        prompt_id="customer.system",
        system_text="x",
        context_text=None,
        source=PromptSource.DB,
        version=3,
        checksum="abcdef0123456789",
    )
    assert prompt_metadata(rendered) == {
        "prompt_id": "customer.system",
        "prompt_source": "db",
        "prompt_version": 3,
        "prompt_checksum": "abcdef01",
    }


def test_prompt_metadata_for_bundled_has_null_version():
    rendered = RenderedPrompt(
        prompt_id="a.b",
        system_text="x",
        context_text=None,
        source=PromptSource.BUNDLED,
        version=None,
        checksum="0011223344556677",
    )
    assert prompt_metadata(rendered)["prompt_version"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_provenance.py -q`
Expected: FAIL — `cannot import name 'prompt_metadata'`

- [ ] **Step 3: Add the helper and call it**

In `shared/utils/langfuse_utils.py`:

```python
def prompt_metadata(rendered) -> dict:
    """Trace metadata identifying which prompt version produced a generation."""
    return {
        "prompt_id": rendered.prompt_id,
        "prompt_source": rendered.source.value,
        "prompt_version": rendered.version,
        "prompt_checksum": rendered.checksum[:8],
    }
```

In `prepare_context.py`, log the provenance of the instructions used alongside the existing size log:

```python
LOGGER.info(
    f"Retrieved system instructions: {rendered.provenance()}, "
    f"system={len(system_instructions)} chars, "
    f"context={len(context_message) if context_message else 0} chars"
)
```

and stash `prompt_metadata(rendered)` on the graph state so the generation node can attach it to the trace.

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests -q && pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/utils/langfuse_utils.py chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py
git add -f shared/tests/test_prompt_provenance.py
git commit -m "feat(prompts): stamp prompt provenance into logs and Langfuse traces"
```

---

### Task 18: Prompts admin page

**Files:**
- Create: `anansi_app/nicegui_app/pages/prompts.py`
- Modify: `anansi_app/nicegui_app/main.py`
- Modify: `anansi_app/nicegui_app/layout.py`
- Test: `anansi_app/tests/test_prompts_page.py`

Follow the structure of `anansi_app/nicegui_app/pages/documents.py` — an async `render()`, a `refresh()` closure, and dialogs for destructive actions.

- [ ] **Step 1: Write the failing test**

The page's view-model logic is testable without NiceGUI; the rendering is not. Extract and test the former.

```python
"""Prompts page view-model."""

import pytest

from anansi_app.nicegui_app.pages.prompts import PromptRow, build_rows, diff_lines


class FakeLibrary:
    def __init__(self, specs, sources):
        self._specs = specs
        self._sources = sources

    def ids(self):
        return list(self._specs)

    def spec(self, prompt_id):
        return self._specs[prompt_id]

    def render(self, prompt_id, **kwargs):
        return self._sources[prompt_id]


def test_build_rows_marks_overridden(monkeypatch):
    from shared.prompts.spec import AccessSpec, PromptSpec
    from shared.prompts.types import PromptSource, RenderedPrompt

    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    spec = PromptSpec(
        id="a.b", description="d", body="x", checksum="c", overridable=True,
        access=AccessSpec(view=["ops"]),
    )
    rendered = RenderedPrompt("a.b", "x", None, PromptSource.DB, 4, "c")
    rows = build_rows(FakeLibrary({"a.b": spec}, {"a.b": rendered}), "root@x.com")
    assert rows == [
        PromptRow(
            prompt_id="a.b", description="d", owner="eng", source="Overridden",
            version=4, overridable=True, can_edit=True, can_publish=True,
        )
    ]


def test_build_rows_hides_prompts_the_user_cannot_view(monkeypatch):
    from shared.prompts.spec import AccessSpec, PromptSpec
    from shared.prompts.types import PromptSource, RenderedPrompt

    for var in ("PROMPT_EDITORS_OPS", "PROMPT_EDITORS_ENG", "PROMPT_ADMINS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GRID_DESIGN_DEV_NO_AUTH", raising=False)
    spec = PromptSpec(id="a.b", description="d", body="x", checksum="c",
                      access=AccessSpec(view=["ops"]))
    rendered = RenderedPrompt("a.b", "x", None, PromptSource.BUNDLED, None, "c")
    assert build_rows(FakeLibrary({"a.b": spec}, {"a.b": rendered}), "nobody@x.com") == []


def test_locked_prompt_is_never_editable(monkeypatch):
    from shared.prompts.spec import PromptSpec
    from shared.prompts.types import PromptSource, RenderedPrompt

    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    spec = PromptSpec(id="locked", description="d", body="x", checksum="c",
                      overridable=False)
    rendered = RenderedPrompt("locked", "x", None, PromptSource.BUNDLED, None, "c")
    rows = build_rows(FakeLibrary({"locked": spec}, {"locked": rendered}), "root@x.com")
    assert rows[0].can_edit is False


def test_diff_lines_marks_additions_and_removals():
    assert diff_lines("a\nb\n", "a\nc\n") == [
        ("  ", "a"),
        ("- ", "b"),
        ("+ ", "c"),
    ]


def test_diff_lines_of_identical_text_is_all_context():
    assert diff_lines("a\n", "a\n") == [("  ", "a")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../anansi_app/tests/test_prompts_page.py -q`
Expected: FAIL — `No module named 'anansi_app.nicegui_app.pages.prompts'`

- [ ] **Step 3: Write the page**

`anansi_app/nicegui_app/pages/prompts.py` — view-model first:

```python
"""Prompts admin page: list, edit, diff, publish, revert."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, List, Tuple

from nicegui import ui

from shared.prompts import PROMPTS
from shared.prompts.access import can_edit_prompt, can_publish_prompt, can_view_prompt
from shared.prompts.types import PromptSource

SOURCE_LABELS = {
    PromptSource.DB: "Overridden",
    PromptSource.GDOC: "Google Doc",
    PromptSource.BUNDLED: "Default",
}


@dataclass(frozen=True)
class PromptRow:
    prompt_id: str
    description: str
    owner: str
    source: str
    version: int | None
    overridable: bool
    can_edit: bool
    can_publish: bool


def build_rows(library: Any, email: str) -> List[PromptRow]:
    """The list view, filtered to what this user may see."""
    rows: List[PromptRow] = []
    for prompt_id in sorted(library.ids()):
        spec = library.spec(prompt_id)
        if not can_view_prompt(spec, email):
            continue
        rendered = library.render(prompt_id)
        rows.append(
            PromptRow(
                prompt_id=prompt_id,
                description=spec.description,
                owner=spec.owner,
                source=SOURCE_LABELS[rendered.source],
                version=rendered.version,
                overridable=spec.overridable,
                can_edit=can_edit_prompt(spec, email),
                can_publish=can_publish_prompt(spec, email),
            )
        )
    return rows


def diff_lines(default_body: str, current_body: str) -> List[Tuple[str, str]]:
    """Line diff of the shipped default against what is live."""
    result: List[Tuple[str, str]] = []
    for line in difflib.ndiff(
        default_body.strip().splitlines(), current_body.strip().splitlines()
    ):
        marker, text = line[:2], line[2:]
        if marker in ("  ", "- ", "+ "):
            result.append((marker, text))
    return result
```

Then the NiceGUI rendering: a searchable table of `build_rows(...)`; a detail dialog with a `ui.textarea` bound to the live body, a diff panel from `diff_lines(spec.body, live_body)`, a variables checklist from `spec.variables`, a version history table from `OverrideStore.versions(...)`, and five buttons — Save draft (`PROMPTS.propose`, enabled by `row.can_edit`), Publish (`PROMPTS.publish`, enabled by `row.can_publish`), Revert to default (`OverrideStore.revert_to_default`), Attach Google Doc (upserts `prompt_doc_bindings` for this prompt, enabled by `row.can_edit`), and Reload cache (`PROMPTS.reload()` plus `OverrideStore.invalidate()`). When `spec.output == "json"`, validate the body's fenced example against `spec.schema` before allowing Save.

Register in `main.py` following the existing gated-page pattern:

```python
@ui.page("/prompts")
async def prompts_page():
    user = auth.current_user()
    if not perms.can_view_bot_admin(user["email"]):
        return ui.label("Not authorised")
    await prompts.render()
```

and add the sidebar link in `layout.py` beside the other `can_admin` entries.

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../anansi_app/tests/test_prompts_page.py -q`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/prompts.py anansi_app/nicegui_app/main.py anansi_app/nicegui_app/layout.py
git add -f anansi_app/tests/test_prompts_page.py
git commit -m "feat(prompts): add the Prompts admin page"
```

**Phase 2 is now complete.** Prompts are editable live, versioned, rollback-able and access-controlled.

---

# Phase 3 — Composable knowledge

---

### Task 19: Knowledge module selection

**Files:**
- Create: `shared/prompts/knowledge.py`
- Test: `shared/tests/test_prompt_knowledge.py`

- [ ] **Step 1: Write the failing test**

```python
"""Knowledge module selection, tiering and budgeting."""

import pytest

from shared.prompts.knowledge import (
    KnowledgeModule,
    apply_overrides,
    budget_pinned,
    render_catalog,
    render_pinned,
    select_modules,
)
from shared.prompts.types import RequestScope


def _module(slug, tags=("grid_ops",), scope="sector", mode="pinned", body="B"):
    return KnowledgeModule(
        id=slug, slug=slug, title=slug.title(), summary=f"About {slug}.",
        body=body, tags=list(tags), scope=scope, mode=mode,
    )


def test_selects_modules_sharing_a_tag():
    modules = [_module("comms", tags=["comms"]), _module("billing", tags=["billing"])]
    picked = select_modules(modules, ["comms"], RequestScope())
    assert [m.slug for m in picked] == ["comms"]


def test_sector_scope_always_applies():
    picked = select_modules([_module("a")], ["grid_ops"], RequestScope(grid="XYZ"))
    assert len(picked) == 1


def test_site_scope_applies_only_to_that_site():
    modules = [_module("abc", scope="site:ABC")]
    assert select_modules(modules, ["grid_ops"], RequestScope(grid="ABC"))
    assert not select_modules(modules, ["grid_ops"], RequestScope(grid="XYZ"))


def test_no_tags_selects_nothing():
    assert select_modules([_module("a")], [], RequestScope()) == []


def test_override_can_force_a_module_on():
    modules = [_module("extra", tags=["unrelated"])]
    picked = apply_overrides([], modules, {"extra": True})
    assert [m.slug for m in picked] == ["extra"]


def test_override_can_force_a_module_off():
    selected = [_module("comms", tags=["comms"])]
    assert apply_overrides(selected, selected, {"comms": False}) == []


def test_budget_keeps_site_scoped_modules_first():
    site = _module("site", scope="site:ABC", body="x" * 60)
    sector = _module("sector", body="y" * 60)
    kept, dropped = budget_pinned([sector, site], limit=100)
    assert [m.slug for m in kept] == ["site"]
    assert [m.slug for m in dropped] == ["sector"]


def test_budget_drops_whole_modules_never_partial():
    modules = [_module("a", body="x" * 200)]
    kept, dropped = budget_pinned(modules, limit=100)
    assert kept == []
    assert [m.slug for m in dropped] == ["a"]


def test_budget_within_limit_keeps_everything():
    modules = [_module("a", body="x"), _module("b", body="y")]
    kept, dropped = budget_pinned(modules, limit=1000)
    assert len(kept) == 2 and dropped == []


def test_render_pinned_has_a_stable_heading():
    out = render_pinned([_module("a", body="Body text.")])
    assert out.startswith("# Technical Knowledge")
    assert "Body text." in out


def test_render_pinned_of_nothing_is_none():
    assert render_pinned([]) is None


def test_catalog_lists_slug_and_summary_only():
    out = render_catalog([_module("a", mode="on_demand", body="SECRET BODY")])
    assert "SECRET BODY" not in out
    assert "About a." in out


def test_catalog_of_nothing_is_none():
    assert render_catalog([]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_knowledge.py -q`
Expected: FAIL — `No module named 'shared.prompts.knowledge'`

- [ ] **Step 3: Write the implementation**

`shared/prompts/knowledge.py`:

```python
"""Knowledge modules — curated, tagged context composed into prompts.

Two tiers. Pinned modules are inlined in full, ordered so the most specific
survives when the budget binds. On-demand modules contribute one catalog line
each; the model fetches a body through the knowledge MCP tool when it needs
one, which keeps the long tail out of a window an agent loop re-sends every
step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from shared.prompts.types import RequestScope
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

PINNED_BUDGET_CHARS = 20000


@dataclass(frozen=True)
class KnowledgeModule:
    id: str
    slug: str
    title: str
    summary: str
    body: str
    tags: List[str] = field(default_factory=list)
    scope: str = "sector"
    mode: str = "pinned"

    @property
    def is_site_scoped(self) -> bool:
        return self.scope.startswith("site:")


def select_modules(
    modules: List[KnowledgeModule], tags: List[str], scope: RequestScope
) -> List[KnowledgeModule]:
    """Modules sharing a tag with the prompt whose scope matches the request."""
    wanted = set(tags)
    if not wanted:
        return []
    return [m for m in modules if wanted & set(m.tags) and scope.matches(m.scope)]


def apply_overrides(
    selected: List[KnowledgeModule],
    all_modules: List[KnowledgeModule],
    overrides: Dict[str, bool],
) -> List[KnowledgeModule]:
    """Apply per-prompt forced-on / forced-off decisions to a tag selection."""
    by_slug = {m.slug: m for m in all_modules}
    result = {m.slug: m for m in selected if overrides.get(m.slug, True)}
    for slug, pinned in overrides.items():
        if pinned and slug in by_slug:
            result[slug] = by_slug[slug]
    return [by_slug[s] for s in sorted(result)]


def budget_pinned(
    modules: List[KnowledgeModule], limit: int = PINNED_BUDGET_CHARS
) -> Tuple[List[KnowledgeModule], List[KnowledgeModule]]:
    """Fit pinned modules into the budget by dropping whole modules.

    Site-scoped material is kept first: it is the most specific and the least
    replaceable. Nothing is ever cut mid-document.
    """
    ordered = sorted(modules, key=lambda m: (not m.is_site_scoped, m.slug))
    kept: List[KnowledgeModule] = []
    dropped: List[KnowledgeModule] = []
    used = 0
    for module in ordered:
        size = len(module.body)
        if used + size <= limit:
            kept.append(module)
            used += size
        else:
            dropped.append(module)
    if dropped:
        LOGGER.warning(
            f"Pinned knowledge exceeded the {limit}-char budget; dropped "
            f"{len(dropped)} module(s): {', '.join(m.slug for m in dropped)}"
        )
    return kept, dropped


def render_pinned(modules: List[KnowledgeModule]) -> Optional[str]:
    if not modules:
        return None
    parts = [f"## {m.title}\n\n{m.body.strip()}" for m in modules]
    return "# Technical Knowledge\n\n" + "\n\n".join(parts)


def render_catalog(modules: List[KnowledgeModule]) -> Optional[str]:
    """Names and one-liners only — never bodies."""
    if not modules:
        return None
    lines = [f"- `{m.slug}` — {m.summary}" for m in modules]
    return (
        "# Available Knowledge\n\n"
        "Fetch any of these with the `get_knowledge_module` tool when relevant:\n\n"
        + "\n".join(lines)
    )
```

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_knowledge.py -q`
Expected: PASS, 13 passed

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/knowledge.py
git add -f shared/tests/test_prompt_knowledge.py
git commit -m "feat(knowledge): add module selection, tiering and budgeting"
```

---

### Task 20: Knowledge store and library wiring

**Files:**
- Modify: `shared/prompts/knowledge.py`
- Modify: `shared/prompts/library.py`
- Test: `shared/tests/test_prompt_knowledge_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
"""Rendering a prompt composes its knowledge into the context channel."""

import pytest

from shared.prompts.bundled import BundledStore
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.library import PromptLibrary
from shared.prompts.types import RequestScope


class FakeKnowledge:
    def __init__(self, modules, overrides=None):
        self._modules = modules
        self._overrides = overrides or {}

    def all_modules(self):
        return self._modules

    def overrides_for(self, prompt_id):
        return self._overrides.get(prompt_id, {})


@pytest.fixture
def lib(tmp_path):
    (tmp_path / "a.b.prompt").write_text(
        "---\nid: a.b\ndescription: d\nknowledge_tags: [grid_ops]\n---\nBody.\n"
    )
    (tmp_path / "none.prompt").write_text("---\nid: none\ndescription: d\n---\nBody.\n")
    return BundledStore(directory=tmp_path)


def _module(slug, mode="pinned"):
    return KnowledgeModule(
        id=slug, slug=slug, title=slug.title(), summary=f"About {slug}.",
        body=f"{slug} body", tags=["grid_ops"], scope="sector", mode=mode,
    )


def test_pinned_module_lands_in_the_context_channel(lib):
    library = PromptLibrary(bundled=lib, knowledge=FakeKnowledge([_module("comms")]))
    out = library.render("a.b", scope=RequestScope())
    assert "# Technical Knowledge" in (out.context_text or "")
    assert "comms body" in out.context_text
    assert out.knowledge_used == ["comms"]


def test_on_demand_module_contributes_only_a_catalog_line(lib):
    library = PromptLibrary(
        bundled=lib, knowledge=FakeKnowledge([_module("sites", mode="on_demand")])
    )
    out = library.render("a.b", scope=RequestScope())
    assert "sites body" not in (out.context_text or "")
    assert "About sites." in out.context_text


def test_prompt_without_tags_gets_no_knowledge(lib):
    library = PromptLibrary(bundled=lib, knowledge=FakeKnowledge([_module("comms")]))
    out = library.render("none", scope=RequestScope())
    assert out.context_text is None
    assert out.knowledge_used == []


def test_per_prompt_override_forces_a_module_off(lib):
    library = PromptLibrary(
        bundled=lib,
        knowledge=FakeKnowledge([_module("comms")], {"a.b": {"comms": False}}),
    )
    assert library.render("a.b", scope=RequestScope()).knowledge_used == []


def test_knowledge_failure_does_not_break_rendering(lib):
    class Broken:
        def all_modules(self):
            raise RuntimeError("db down")

        def overrides_for(self, prompt_id):
            return {}

    out = PromptLibrary(bundled=lib, knowledge=Broken()).render("a.b")
    assert out.system_text == "Body."
    assert out.knowledge_used == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests/test_prompt_knowledge_wiring.py -q`
Expected: FAIL — `PromptLibrary() got an unexpected keyword argument 'knowledge'`

- [ ] **Step 3: Add `KnowledgeStore` and wire it in**

Append to `shared/prompts/knowledge.py`:

```python
class KnowledgeStore:
    """Reads knowledge_modules and prompt_knowledge_overrides.

    Degrades to "no knowledge" whenever the tables are absent or unreachable —
    a prompt must still render.
    """

    def __init__(self, client=None, ttl_seconds: int = 300) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._cache: Optional[List[KnowledgeModule]] = None
        self._expires = 0.0

    @classmethod
    def from_env(cls) -> "KnowledgeStore":
        from shared.config.db_credentials import chat_db_service_key, chat_db_url

        url, key = chat_db_url(), chat_db_service_key()
        if not (url and key):
            return cls(client=None)
        try:
            from supabase import create_client

            return cls(client=create_client(url, key))
        except Exception:
            LOGGER.warning("Could not build the knowledge store client", exc_info=True)
            return cls(client=None)

    def all_modules(self) -> List[KnowledgeModule]:
        import time

        if self._cache is not None and time.time() < self._expires:
            return self._cache
        if not self._client:
            return []
        try:
            result = (
                self._client.table("knowledge_modules")
                .select("id, slug, title, summary, body, tags, scope, mode")
                .eq("is_active", True)
                .execute()
            )
            self._cache = [KnowledgeModule(**row) for row in (result.data or [])]
        except Exception:
            LOGGER.warning("Knowledge module fetch failed; continuing without", exc_info=True)
            self._cache = []
        self._expires = time.time() + self._ttl
        return self._cache

    def overrides_for(self, prompt_id: str) -> Dict[str, bool]:
        if not self._client:
            return {}
        try:
            result = (
                self._client.table("prompt_knowledge_overrides")
                .select("module_id, pinned")
                .eq("prompt_id", prompt_id)
                .execute()
            )
        except Exception:
            LOGGER.warning(f"Knowledge overrides fetch failed for '{prompt_id}'", exc_info=True)
            return {}
        by_id = {m.id: m.slug for m in self.all_modules()}
        return {
            by_id[row["module_id"]]: row["pinned"]
            for row in (result.data or [])
            if row["module_id"] in by_id
        }
```

In `shared/prompts/library.py`, add a `knowledge` constructor parameter (defaulting to `None`, stored as `self._knowledge`) and the import it needs:

```python
from shared.prompts.knowledge import (
    apply_overrides,
    budget_pinned,
    render_catalog,
    render_pinned,
    select_modules,
)
```

Then, in `PromptLibrary.render`, after `split_sections`, compose knowledge into the context channel:

```python
knowledge_text, used = self._compose_knowledge(spec, scope or RequestScope())
if knowledge_text:
    context_text = f"{context_text}\n\n{knowledge_text}" if context_text else knowledge_text
```

with:

```python
def _compose_knowledge(self, spec, scope):
    """Resolve, budget and render this prompt's knowledge. Never raises."""
    if self._knowledge is None or not spec.knowledge_tags:
        return None, []
    try:
        modules = self._knowledge.all_modules()
        overrides = self._knowledge.overrides_for(spec.id)
    except Exception:
        LOGGER.warning(
            f"Knowledge lookup failed for '{spec.id}'; rendering without it", exc_info=True
        )
        return None, []

    chosen = apply_overrides(
        select_modules(modules, spec.knowledge_tags, scope), modules, overrides
    )
    pinned, dropped = budget_pinned([m for m in chosen if m.mode == "pinned"])
    on_demand = [m for m in chosen if m.mode == "on_demand"]

    blocks = [b for b in (render_pinned(pinned), render_catalog(on_demand)) if b]
    used = [m.slug for m in pinned] + [m.slug for m in on_demand]
    return ("\n\n".join(blocks) or None), used
```

Set `knowledge_used=used` on the returned `RenderedPrompt`, and add `KnowledgeStore.from_env()` to `_build_default_library`.

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../shared/tests -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/knowledge.py shared/prompts/library.py
git add -f shared/tests/test_prompt_knowledge_wiring.py
git commit -m "feat(knowledge): compose tagged modules into prompt context"
```

---

### Task 21: `get_knowledge_module` MCP tool

**Files:**
- Modify: `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py`
- Modify: `mcp_servers/servers/knowledge_server/tool_schemas.py`
- Test: `mcp_servers/tests/test_knowledge_module_tool.py`

- [ ] **Step 1: Write the failing test**

```python
"""The on-demand tier's fetch tool."""

import pytest

from mcp_servers.servers.knowledge_server.knowledge_mcp_server import (
    fetch_knowledge_module,
)


class FakeStore:
    def __init__(self, modules):
        self._modules = modules

    def all_modules(self):
        return self._modules


def _module(slug):
    from shared.prompts.knowledge import KnowledgeModule

    return KnowledgeModule(
        id=slug, slug=slug, title=slug.title(), summary="s",
        body=f"{slug} full body", tags=[], scope="sector", mode="on_demand",
    )


def test_returns_the_body_for_a_known_slug():
    out = fetch_knowledge_module("sites", store=FakeStore([_module("sites")]))
    assert "sites full body" in out


def test_unknown_slug_returns_a_helpful_message_not_an_exception():
    out = fetch_knowledge_module("nope", store=FakeStore([_module("sites")]))
    assert "nope" in out and "sites" in out


def test_empty_store_says_so():
    assert "no knowledge modules" in fetch_knowledge_module("x", store=FakeStore([])).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../mcp_servers/tests/test_knowledge_module_tool.py -q`
Expected: FAIL — `cannot import name 'fetch_knowledge_module'`

- [ ] **Step 3: Add the tool**

```python
def fetch_knowledge_module(slug: str, store=None) -> str:
    """Return one knowledge module's full body by slug.

    Backs the on-demand tier: the model sees only slug and summary in context
    and calls this when it decides a module is relevant.
    """
    from shared.prompts.knowledge import KnowledgeStore

    store = store or KnowledgeStore.from_env()
    modules = {m.slug: m for m in store.all_modules()}
    if not modules:
        return "No knowledge modules are configured."
    module = modules.get(slug)
    if not module:
        return (
            f"No knowledge module named '{slug}'. Available: "
            + ", ".join(sorted(modules))
        )
    return f"# {module.title}\n\n{module.body}"
```

Register it in `tool_schemas.py` beside the existing knowledge tools, with a description that tells the model to call it when the catalog block mentions a relevant slug.

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../mcp_servers/tests -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_servers/servers/knowledge_server/
git add -f mcp_servers/tests/test_knowledge_module_tool.py
git commit -m "feat(knowledge): add get_knowledge_module tool for the on-demand tier"
```

---

### Task 22: Knowledge Modules admin page

**Files:**
- Create: `anansi_app/nicegui_app/pages/knowledge_modules.py`
- Modify: `anansi_app/nicegui_app/pages/prompts.py`
- Modify: `anansi_app/nicegui_app/main.py`, `layout.py`
- Test: `anansi_app/tests/test_knowledge_modules_page.py`

- [ ] **Step 1: Write the failing test**

```python
"""Knowledge Modules page view-model and the Prompts page knowledge tab."""

import pytest

from anansi_app.nicegui_app.pages.knowledge_modules import (
    ModuleRow,
    build_module_rows,
    validate_module,
)
from anansi_app.nicegui_app.pages.prompts import build_knowledge_tab
from shared.prompts.knowledge import KnowledgeModule


def _module(slug, tags=("grid_ops",), mode="pinned"):
    return KnowledgeModule(
        id=slug, slug=slug, title=slug.title(), summary="s", body="b" * 40,
        tags=list(tags), scope="sector", mode=mode,
    )


def test_build_module_rows_reports_size():
    rows = build_module_rows([_module("comms")])
    assert rows == [ModuleRow(slug="comms", title="Comms", tags=["grid_ops"],
                              scope="sector", mode="pinned", chars=40)]


def test_knowledge_tab_marks_tag_derived_versus_overridden():
    tab = build_knowledge_tab(
        prompt_tags=["grid_ops"], modules=[_module("comms"), _module("extra", tags=["other"])],
        overrides={"extra": True},
    )
    by_slug = {row.slug: row for row in tab}
    assert by_slug["comms"].checked is True and by_slug["comms"].origin == "tag"
    assert by_slug["extra"].checked is True and by_slug["extra"].origin == "override"


def test_knowledge_tab_shows_a_forced_off_module_as_unchecked():
    tab = build_knowledge_tab(
        prompt_tags=["grid_ops"], modules=[_module("comms")], overrides={"comms": False},
    )
    assert tab[0].checked is False and tab[0].origin == "override"


def test_knowledge_tab_totals_only_pinned_checked_modules():
    tab = build_knowledge_tab(
        prompt_tags=["grid_ops"],
        modules=[_module("a"), _module("b", mode="on_demand")],
        overrides={},
    )
    assert sum(row.chars for row in tab if row.checked and row.mode == "pinned") == 40


def test_validate_module_requires_a_summary_for_on_demand():
    with pytest.raises(ValueError, match="summary"):
        validate_module(slug="a", title="A", summary="", body="b", mode="on_demand")


def test_validate_module_rejects_a_bad_scope():
    with pytest.raises(ValueError, match="scope"):
        validate_module(slug="a", title="A", summary="s", body="b", scope="nonsense")


def test_validate_module_accepts_a_site_scope():
    validate_module(slug="a", title="A", summary="s", body="b", scope="site:ABC")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../anansi_app/tests/test_knowledge_modules_page.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Write the pages**

`knowledge_modules.py` — `ModuleRow`, `build_module_rows`, and:

```python
VALID_MODES = {"pinned", "on_demand"}


def validate_module(slug, title, summary, body, scope="sector", mode="pinned"):
    """Reject a module that would fail silently at render time."""
    if not slug or not title or not body:
        raise ValueError("slug, title and body are required")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
    if mode == "on_demand" and not summary.strip():
        raise ValueError(
            "an on_demand module needs a summary: it is the only thing the model "
            "sees before deciding to fetch the body"
        )
    if scope != "sector" and not (scope.startswith("site:") or scope.startswith("org:")):
        raise ValueError("scope must be 'sector', 'site:<name>' or 'org:<id>'")
```

plus a CRUD table gated by each module's `edit_groups`.

In `prompts.py` add the knowledge tab view-model:

```python
@dataclass(frozen=True)
class KnowledgeTabRow:
    slug: str
    title: str
    mode: str
    chars: int
    checked: bool
    origin: str  # "tag" | "override"


def build_knowledge_tab(prompt_tags, modules, overrides):
    """The per-prompt checkbox grid: tag-derived, individually overridable."""
    wanted = set(prompt_tags)
    rows = []
    for module in sorted(modules, key=lambda m: m.slug):
        by_tag = bool(wanted & set(module.tags))
        if module.slug in overrides:
            checked, origin = overrides[module.slug], "override"
        else:
            checked, origin = by_tag, "tag"
        if not (by_tag or module.slug in overrides):
            continue
        rows.append(
            KnowledgeTabRow(
                slug=module.slug, title=module.title, mode=module.mode,
                chars=len(module.body), checked=checked, origin=origin,
            )
        )
    return rows
```

Render it as checkboxes with a running `sum(chars)` against `PINNED_BUDGET_CHARS`, and gate toggling on the prompt's `edit` verb — never the module's.

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest ../anansi_app/tests -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/ anansi_app/nicegui_app/main.py anansi_app/nicegui_app/layout.py
git add -f anansi_app/tests/test_knowledge_modules_page.py
git commit -m "feat(knowledge): add Knowledge Modules page and per-prompt knowledge tab"
```

---

### Task 23: Seed the first knowledge modules from the existing docs

**Files:**
- Create: `scripts/seed_knowledge_modules.py`
- Test: `chat_orchestrator/tests/test_seed_knowledge_modules.py`

The sector knowledge currently pasted into the customer and staff Google Docs needs a path out. This script proposes modules from an existing doc's `## ` sections; a human reviews and saves them in the UI.

- [ ] **Step 1: Write the failing test**

```python
"""Seeding knowledge modules from an existing instructions document."""

from scripts.seed_knowledge_modules import propose_modules

DOC = """## Power and Comms

Meters lose GSM when the grid is down.

## Site ABC Notes

The DCU sits in the container.
"""


def test_proposes_one_module_per_heading():
    assert [m["slug"] for m in propose_modules(DOC)] == ["power-and-comms", "site-abc-notes"]


def test_body_excludes_the_heading():
    modules = propose_modules(DOC)
    assert modules[0]["body"].strip() == "Meters lose GSM when the grid is down."


def test_summary_is_the_first_sentence():
    assert propose_modules(DOC)[0]["summary"] == "Meters lose GSM when the grid is down."


def test_site_headings_are_proposed_as_site_scoped():
    assert propose_modules(DOC)[1]["scope"] == "site:ABC"


def test_non_site_headings_default_to_sector():
    assert propose_modules(DOC)[0]["scope"] == "sector"


def test_empty_document_proposes_nothing():
    assert propose_modules("") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest tests/test_seed_knowledge_modules.py -q`
Expected: FAIL — `No module named 'scripts.seed_knowledge_modules'`

- [ ] **Step 3: Write the script**

```python
"""Propose knowledge modules from an existing instructions document.

Prints JSON for review. Writes nothing — a human pastes the reviewed modules
into the Knowledge Modules page, which is where the audit trail lives.

Usage:
    python -m scripts.seed_knowledge_modules customer.system
"""

from __future__ import annotations

import json
import re
import sys
from typing import Dict, List

_HEADING = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_SITE = re.compile(r"\bsite\s+([A-Za-z0-9_-]+)\b", re.IGNORECASE)


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _summary(body: str) -> str:
    first = body.strip().split("\n\n")[0].strip()
    sentence = first.split(". ")[0].strip()
    return sentence if sentence.endswith(".") else f"{sentence}."


def propose_modules(text: str) -> List[Dict[str, str]]:
    """One proposed module per '## ' heading."""
    matches = list(_HEADING.finditer(text))
    modules: List[Dict[str, str]] = []
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if not body:
            continue
        site = _SITE.search(title)
        modules.append(
            {
                "slug": _slug(title),
                "title": title,
                "summary": _summary(body),
                "body": body,
                "tags": [],
                "scope": f"site:{site.group(1).upper()}" if site else "sector",
                "mode": "pinned",
            }
        )
    return modules


if __name__ == "__main__":
    from shared.prompts import PROMPTS

    prompt_id = sys.argv[1] if len(sys.argv) > 1 else "customer.system"
    print(json.dumps(propose_modules(PROMPTS.text(prompt_id)), indent=2))
```

- [ ] **Step 4: Run tests**

Run: `cd chat_orchestrator && source .venv/bin/activate && pytest tests/test_seed_knowledge_modules.py -q`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/seed_knowledge_modules.py
git add -f chat_orchestrator/tests/test_seed_knowledge_modules.py
git commit -m "feat(knowledge): add a seeding script to extract modules from existing docs"
```

---

### Task 24: Documentation

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`

- [ ] **Step 1: Rewrite the README's instructions section**

Replace the "Create System Instructions in Google Docs" walkthrough (README.md:100-120) and the `chat_orchestrator/instructions/` note (README.md:225) with a Prompt Library section covering: where prompts live, the resolution order, how to edit one in the admin app, the three groups and three verbs, what `overridable: false` means, and the fact that `db/migrations/0006_prompt_library.sql` must be applied before live editing works.

Update the "System Instructions Flow" diagram (README.md:272-300) and the file-map entry for `instructions_provider.py` (README.md:525, README.md:1022).

- [ ] **Step 2: Add a contributor note**

In `CONTRIBUTING.md`, document adding a prompt: create `shared/prompts/library/<id>.prompt`, choose `overridable` (structured output means `false`), declare `variables` and `access`, then regenerate the parity snapshot.

- [ ] **Step 3: Verify no stale references remain**

```bash
grep -rn "chat_orchestrator/instructions" README.md CONTRIBUTING.md docs/ || echo "clean"
grep -rn "GRAFANA_PANEL_DESCRIPTION_PROMPT" README.md docs/ || echo "clean"
```

Expected: `clean` twice

- [ ] **Step 4: Commit**

```bash
git add README.md CONTRIBUTING.md
git commit -m "docs: document the prompt library and retire the Google Docs walkthrough"
```

---

### Task 25: Full verification

- [ ] **Step 1: Run every suite**

```bash
cd chat_orchestrator && source .venv/bin/activate && pytest tests/ -q && pytest ../shared/tests -q && pytest ../anansi_app/tests -q && pytest ../mcp_servers/tests -q
```

Expected: all PASS

- [ ] **Step 2: Run the full pre-commit hook**

```bash
pre-commit run --all-files
```

Expected: all hooks pass. If `test-wiring` reports untracked files under any `tests/` directory, vet each for operator data, `git add -f` it explicitly, and re-run.

- [ ] **Step 3: Confirm every test file actually got committed**

```bash
git log --stat --oneline feature/prompt-library ^main | grep -c "test_"
git status --short
```

Expected: a non-zero count of test files in the branch's diff, and a clean status.

- [ ] **Step 4: Confirm no prompt text remains outside the library**

```bash
grep -rnE '^[A-Z_]*PROMPT[A-Z_]* *=' --include="*.py" chat_orchestrator/ shared/ mcp_servers/ anansi_app/ | grep -v ".venv" || echo "clean"
grep -rn "_load_fallback_instructions\|_load_fallback_expert_instructions" --include="*.py" . | grep -v ".venv" || echo "clean"
```

Expected: `clean` twice

- [ ] **Step 5: Confirm the SQL file is the only migration added**

```bash
git diff --name-only main...feature/prompt-library -- db/
```

Expected: exactly `db/migrations/0006_prompt_library.sql` and `db/schema/chat_db.sql`

- [ ] **Step 6: Commit any fixes, then stop**

```bash
git status --short
```

**Do not push.** Report to the user: the branch name, the test counts, and that `db/migrations/0006_prompt_library.sql` is ready to paste into the Supabase SQL editor.

---

## Post-implementation notes for the user

**Applying the SQL.** `db/migrations/0006_prompt_library.sql` is one idempotent file for the Supabase SQL editor against `chat_db`. Apply it before using the Prompts page — until then the app runs bundled-only, which is a supported state, not a broken one.

**Setting the groups.** Three env vars must be set in the deployment for live editing to work at all: `PROMPT_EDITORS_OPS`, `PROMPT_EDITORS_ENG`, `PROMPT_ADMINS`. They fail closed — unset means nobody can edit anything, and the bot still runs normally.

**Deferred to a later plan.** The golden-set eval gate (Phase 5 in the spec) is not in this plan. It needs a fixture of real conversations, which needs a decision about operator data handling that this refactor does not have to resolve. Everything here is built so the gate slots into `PromptLibrary.publish` without rework.
