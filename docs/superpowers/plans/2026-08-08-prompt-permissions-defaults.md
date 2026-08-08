# Prompt Permissions Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unlock `ticketing.jira_issue_types` and `ticketing.correlation` for ops/eng editing, default the other 14 currently-locked prompts to admin-only, and keep every test/docstring that asserts the old policy honest about the new one.

**Architecture:** Frontmatter-only. Every change is a `.prompt` file's `overridable`/`access`/`description` fields, plus the tests and docstrings that assert those fields — zero changes to `access.py`, `core.py`, or the Prompts admin UI. Full rationale: [`docs/superpowers/specs/2026-08-08-prompt-permissions-design.md`](../specs/2026-08-08-prompt-permissions-design.md).

**Tech Stack:** Python 3.11, pytest, YAML frontmatter (`shared/prompts/spec.py`'s parser). No new dependencies.

---

## File Structure

**Modify — prompt frontmatter:**
- `shared/prompts/library/ticketing.jira_issue_types.prompt` — full unlock
- `shared/prompts/library/ticketing.correlation.prompt` — split-gate unlock + description
- 14 other `.prompt` files (listed in Task 2) — `overridable` flip only

**Modify — tests (each asserts a policy that's changing):**
- `shared/tests/test_prompt_library_contents.py` — ticketing policy
- `chat_orchestrator/tests/services/ticketing/test_correlation_rules.py` — correlation-specific
- `shared/tests/test_prompt_services.py` — 6 of the 14 (service prompts)
- `shared/tests/test_prompt_ingestion.py` — 6 of the 14 (ingestion prompts)
- `shared/tests/test_prompt_misc.py` — remaining 2 of the 14

**Modify — production docstrings (no behavior change, but currently claim a guarantee that's going away):**
- `chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py`

No files created. No files deleted.

---

## Task 1: Unlock the two ticketing prompts

**Files:**
- Modify: `shared/prompts/library/ticketing.jira_issue_types.prompt`
- Modify: `shared/prompts/library/ticketing.correlation.prompt`
- Modify: `shared/tests/test_prompt_library_contents.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_correlation_rules.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py`

- [ ] **Step 1: Update the failing tests in `shared/tests/test_prompt_library_contents.py`**

Replace the module docstring:

```python
"""Every prompt in the library parses, and prompts with a specific override policy keep it."""
```

Replace the `NON_OVERRIDABLE`/`OVERRIDABLE` block:

```python
# Historically Google-Doc-driven (VERIFICATION_DOC_ID) with no bundled
# fallback at all; kept overridable so the doc keeps working exactly as
# before (Phase 1 parity), even though verification is safety-sensitive.
OVERRIDABLE = {
    "verification.criteria",
}

# Both unlocked for ops/eng drafting, but correlation keeps publish eng-only
# -- it feeds alert-grouping decisions and used to be overridable: false for
# that reason (see correlation_rules.py's docstring). Drafting is open to
# both; only eng can promote a correlation change to live. See
# docs/superpowers/specs/2026-08-08-prompt-permissions-design.md.
TICKETING_ACCESS = {
    "ticketing.jira_issue_types": (["eng", "ops"], ["eng", "ops"]),
    "ticketing.correlation": (["eng", "ops"], ["eng"]),
}
```

Replace the `test_protected_prompts_are_not_overridable` test:

```python
@pytest.mark.parametrize("prompt_id", sorted(TICKETING_ACCESS))
def test_ticketing_prompts_have_the_expected_access(prompt_id):
    spec = PROMPTS.spec(prompt_id)
    edit, publish = TICKETING_ACCESS[prompt_id]
    assert spec.overridable is True
    assert sorted(spec.access.edit) == edit
    assert sorted(spec.access.publish) == publish
```

(Leave `test_doc_driven_prompts_stay_overridable` and everything below it untouched.)

- [ ] **Step 2: Update the failing test in `chat_orchestrator/tests/services/ticketing/test_correlation_rules.py`**

Replace:

```python
    def test_the_ticketing_correlation_prompt_is_not_overridable(self):
        """There is intentionally no document id or DB override for this
        policy: the guarantee this module's docstring describes is enforced
        by the prompt spec, not by a fallback-of-a-fallback here."""
        from shared.prompts import PROMPTS

        assert PROMPTS.spec("ticketing.correlation").overridable is False
```

with:

```python
    def test_the_ticketing_correlation_prompt_requires_eng_to_publish(self):
        """Drafting a grouping-rule change is open to ops/eng, but only eng
        can publish one live -- the guarantee this module's docstring used
        to describe (a PR-only lock) is now a permission gate enforced by
        the prompt spec's access.publish, not a fallback-of-a-fallback here.
        See docs/superpowers/specs/2026-08-08-prompt-permissions-design.md."""
        from shared.prompts import PROMPTS

        spec = PROMPTS.spec("ticketing.correlation")
        assert spec.overridable is True
        assert sorted(spec.access.edit) == ["eng", "ops"]
        assert spec.access.publish == ["eng"]
```

- [ ] **Step 3: Run both test files and confirm they fail for the right reason**

Run: `uv run pytest ../shared/tests/test_prompt_library_contents.py tests/services/ticketing/test_correlation_rules.py -v` (from `chat_orchestrator/` in the worktree)

Expected: 2 FAILs —
`test_ticketing_prompts_have_the_expected_access[ticketing.jira_issue_types]` and
`test_ticketing_prompts_have_the_expected_access[ticketing.correlation]` both fail with `assert False is True` (frontmatter still says `overridable: false`). `test_the_ticketing_correlation_prompt_requires_eng_to_publish` fails the same way. Everything else in both files still passes.

- [ ] **Step 4: Edit `shared/prompts/library/ticketing.jira_issue_types.prompt`'s frontmatter**

Replace:

```yaml
overridable: false
```

with:

```yaml
overridable: true
```

Replace:

```yaml
access:
  view: [ops, eng]
  edit: []
  publish: []
```

with:

```yaml
access:
  view: [ops, eng]
  edit: [ops, eng]
  publish: [ops, eng]
```

- [ ] **Step 5: Edit `shared/prompts/library/ticketing.correlation.prompt`'s frontmatter**

Replace:

```yaml
description: Alert-correlation grouping policy. Versioned with the application; not editable at runtime.
```

with:

```yaml
description: Alert-correlation grouping policy. Draft changes are open to ops/eng from the Prompts admin page; only eng can publish one live.
```

Replace:

```yaml
overridable: false
```

with:

```yaml
overridable: true
```

Replace:

```yaml
access:
  view: [ops, eng]
  edit: []
  publish: []
```

with:

```yaml
access:
  view: [ops, eng]
  edit: [ops, eng]
  publish: [eng]
```

- [ ] **Step 6: Rewrite the two docstrings in `chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py`**

Replace the module docstring:

```python
"""Versioned alert-correlation policy and supporting operational context.

The LLM instructions and safety bounds in this module ship with the
application. Deployments may disable correlation with the kill switch, but
cannot silently substitute different grouping rules, confidence bounds, or
prompt limits.
"""
```

with:

```python
"""Versioned alert-correlation policy and supporting operational context.

The safety bounds in this module (``CorrelationPolicy`` below) ship with the
application and cannot be changed without a PR. The grouping-rules prompt
text is different: ops/eng can draft a change from the Prompts admin page,
but only eng can publish one live -- see ``get_correlation_instructions``
and docs/superpowers/specs/2026-08-08-prompt-permissions-design.md.
Deployments may disable correlation entirely with the kill switch.
"""
```

Replace the `get_correlation_instructions` docstring:

```python
def get_correlation_instructions() -> Dict[str, str]:
    """Load the bundled correlation policy.

    There is intentionally no document id or other deployment override: rule
    changes are reviewed and versioned with the application. The
    ``ticketing.correlation`` prompt is declared ``overridable: false``, so
    the prompt library always resolves it from the bundled file regardless
    of any DB or Google Doc state — the guarantee this module's docstring
    describes is enforced one layer down now, not duplicated here.
    """
    return {"system_instructions": PROMPTS.render("ticketing.correlation").system_text}
```

with:

```python
def get_correlation_instructions() -> Dict[str, str]:
    """Load the live correlation policy (bundled default, or a published
    override).

    ``ticketing.correlation`` is ``overridable: true`` with
    ``access.edit: [eng, ops]`` and ``access.publish: [eng]``: ops/eng can
    draft a grouping-rule change from the Prompts admin page, but only an
    eng account can publish it live. That is a permission gate, not code
    review -- nothing here re-checks a published change's content, and an
    eng account can draft and publish its own change with nobody else
    looking at it. There is still no document override for this prompt.
    """
    return {"system_instructions": PROMPTS.render("ticketing.correlation").system_text}
```

- [ ] **Step 7: Run the same tests again and confirm they pass**

Run: `uv run pytest ../shared/tests/test_prompt_library_contents.py tests/services/ticketing/test_correlation_rules.py -v` (from `chat_orchestrator/`)

Expected: `10 passed` for `test_prompt_library_contents.py` (9 today, minus the retired single-param `test_protected_prompts_are_not_overridable`, plus the new 2-param `test_ticketing_prompts_have_the_expected_access` = 10), `9 passed` for `test_correlation_rules.py` (unchanged count -- one test renamed, not added or removed). Zero failures.

- [ ] **Step 8: Commit**

```bash
git add shared/prompts/library/ticketing.jira_issue_types.prompt \
        shared/prompts/library/ticketing.correlation.prompt \
        shared/tests/test_prompt_library_contents.py \
        chat_orchestrator/tests/services/ticketing/test_correlation_rules.py \
        chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py
git commit -m "feat(prompts): unlock ticketing prompts for ops/eng, eng-gated publish on correlation

ticketing.jira_issue_types: full edit+publish for ops/eng.
ticketing.correlation: edit open to ops/eng, publish stays eng-only --
replaces the old overridable:false PR-only lock with a permission gate.
See docs/superpowers/specs/2026-08-08-prompt-permissions-design.md."
```

---

## Task 2: Default the other 14 locked prompts to admin-only

**Files:**
- Modify: `shared/prompts/library/context_filter.relevance.prompt`
- Modify: `shared/prompts/library/doc_editing.edit_highlighted.prompt`
- Modify: `shared/prompts/library/doc_editor.locate_edits.prompt`
- Modify: `shared/prompts/library/ingestion.classify_document.prompt`
- Modify: `shared/prompts/library/ingestion.detect_contradictions.prompt`
- Modify: `shared/prompts/library/ingestion.extract_entities.prompt`
- Modify: `shared/prompts/library/ingestion.improve_content.modification.prompt`
- Modify: `shared/prompts/library/ingestion.improve_content.naming.prompt`
- Modify: `shared/prompts/library/ingestion.improve_content.quality_eval.prompt`
- Modify: `shared/prompts/library/intent_router.route.prompt`
- Modify: `shared/prompts/library/procedure.match.prompt`
- Modify: `shared/prompts/library/thread_assignment.classify.prompt`
- Modify: `shared/prompts/library/verification.sanitize.prompt`
- Modify: `shared/prompts/library/verification.sanitize_system.prompt`
- Modify: `shared/tests/test_prompt_services.py`
- Modify: `shared/tests/test_prompt_ingestion.py`
- Modify: `shared/tests/test_prompt_misc.py`

- [ ] **Step 1: Update the failing test in `shared/tests/test_prompt_services.py`**

Replace:

```python
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

with:

```python
OVERRIDABLE = {"conversation.summarize", "procedure.suggest"}

# Unlocked (was overridable: false) but with no ops/eng grant added -- only
# an admin can edit/publish these, via access.py's is_prompt_admin() bypass.
# See docs/superpowers/specs/2026-08-08-prompt-permissions-design.md.
ADMIN_ONLY = {
    "context_filter.relevance",
    "thread_assignment.classify",
    "intent_router.route",
    "procedure.match",
    "verification.sanitize",
    "verification.sanitize_system",
}


@pytest.mark.parametrize("prompt_id", sorted(OVERRIDABLE | ADMIN_ONLY))
def test_service_prompt_exists(prompt_id):
    assert prompt_id in PROMPTS.ids()


@pytest.mark.parametrize("prompt_id", sorted(ADMIN_ONLY))
def test_admin_only_service_prompts_have_no_team_grants(prompt_id):
    spec = PROMPTS.spec(prompt_id)
    assert spec.overridable is True
    assert spec.access.edit == []
    assert spec.access.publish == []


@pytest.mark.parametrize("prompt_id", sorted(OVERRIDABLE))
def test_ops_editable_service_prompts_are_overridable(prompt_id):
    assert PROMPTS.spec(prompt_id).overridable is True
```

- [ ] **Step 2: Update the failing test in `shared/tests/test_prompt_ingestion.py`**

Replace:

```python
LOCKED_IDS = [
    "ingestion.classify_document",
    "ingestion.detect_contradictions",
    "ingestion.extract_entities",
    "ingestion.improve_content.quality_eval",
    "ingestion.improve_content.modification",
    "ingestion.improve_content.naming",
]

# Not an LLM prompt — a static user-facing message. Safe for ops to edit.
OVERRIDABLE_IDS = [
    "ingestion.fetch_document.type_selection",
]


def test_all_ingestion_prompts_exist():
    for prompt_id in LOCKED_IDS + OVERRIDABLE_IDS:
        assert prompt_id in PROMPTS.ids()


def test_locked_ingestion_prompts_are_locked():
    for prompt_id in LOCKED_IDS:
        assert PROMPTS.spec(prompt_id).overridable is False


def test_type_selection_is_overridable_and_has_no_variables():
    spec = PROMPTS.spec("ingestion.fetch_document.type_selection")
    assert spec.overridable is True
    assert spec.variables == []


def test_json_prompts_declare_a_schema():
    for prompt_id in LOCKED_IDS:
        spec = PROMPTS.spec(prompt_id)
        if spec.output == "json":
            assert spec.schema, f"{prompt_id} declares json output but no schema"
```

with:

```python
# Unlocked (was overridable: false) but with no ops/eng grant added -- only
# an admin can edit/publish these, via access.py's is_prompt_admin() bypass.
# See docs/superpowers/specs/2026-08-08-prompt-permissions-design.md.
ADMIN_ONLY_IDS = [
    "ingestion.classify_document",
    "ingestion.detect_contradictions",
    "ingestion.extract_entities",
    "ingestion.improve_content.quality_eval",
    "ingestion.improve_content.modification",
    "ingestion.improve_content.naming",
]

# Not an LLM prompt — a static user-facing message. Safe for ops to edit.
OVERRIDABLE_IDS = [
    "ingestion.fetch_document.type_selection",
]


def test_all_ingestion_prompts_exist():
    for prompt_id in ADMIN_ONLY_IDS + OVERRIDABLE_IDS:
        assert prompt_id in PROMPTS.ids()


def test_admin_only_ingestion_prompts_have_no_team_grants():
    for prompt_id in ADMIN_ONLY_IDS:
        spec = PROMPTS.spec(prompt_id)
        assert spec.overridable is True
        assert spec.access.edit == []
        assert spec.access.publish == []


def test_type_selection_is_overridable_and_has_no_variables():
    spec = PROMPTS.spec("ingestion.fetch_document.type_selection")
    assert spec.overridable is True
    assert spec.variables == []


def test_json_prompts_declare_a_schema():
    for prompt_id in ADMIN_ONLY_IDS:
        spec = PROMPTS.spec(prompt_id)
        if spec.output == "json":
            assert spec.schema, f"{prompt_id} declares json output but no schema"
```

- [ ] **Step 3: Update the failing test in `shared/tests/test_prompt_misc.py`**

Replace:

```python
def test_all_remaining_prompts_exist():
    for prompt_id in IDS:
        assert prompt_id in PROMPTS.ids()


def test_grafana_prompt_flag_is_retired():
```

with:

```python
def test_all_remaining_prompts_exist():
    for prompt_id in IDS:
        assert prompt_id in PROMPTS.ids()


# Unlocked (was overridable: false) but with no ops/eng grant added -- only
# an admin can edit/publish these, via access.py's is_prompt_admin() bypass.
# See docs/superpowers/specs/2026-08-08-prompt-permissions-design.md.
ADMIN_ONLY_IDS = ["doc_editing.edit_highlighted", "doc_editor.locate_edits"]


def test_admin_only_misc_prompts_have_no_team_grants():
    for prompt_id in ADMIN_ONLY_IDS:
        spec = PROMPTS.spec(prompt_id)
        assert spec.overridable is True
        assert spec.access.edit == []
        assert spec.access.publish == []


def test_grafana_prompt_flag_is_retired():
```

- [ ] **Step 4: Run all three test files and confirm they fail for the right reason**

Run: `uv run pytest ../shared/tests/test_prompt_services.py ../shared/tests/test_prompt_ingestion.py ../shared/tests/test_prompt_misc.py -v` (from `chat_orchestrator/`)

Expected: `test_admin_only_service_prompts_have_no_team_grants` (6 cases), `test_admin_only_ingestion_prompts_have_no_team_grants` (1 case, loops internally), and `test_admin_only_misc_prompts_have_no_team_grants` (1 case, loops internally) all FAIL with `assert False is True` — frontmatter still says `overridable: false` for all 14. Everything else passes.

- [ ] **Step 5: Flip `overridable` on all 14 files**

Run this from the worktree root (`/Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/prompt-permissions-defaults`):

```bash
python3 -c "
import pathlib

files = [
    'context_filter.relevance.prompt',
    'doc_editing.edit_highlighted.prompt',
    'doc_editor.locate_edits.prompt',
    'ingestion.classify_document.prompt',
    'ingestion.detect_contradictions.prompt',
    'ingestion.extract_entities.prompt',
    'ingestion.improve_content.modification.prompt',
    'ingestion.improve_content.naming.prompt',
    'ingestion.improve_content.quality_eval.prompt',
    'intent_router.route.prompt',
    'procedure.match.prompt',
    'thread_assignment.classify.prompt',
    'verification.sanitize.prompt',
    'verification.sanitize_system.prompt',
]
base = pathlib.Path('shared/prompts/library')
for name in files:
    p = base / name
    text = p.read_text()
    new_text = text.replace('overridable: false\n', 'overridable: true\n', 1)
    assert new_text != text, f'{name}: expected literal \"overridable: false\" not found'
    p.write_text(new_text)
    print(f'updated {name}')
"
```

Expected output: 14 lines, one `updated <name>` per file, no `AssertionError`.

- [ ] **Step 6: Run the same three test files again and confirm they pass**

Run: `uv run pytest ../shared/tests/test_prompt_services.py ../shared/tests/test_prompt_ingestion.py ../shared/tests/test_prompt_misc.py -v` (from `chat_orchestrator/`)

Expected: all PASS, zero failures.

- [ ] **Step 7: Commit**

```bash
git add shared/prompts/library/context_filter.relevance.prompt \
        shared/prompts/library/doc_editing.edit_highlighted.prompt \
        shared/prompts/library/doc_editor.locate_edits.prompt \
        shared/prompts/library/ingestion.classify_document.prompt \
        shared/prompts/library/ingestion.detect_contradictions.prompt \
        shared/prompts/library/ingestion.extract_entities.prompt \
        shared/prompts/library/ingestion.improve_content.modification.prompt \
        shared/prompts/library/ingestion.improve_content.naming.prompt \
        shared/prompts/library/ingestion.improve_content.quality_eval.prompt \
        shared/prompts/library/intent_router.route.prompt \
        shared/prompts/library/procedure.match.prompt \
        shared/prompts/library/thread_assignment.classify.prompt \
        shared/prompts/library/verification.sanitize.prompt \
        shared/prompts/library/verification.sanitize_system.prompt \
        shared/tests/test_prompt_services.py \
        shared/tests/test_prompt_ingestion.py \
        shared/tests/test_prompt_misc.py
git commit -m "feat(prompts): default the other 14 locked prompts to admin-only

overridable: true on all 14, access.edit/access.publish left empty --
access.py's is_prompt_admin() bypass already grants any admin edit/publish
on an overridable prompt regardless of its access list, so no explicit
grant is needed (matches how every other prompt in the library already
never lists 'admin'). ops/eng get nothing new on these 14.
See docs/superpowers/specs/2026-08-08-prompt-permissions-design.md."
```

---

## Task 3: Full verification sweep

- [ ] **Step 1: Run the complete affected test surface from a clean slate**

Run (from `chat_orchestrator/` in the worktree):

```bash
uv run pytest ../shared/tests/test_prompt_library_contents.py \
              ../shared/tests/test_prompt_services.py \
              ../shared/tests/test_prompt_ingestion.py \
              ../shared/tests/test_prompt_misc.py \
              ../shared/tests/test_prompt_access.py \
              ../shared/tests/test_prompt_spec.py \
              ../shared/tests/test_prompt_write_api.py \
              ../shared/tests/test_prompt_library.py \
              tests/services/ticketing/ \
              tests/test_prompt_parity.py \
              -v
```

Expected: all PASS, zero failures. (`test_prompt_access.py`/`test_prompt_spec.py`/`test_prompt_library.py` exercise `access.py`'s logic and the parser against synthetic fixtures, not real prompts — included as a regression check since this plan touches real prompts' access fields, even though no code in those modules changes.)

- [ ] **Step 2: Full project verification**

Run from the worktree root:

```bash
pre-commit run --all-files
```

Expected: all hooks pass. If `test-wiring` or any hook reports an untracked file under a `tests/` directory, stop and check whether it's one of the files this plan intentionally modified (it shouldn't be — this plan only modifies existing tracked test files, creates none) before doing anything else.

- [ ] **Step 3: If everything is clean, no commit needed for this task** (verification-only; Task 1 and Task 2 already committed their own changes).

---

## Task 4: Push and open the PR

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feature/prompt-permissions-defaults
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "Unlock ticketing prompts for ops/eng; default other locked prompts to admin-only" --body "$(cat <<'EOF'
## Summary
- `ticketing.jira_issue_types`: fully unlocked, edit+publish for ops/eng.
- `ticketing.correlation`: unlocked with a split gate -- edit open to ops/eng, publish stays eng-only. Replaces the old `overridable: false` PR-only lock (see `correlation_rules.py`'s docstring) with a permission gate; this is weaker than code review (an eng account can draft and publish its own change solo) and the rewritten docstrings say so.
- The other 14 currently-locked prompts default to admin-only: `overridable: true`, access lists left empty (admins already bypass empty access lists via `is_prompt_admin()`; ops/eng get nothing new).
- The 11 already-overridable prompts are untouched.
- Updated every test that asserted the old locked state (`test_prompt_library_contents.py`, `test_prompt_services.py`, `test_prompt_ingestion.py`, `test_prompt_misc.py`, `test_correlation_rules.py`) to assert the new one instead of just deleting coverage.

No changes to `access.py`, `core.py`, or the Prompts admin UI -- the existing frontmatter + access-list machinery already expresses all of this.

Full design rationale: `docs/superpowers/specs/2026-08-08-prompt-permissions-design.md`

## Test plan
- [x] `pytest` green on every test file this PR touches (see commits)
- [x] `pre-commit run --all-files` clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Report the PR URL back**

`gh pr create` prints the PR URL on success — share it directly rather than re-deriving it.
