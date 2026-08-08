# Model Tier Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ~7 scattered, inconsistently-named model environment variables with exactly three named tiers (`MODEL_THINKING`/`MODEL_FAST`/`MODEL_LITE`), give every prompt in the library an explicit, admin-changeable tier, and migrate every consumer across `chat_orchestrator`, `mcp_servers`, and `anansi_app` to the new mechanism.

**Architecture:** A `resolve_model(tier)` helper reads the 3 new env vars, registered in the existing `flag_registry.py` (making them live-editable via `anansi_app`'s Settings page for free — no new UI needed for that axis). Per-prompt tier choice is a new `prompt_model_overrides` DB table (frontmatter default + live override, same shape as `prompt_doc_bindings`), surfaced as a dropdown in the Prompts admin page, gated by each prompt's existing `access.edit`/`access.publish` groups. Full rationale: [`docs/superpowers/specs/2026-08-08-model-tier-selection-design.md`](../specs/2026-08-08-model-tier-selection-design.md).

**Tech Stack:** Python 3.11, pytest, NiceGUI (admin UI), Supabase/Postgres (override table), DigitalOcean App Platform (env vars).

---

## File Structure

**Create:**
- `shared/llm/model_tiers.py` — `resolve_model(tier)` and the `TIER_ENV_VARS` mapping
- `db/migrations/0015_prompt_model_overrides.sql` — the new override table

**Modify — core mechanism:**
- `shared/prompts/spec.py` — `PromptSpec.model` becomes a required 3-way literal
- `shared/prompts/overrides.py` — `OverrideStore` gains model-override read/write, mirroring `prompt_doc_bindings`
- `shared/prompts/core.py` — `PromptLibrary.spec()` merges the override in
- `anansi_app/nicegui_app/pages/prompts.py` — Tier dropdown in the detail dialog

**Modify — flag registry & settings:**
- `shared/config/flag_registry.py` — retire 6 flags, add 3
- `shared/config/flags.env.example`, `chat_orchestrator/.env.example` — same
- `anansi_app/tests/test_model_settings.py`, `anansi_app/tests/test_settings_page.py`, `chat_orchestrator/tests/test_flag_registry.py` — updated for the retired/added flags

**Modify — prompt-tied call sites (9 files):**
- `chat_orchestrator/orchestrator/services/intent_router.py`
- `chat_orchestrator/orchestrator/services/thread_assignment.py`
- `chat_orchestrator/orchestrator/services/conversation_summarizer.py`
- `chat_orchestrator/orchestrator/services/context_filter.py`
- `chat_orchestrator/orchestrator/services/verification_service.py` (the one requiring a real refactor — see Task 6)
- `chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/detect_contradictions.py`
- `chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/improve_content.py`
- `chat_orchestrator/orchestrator/experts/handlers/context_expert/propose_module.py`
- `chat_orchestrator/orchestrator/services/command_registry.py`

**Modify — prompt-independent call sites:**
- `chat_orchestrator/handler.py`
- `mcp_servers/servers/reference_server/reference_mcp_server.py`
- `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py`

**Modify — all 27 `.prompt` files** (add `model:` field — script, like the permissions work's 14-file flip)

**Modify — one prompt's content:** `shared/prompts/library/experts.definitions.prompt` (line 1002: `GEMINI_AGENT_PRO_MODEL` → `thinking`)

**Modify — deployment config:** `.do/app.example.yaml`, `.do/app.image.example.yaml`, `chat_orchestrator/project.yml`

**Modify — docs:** `shared/llm/pricing.py` (docstring only), `shared/README.md`, `chat_orchestrator/README.md`, root `README.md`, `chat_orchestrator/.env.example`

**Explicitly not modified:** `docs/superpowers/plans/2026-07-28-*.md` and other historical plan docs that happen to mention these env vars — those are records of what was true when written, not living documentation; rewriting them would be revisionist. `EMBEDDING_MODEL` — different capability, confirmed non-goal.

---

## Task 1: Core mechanism — `resolve_model()` and the override table

**Files:**
- Create: `shared/llm/model_tiers.py`
- Create: `db/migrations/0015_prompt_model_overrides.sql`
- Test: `shared/tests/test_model_tiers.py`

- [ ] **Step 1: Write the failing test**

```python
# shared/tests/test_model_tiers.py
"""Tier -> model resolution reads exactly MODEL_THINKING/MODEL_FAST/MODEL_LITE."""

import pytest

from shared.llm.model_tiers import TIER_ENV_VARS, resolve_model


def test_tier_env_vars_are_exactly_the_three_tiers():
    assert set(TIER_ENV_VARS) == {"thinking", "fast", "lite"}
    assert TIER_ENV_VARS["thinking"] == "MODEL_THINKING"
    assert TIER_ENV_VARS["fast"] == "MODEL_FAST"
    assert TIER_ENV_VARS["lite"] == "MODEL_LITE"


def test_resolve_model_reads_the_right_env_var(monkeypatch):
    monkeypatch.setenv("MODEL_THINKING", "gemini-pro-latest")
    monkeypatch.setenv("MODEL_FAST", "gemini-flash-latest")
    monkeypatch.setenv("MODEL_LITE", "gemini-2.5-flash-lite")
    assert resolve_model("thinking") == "gemini-pro-latest"
    assert resolve_model("fast") == "gemini-flash-latest"
    assert resolve_model("lite") == "gemini-2.5-flash-lite"


def test_resolve_model_rejects_unknown_tier():
    with pytest.raises(ValueError, match="unknown tier"):
        resolve_model("medium")


def test_resolve_model_raises_on_unset_env_var(monkeypatch):
    monkeypatch.delenv("MODEL_LITE", raising=False)
    with pytest.raises(RuntimeError, match="MODEL_LITE"):
        resolve_model("lite")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd chat_orchestrator && uv run pytest ../shared/tests/test_model_tiers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.llm.model_tiers'`

- [ ] **Step 3: Write the implementation**

```python
# shared/llm/model_tiers.py
"""Resolves a prompt's named quality tier to the model id currently
configured for it.

Three tiers only, no exceptions -- thinking/fast/lite each map to one
environment variable, live-editable via anansi_app's Settings page (they're
registered in shared/config/flag_registry.py the same way GEMINI_MODEL was
before this). GEMINI_FALLBACK_MODEL is deliberately not a tier -- it's a
failure-fallback concept, renamed to FALLBACK_MODEL and read directly
wherever it's needed, not through this module.
"""

from __future__ import annotations

import os

TIER_ENV_VARS = {
    "thinking": "MODEL_THINKING",
    "fast": "MODEL_FAST",
    "lite": "MODEL_LITE",
}


def resolve_model(tier: str) -> str:
    """The model id currently configured for ``tier``.

    Raises ``ValueError`` for a tier outside the three, ``RuntimeError`` if
    the corresponding env var isn't set -- never silently falls back to an
    empty string, since that would surface as a confusing provider-side
    "model not found" error far from its actual cause.
    """
    env_var = TIER_ENV_VARS.get(tier)
    if env_var is None:
        raise ValueError(f"unknown tier {tier!r}; expected one of {sorted(TIER_ENV_VARS)}")
    value = os.getenv(env_var, "").strip()
    if not value:
        raise RuntimeError(f"{env_var} is not set; cannot resolve tier {tier!r}")
    return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd chat_orchestrator && uv run pytest ../shared/tests/test_model_tiers.py -v`
Expected: `4 passed`

- [ ] **Step 5: Write the DB migration**

```sql
-- db/migrations/0015_prompt_model_overrides.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent: safe
-- to re-run. No application code requires this table to exist -- resolution
-- degrades to each prompt's frontmatter `model` field when absent, same
-- pattern as prompt_doc_bindings (0006_prompt_library.sql).

BEGIN;

CREATE TABLE IF NOT EXISTS prompt_model_overrides (
    prompt_id    text PRIMARY KEY,
    tier         text NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    CONSTRAINT prompt_model_overrides_tier_chk CHECK (tier IN ('thinking', 'fast', 'lite'))
);

DROP TRIGGER IF EXISTS trg_prompt_model_overrides_updated_at ON prompt_model_overrides;
CREATE TRIGGER trg_prompt_model_overrides_updated_at
    BEFORE UPDATE ON prompt_model_overrides
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMIT;
```

- [ ] **Step 6: Commit**

```bash
git add shared/llm/model_tiers.py shared/tests/test_model_tiers.py db/migrations/0015_prompt_model_overrides.sql
git commit -m "feat(llm): add resolve_model() and prompt_model_overrides migration

Three tiers (thinking/fast/lite), each backed by one env var. The
override table mirrors prompt_doc_bindings's shape: row present = live
override, absent = frontmatter's model field is authoritative."
```

---

## Task 2: `PromptSpec.model` becomes a required tier enum, `OverrideStore` and `PromptLibrary.spec()` merge in the override

**Files:**
- Modify: `shared/prompts/spec.py`
- Modify: `shared/prompts/overrides.py`
- Modify: `shared/prompts/core.py`
- Test: `shared/tests/test_prompt_spec.py`, `shared/tests/test_prompt_library.py`

- [ ] **Step 1: Read `shared/prompts/spec.py`, `shared/prompts/overrides.py`, and `shared/prompts/core.py` in full before editing** (all three were read during design/audit but re-read at implementation time to confirm no drift since).

- [ ] **Step 2: Write the failing tests**

Add to `shared/tests/test_prompt_spec.py`:

```python
def test_model_field_parses_a_valid_tier():
    text = "---\nid: a.b\ndescription: d\nmodel: fast\n---\nbody"
    spec = parse_prompt_file(text, path="a.b.prompt")
    assert spec.model == "fast"


def test_model_field_rejects_an_invalid_tier():
    text = "---\nid: a.b\ndescription: d\nmodel: medium\n---\nbody"
    with pytest.raises(ValueError, match="model"):
        parse_prompt_file(text, path="a.b.prompt")


def test_model_field_is_required():
    text = "---\nid: a.b\ndescription: d\n---\nbody"
    with pytest.raises(ValueError, match="model"):
        parse_prompt_file(text, path="a.b.prompt")
```

Add to `shared/tests/test_prompt_library.py` (find the existing fixture pattern used for `lib.spec("locked")` and mirror it for a model-override case): a test asserting that when `OverrideStore.model_tier_for(prompt_id)` returns a tier, `PromptLibrary.spec(prompt_id).model` reflects the override, not the frontmatter default; and that with no override, it falls back to frontmatter.

- [ ] **Step 3: Run tests, confirm they fail** (`ModuleNotFoundError`/`AttributeError`/`TypeError` depending on which; every existing `.prompt` file will also start failing to parse since none has a `model:` field yet -- expected, fixed in Task 8)

- [ ] **Step 4: Implement in `spec.py`**

In `PromptSpec`, replace:

```python
model: Optional[str] = None
```

with:

```python
model: str = "fast"
```

(field keeps a default so partial/legacy construction in tests doesn't break, but `parse_prompt_file` below makes it a hard requirement for real `.prompt` files.)

In `parse_prompt_file`, after the existing `output`/`schema` validation block, add:

```python
    model = raw.get("model")
    if model not in ("thinking", "fast", "lite"):
        raise ValueError(
            f"{path}: frontmatter 'model' must be one of thinking/fast/lite, got {model!r}"
        )
```

and pass `model=model` into the returned `PromptSpec(...)`.

- [ ] **Step 5: Implement in `overrides.py`**

Add a cached batch-fetch method mirroring `_doc_bindings()`, plus write/clear methods mirroring `set_doc_binding`/`clear_doc_binding`:

```python
    def _model_overrides(self) -> Dict[str, str]:
        """Every prompt_id -> tier override, cached like _doc_bindings()."""
        if self._model_override_cache is not None and time.time() < self._model_override_expires:
            return self._model_override_cache
        if not self._client:
            return {}
        try:
            result = (
                self._client.table("prompt_model_overrides")
                .select("prompt_id, tier")
                .execute()
            )
            self._model_override_cache = {
                row["prompt_id"]: row["tier"] for row in (result.data or [])
            }
        except Exception:
            LOGGER.warning("Model override fetch failed; using bundled tiers only", exc_info=True)
            self._model_override_cache = {}
        self._model_override_expires = time.time() + DOC_BINDING_TTL_SECONDS
        return self._model_override_cache

    def model_tier_for(self, prompt_id: str) -> Optional[str]:
        return self._model_overrides().get(prompt_id)

    def all_model_overrides(self) -> Dict[str, str]:
        return dict(self._model_overrides())

    def set_model_override(self, prompt_id: str, tier: str, actor: str) -> None:
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        self._client.table("prompt_model_overrides").upsert(
            {"prompt_id": prompt_id, "tier": tier, "updated_by": actor}
        ).execute()
        self.invalidate()
        LOGGER.info(f"Set model tier for {prompt_id} -> {tier} by {actor}")

    def clear_model_override(self, prompt_id: str, actor: str) -> None:
        if not self._client:
            raise RuntimeError("prompt override store is not configured")
        self._client.table("prompt_model_overrides").delete().eq("prompt_id", prompt_id).execute()
        self.invalidate()
        LOGGER.info(f"Reverted {prompt_id}'s model tier to bundled default by {actor}")
```

Add `self._model_override_cache: Optional[Dict[str, str]] = None` and `self._model_override_expires: float = 0.0` to `__init__`, and clear both in `invalidate()`.

- [ ] **Step 6: Implement in `core.py`**

Change `PromptLibrary.spec()`:

```python
    def spec(self, prompt_id: str) -> PromptSpec:
        base = self._bundled.get(prompt_id)
        if self._overrides is None:
            return base
        tier = self._overrides.model_tier_for(prompt_id)
        if tier is None:
            return base
        return dataclasses.replace(base, model=tier)
```

(add `import dataclasses` at the top of the file). This is the only merge point needed -- `render()`/`resolve()`/`propose()`/`publish()` don't read `.model` at all today, so no other method in this class needs touching for the merge itself (they will need touching in later tasks to actually *use* `.model` -- that's Task 3+, not this one).

- [ ] **Step 7: Run tests, confirm the ones from Step 2 pass** (existing `.prompt`-file-parsing tests will still fail until Task 8 adds `model:` to every file -- that's expected and tracked there, not a regression to chase down now)

- [ ] **Step 8: Commit**

```bash
git add shared/prompts/spec.py shared/prompts/overrides.py shared/prompts/core.py \
        shared/tests/test_prompt_spec.py shared/tests/test_prompt_library.py
git commit -m "feat(prompts): PromptSpec.model becomes a required tier enum with DB override

Mirrors the doc-binding pattern exactly: prompt_model_overrides row
present = live override, absent = frontmatter's model field wins.
PromptLibrary.spec() is the single merge point every other consumer
already goes through."
```

---

## Task 3: Register the 3 new flags, retire the 6 old ones

**Files:**
- Modify: `shared/config/flag_registry.py`
- Modify: `shared/config/flags.env.example`
- Modify: `chat_orchestrator/.env.example`
- Modify: `chat_orchestrator/tests/test_flag_registry.py`
- Modify: `anansi_app/tests/test_model_settings.py`, `anansi_app/tests/test_settings_page.py`

- [ ] **Step 1: Read `shared/config/flag_registry.py`'s full "AI Models & Providers" block (lines ~325-460), `chat_orchestrator/tests/test_flag_registry.py`, `anansi_app/tests/test_model_settings.py`, and `anansi_app/tests/test_settings_page.py` in full.**

- [ ] **Step 2: Replace the 6 retired flags with the 3 new ones in `flag_registry.py`**

Replace:

```python
    _s(
        "GEMINI_MODEL",
        "gemini-2.5-flash",
        "Primary generation model id for the selected provider.",
        group="models",
        label="Main model",
    ),
    _s(
        "GEMINI_FALLBACK_MODEL",
        "gemini-2.5-flash-lite",
        "Fallback generation model id for the selected provider.",
        group="models",
        label="Fallback model",
    ),
    _s(
        "GEMINI_DEEP_THINKING_MODEL",
        "gemini-pro-latest",
        "Model for deep-thinking tasks (document editing, complex analysis).",
        group="models",
        label="Deep-thinking model",
    ),
    _s(
        "INTENT_ROUTER_MODEL",
        "gemini-2.5-flash-lite",
        "Lightweight model for structured natural-language expert routing.",
        group="models",
        label="Intent router model",
    ),
    _s(
        "VERIFICATION_MODEL",
        "gemini-2.5-flash-lite",
        "Model used for response verification.",
        group="models",
        label="Response verification model",
    ),
```

with:

```python
    _s(
        "MODEL_THINKING",
        "gemini-pro-latest",
        "Model for complex-reasoning tasks (deep analysis, multi-step agent work).",
        group="models",
        label="Thinking-tier model",
    ),
    _s(
        "MODEL_FAST",
        "gemini-flash-latest",
        "Model for the general-purpose default tier.",
        group="models",
        label="Fast-tier model",
    ),
    _s(
        "MODEL_LITE",
        "gemini-2.5-flash-lite",
        "Model for lightweight/high-volume tasks (classification, verification).",
        group="models",
        label="Lite-tier model",
    ),
    _s(
        "FALLBACK_MODEL",
        "gemini-2.5-flash-lite",
        "Fallback generation model id, used when the primary call fails. Not a quality tier.",
        group="models",
        label="Fallback model",
    ),
```

(`GEMINI_AGENT_PRO_MODEL` -- find and remove its `_s(...)` block too, a few lines below this one; it collapses into `MODEL_THINKING`, same as `GEMINI_DEEP_THINKING_MODEL`.)

`GEMINI_LITE_MAX_OUTPUT_TOKENS` and `GEMINI_MAX_OUTPUT_TOKENS` are a different concern (token limits, not model selection) -- leave both untouched.

- [ ] **Step 3: Update `flags.env.example` and `chat_orchestrator/.env.example`** to replace the same 6 var names with the 4 new ones (`MODEL_THINKING`/`MODEL_FAST`/`MODEL_LITE`/`FALLBACK_MODEL`), matching whatever comment style the surrounding entries already use in each file.

- [ ] **Step 4: Update the three test files** for whatever they currently assert about the retired/added flag names (read each file's relevant assertions in Step 1 first, then update the specific flag-name literals -- the retired 6 names replaced by the new 4, same assertion shapes).

- [ ] **Step 5: Run the flag/settings test suites**

Run: `cd chat_orchestrator && uv run pytest tests/test_flag_registry.py -v`
Run: `cd anansi_app && python -m pytest tests/test_model_settings.py tests/test_settings_page.py -v` (check `anansi_app`'s own test-running convention first -- it may have its own venv/runner distinct from `chat_orchestrator`'s `uv`; look for an `anansi_app/pyproject.toml` or similar before assuming `uv run` applies here too)
Expected: all passing, referencing only the 4 new names.

- [ ] **Step 6: Commit**

```bash
git add shared/config/flag_registry.py shared/config/flags.env.example \
        chat_orchestrator/.env.example chat_orchestrator/tests/test_flag_registry.py \
        anansi_app/tests/test_model_settings.py anansi_app/tests/test_settings_page.py
git commit -m "feat(config): retire 6 scattered model flags, register the 3 tiers + FALLBACK_MODEL

Registering MODEL_THINKING/MODEL_FAST/MODEL_LITE in flag_registry.py
makes them live-editable via anansi_app's existing Settings page for
free -- same mechanism GEMINI_MODEL already used, no new UI required
for this axis."
```

---

## Task 4: Migrate the simple 1:1 prompt-tied call sites (5 files)

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/intent_router.py`
- Modify: `chat_orchestrator/orchestrator/services/thread_assignment.py`
- Modify: `chat_orchestrator/orchestrator/services/conversation_summarizer.py`
- Modify: `chat_orchestrator/orchestrator/services/context_filter.py`
- Modify: `chat_orchestrator/orchestrator/experts/handlers/context_expert/propose_module.py`

- [ ] **Step 1: Read each file's current model-resolution line in context** (each was already located precisely during the audit; re-confirm no drift: `intent_router.py:33`, `thread_assignment.py:132,265`, `conversation_summarizer.py:57`, `context_filter.py:50`, `propose_module.py:62`).

- [ ] **Step 2: `conversation_summarizer.py`** -- replace:

```python
        self._model = model or os.getenv("VERIFICATION_MODEL", "gemini-2.5-flash-lite")
```

with:

```python
        from shared.llm.model_tiers import resolve_model
        from shared.prompts import PROMPTS

        self._model = model or resolve_model(PROMPTS.spec("conversation.summarize").model)
```

- [ ] **Step 3: `context_filter.py`** -- same replacement, prompt id `"context_filter.relevance"`.

- [ ] **Step 4: `verification_service.py` is NOT touched in this task** -- see Task 6, it needs a structural change, not a line swap.

- [ ] **Step 5: `intent_router.py:33`** -- replace:

```python
    return os.getenv("INTENT_ROUTER_MODEL") or get_settings().gemini.fallback_model
```

with:

```python
    from shared.llm.model_tiers import resolve_model
    from shared.prompts import PROMPTS

    return resolve_model(PROMPTS.spec("intent_router.route").model)
```

(drop the `get_settings().gemini.fallback_model` fallback entirely -- `resolve_model` already raises clearly if `MODEL_LITE` is unset, which is a better failure mode than silently falling through to an unrelated fallback model.)

- [ ] **Step 6: `thread_assignment.py`** -- both `os.getenv("THREAD_CLASSIFIER_MODEL", DEFAULT_CLASSIFIER_MODEL)` call sites (lines 132, 265) replace with `resolve_model(PROMPTS.spec("thread_assignment.classify").model)`; remove the now-unused `DEFAULT_CLASSIFIER_MODEL` constant (line 35) if nothing else references it (`grep -n DEFAULT_CLASSIFIER_MODEL thread_assignment.py` to confirm before deleting).

- [ ] **Step 7: `propose_module.py:62`** -- replace `os.getenv("GEMINI_MODEL", "gemini-2.5-flash")` with `resolve_model("fast")` -- this one is prompt-independent (confirm by reading the function it's in during Step 1; if it turns out to be tied to a specific prompt_id after all, use that prompt's tier instead of a hardcoded `"fast"`).

- [ ] **Step 8: Run each file's existing test suite**

Run: `cd chat_orchestrator && uv run pytest tests/services/test_intent_router.py tests/services/test_thread_assignment.py tests/services/test_conversation_summarizer.py tests/services/test_context_filter.py -v` (confirm these exact test file names first -- `ls tests/services/ | grep -iE "intent_router|thread_assignment|conversation_summarizer|context_filter"`)
Expected: all passing (tests likely mock `get_default_generation_gateway`/`resolve_model`, not real API calls -- if any test asserts on the literal old env var name, update it to assert on the tier instead).

- [ ] **Step 9: Commit**

```bash
git add chat_orchestrator/orchestrator/services/intent_router.py \
        chat_orchestrator/orchestrator/services/thread_assignment.py \
        chat_orchestrator/orchestrator/services/conversation_summarizer.py \
        chat_orchestrator/orchestrator/services/context_filter.py \
        chat_orchestrator/orchestrator/experts/handlers/context_expert/propose_module.py
git commit -m "feat(llm): migrate 5 single-prompt call sites to resolve_model()"
```

---

## Task 5: Migrate the ingestion call sites and `command_registry.py`

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/detect_contradictions.py`
- Modify: `chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/improve_content.py`
- Modify: `chat_orchestrator/orchestrator/services/command_registry.py`

- [ ] **Step 1: Read all three files' relevant sections** (`detect_contradictions.py:120`, `improve_content.py:28-33`'s `_call_gemini` helper and every caller, `command_registry.py`'s `model_override` field definition at line ~60 and its one usage at line 807).

- [ ] **Step 2: `detect_contradictions.py`** -- replace `os.getenv("GEMINI_MODEL")` with `resolve_model(PROMPTS.spec("ingestion.detect_contradictions").model)`.

- [ ] **Step 3: `improve_content.py`** -- `_call_gemini`'s `model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")` is shared across this file's callers; confirm in Step 1 whether it's called for exactly one prompt id or several (the plan's earlier audit found `ingestion.improve_content.modification`, `.naming`, and `.quality_eval` all in this handler's package -- if `_call_gemini` truly serves all three with no way to distinguish which prompt is active at call time, either pass the prompt_id through to `_call_gemini` as a parameter so it can resolve the right tier per-call, or -- if that's too invasive for this task -- resolve once via `ingestion.classify_document`'s tier as a reasonable stand-in for "the ingestion fast-tier default" and note the imprecision in the commit message. Prefer the parameter-passing fix; only fall back to the stand-in if `_call_gemini`'s call sites make threading the prompt_id through genuinely awkward.

- [ ] **Step 4: `command_registry.py`** -- change the `model_override: str = ""` field's semantics from "env var name" to "tier name". Replace the one usage:

```python
        model_override="GEMINI_DEEP_THINKING_MODEL",
```

with:

```python
        model_override="thinking",
```

Find wherever `model_override` is actually *read* (grep `model_override` for the consuming code, not just the field definition and this one assignment) and update that resolution from `os.getenv(model_override)` to `resolve_model(model_override)`.

- [ ] **Step 5: Run tests for all three**

Run: `cd chat_orchestrator && uv run pytest tests/experts/handlers/ingestion_expert/ tests/services/test_command_registry.py -v` (confirm exact test paths first)
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/detect_contradictions.py \
        chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/improve_content.py \
        chat_orchestrator/orchestrator/services/command_registry.py
git commit -m "feat(llm): migrate ingestion handlers and command_registry's model_override to tiers"
```

---

## Task 6: Refactor `verification_service.py` for independent per-prompt tiers

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/verification_service.py`
- Test: `chat_orchestrator/tests/services/test_verification_service.py` (confirm exact name first)

**Why this one is different:** `_call_gemini` (line 277) is a shared helper reading one instance-level `self._gateway`/`self._model`, but it's called from both `verify_response()` (using `verification.criteria` as the system instruction) and `sanitize_technical_response()` (using `verification.sanitize`/`verification.sanitize_system`). Giving each prompt its own independently-changeable tier means `_call_gemini` can no longer assume one shared model for the whole service instance.

- [ ] **Step 1: Read the full file, specifically `verify_response()` (line 77) to confirm exactly how/where it renders `verification.criteria`, and `_call_gemini` (line 277) in full.**

- [ ] **Step 2: Change `_call_gemini` to take a `model` parameter instead of reading `self._model`:**

```python
    async def _call_gemini(
        self,
        system_instruction: str,
        user_message: str,
        model: str,
    ) -> str:
        """Make a Gemini API call for verification."""
        LOGGER.debug(f"Verification model: {model}")

        gateway = get_default_generation_gateway(api_key=self._api_key, default_model=model)
        result = await gateway.generate(
            [
                LLMMessage(role="system", text=system_instruction),
                LLMMessage(role="user", text=user_message),
            ],
            # ... (keep the rest of this call exactly as it is today)
```

(This trades one gateway built at construction time for one built per call -- confirm from reading `GenerationGateway`'s constructor in Step 1 whether that's cheap/stateless enough to do per-call; if it does expensive setup, cache gateways per-tier in a dict on `self` instead of rebuilding every call.)

- [ ] **Step 3: Update `verify_response()`'s call to `_call_gemini`** to pass `model=resolve_model(PROMPTS.spec("verification.criteria").model)`.

- [ ] **Step 4: Update `sanitize_technical_response()`'s call to `_call_gemini`** (around line 386-394) to pass `model=resolve_model(PROMPTS.spec("verification.sanitize").model)` -- using `verification.sanitize`'s tier for both the sanitize prompt and its `verification.sanitize_system` companion, since they're always rendered together as one call and don't have independently meaningful tiers in practice.

- [ ] **Step 5: Remove the now-unused constructor-level `self._model`/`self._gateway`** if nothing else in the class reads them after Steps 2-4 (`grep -n "self\._model\|self\._gateway" verification_service.py` to confirm before deleting); keep `self._api_key`.

- [ ] **Step 6: Run the test suite**

Run: `cd chat_orchestrator && uv run pytest tests/services/test_verification_service.py -v` (confirm exact filename first)
Expected: all passing. Existing tests likely construct `ResponseVerificationService(model=...)` directly -- if the constructor's `model` parameter is now unused, decide whether to keep it as a deprecated no-op (simplest, least test churn) or remove it and update every test call site (cleaner, more test edits). Prefer keeping it as a no-op unless it's actively misleading in context.

- [ ] **Step 7: Commit**

```bash
git add chat_orchestrator/orchestrator/services/verification_service.py \
        chat_orchestrator/tests/services/test_verification_service.py
git commit -m "refactor(verification): resolve each prompt's own tier instead of one shared model

verify_response() and sanitize_technical_response() render three
different prompts (verification.criteria, verification.sanitize,
verification.sanitize_system) through one _call_gemini helper that
used to share a single constructor-level model. Now each caller
resolves its own prompt's tier, so admin-changing one prompt's tier
doesn't silently affect the others sharing this service."
```

---

## Task 7: Migrate prompt-independent consumers

**Files:**
- Modify: `chat_orchestrator/handler.py`
- Modify: `mcp_servers/servers/reference_server/reference_mcp_server.py`
- Modify: `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py`

- [ ] **Step 1: Read all three files' relevant sections** (`handler.py:3371-3372`, `reference_mcp_server.py:202`, `knowledge_mcp_server.py:162`) to confirm none of them are actually prompt-tied after all (re-verify -- these were identified as prompt-independent during the audit, but confirm by checking what each surrounding function actually does before assuming).

- [ ] **Step 2: `handler.py`** -- replace:

```python
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            fallback_model=os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite"),
```

with:

```python
            model=resolve_model("fast"),
            fallback_model=os.environ.get("FALLBACK_MODEL", "gemini-2.5-flash-lite"),
```

(add `from shared.llm.model_tiers import resolve_model` to the imports; `fallback_model` stays a plain env var read per the design's decision that `FALLBACK_MODEL` isn't part of the tier system.)

- [ ] **Step 3: `reference_mcp_server.py:202` and `knowledge_mcp_server.py:162`** -- both replace `os.getenv("GEMINI_MODEL", "gemini-2.5-flash")` with `resolve_model("fast")` (import `shared.llm.model_tiers` -- confirm `mcp_servers` can import from `shared` the same way `chat_orchestrator` does; check an existing `mcp_servers` file's imports for the established pattern first).

- [ ] **Step 4: Run affected tests**

Run: `cd chat_orchestrator && uv run pytest tests/test_handler*.py -v` (confirm exact glob)
Run: whatever `mcp_servers`' own test invocation is (check `mcp_servers/` for its own `pyproject.toml`/`requirements.txt` and test runner convention before assuming `uv` applies there)

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/handler.py \
        mcp_servers/servers/reference_server/reference_mcp_server.py \
        mcp_servers/servers/knowledge_server/knowledge_mcp_server.py
git commit -m "feat(llm): migrate prompt-independent GEMINI_MODEL readers to resolve_model('fast')"
```

---

## Task 8: Add `model:` to every one of the 27 `.prompt` files

**Files:** all 27 files under `shared/prompts/library/*.prompt`

- [ ] **Step 1: Confirm the final per-prompt tier assignment.** High confidence (already verified against `flag_registry.py`'s actual registered defaults during the design audit):

| Tier | Prompts |
|---|---|
| **thinking** | (none in the library itself -- `GEMINI_AGENT_PRO_MODEL`'s only prompt-adjacent consumer is `experts.definitions.prompt`'s embedded text, handled in Task 9, not via a `model:` frontmatter field) |
| **lite** | `context_filter.relevance`, `thread_assignment.classify`, `intent_router.route`, `verification.sanitize`, `verification.sanitize_system`, `conversation.summarize` |
| **fast** | `ingestion.classify_document`, `ingestion.detect_contradictions`, `ingestion.extract_entities`, `ingestion.improve_content.modification`, `ingestion.improve_content.naming`, `ingestion.improve_content.quality_eval` |

The remaining prompts never had explicit model selection and get **fast** by proposal, not a verified finding -- list them out and sanity-check each by name before running the script, don't apply blind: `doc_editing.edit_highlighted`, `doc_editor.locate_edits`, `ticketing.jira_issue_types`, `ticketing.correlation`, `procedure.match`, `procedure.suggest`, `experts.definitions`, `grafana.panel_description`, `gtr.analysis_conversation`, `knowledge.summarize_topic`, `ingestion.fetch_document.type_selection`, `customer.system`, `staff.system`, `troubleshooting.procedures`, `verification.criteria`.

(27 total: 6 lite + 6 fast + 15 fast-by-default = 27. ✓)

- [ ] **Step 2: Add `model:` to each file's frontmatter**, placed after `overridable:` (matching where `output:` already sits in every file), via a script mirroring the permissions work's 14-file migration:

```bash
python3 -c "
import pathlib

lite = ['context_filter.relevance', 'thread_assignment.classify', 'intent_router.route',
        'verification.sanitize', 'verification.sanitize_system', 'conversation.summarize']
fast = ['ingestion.classify_document', 'ingestion.detect_contradictions', 'ingestion.extract_entities',
        'ingestion.improve_content.modification', 'ingestion.improve_content.naming',
        'ingestion.improve_content.quality_eval', 'doc_editing.edit_highlighted', 'doc_editor.locate_edits',
        'ticketing.jira_issue_types', 'ticketing.correlation', 'procedure.match', 'procedure.suggest',
        'experts.definitions', 'grafana.panel_description', 'gtr.analysis_conversation',
        'knowledge.summarize_topic', 'ingestion.fetch_document.type_selection', 'customer.system',
        'staff.system', 'troubleshooting.procedures', 'verification.criteria']

tier_for = {pid: 'lite' for pid in lite}
tier_for.update({pid: 'fast' for pid in fast})

base = pathlib.Path('shared/prompts/library')
files = sorted(base.glob('*.prompt'))
assert len(files) == 27, f'expected 27 prompt files, found {len(files)}'
missing = set(tier_for) - {f.stem for f in files}
assert not missing, f'tier_for names files that do not exist: {missing}'

for f in files:
    prompt_id = f.stem
    tier = tier_for.get(prompt_id)
    assert tier, f'{prompt_id}: no tier assigned -- every one of the 27 must get one'
    text = f.read_text()
    marker = 'overridable: true\n' if 'overridable: true\n' in text else 'overridable: false\n'
    new_text = text.replace(marker, f'{marker}model: {tier}\n', 1)
    assert new_text != text, f'{prompt_id}: could not find insertion point'
    f.write_text(new_text)
    print(f'{prompt_id}: model: {tier}')
"
```

- [ ] **Step 3: Run the full parser/library test suite**

Run: `cd chat_orchestrator && uv run pytest ../shared/tests/ -v -k "prompt"`
Expected: all passing -- every `.prompt` file now parses with a valid `model` field.

- [ ] **Step 4: Commit**

```bash
git add shared/prompts/library/*.prompt
git commit -m "feat(prompts): add model tier to all 27 prompts

lite: context_filter/thread_assignment/intent_router/verification.sanitize*/
conversation.summarize (matches their previously-shared VERIFICATION_MODEL/
THREAD_CLASSIFIER_MODEL/INTENT_ROUTER_MODEL default exactly -- zero
behavior change). fast: everything else, including 15 prompts that never
had explicit selection before now default to the general-purpose tier."
```

---

## Task 9: `experts.definitions.prompt`'s embedded reference, deployment config, pricing docstring

**Files:**
- Modify: `shared/prompts/library/experts.definitions.prompt`
- Modify: `.do/app.example.yaml`, `.do/app.image.example.yaml`, `chat_orchestrator/project.yml`
- Modify: `shared/llm/pricing.py`

- [ ] **Step 1: Read the full context around `experts.definitions.prompt:994-1010`** (already read during audit; re-confirm no drift), and the full `.do/app.example.yaml`/`.do/app.image.example.yaml`/`chat_orchestrator/project.yml` files in Step-appropriate detail before editing.

- [ ] **Step 2: `experts.definitions.prompt`** -- replace line 1002:

```
GEMINI_AGENT_PRO_MODEL
```

with:

```
thinking
```

(this is prompt *content* an LLM parses into `ExpertConfig` objects, not code -- confirm via `expert_instructions_provider.py`'s parsing logic, read in Task 1's original audit, that a bare tier name like `thinking` is an acceptable replacement value for this field, not just an env var name specifically.)

- [ ] **Step 3: `.do/app.example.yaml`** -- in the app-level `envs:` block (lines 151-168 today), replace the `GEMINI_MODEL`/`GEMINI_FALLBACK_MODEL`/`VERIFICATION_MODEL`/`THREAD_CLASSIFIER_MODEL` entries with `MODEL_THINKING`/`MODEL_FAST`/`MODEL_LITE`/`FALLBACK_MODEL`, **keeping them at app level** -- this is the precedence-gotcha the file's own comment warns about. Update the comment at line 157-158 to name the new variables instead of the retired ones. Search the rest of the file for any other occurrence of the 6 retired names (`grep -n "_MODEL" .do/app.example.yaml`) and update those too, including the `GEMINI_DEEP_THINKING_MODEL`/`GEMINI_AGENT_PRO_MODEL` entries wherever they appear (not yet located precisely -- find and confirm during this step, since the earlier audit's grep found `VERIFICATION_MODEL`/`GEMINI_MODEL`/`GEMINI_FALLBACK_MODEL`/`THREAD_CLASSIFIER_MODEL` explicitly but not those two by name in this specific file).

- [ ] **Step 4: `.do/app.image.example.yaml`** -- same treatment; this file's structure may differ from `app.example.yaml`'s (it's a separate "image-based" deployment variant per the filename) -- read it fully before assuming the same line numbers/structure apply.

- [ ] **Step 5: `chat_orchestrator/project.yml`** -- replace the `GEMINI_MODEL` entry (lines 103-105, `value: ${GEMINI_MODEL:-gemini-flash-latest}`) with `MODEL_FAST` using the same default-value syntax (`${MODEL_FAST:-gemini-flash-latest}`); search the rest of the file for other retired-name occurrences.

- [ ] **Step 6: `shared/llm/pricing.py`** -- update the docstring (lines 10-13) from:

```python
Prices below cover the direct Gemini Developer API models configured via
``GEMINI_MODEL`` / ``GEMINI_FALLBACK_MODEL`` / ``GEMINI_AGENT_PRO_MODEL`` /
``GEMINI_DEEP_THINKING_MODEL`` / ``VERIFICATION_MODEL`` in
``orchestrator/config/settings.py``.
```

to:

```python
Prices below cover the direct Gemini Developer API models configured via
``MODEL_THINKING`` / ``MODEL_FAST`` / ``MODEL_LITE`` / ``FALLBACK_MODEL``
(see ``shared/llm/model_tiers.py``).
```

No functional change -- confirmed during the design audit that this file's actual cost lookups key off resolved model strings, not env var names.

- [ ] **Step 7: Verify no other file references the 6 retired names**

Run: `command grep -rln "GEMINI_MODEL\|GEMINI_FALLBACK_MODEL\|GEMINI_DEEP_THINKING_MODEL\|GEMINI_AGENT_PRO_MODEL\|VERIFICATION_MODEL\|THREAD_CLASSIFIER_MODEL\|INTENT_ROUTER_MODEL" . 2>/dev/null | grep -v '__pycache__\|\.pyc$\|\.git/\|docs/superpowers/plans/2026-07\|docs/superpowers/plans/2026-08-05\|docs/superpowers/plans/2026-08-06'`

Expected: only `README.md` files (Step 8) and this plan/design doc's own files remain -- everything else should already be migrated by this point in the plan. Anything else found here means a call site this plan didn't anticipate; investigate and add a task rather than skip it.

- [ ] **Step 8: Update `shared/README.md`, `chat_orchestrator/README.md`, root `README.md`** for the renamed variables wherever they're mentioned (read each occurrence in context -- these are prose docs, not config, so match the surrounding style rather than a mechanical find-replace).

- [ ] **Step 9: Commit**

```bash
git add shared/prompts/library/experts.definitions.prompt \
        .do/app.example.yaml .do/app.image.example.yaml chat_orchestrator/project.yml \
        shared/llm/pricing.py shared/README.md chat_orchestrator/README.md README.md
git commit -m "feat(deploy): migrate deployment config, experts.definitions, and docs to the 3 tiers

New env vars added at DO app level (not per-service), matching the
existing precedence-safety pattern already documented in
.do/app.example.yaml for VERIFICATION_MODEL/THREAD_CLASSIFIER_MODEL."
```

---

## Task 10: Admin UI — Tier dropdown in the Prompts page

**Files:**
- Modify: `anansi_app/nicegui_app/pages/prompts.py`
- Modify: `anansi_app/tests/test_prompts_page.py`

- [ ] **Step 1: Read `anansi_app/nicegui_app/pages/prompts.py`'s full `_open_detail_dialog` function again** (already read in full during the permissions work; re-confirm current state, since this plan's Task 8 changed every prompt's frontmatter and could theoretically have shifted anything nearby -- it hasn't, but verify).

- [ ] **Step 2: Add a Tier dropdown next to the existing Google Doc section**, gated the same way `doc_id_input`/`override_switch` already are (`row.can_edit`/`row.can_publish`):

```python
                ui.separator().classes("q-my-sm")
                ui.label("Model Tier").classes("text-caption text-bold")
                tier_select = ui.select(
                    ["thinking", "fast", "lite"], value=spec.model, label="Tier"
                ).props("dense").classes("w-48")
                tier_select.set_enabled(row.can_edit)

                async def save_tier() -> None:
                    try:
                        store.set_model_override(row.prompt_id, tier_select.value, actor=user_email)
                        ui.notify(f"Tier set to {tier_select.value}", type="positive")
                        dialog.close()
                        refresh()
                    except (PermissionError, RuntimeError) as e:
                        ui.notify(str(e), type="negative")

                async def revert_tier() -> None:
                    try:
                        store.clear_model_override(row.prompt_id, actor=user_email)
                        ui.notify("Tier reverted to bundled default", type="positive")
                        dialog.close()
                        refresh()
                    except (PermissionError, RuntimeError) as e:
                        ui.notify(str(e), type="negative")

                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Revert tier", on_click=revert_tier).props("flat").set_visibility(
                        row.can_edit
                    )
                    ui.button("Save tier", on_click=save_tier).props("flat").set_visibility(
                        row.can_edit
                    )
```

Place this block right after the existing Google Doc section's `override_banner` and before the body-editing `ui.separator()`/buttons row (read the exact surrounding structure in Step 1 to confirm placement -- don't guess the insertion point without checking against current file content).

- [ ] **Step 3: Add a Tier column to the list view's `_render_row`** (optional but consistent with how `source`/`version`/`locked` badges already show at a glance) -- if added, a one-line `ui.label(f"tier:{row.tier}")` sourced from a new `tier` field on `PromptRow`, populated in `build_rows()` from `PROMPTS.spec(prompt_id).model`.

- [ ] **Step 4: Update `test_prompts_page.py`** for whatever it currently asserts about the dialog's contents, adding coverage for the new Tier dropdown's visibility/save/revert following the exact same pattern the existing Google Doc section's tests already use (read those tests first, mirror their structure).

- [ ] **Step 5: Manual verification** -- since this is a NiceGUI page without the automated test coverage the backend has, run the app locally (or via the `run` skill if configured for this project) and confirm: opening a prompt shows its current tier; changing and saving updates `prompt_model_overrides`; reverting removes the row and the dropdown falls back to the frontmatter default.

- [ ] **Step 6: Commit**

```bash
git add anansi_app/nicegui_app/pages/prompts.py anansi_app/tests/test_prompts_page.py
git commit -m "feat(prompts-ui): add live Tier dropdown to the Prompts admin page

Saves to prompt_model_overrides, gated by the same access.edit grant
as the body Save button. Revert removes the override row, falling
back to the prompt's frontmatter model field."
```

---

## Task 11: Full verification sweep

- [ ] **Step 1: Run the complete affected test surface**

Run (from `chat_orchestrator/`): `uv run pytest ../shared/tests/ tests/ -v`
Run (from `anansi_app/`, using whatever its own test invocation turns out to be per Task 3 Step 5's investigation): its full suite.
Run (from `mcp_servers/`, using whatever its own test invocation turns out to be per Task 7's investigation): its full suite, or at minimum the two touched files' tests.

Expected: all passing. Investigate and fix (don't skip) anything red that this plan's changes plausibly caused; if something fails and is clearly pre-existing/unrelated (per this repo's own CLAUDE.md guidance on distinguishing the two), flag it separately rather than folding an unrelated fix into this PR.

- [ ] **Step 2: Full project verification**

Run from the worktree root: `uvx pre-commit run --all-files`
Expected: clean. If `test-wiring` flags any untracked file under a `tests/` directory, force-add it after vetting for operator data, per this repo's established process.

- [ ] **Step 3: No commit needed for this task** (verification-only).

---

## Task 12: Push and open the PR

- [ ] **Step 1: Push**

```bash
git push -u origin feature/model-tier-selection
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "Replace scattered model env vars with three named tiers (thinking/fast/lite)" --body "$(cat <<'EOF'
## Summary
- MODEL_THINKING/MODEL_FAST/MODEL_LITE replace GEMINI_MODEL, GEMINI_FALLBACK_MODEL (renamed FALLBACK_MODEL, kept separate -- not a quality tier), GEMINI_DEEP_THINKING_MODEL, GEMINI_AGENT_PRO_MODEL, VERIFICATION_MODEL, THREAD_CLASSIFIER_MODEL, and INTENT_ROUTER_MODEL.
- Registered in flag_registry.py -- live-editable via anansi_app's existing Settings page for free, same mechanism GEMINI_MODEL already used.
- Every prompt in the library gets an explicit `model:` tier in frontmatter, with a new prompt_model_overrides table (mirrors prompt_doc_bindings) letting an admin change any prompt's tier live from the Prompts page, no PR needed -- gated by that prompt's existing edit/publish grants.
- Migrated every consumer: the ~10 prompt-tied services, verification_service.py's 3-prompts-one-model structural issue (each prompt now resolves its own tier independently), prompt-independent consumers in mcp_servers and handler.py, command_registry.py's separate model_override mechanism, and DO deployment config (added at app level -- see the precedence comment already in .do/app.example.yaml).
- pricing.py's docstring updated; its cost logic was already keyed off resolved model strings, not env var names, so no functional change there.

Full design rationale: docs/superpowers/specs/2026-08-08-model-tier-selection-design.md

## Test plan
- [x] Full affected test suite green across chat_orchestrator, shared, mcp_servers, anansi_app
- [x] pre-commit run --all-files clean
- [ ] Manual verification of the Tier dropdown's save/revert in the admin UI

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Report the PR URL back.**
