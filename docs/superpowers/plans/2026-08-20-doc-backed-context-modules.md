# Doc-Backed Context Modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator attach a live Google Doc or Sheet as a context module, resolved per-request and gated against that document's own Drive ACL.

**Architecture:** `gdoc` moves off `PromptLibrary`'s synchronous render path onto `JitContextResolver`, where `ResolutionContext` already carries the caller's email. `GDocProvider` becomes an async `ContextProvider` that checks Drive access before fetching. Three separate read paths get the same gate: pinned context, the on-demand catalog line, and the `get_knowledge_module` MCP tool.

**Tech Stack:** Python 3.11, pytest, Supabase (`chat_db`), Google Drive/Docs/Sheets APIs via service account, NiceGUI (admin app).

**Spec:** `docs/superpowers/specs/2026-08-20-doc-backed-context-modules-design.md`

---

## Orientation for the implementer

Read this before Task 1. It is the context you cannot get from the diff.

**Three apps share one `shared/` package.** `chat_orchestrator` (the bot),
`anansi_app` (the NiceGUI admin UI), `mcp_servers` (tool servers). `shared/`
must never import from any of them.

**How to run tests.** There is no venv at the repo root. Everything runs from
`chat_orchestrator`:

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_gdoc_provider.py -q
```

| Suite | Command (from repo root) |
|---|---|
| shared | `cd chat_orchestrator && uv run pytest ../shared -q` |
| orchestrator | `cd chat_orchestrator && uv run pytest tests/ -q` |
| mcp_servers | `PYTHONPATH="$PWD:$PWD/mcp_servers" pytest mcp_servers/tests -q` |
| anansi_app | `PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests -q` |

**Async tests under `shared/tests/` MUST carry `@pytest.mark.asyncio`.**
`chat_orchestrator/pyproject.toml` sets `asyncio_mode = "auto"`, but the repo-root
`pyproject.toml` does not. A test targeted directly can resolve the root config
and silently skip. Every async test in this plan has the marker — do not remove it.

**New test files need `git add -f`.** `.gitignore` denies `tests/` by default.
A plain `git add` on a new test file is a silent no-op: the commit succeeds, the
file never reaches the remote, CI never runs it. This plan creates two new test
files (Tasks 1 and 3) and every commit step for them uses `-f`.

**`docs/superpowers/plans/` is also gitignored** — committing this plan needs
`git add -f` too.

**Before pushing:** `pre-commit run --all-files`, not just `pytest`.

**Do not run `pytest` with no path.** Other sessions may share this checkout.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `shared/utils/drive_permissions.py` | Drive ACL checks. Gains `strict` mode + `domain` grants. | 1 |
| `shared/tests/test_drive_permissions.py` | **New.** Covers the ACL matrix. | 1 |
| `db/migrations/0018_doc_backed_modules.sql` | **New.** Audience/tab columns, constraint fixes, scope rename. | 2 |
| `db/schema/chat_db.sql` | Schema of record, kept in step with the migration. | 2 |
| `shared/prompts/knowledge.py` | Module value type, selection, budgeting. Gains fields + `JIT_SOURCES`. | 2, 5, 9 |
| `shared/utils/gdrive_doc_fetcher.py` | Drive fetch/convert. Gains sheet → markdown. | 3 |
| `shared/tests/test_sheet_markdown.py` | **New.** Table conversion and truncation. | 3 |
| `shared/prompts/providers_gdoc.py` | The provider. Becomes async + access-gated. | 4, 8 |
| `shared/prompts/providers.py` | Provider protocol. Gains optional `visible_to`. | 7 |
| `shared/prompts/core.py` | Deletes the sync gdoc path. | 5 |
| `chat_orchestrator/orchestrator/services/jit_context_resolver.py` | Registers the provider, budgets output, gates the catalog. | 5, 6, 7 |
| `shared/prompts/types.py` | `RequestScope.matches` accepts `global`. | 9 |
| `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py` | Gates the on-demand tool. | 8 |
| `anansi_app/nicegui_app/pages/knowledge_modules.py` | Source picker, audience, live preview, scope dropdown. | 10 |
| `chat_orchestrator/.../context_expert/store_module.py` | Persists doc-linked modules. | 11 |
| `chat_orchestrator/.../ingestion_expert/fetch_document.py` | Accepts sheets; offers live-link. | 11 |
| `shared/prompts/library/experts.definitions.prompt` | `/learn` workflow copy. | 11 |

---

## Phase 1 — Drive permission hardening

### Task 1: `strict` mode and `domain` grants on `user_can_access`

`user_can_access` currently ends with a blanket grant: if the service account can
reach the file at all, read is allowed. For a link-shared or Shared Drive
document that lets **every** caller through — including customers — which would
make the whole feature decorative. `strict=True` removes only that final branch.

It also ignores `type: "domain"` permissions entirely today. "Shared with everyone
at the company" is the most common sharing mode for an internal ops document, so
without handling it, strict mode false-denies most staff.

**Files:**
- Modify: `shared/utils/drive_permissions.py`
- Test: `shared/tests/test_drive_permissions.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `shared/tests/test_drive_permissions.py`:

```python
"""Drive ACL evaluation: which permission entries grant which callers."""

import pytest

from shared.utils.drive_permissions import ROLE_RANK, _permission_grants


def _grants(perm, email="tech@example.com", required="reader", need_write=False):
    return _permission_grants(perm, email, ROLE_RANK[required], need_write)


def test_exact_email_match_grants_read():
    assert _grants({"type": "user", "emailAddress": "Tech@Example.com", "role": "reader"})


def test_email_match_is_case_insensitive():
    assert _grants({"type": "user", "emailAddress": "TECH@EXAMPLE.COM", "role": "reader"})


def test_a_different_email_does_not_grant():
    assert not _grants({"type": "user", "emailAddress": "other@example.com", "role": "reader"})


def test_reader_role_does_not_satisfy_a_write_requirement():
    perm = {"type": "user", "emailAddress": "tech@example.com", "role": "reader"}
    assert not _grants(perm, required="writer", need_write=True)


def test_anyone_grants_read_but_never_write():
    perm = {"type": "anyone", "role": "writer"}
    assert _grants(perm)
    assert not _grants(perm, required="writer", need_write=True)


def test_domain_permission_grants_a_matching_domain():
    """'Shared with everyone at the company' is the common case for an ops doc."""
    assert _grants({"type": "domain", "domain": "Example.com", "role": "reader"})


def test_domain_permission_does_not_grant_another_domain():
    perm = {"type": "domain", "domain": "example.com", "role": "reader"}
    assert not _grants(perm, email="outsider@elsewhere.com")


def test_domain_permission_does_not_grant_a_lookalike_suffix():
    """notexample.com must not match example.com."""
    perm = {"type": "domain", "domain": "example.com", "role": "reader"}
    assert not _grants(perm, email="tech@notexample.com")


def test_group_permission_never_grants():
    """Expanding group membership needs the Admin SDK, which is not wired up."""
    perm = {"type": "group", "emailAddress": "ops@example.com", "role": "writer"}
    assert not _grants(perm)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_drive_permissions.py -q
```

Expected: collection error — `ImportError: cannot import name '_permission_grants'`.

- [ ] **Step 3: Add the helper**

In `shared/utils/drive_permissions.py`, immediately after the `ROLE_RANK` dict:

```python
def _permission_grants(
    perm: dict,
    user_email: str,
    required_rank: int,
    need_write: bool,
) -> bool:
    """Whether one Drive permission entry grants this user the required role.

    Extracted so the permissions.list() and files.get() branches below cannot
    drift apart -- they previously carried two near-identical copies of this
    logic, and only one of them would have gained `domain` support.

    'group' entries deliberately never grant: expanding group membership needs
    the Admin SDK, which is not wired up. Direct shares are the workaround.
    """
    if ROLE_RANK.get(perm.get("role", ""), -1) < required_rank:
        return False

    if perm.get("emailAddress", "").lower() == user_email.lower():
        return True

    perm_type = perm.get("type")
    if perm_type == "anyone":
        return not need_write
    if perm_type == "domain":
        domain = (perm.get("domain") or "").lower()
        # endswith on "@" + domain, not a bare suffix check: "notexample.com"
        # must not satisfy a permission for "example.com".
        return bool(domain) and user_email.lower().endswith("@" + domain)
    return False
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_drive_permissions.py -q
```

Expected: 9 passed.

- [ ] **Step 5: Wire the helper into both branches and add `strict`**

In `shared/utils/drive_permissions.py`, change the signature:

```python
async def user_can_access(
    file_id: str,
    user_email: str | None,
    need_write: bool = False,
    strict: bool = False,
) -> bool:
    """Check if a user has access to a Google Drive file.

    Returns False (fail-closed) if email is None. Logs denied access at
    WARNING level for audit.

    ``strict=True`` removes only the final "service account could reach the
    file, so allow read" fallback. Everything else -- explicit email match,
    'anyone' link sharing, domain-wide sharing, and the files.get() retry when
    permissions.list() is unavailable -- behaves identically. Callers that
    surface document *content* to an end user must pass strict=True: without
    it, any link-shared or Shared Drive file is readable by everyone.
    """
```

Add `domain` to **both** `fields` strings so domain permissions are actually
returned — the helper cannot grant on a field that was never requested:

```python
                    fields="permissions(emailAddress,role,type,domain)",
```

```python
                    fields="id,permissions(emailAddress,role,type,domain)",
```

Replace the two inline permission loops with the helper. The `permissions.list()`
branch becomes:

```python
        if permissions:
            for perm in permissions:
                if _permission_grants(perm, user_email, required_rank, need_write):
                    return True
```

The `files.get()` branch becomes:

```python
            for perm in meta.get("permissions", []):
                if _permission_grants(perm, user_email, required_rank, need_write):
                    return True

            # Service account reached the file but sees no matching permission
            # → inherited/link-share access. Grant read; write still requires
            # an explicit direct share. strict=True withholds it: this branch
            # would otherwise grant every caller read on any link-shared or
            # Shared Drive file.
            if not need_write and meta.get("id"):
                if strict:
                    LOGGER.info(
                        f"Strict check withheld {file_id} from {user_email}: reachable "
                        f"by the service account but no permission entry matches"
                    )
                else:
                    LOGGER.info(
                        f"Drive fallback: granting read access to {user_email} for {file_id}"
                        " (service account can access file — link or inherited share)"
                    )
                    return True
```

- [ ] **Step 6: Add tests for `strict` at the `user_can_access` level**

Append to `shared/tests/test_drive_permissions.py`:

```python
class _FakeExecutable:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error

    def execute(self):
        if self._error:
            raise self._error
        return self._result


class _FakeService:
    """Minimal stand-in for the Drive v3 client used by user_can_access."""

    def __init__(self, list_result=None, list_error=None, get_result=None):
        self._list_result, self._list_error = list_result, list_error
        self._get_result = get_result

    def permissions(self):
        return self

    def files(self):
        return self

    def list(self, **_kwargs):
        return _FakeExecutable(self._list_result, self._list_error)

    def get(self, **_kwargs):
        return _FakeExecutable(self._get_result)


@pytest.fixture
def patched_drive(monkeypatch):
    """Swap out credentials + client construction; hand back a setter."""
    import shared.utils.drive_permissions as dp

    monkeypatch.setattr(dp, "get_drive_credentials", lambda: object())
    holder = {}
    monkeypatch.setattr(dp, "build", lambda *a, **kw: holder["service"])
    return holder


@pytest.mark.asyncio
async def test_no_email_fails_closed(patched_drive):
    from shared.utils.drive_permissions import user_can_access

    patched_drive["service"] = _FakeService()
    assert await user_can_access("file-1", None) is False


@pytest.mark.asyncio
async def test_strict_withholds_the_service_account_reachability_grant(patched_drive):
    """The whole point: a link-shared doc must not be readable by everyone."""
    from shared.utils.drive_permissions import user_can_access

    patched_drive["service"] = _FakeService(
        list_error=RuntimeError("403"),
        get_result={"id": "file-1", "permissions": []},
    )
    assert await user_can_access("file-1", "tech@example.com", strict=True) is False


@pytest.mark.asyncio
async def test_non_strict_keeps_the_reachability_grant(patched_drive):
    """Existing callers (e.g. /learn's fetch_document) must not change behaviour."""
    from shared.utils.drive_permissions import user_can_access

    patched_drive["service"] = _FakeService(
        list_error=RuntimeError("403"),
        get_result={"id": "file-1", "permissions": []},
    )
    assert await user_can_access("file-1", "tech@example.com") is True


@pytest.mark.asyncio
async def test_strict_still_grants_on_an_explicit_share(patched_drive):
    from shared.utils.drive_permissions import user_can_access

    patched_drive["service"] = _FakeService(
        list_result={
            "permissions": [
                {"type": "user", "emailAddress": "tech@example.com", "role": "reader"}
            ]
        }
    )
    assert await user_can_access("file-1", "tech@example.com", strict=True) is True


@pytest.mark.asyncio
async def test_strict_still_grants_on_a_domain_share(patched_drive):
    from shared.utils.drive_permissions import user_can_access

    patched_drive["service"] = _FakeService(
        list_result={"permissions": [{"type": "domain", "domain": "example.com", "role": "reader"}]}
    )
    assert await user_can_access("file-1", "tech@example.com", strict=True) is True


@pytest.mark.asyncio
async def test_an_api_explosion_fails_closed(patched_drive):
    from shared.utils.drive_permissions import user_can_access

    def _boom(*_a, **_kw):
        raise RuntimeError("network down")

    import shared.utils.drive_permissions as dp

    patched_drive["service"] = _FakeService()
    dp.get_drive_credentials = _boom
    assert await user_can_access("file-1", "tech@example.com", strict=True) is False
```

- [ ] **Step 7: Run the full file**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_drive_permissions.py -q
```

Expected: 15 passed.

- [ ] **Step 8: Confirm no existing caller regressed**

```bash
cd chat_orchestrator && uv run pytest tests/ -q -k "fetch_document or ingestion or drive"
```

Expected: all pass. `/learn` calls `user_can_access` without `strict`, so its
behaviour is unchanged by design — `test_non_strict_keeps_the_reachability_grant`
is the guard for that.

- [ ] **Step 9: Commit**

```bash
git add shared/utils/drive_permissions.py
git add -f shared/tests/test_drive_permissions.py
git commit -m "feat(drive): add strict access mode and domain-share support

strict=True drops only the 'service account could reach the file' grant,
which would otherwise make any link-shared or Shared Drive document
readable by every caller. Domain permissions ('shared with everyone at
the company') are now honoured -- without them strict mode would
false-deny most staff."
```

---

## Phase 2 — Data model

### Task 2: Migration, schema, and module fields

**Files:**
- Create: `db/migrations/0018_doc_backed_modules.sql`
- Modify: `db/schema/chat_db.sql:1242-1265`
- Modify: `shared/prompts/knowledge.py`
- Test: `shared/tests/test_prompt_knowledge.py`, `shared/tests/test_prompt_knowledge_store.py`

- [ ] **Step 1: Write the failing tests**

Append to `shared/tests/test_prompt_knowledge.py`:

```python
def test_a_module_carries_its_audience_and_tab():
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(
        id="d", slug="specs", title="Specs", summary="s", body=None,
        source="gdoc", source_ref="abc123", source_tab="Thresholds",
        doc_audience="acl_mirror", doc_audience_set_by=None,
    )
    assert module.source_tab == "Thresholds"
    assert module.doc_audience == "acl_mirror"


def test_audience_defaults_to_none_for_a_typed_module():
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(id="m", slug="m", title="M", summary="s", body="text")
    assert module.doc_audience is None
    assert module.source_tab is None
```

Append to `shared/tests/test_prompt_knowledge_store.py`:

```python
def test_the_store_selects_the_audience_columns():
    """KnowledgeModule(**row) requires the select and the dataclass to match.

    Miss a column here and every doc module looks unaudienced, which the
    provider reads as 'not acl_mirror' -- i.e. it would fail *open*.
    """
    client = _client_returning([])
    KnowledgeStore(client=client).all_modules()

    assert "doc_audience" in client.table_obj.selected
    assert "doc_audience_set_by" in client.table_obj.selected
    assert "source_tab" in client.table_obj.selected
```

Read the existing `_client_returning` / `client.table_obj.selected` helpers at the
top of `test_prompt_knowledge_store.py` and reuse them exactly — do not invent new
fakes. The existing test `test_the_store_selects_source_columns` (around line 186)
is the template.

- [ ] **Step 2: Run to verify failure**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_prompt_knowledge.py ../shared/tests/test_prompt_knowledge_store.py -q
```

Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'source_tab'`.

- [ ] **Step 3: Add the dataclass fields**

In `shared/prompts/knowledge.py`, extend `KnowledgeModule` (keep the existing
field order; append the new ones so positional construction in existing tests
still works):

```python
    source: str = "manual"
    source_ref: Optional[str] = None
    source_tab: Optional[str] = None
    # Only meaningful for source='gdoc'. 'acl_mirror' resolves the body only
    # for a caller who can read the file in Drive; 'published' resolves for
    # everyone the prompt serves. None for every other source.
    doc_audience: Optional[str] = None
    doc_audience_set_by: Optional[str] = None
```

- [ ] **Step 4: Add the columns to the store's select**

In `shared/prompts/knowledge.py`, `all_modules()`:

```python
                .select(
                    "id, slug, title, summary, body, tags, scope, mode, source, "
                    "source_ref, source_tab, doc_audience, doc_audience_set_by"
                )
```

- [ ] **Step 5: Run to verify pass**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_prompt_knowledge.py ../shared/tests/test_prompt_knowledge_store.py -q
```

Expected: all pass.

- [ ] **Step 6: Write the migration**

Create `db/migrations/0018_doc_backed_modules.sql`:

```sql
-- 0018_doc_backed_modules.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- docs/superpowers/plans/2026-08-20-doc-backed-context-modules.md.
-- A gdoc module stores no body and carries an explicit audience decision.
-- Also renames the catch-all scope from 'sector' to 'global'.

BEGIN;

ALTER TABLE knowledge_modules ADD COLUMN IF NOT EXISTS source_tab          text;
ALTER TABLE knowledge_modules ADD COLUMN IF NOT EXISTS doc_audience        text;
ALTER TABLE knowledge_modules ADD COLUMN IF NOT EXISTS doc_audience_set_by text;

-- Safe default for anything that predates this migration. acl_mirror
-- tightens (fewer callers see it), never loosens.
UPDATE knowledge_modules
    SET doc_audience = 'acl_mirror'
    WHERE source = 'gdoc' AND doc_audience IS NULL;

-- Today's constraint exempts only graph/directory/episodic, forcing a stored
-- body on exactly the source that must not have one.
ALTER TABLE knowledge_modules
    DROP CONSTRAINT IF EXISTS knowledge_modules_body_required_chk;
ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_body_required_chk
        CHECK (source IN ('gdoc', 'graph', 'directory', 'episodic') OR body IS NOT NULL);

ALTER TABLE knowledge_modules
    DROP CONSTRAINT IF EXISTS knowledge_modules_doc_audience_chk;
ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_doc_audience_chk
        CHECK ((source = 'gdoc' AND doc_audience IN ('acl_mirror', 'published'))
               OR (source <> 'gdoc' AND doc_audience IS NULL));

ALTER TABLE knowledge_modules
    DROP CONSTRAINT IF EXISTS knowledge_modules_gdoc_ref_chk;
ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_gdoc_ref_chk
        CHECK (source <> 'gdoc' OR source_ref IS NOT NULL);

-- 'sector' -> 'global'. RequestScope.matches() accepts both permanently, so
-- a row missed here still resolves rather than going silently dark.
UPDATE knowledge_modules SET scope = 'global' WHERE scope = 'sector';
ALTER TABLE knowledge_modules ALTER COLUMN scope SET DEFAULT 'global';

COMMIT;

-- Report what changed. Any pre-existing gdoc row tightens from "everyone"
-- to "ACL-gated" -- a real behaviour change. No code path can currently
-- create one, so the expected count is 0.
SELECT source, doc_audience, count(*)
    FROM knowledge_modules
    GROUP BY source, doc_audience
    ORDER BY source;
```

- [ ] **Step 7: Mirror it into the schema of record**

In `db/schema/chat_db.sql`, update the `knowledge_modules` block (lines 1242-1265)
so a fresh database matches a migrated one:

```sql
CREATE TABLE IF NOT EXISTS knowledge_modules (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                text NOT NULL UNIQUE,
    title               text NOT NULL,
    summary             text NOT NULL,
    body                text,
    tags                text[] NOT NULL DEFAULT '{}',
    scope               text NOT NULL DEFAULT 'global',
    mode                text NOT NULL DEFAULT 'pinned',
    source              text NOT NULL DEFAULT 'manual',
    source_ref          text,
    -- Sheet tab name. NULL means the first tab (or a Doc, which has none).
    source_tab          text,
    -- gdoc only. 'acl_mirror' = resolve only for a caller who can read the
    -- file in Drive. 'published' = resolve for everyone the prompt serves.
    doc_audience        text,
    -- Who chose 'published'. Separate from updated_by, which any later title
    -- edit clobbers.
    doc_audience_set_by text,
    edit_groups         text[] NOT NULL DEFAULT '{}',
    version             integer NOT NULL DEFAULT 1,
    is_active           boolean NOT NULL DEFAULT true,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          text,
    CONSTRAINT knowledge_modules_mode_chk CHECK (mode IN ('pinned', 'on_demand')),
    CONSTRAINT knowledge_modules_source_chk
        CHECK (source IN ('manual', 'gdoc', 'ingested', 'graph', 'directory', 'episodic')),
    -- A gdoc or provider-backed module stores no body -- it resolves at
    -- request time. See 0018_doc_backed_modules.sql.
    CONSTRAINT knowledge_modules_body_required_chk
        CHECK (source IN ('gdoc', 'graph', 'directory', 'episodic') OR body IS NOT NULL),
    CONSTRAINT knowledge_modules_doc_audience_chk
        CHECK ((source = 'gdoc' AND doc_audience IN ('acl_mirror', 'published'))
               OR (source <> 'gdoc' AND doc_audience IS NULL)),
    CONSTRAINT knowledge_modules_gdoc_ref_chk
        CHECK (source <> 'gdoc' OR source_ref IS NOT NULL)
);
```

- [ ] **Step 8: Run the shared suite**

```bash
cd chat_orchestrator && uv run pytest ../shared -q
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add shared/prompts/knowledge.py db/migrations/0018_doc_backed_modules.sql db/schema/chat_db.sql shared/tests/test_prompt_knowledge.py shared/tests/test_prompt_knowledge_store.py
git commit -m "feat(context): add audience and tab columns for doc-backed modules

A gdoc module stores no body and carries an explicit audience decision.
The old body-required constraint exempted only graph/directory/episodic,
forcing a stored body on exactly the source that must not have one."
```

> **Deploy note:** merging this does **not** apply the migration to production
> `chat_db`. It must be run by hand in the Supabase SQL editor. Nothing in this
> plan works in production until it is.

---

## Phase 3 — Sheets as markdown

### Task 3: `fetch_google_sheet_markdown`

The existing markdown path uses the **Docs API** (`documents().get()`), which is
Docs-only. Sheets need the Sheets API — already wired via `get_sheets_credentials()`
and used in four places, so no new credential plumbing.

**Files:**
- Modify: `shared/utils/gdrive_doc_fetcher.py`
- Test: `shared/tests/test_sheet_markdown.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `shared/tests/test_sheet_markdown.py`:

```python
"""Sheet values -> markdown table."""

from shared.utils.gdrive_doc_fetcher import rows_to_markdown_table


def test_a_simple_sheet_becomes_a_markdown_table():
    table = rows_to_markdown_table([["Code", "Meaning"], ["E01", "Undervoltage"]])

    assert table == (
        "| Code | Meaning |\n"
        "| --- | --- |\n"
        "| E01 | Undervoltage |"
    )


def test_ragged_rows_are_padded_to_the_header_width():
    """Sheets omits trailing empty cells; an unpadded row renders as garbage."""
    table = rows_to_markdown_table([["A", "B", "C"], ["1"]])

    assert table.splitlines()[-1] == "| 1 |  |  |"


def test_cells_wider_than_the_header_are_dropped():
    table = rows_to_markdown_table([["A"], ["1", "2", "3"]])

    assert table.splitlines()[-1] == "| 1 |"


def test_pipes_in_a_cell_are_escaped():
    table = rows_to_markdown_table([["A"], ["x|y"]])

    assert "x\\|y" in table


def test_newlines_in_a_cell_become_spaces():
    table = rows_to_markdown_table([["A"], ["line1\nline2"]])

    assert "| line1 line2 |" in table


def test_fully_blank_rows_are_dropped():
    table = rows_to_markdown_table([["A"], ["", "  "], ["1"]])

    assert table.splitlines() == ["| A |", "| --- |", "| 1 |"]


def test_an_empty_sheet_returns_empty_string():
    assert rows_to_markdown_table([]) == ""
    assert rows_to_markdown_table([[""]]) == ""


def test_a_row_cap_truncates_and_says_so():
    rows = [["N"]] + [[str(i)] for i in range(10)]

    table = rows_to_markdown_table(rows, max_rows=3)

    assert "_(truncated: showing first 3 of 10 rows)_" in table
    assert "| 3 |" not in table


def test_an_untruncated_table_has_no_footer():
    table = rows_to_markdown_table([["N"], ["1"]], max_rows=50)

    assert "truncated" not in table


def test_a_char_cap_drops_whole_rows_never_partial_ones():
    rows = [["N"]] + [[f"value-{i:03d}"] for i in range(100)]

    table = rows_to_markdown_table(rows, max_rows=100, max_chars=200)

    body = [ln for ln in table.splitlines() if ln.startswith("| value-")]
    assert body, "expected at least one data row to survive"
    assert all(ln.endswith(" |") for ln in body)
    assert "truncated" in table
```

- [ ] **Step 2: Run to verify failure**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_sheet_markdown.py -q
```

Expected: `ImportError: cannot import name 'rows_to_markdown_table'`.

- [ ] **Step 3: Implement the conversion**

Append to `shared/utils/gdrive_doc_fetcher.py`, after `parse_sections`:

```python
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
DOCUMENT_MIME = "application/vnd.google-apps.document"

SHEET_MAX_ROWS = 200
SHEET_MAX_CHARS = 20000


def _sheet_cell(value) -> str:
    """A pipe or newline would break the table row the cell sits in."""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def rows_to_markdown_table(
    rows: list,
    max_rows: int = SHEET_MAX_ROWS,
    max_chars: int = SHEET_MAX_CHARS,
) -> str:
    """Sheet values as a markdown table, capped by rows and characters.

    The first non-blank row is the header and fixes the column count. The
    Sheets API omits trailing empty cells, so data rows are ragged and get
    padded; over-wide rows are cut to the header width. Truncation drops
    whole rows and says so in a footer -- a silently capped table reads as
    the complete table to both an operator and a model.
    """
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return ""

    header = [_sheet_cell(c) for c in rows[0]]
    width = len(header)
    if width == 0:
        return ""

    total = len(rows) - 1
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in rows[1 : max_rows + 1]:
        cells = [_sheet_cell(c) for c in list(row)[:width]]
        cells += [""] * (width - len(cells))
        lines.append("| " + " | ".join(cells) + " |")

    # Drop whole rows until the char cap is met. Never cut mid-row: a partial
    # row is invalid markdown and renders as literal pipes.
    while len("\n".join(lines)) > max_chars and len(lines) > 3:
        lines.pop()

    shown = len(lines) - 2
    table = "\n".join(lines)
    if shown < total:
        table += f"\n\n_(truncated: showing first {shown:,} of {total:,} rows)_"
    return table


def fetch_google_sheet_markdown(file_id: str, tab: Optional[str] = None) -> Optional[str]:
    """Export one sheet tab as a markdown table.

    Uses the Sheets API rather than Drive's CSV export, which can only ever
    return the first tab. ``tab=None`` means the first tab.
    """
    from googleapiclient.discovery import build

    from shared.utils.google_auth import get_sheets_credentials

    service = build("sheets", "v4", credentials=get_sheets_credentials())
    if not tab:
        meta = service.spreadsheets().get(spreadsheetId=file_id).execute()
        sheets = meta.get("sheets") or []
        if not sheets:
            return None
        tab = sheets[0]["properties"]["title"]

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=file_id, range=tab)
        .execute()
    )
    return rows_to_markdown_table(result.get("values") or []) or None
```

- [ ] **Step 4: Run to verify pass**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_sheet_markdown.py -q
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/utils/gdrive_doc_fetcher.py
git add -f shared/tests/test_sheet_markdown.py
git commit -m "feat(drive): render a Sheet tab as a markdown table

Uses the Sheets API rather than Drive's CSV export, which can only ever
return the first tab. Truncation drops whole rows and announces itself --
a silently capped table reads as the complete table."
```

---

## Phase 4 — The provider

### Task 4: `GDocProvider` becomes async and access-gated

**Files:**
- Modify: `shared/prompts/providers_gdoc.py`
- Test: `shared/tests/test_gdoc_provider.py`

Keep the existing sync `body_for` for now — `knowledge_mcp_server.py` still calls
it until Task 8, which deletes it.

- [ ] **Step 1: Write the failing tests**

Replace the contents of `shared/tests/test_gdoc_provider.py` with:

```python
"""Google Doc/Sheet-backed context modules and their access gate."""

import pytest

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.prompts.providers_gdoc import GDocProvider
from shared.prompts.types import RequestScope

DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _module(source_ref="doc-abc", audience="acl_mirror", tab=None):
    return KnowledgeModule(
        id="d", slug="procedures", title="Procedures", summary="How-tos.",
        body=None, source="gdoc", source_ref=source_ref, source_tab=tab,
        doc_audience=audience,
    )


def _ctx(email="tech@example.com"):
    return ResolutionContext(scope=RequestScope(), user_email=email)


def _provider(allowed=True, doc_text="doc body", sheet_text="| A |", mime=DOC_MIME, **kw):
    async def _can_access(_file_id, _email, strict=False):
        return allowed

    return GDocProvider(
        fetch=lambda _id: doc_text,
        fetch_sheet=lambda _id, _tab: sheet_text,
        mime_for=lambda _id: mime,
        can_access=_can_access,
        **kw,
    )


@pytest.mark.asyncio
async def test_an_allowed_caller_gets_the_doc_body():
    assert await _provider().resolve(_module(), _ctx()) == "doc body"


@pytest.mark.asyncio
async def test_a_denied_caller_gets_nothing():
    assert await _provider(allowed=False).resolve(_module(), _ctx()) is None


@pytest.mark.asyncio
async def test_a_published_module_skips_the_access_check_entirely():
    """The deliberate opt-out: curated doc content meant for customers."""
    checked = []

    async def _can_access(file_id, _email, strict=False):
        checked.append(file_id)
        return False

    provider = GDocProvider(
        fetch=lambda _id: "public body",
        mime_for=lambda _id: DOC_MIME,
        can_access=_can_access,
    )
    result = await provider.resolve(_module(audience="published"), _ctx())

    assert result == "public body"
    assert checked == []


@pytest.mark.asyncio
async def test_a_caller_with_no_email_is_denied():
    assert await _provider().resolve(_module(), _ctx(email=None)) is None


@pytest.mark.asyncio
async def test_a_module_without_a_source_ref_resolves_to_none():
    assert await _provider().resolve(_module(source_ref=None), _ctx()) is None


@pytest.mark.asyncio
async def test_a_sheet_module_uses_the_sheet_fetcher():
    provider = _provider(mime=SHEET_MIME, sheet_text="| Code |\n| --- |\n| E01 |")
    assert "E01" in await provider.resolve(_module(tab="Errors"), _ctx())


@pytest.mark.asyncio
async def test_the_tab_is_passed_through_to_the_sheet_fetcher():
    seen = {}

    def _fetch_sheet(file_id, tab):
        seen["tab"] = tab
        return "| A |"

    async def _ok(*_a, **_kw):
        return True

    provider = GDocProvider(
        fetch_sheet=_fetch_sheet, mime_for=lambda _id: SHEET_MIME, can_access=_ok
    )
    await provider.resolve(_module(tab="Thresholds"), _ctx())

    assert seen["tab"] == "Thresholds"


@pytest.mark.asyncio
async def test_a_failing_fetch_resolves_to_none():
    def _boom(_id):
        raise RuntimeError("403")

    async def _ok(*_a, **_kw):
        return True

    provider = GDocProvider(fetch=_boom, mime_for=lambda _id: DOC_MIME, can_access=_ok)
    assert await provider.resolve(_module(), _ctx()) is None


@pytest.mark.asyncio
async def test_a_failing_access_check_fails_closed():
    async def _boom(*_a, **_kw):
        raise RuntimeError("drive down")

    provider = GDocProvider(
        fetch=lambda _id: "body", mime_for=lambda _id: DOC_MIME, can_access=_boom
    )
    assert await provider.resolve(_module(), _ctx()) is None


@pytest.mark.asyncio
async def test_an_empty_doc_resolves_to_none():
    assert await _provider(doc_text="   ").resolve(_module(), _ctx()) is None


@pytest.mark.asyncio
async def test_content_is_cached_per_file_and_tab():
    calls = []

    def _fetch(file_id):
        calls.append(file_id)
        return "body"

    async def _ok(*_a, **_kw):
        return True

    provider = GDocProvider(fetch=_fetch, mime_for=lambda _id: DOC_MIME, can_access=_ok)
    await provider.resolve(_module(), _ctx())
    await provider.resolve(_module(), _ctx())

    assert calls == ["doc-abc"]


@pytest.mark.asyncio
async def test_access_is_cached_per_file_and_caller():
    calls = []

    async def _can_access(file_id, email, strict=False):
        calls.append((file_id, email))
        return True

    provider = GDocProvider(
        fetch=lambda _id: "body", mime_for=lambda _id: DOC_MIME, can_access=_can_access
    )
    await provider.resolve(_module(), _ctx("a@example.com"))
    await provider.resolve(_module(), _ctx("a@example.com"))
    await provider.resolve(_module(), _ctx("b@example.com"))

    assert calls == [("doc-abc", "a@example.com"), ("doc-abc", "b@example.com")]


@pytest.mark.asyncio
async def test_the_access_check_is_always_strict():
    """Non-strict would grant every caller read on any link-shared file."""
    seen = {}

    async def _can_access(_file_id, _email, strict=False):
        seen["strict"] = strict
        return True

    provider = GDocProvider(
        fetch=lambda _id: "body", mime_for=lambda _id: DOC_MIME, can_access=_can_access
    )
    await provider.resolve(_module(), _ctx())

    assert seen["strict"] is True


@pytest.mark.asyncio
async def test_visible_to_does_not_fetch_content():
    """It gates the on-demand catalog line, where the body is not wanted."""
    fetched = []

    async def _ok(*_a, **_kw):
        return True

    provider = GDocProvider(
        fetch=lambda i: fetched.append(i), mime_for=lambda _id: DOC_MIME, can_access=_ok
    )
    assert await provider.visible_to(_module(), _ctx()) is True
    assert fetched == []


@pytest.mark.asyncio
async def test_invalidate_clears_both_caches():
    calls = []

    async def _can_access(file_id, email, strict=False):
        calls.append("access")
        return True

    def _fetch(file_id):
        calls.append("fetch")
        return "body"

    provider = GDocProvider(
        fetch=_fetch, mime_for=lambda _id: DOC_MIME, can_access=_can_access
    )
    await provider.resolve(_module(), _ctx())
    provider.invalidate()
    await provider.resolve(_module(), _ctx())

    assert calls == ["access", "fetch", "access", "fetch"]
```

- [ ] **Step 2: Run to verify failure**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_gdoc_provider.py -q
```

Expected: FAIL — `GDocProvider.__init__() got an unexpected keyword argument 'fetch_sheet'`.

- [ ] **Step 3: Rewrite the provider**

Replace the body of `shared/prompts/providers_gdoc.py` (keep the module docstring
header but update it):

```python
"""Google Drive-backed context module bodies.

Async and permission-gated: a module whose ``doc_audience`` is 'acl_mirror'
resolves only for a caller who can read the underlying file in Drive. This is
the reason `gdoc` sits on the JitContextResolver path rather than inside
PromptLibrary's synchronous render -- only the async path carries the caller's
identity (see shared/prompts/providers.py's ResolutionContext).

Every failure mode resolves to None, which the resolver treats as "this module
contributes nothing" rather than an error. That includes a denied access check,
a missing source_ref, a Drive outage and an empty document -- all fail closed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Optional, Tuple

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DEFAULT_TTL_SECONDS = 300
# Shorter than the content TTL on purpose: this is the revocation window.
# Someone who loses Drive access keeps resolving the body for up to this long.
DEFAULT_ACCESS_TTL_SECONDS = 60

SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"


class GDocProvider:
    """Resolves the `gdoc` source by Drive file id, gated on the caller."""

    source = "gdoc"

    def __init__(
        self,
        fetch: Optional[Callable[[str], Optional[str]]] = None,
        fetch_sheet: Optional[Callable[[str, Optional[str]], Optional[str]]] = None,
        mime_for: Optional[Callable[[str], Optional[str]]] = None,
        can_access: Optional[Callable[..., Any]] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        access_ttl_seconds: int = DEFAULT_ACCESS_TTL_SECONDS,
    ) -> None:
        self._fetch = fetch or _default_fetch
        self._fetch_sheet = fetch_sheet or _default_fetch_sheet
        self._mime_for = mime_for or _default_mime_for
        self._can_access = can_access or _default_can_access
        self._ttl = ttl_seconds
        self._access_ttl = access_ttl_seconds
        self._cache: Dict[Tuple[str, Optional[str]], Tuple[float, Optional[str]]] = {}
        self._access: Dict[Tuple[str, str], Tuple[float, bool]] = {}

    def invalidate(self) -> None:
        self._cache.clear()
        self._access.clear()

    async def visible_to(self, module: KnowledgeModule, ctx: ResolutionContext) -> bool:
        """Whether this caller may see the module at all. Never raises.

        Separate from resolve() so the on-demand catalog can be filtered
        without fetching any content: a denied module's summary must not
        appear, and the model must not spend a turn fetching what it will
        be refused.
        """
        if module.doc_audience == "published":
            return True

        file_id = module.source_ref
        if not file_id or not ctx.user_email:
            return False

        key = (file_id, ctx.user_email)
        hit = self._access.get(key)
        if hit and hit[0] > time.time():
            return hit[1]

        try:
            allowed = await self._can_access(file_id, ctx.user_email, strict=True)
        except Exception:
            LOGGER.warning(
                f"Drive access check failed for module '{module.slug}'; withholding",
                exc_info=True,
            )
            return False

        self._access[key] = (time.time() + self._access_ttl, bool(allowed))
        return bool(allowed)

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]:
        """The document's text for this caller, or None. Never raises."""
        if not module.source_ref:
            LOGGER.warning(f"Module '{module.slug}' is gdoc-sourced but has no source_ref")
            return None

        if not await self.visible_to(module, ctx):
            LOGGER.info(
                f"Module '{module.slug}' withheld from {ctx.user_email}: no Drive access"
            )
            return None

        return await self._body(module.source_ref, module.source_tab, module.slug)

    async def _body(
        self, file_id: str, tab: Optional[str], slug: str
    ) -> Optional[str]:
        key = (file_id, tab)
        hit = self._cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]

        try:
            # One mime lookup per cache miss, not per request -- the type of a
            # file is stable, and this sits behind the content cache.
            mime = await asyncio.to_thread(self._mime_for, file_id)
            if mime == SPREADSHEET_MIME:
                body = await asyncio.to_thread(self._fetch_sheet, file_id, tab)
            else:
                body = await asyncio.to_thread(self._fetch, file_id)
        except Exception:
            LOGGER.warning(f"Drive fetch failed for module '{slug}'", exc_info=True)
            return None

        body = body.strip() if body else None
        self._cache[key] = (time.time() + self._ttl, body or None)
        return body or None


def _default_fetch(file_id: str) -> Optional[str]:
    from shared.prompts.gdoc import fetch_doc_text

    return fetch_doc_text(file_id)


def _default_fetch_sheet(file_id: str, tab: Optional[str]) -> Optional[str]:
    from shared.utils.gdrive_doc_fetcher import fetch_google_sheet_markdown

    return fetch_google_sheet_markdown(file_id, tab)


def _default_mime_for(file_id: str) -> Optional[str]:
    from shared.utils.gdrive_doc_fetcher import GoogleDriveDocFetcher

    meta = GoogleDriveDocFetcher().get_file_metadata(file_id) or {}
    return meta.get("mimeType")


async def _default_can_access(file_id: str, user_email: str, strict: bool = True) -> bool:
    from shared.utils.drive_permissions import user_can_access

    return await user_can_access(file_id, user_email, strict=strict)


__all__ = ["GDocProvider"]
```

Note this **deletes** the old sync `body_for`. `knowledge_mcp_server.py` still
calls it and will break until Task 8 — that is expected and is why Task 8 follows
immediately.

- [ ] **Step 4: Run to verify pass**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_gdoc_provider.py -q
```

Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/prompts/providers_gdoc.py shared/tests/test_gdoc_provider.py
git commit -m "feat(context): gate doc-backed module bodies on the caller's Drive access

GDocProvider becomes an async ContextProvider. An acl_mirror module resolves
only for a caller who can read the file; published skips the check by explicit
operator decision. Access is cached separately from content -- that TTL is the
revocation window. Adds Sheet support via mime dispatch."
```

### Task 5: Move `gdoc` onto the JIT path

**Files:**
- Modify: `shared/prompts/knowledge.py:24-28`
- Modify: `shared/prompts/core.py` (constructor, `_with_resolved_body`, `_compose_knowledge`, `_build_default_library`)
- Modify: `chat_orchestrator/orchestrator/services/jit_context_resolver.py:120-142`
- Test: `shared/tests/test_prompt_knowledge_wiring.py`

- [ ] **Step 1: Update the wiring tests**

In `shared/tests/test_prompt_knowledge_wiring.py`, the two gdoc tests (around
lines 148 and 175) assert that `PromptLibrary` resolves a gdoc body synchronously.
That is exactly the behaviour being removed. Replace both with:

```python
def test_a_gdoc_module_is_left_to_the_jit_resolver():
    """gdoc is JIT now: PromptLibrary must not try to resolve it inline.

    It has no caller identity (RequestScope carries grid and organization
    only), so resolving here would mean serving document content with no
    access check at all.
    """
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(
        id="d", slug="doc-module", title="Doc", summary="From a doc.",
        body=None, mode="pinned", source="gdoc", source_ref="doc-1",
        doc_audience="acl_mirror",
    )
    assert module.is_jit is True
```

Then find every other reference to `gdoc_module_provider` in that file and delete
those cases — the constructor argument no longer exists.

```bash
cd chat_orchestrator && uv run grep -rn "gdoc_module_provider\|_with_resolved_body" ../shared ../chat_orchestrator ../anansi_app ../mcp_servers
```

Every hit must be resolved by the end of this task.

- [ ] **Step 2: Run to verify failure**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_prompt_knowledge_wiring.py -q
```

Expected: FAIL — `module.is_jit` is `False` while `gdoc` is not in `JIT_SOURCES`.

- [ ] **Step 3: Add `gdoc` to `JIT_SOURCES`**

In `shared/prompts/knowledge.py`, replace the `JIT_SOURCES` block:

```python
# Sources whose body is produced per-request rather than stored. All of them
# need the caller's identity: graph/directory/episodic filter database rows by
# permission, and gdoc checks the caller against the document's Drive ACL.
# PromptLibrary.render() is synchronous and carries no identity, so these
# resolve through JitContextResolver instead.
JIT_SOURCES: Tuple[str, ...] = ("gdoc", "graph", "directory", "episodic")
```

- [ ] **Step 4: Delete the synchronous gdoc path**

In `shared/prompts/core.py`:

1. Remove `gdoc_module_provider: Optional[Any] = None,` from `__init__`'s signature.
2. Remove `self._gdoc_modules = gdoc_module_provider`.
3. Delete the whole `_with_resolved_body` method (around line 173).
4. In `_compose_knowledge`, delete the resolution line and update the comment:

```python
        chosen = select_for_prompt(modules, pins, scope)
        # JIT sources (gdoc/graph/directory/episodic) all need the caller's
        # identity, which render() does not carry. JitContextResolver handles
        # them and appends its output to context_message instead.
        chosen = [m for m in chosen if not m.is_jit]
        chosen = [m for m in chosen if m.body]
```

5. In `_build_default_library`, remove the `GDocProvider` import and the
   `gdoc_module_provider=GDocProvider(),` argument.
6. If `dataclasses` is now unused at module level, remove the import.

- [ ] **Step 5: Register the provider with the JIT registry**

In `chat_orchestrator/orchestrator/services/jit_context_resolver.py`, add to
`build_default_registry()` before the `DirectoryProvider` block:

```python
    try:
        from shared.prompts.providers_gdoc import GDocProvider

        registry.register(GDocProvider())
    except Exception:
        LOGGER.warning("GDocProvider unavailable", exc_info=True)
```

- [ ] **Step 6: Run the suites**

```bash
cd chat_orchestrator && uv run pytest ../shared -q && uv run pytest tests/ -q
```

Expected: all pass. If `test_prompt_parity.py` or a "bundled" test fails, check
`chat_orchestrator/.env` for real credentials before suspecting this change —
see `CLAUDE.md`, "A local `.env` with real credentials makes some tests silently
non-hermetic".

- [ ] **Step 7: Commit**

```bash
git add shared/prompts/knowledge.py shared/prompts/core.py chat_orchestrator/orchestrator/services/jit_context_resolver.py shared/tests/test_prompt_knowledge_wiring.py
git commit -m "refactor(context): resolve gdoc modules on the JIT path

PromptLibrary.render() carries no caller identity, so resolving a document
there meant serving its content with no access check. JitContextResolver
already carries user_email; gdoc now resolves there like every other
per-request source."
```

---

## Phase 5 — Close the three gates

### Task 6: Budget the JIT block

Moving `gdoc` to JIT silently removed its size cap. `_with_resolved_body` used to
run *before* `budget_pinned`, so a resolved document counted against the 20,000
character budget. `resolve_for_prompt` joins resolved bodies with no cap at all.

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/jit_context_resolver.py`
- Test: `chat_orchestrator/tests/test_jit_context_resolver.py`

- [ ] **Step 1: Write the failing tests**

Append to `chat_orchestrator/tests/test_jit_context_resolver.py`:

```python
def test_budget_resolved_keeps_everything_that_fits():
    from orchestrator.services.jit_context_resolver import budget_resolved
    from shared.prompts.knowledge import KnowledgeModule

    a = KnowledgeModule(id="1", slug="a", title="A", summary="s", source="gdoc")
    b = KnowledgeModule(id="2", slug="b", title="B", summary="s", source="gdoc")

    kept = budget_resolved([(a, "x" * 10), (b, "y" * 10)], limit=100)

    assert [m.slug for m, _ in kept] == ["a", "b"]


def test_budget_resolved_drops_whole_modules_not_fragments():
    from orchestrator.services.jit_context_resolver import budget_resolved
    from shared.prompts.knowledge import KnowledgeModule

    a = KnowledgeModule(id="1", slug="a", title="A", summary="s", source="gdoc")
    b = KnowledgeModule(id="2", slug="b", title="B", summary="s", source="gdoc")

    kept = budget_resolved([(a, "x" * 60), (b, "y" * 60)], limit=100)

    assert len(kept) == 1
    assert len(kept[0][1]) == 60


def test_budget_resolved_keeps_site_scoped_material_first():
    """Most specific, least replaceable -- same rule as budget_pinned."""
    from orchestrator.services.jit_context_resolver import budget_resolved
    from shared.prompts.knowledge import KnowledgeModule

    general = KnowledgeModule(id="1", slug="a", title="A", summary="s", source="gdoc")
    site = KnowledgeModule(
        id="2", slug="z", title="Z", summary="s", source="gdoc", scope="site:ABC"
    )

    kept = budget_resolved([(general, "x" * 60), (site, "y" * 60)], limit=100)

    assert [m.slug for m, _ in kept] == ["z"]


def test_budget_resolved_on_an_empty_list_is_empty():
    from orchestrator.services.jit_context_resolver import budget_resolved

    assert budget_resolved([], limit=100) == []
```

- [ ] **Step 2: Run to verify failure**

```bash
cd chat_orchestrator && uv run pytest tests/test_jit_context_resolver.py -q -k budget
```

Expected: `ImportError: cannot import name 'budget_resolved'`.

- [ ] **Step 3: Implement the budget**

In `chat_orchestrator/orchestrator/services/jit_context_resolver.py`, after
`DEFAULT_TIMEOUT_SECONDS`:

```python
# Matches PromptLibrary's PINNED_BUDGET_CHARS. budget_pinned never sees these
# bodies -- a provider body has no length until it resolves, which happens
# here -- so without this one large document uncaps every prompt it is pinned
# to.
JIT_BUDGET_CHARS = 20000


def budget_resolved(resolved, limit: int = JIT_BUDGET_CHARS):
    """Fit resolved bodies into the budget by dropping whole modules.

    Site-scoped material is kept first: most specific, least replaceable.
    Mirrors shared.prompts.knowledge.budget_pinned, including never cutting
    a document in half.
    """
    kept, dropped, used = [], [], 0
    for module, text in sorted(
        resolved, key=lambda pair: (not pair[0].is_site_scoped, pair[0].slug)
    ):
        if used + len(text) <= limit:
            kept.append((module, text))
            used += len(text)
        else:
            dropped.append(module)
    if dropped:
        LOGGER.warning(
            f"Live context exceeded the {limit}-char budget; dropped "
            f"{len(dropped)} module(s): {', '.join(m.slug for m in dropped)}"
        )
    return kept
```

- [ ] **Step 4: Apply it in `resolve_for_prompt`**

Change the resolution line:

```python
        resolved = budget_resolved(await self._resolve_all(pinned, ctx))
```

- [ ] **Step 5: Run to verify pass**

```bash
cd chat_orchestrator && uv run pytest tests/test_jit_context_resolver.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/services/jit_context_resolver.py chat_orchestrator/tests/test_jit_context_resolver.py
git commit -m "fix(context): budget the live-context block

budget_pinned never sees a provider body -- it has no length until it
resolves. Without a cap here, one large document uncaps every prompt it
is pinned to."
```

### Task 7: Gate the on-demand catalog line

An on-demand module contributes `- slug — summary` without ever being resolved.
The summary can itself be sensitive, and the model will spend a turn fetching
something it will be refused.

**Files:**
- Modify: `shared/prompts/providers.py`
- Modify: `chat_orchestrator/orchestrator/services/jit_context_resolver.py`
- Test: `chat_orchestrator/tests/test_jit_context_resolver.py`

- [ ] **Step 1: Write the failing tests**

Append to `chat_orchestrator/tests/test_jit_context_resolver.py`:

```python
import pytest


class _Store:
    def __init__(self, modules, pins):
        self._modules, self._pins = modules, pins

    def all_modules(self):
        return self._modules

    def overrides_for(self, _prompt_id):
        return self._pins


class _GatedProvider:
    source = "gdoc"

    def __init__(self, visible):
        self._visible = visible

    async def visible_to(self, _module, _ctx):
        return self._visible

    async def resolve(self, _module, _ctx):
        return "body"


def _on_demand_module(slug="secret-doc"):
    from shared.prompts.knowledge import KnowledgeModule

    # No explicit scope: this task runs before the sector -> global rename in
    # Task 9, and the dataclass default matches under both spellings.
    return KnowledgeModule(
        id="1", slug=slug, title="T", summary="A sensitive summary.",
        body=None, mode="on_demand", source="gdoc", source_ref="doc-1",
        doc_audience="acl_mirror",
    )


def _resolver(provider, module):
    from orchestrator.services.jit_context_resolver import JitContextResolver
    from shared.prompts.providers import ProviderRegistry

    registry = ProviderRegistry()
    registry.register(provider)
    return JitContextResolver(
        store=_Store([module], {module.slug: True}), registry=registry
    )


@pytest.mark.asyncio
async def test_a_denied_on_demand_module_is_absent_from_the_catalog():
    """Its summary must not leak, and the model must not try to fetch it."""
    from shared.prompts.providers import ResolutionContext
    from shared.prompts.types import RequestScope

    module = _on_demand_module()
    resolver = _resolver(_GatedProvider(visible=False), module)

    text, used = await resolver.resolve_for_prompt(
        "customer.system", ResolutionContext(scope=RequestScope(), user_email="a@b.com")
    )

    assert "secret-doc" not in text
    assert "A sensitive summary." not in text
    assert used == []


@pytest.mark.asyncio
async def test_an_allowed_on_demand_module_still_appears():
    from shared.prompts.providers import ResolutionContext
    from shared.prompts.types import RequestScope

    module = _on_demand_module()
    resolver = _resolver(_GatedProvider(visible=True), module)

    text, used = await resolver.resolve_for_prompt(
        "customer.system", ResolutionContext(scope=RequestScope(), user_email="a@b.com")
    )

    assert "secret-doc" in text
    assert used == ["secret-doc"]


@pytest.mark.asyncio
async def test_a_provider_without_visible_to_is_not_filtered():
    """graph/directory/episodic do their own filtering inside resolve()."""
    from shared.prompts.providers import ResolutionContext
    from shared.prompts.types import RequestScope

    class _Plain:
        source = "gdoc"

        async def resolve(self, _module, _ctx):
            return "body"

    module = _on_demand_module()
    resolver = _resolver(_Plain(), module)

    text, _used = await resolver.resolve_for_prompt(
        "customer.system", ResolutionContext(scope=RequestScope(), user_email="a@b.com")
    )

    assert "secret-doc" in text


@pytest.mark.asyncio
async def test_a_visibility_check_that_raises_fails_closed():
    from shared.prompts.providers import ResolutionContext
    from shared.prompts.types import RequestScope

    class _Boom:
        source = "gdoc"

        async def visible_to(self, _module, _ctx):
            raise RuntimeError("drive down")

        async def resolve(self, _module, _ctx):
            return "body"

    module = _on_demand_module()
    resolver = _resolver(_Boom(), module)

    text, used = await resolver.resolve_for_prompt(
        "customer.system", ResolutionContext(scope=RequestScope(), user_email="a@b.com")
    )

    assert "secret-doc" not in text
    assert used == []
```

- [ ] **Step 2: Run to verify failure**

```bash
cd chat_orchestrator && uv run pytest tests/test_jit_context_resolver.py -q -k "catalog or on_demand or visibility"
```

Expected: FAIL — the denied module still appears in the catalog text.

- [ ] **Step 3: Document `visible_to` in the protocol**

In `shared/prompts/providers.py`, extend the `ContextProvider` docstring and add
the optional method:

```python
@runtime_checkable
class ContextProvider(Protocol):
    """Produces a module's body at render time.

    Returning None means "nothing to contribute" and is normal, not an
    error -- an episodic module for a grid with no distillation yet, for
    instance. Raising is also survivable: the resolver catches it.

    ``visible_to`` is optional. A provider that defines it is asked before
    its module's on-demand catalog line is rendered, so a caller never sees
    the name or summary of something they could not fetch. Providers that
    filter inside resolve() (graph, directory, episodic all filter database
    rows by permission) simply omit it; the resolver duck-types the call.
    """

    source: str

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]: ...
```

- [ ] **Step 4: Filter the catalog in the resolver**

In `jit_context_resolver.py`, change the `on_demand` line inside
`resolve_for_prompt`:

```python
        on_demand = await self._visible_only([m for m in chosen if m.mode != "pinned"], ctx)
```

and add the method after `_resolve_all`:

```python
    async def _visible_only(
        self, modules: List[KnowledgeModule], ctx: ResolutionContext
    ) -> List[KnowledgeModule]:
        """Drop on-demand modules this caller may not fetch.

        A catalog line carries the module's summary, which can itself be
        sensitive -- and listing something the caller will be refused wastes
        a model turn. Providers without a visible_to (graph, directory,
        episodic) filter inside resolve() instead and pass through here.
        """
        out: List[KnowledgeModule] = []
        for module in modules:
            provider = self._registry.get(module.source)
            check = getattr(provider, "visible_to", None) if provider else None
            if check is None:
                out.append(module)
                continue
            try:
                if await asyncio.wait_for(check(module, ctx), timeout=self.timeout_seconds):
                    out.append(module)
            except Exception:
                LOGGER.warning(
                    f"Visibility check failed for '{module.slug}'; withholding",
                    exc_info=True,
                )
        return out
```

- [ ] **Step 5: Run to verify pass**

```bash
cd chat_orchestrator && uv run pytest tests/test_jit_context_resolver.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add shared/prompts/providers.py chat_orchestrator/orchestrator/services/jit_context_resolver.py chat_orchestrator/tests/test_jit_context_resolver.py
git commit -m "fix(context): gate the on-demand catalog on caller visibility

A catalog line carries the module's summary, which can itself be sensitive,
and listing something the caller will be refused wastes a model turn."
```

### Task 8: Gate the `get_knowledge_module` MCP tool

`fetch_knowledge_module` resolves a gdoc body with **no caller identity at all**.
The model can name any slug and read any attached document — a larger exposure
than the render path. `tool_executor` already injects `user_email` into every MCP
call's arguments (`tool_executor.py:222`); the handler simply ignores it.

Also: now that `gdoc` is JIT, the existing `if module.is_jit:` refusal branch
would swallow doc modules and make on-demand doc fetching impossible. It must
narrow to the three genuinely-unfetchable sources.

No tool-schema change: `user_email` is injected, never declared. No
`tool_definitions.json` regeneration is needed.

**Files:**
- Modify: `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py:250-300`
- Test: `mcp_servers/tests/servers/knowledge_server/test_knowledge_module_tool.py`

- [ ] **Step 1: Write the failing tests**

Append to `mcp_servers/tests/servers/knowledge_server/test_knowledge_module_tool.py`:

```python
import pytest

from shared.prompts.knowledge import KnowledgeModule


class _Store:
    def __init__(self, modules):
        self._modules = modules

    def all_modules(self):
        return self._modules


class _Provider:
    def __init__(self, visible=True, body="doc body"):
        self._visible, self._body = visible, body

    async def visible_to(self, _module, _ctx):
        return self._visible

    async def resolve(self, _module, _ctx):
        return self._body if self._visible else None


def _doc_module(slug="specs"):
    return KnowledgeModule(
        id="1", slug=slug, title="Specs", summary="s", body=None,
        source="gdoc", source_ref="doc-1", doc_audience="acl_mirror",
    )


@pytest.mark.asyncio
async def test_an_allowed_caller_gets_the_document_body():
    from servers.knowledge_server.knowledge_mcp_server import fetch_knowledge_module

    text = await fetch_knowledge_module(
        "specs", user_email="tech@example.com",
        store=_Store([_doc_module()]), gdoc_provider=_Provider(visible=True),
    )

    assert "doc body" in text


@pytest.mark.asyncio
async def test_a_denied_caller_is_refused_and_told_why():
    """An empty body would read to the model as 'this module has no content'."""
    from servers.knowledge_server.knowledge_mcp_server import fetch_knowledge_module

    text = await fetch_knowledge_module(
        "specs", user_email="outsider@example.com",
        store=_Store([_doc_module()]), gdoc_provider=_Provider(visible=False),
    )

    assert "doc body" not in text
    assert "access" in text.lower()


@pytest.mark.asyncio
async def test_a_caller_with_no_email_is_refused():
    from servers.knowledge_server.knowledge_mcp_server import fetch_knowledge_module

    text = await fetch_knowledge_module(
        "specs", user_email=None,
        store=_Store([_doc_module()]), gdoc_provider=_Provider(visible=False),
    )

    assert "doc body" not in text


@pytest.mark.asyncio
async def test_a_doc_module_is_still_fetchable_on_demand():
    """gdoc became JIT in this change; the is_jit refusal must not swallow it."""
    from servers.knowledge_server.knowledge_mcp_server import fetch_knowledge_module

    text = await fetch_knowledge_module(
        "specs", user_email="tech@example.com",
        store=_Store([_doc_module()]), gdoc_provider=_Provider(visible=True),
    )

    assert "cannot be fetched on demand" not in text


@pytest.mark.asyncio
async def test_a_graph_module_is_still_refused():
    from servers.knowledge_server.knowledge_mcp_server import fetch_knowledge_module

    module = KnowledgeModule(
        id="2", slug="graph-ctx", title="Graph", summary="s", body=None, source="graph"
    )
    text = await fetch_knowledge_module(
        "graph-ctx", user_email="tech@example.com", store=_Store([module])
    )

    assert "cannot be fetched on demand" in text


@pytest.mark.asyncio
async def test_a_manual_module_needs_no_email():
    from servers.knowledge_server.knowledge_mcp_server import fetch_knowledge_module

    module = KnowledgeModule(
        id="3", slug="typed", title="Typed", summary="s", body="typed body"
    )
    text = await fetch_knowledge_module("typed", user_email=None, store=_Store([module]))

    assert "typed body" in text
```

Existing tests in this file call `fetch_knowledge_module(slug, store=...)`
synchronously. Update each to `await` and add `@pytest.mark.asyncio`. Run the
file first to see the exact list.

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH="$PWD:$PWD/mcp_servers" pytest mcp_servers/tests/servers/knowledge_server/test_knowledge_module_tool.py -q
```

Expected: FAIL — `fetch_knowledge_module() got an unexpected keyword argument 'user_email'`.

- [ ] **Step 3: Rewrite the fetch function**

In `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py`, replace
`fetch_knowledge_module` (line 250) and its handler:

```python
# Sources whose body depends on row-level database permissions this server
# does not carry. They are composed into context automatically instead.
# `gdoc` is deliberately NOT here: it is JIT, but it resolves from a caller's
# Drive access, which this server *can* check.
UNFETCHABLE_SOURCES = ("graph", "directory", "episodic")


async def fetch_knowledge_module(
    slug: str,
    user_email: Optional[str] = None,
    store: Any = None,
    gdoc_provider: Any = None,
) -> str:
    """Return one knowledge module's full body by slug, for this caller.

    Backs the on-demand tier: the model sees only slug and summary in context
    and calls this when it decides a module is relevant.

    A gdoc module is gated on the caller's Drive access -- without that this
    tool is a read primitive for every attached document, since the model
    chooses the slug. A graph/directory/episodic module cannot resolve here:
    its body depends on row-level permissions this server does not carry, so
    say so plainly rather than returning an empty body, which the model would
    read as "this module has no content".
    """
    from shared.prompts.knowledge import KnowledgeStore

    store = store or KnowledgeStore.from_env()
    modules = {m.slug: m for m in store.all_modules()}
    if not modules:
        return "No knowledge modules are configured."
    module = modules.get(slug)
    if not module:
        return f"No knowledge module named '{slug}'. Available: " + ", ".join(sorted(modules))

    if module.source in UNFETCHABLE_SOURCES:
        return (
            f"'{slug}' is live context. It is composed into your context "
            f"automatically when relevant and cannot be fetched on demand here."
        )

    body = module.body
    if module.source == "gdoc":
        from shared.prompts.providers import ResolutionContext
        from shared.prompts.types import RequestScope

        if gdoc_provider is None:
            from shared.prompts.providers_gdoc import GDocProvider

            gdoc_provider = GDocProvider()

        ctx = ResolutionContext(scope=RequestScope(), user_email=user_email)
        if not await gdoc_provider.visible_to(module, ctx):
            return (
                f"You do not have access to the document behind '{slug}'. "
                f"Ask the document owner to share it with you."
            )
        body = await gdoc_provider.resolve(module, ctx)

    if not body:
        return f"Knowledge module '{slug}' could not be loaded from its source."

    return f"# {module.title}\n\n{body}"


@registry.tool("get_knowledge_module", _SCHEMAS_BY_NAME["get_knowledge_module"])
async def _handle_get_knowledge_module(arguments: dict) -> list[types.TextContent]:
    """Handle get_knowledge_module tool call."""
    slug = arguments.get("slug", "")
    if not slug:
        return [types.TextContent(type="text", text="Error: slug is required")]
    # user_email is injected by the orchestrator's tool_executor, not declared
    # in the tool schema and never model-controlled.
    return [
        types.TextContent(
            type="text",
            text=await fetch_knowledge_module(slug, user_email=arguments.get("user_email")),
        )
    ]
```

Confirm `Optional` and `Any` are imported at the top of the file; add them to the
existing `typing` import if not.

- [ ] **Step 4: Run to verify pass**

```bash
PYTHONPATH="$PWD:$PWD/mcp_servers" pytest mcp_servers/tests/servers/knowledge_server -q
```

Expected: all pass.

- [ ] **Step 5: Confirm the whole mcp_servers suite still collects**

```bash
PYTHONPATH="$PWD:$PWD/mcp_servers" pytest mcp_servers/tests -q
```

Expected: all pass. Run the whole directory, not just the subdirectory — several
files in this suite rely on collection-order side effects for `sys.path`.

- [ ] **Step 6: Commit**

```bash
git add mcp_servers/servers/knowledge_server/knowledge_mcp_server.py mcp_servers/tests/servers/knowledge_server/test_knowledge_module_tool.py
git commit -m "fix(knowledge): gate get_knowledge_module on the caller's Drive access

The tool resolved a gdoc body with no caller identity at all, so the model
could name any slug and read any attached document. user_email is already
injected by tool_executor; the handler simply ignored it."
```

---

## Phase 6 — Scope

### Task 9: `sector` → `global`

`scope` is read in exactly one place — `select_for_prompt` → `RequestScope.matches`.
It has no relationship to Telegram chats, topics, or run routing.

**Files:**
- Modify: `shared/prompts/types.py:41-48`
- Modify: `shared/prompts/knowledge.py` (dataclass default)
- Modify: `chat_orchestrator/.../context_expert/store_module.py:38`
- Test: `shared/tests/test_prompt_knowledge.py`

The database side already shipped in Task 2's migration.

- [ ] **Step 1: Write the failing tests**

Append to `shared/tests/test_prompt_knowledge.py`:

```python
def test_global_scope_matches_every_request():
    from shared.prompts.types import RequestScope

    assert RequestScope().matches("global") is True
    assert RequestScope(organization_id="7").matches("global") is True


def test_sector_is_still_accepted_as_a_synonym():
    """matches() fails closed on an unknown scope, so a row the migration
    missed would go silently dark. Both spellings work, permanently."""
    from shared.prompts.types import RequestScope

    assert RequestScope().matches("sector") is True


def test_an_unknown_scope_still_matches_nothing():
    from shared.prompts.types import RequestScope

    assert RequestScope().matches("universe") is False


def test_a_new_module_defaults_to_global_scope():
    from shared.prompts.knowledge import KnowledgeModule

    assert KnowledgeModule(id="m", slug="m", title="M", summary="s", body="b").scope == "global"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd chat_orchestrator && uv run pytest ../shared/tests/test_prompt_knowledge.py -q -k "global or sector"
```

Expected: FAIL — `matches("global")` returns False.

- [ ] **Step 3: Accept both spellings**

In `shared/prompts/types.py`:

```python
    def matches(self, scope: str) -> bool:
        """Whether a module declaring ``scope`` applies to this request.

        'sector' is the pre-0018 spelling of 'global' and stays accepted
        permanently: this method fails closed on an unknown value, so a row
        the rename missed would stop contributing with no error anywhere.
        """
        if scope in ("global", "sector"):
            return True
        if scope.startswith("site:"):
            return bool(self.grid) and scope[5:].lower() == (self.grid or "").lower()
        if scope.startswith("org:"):
            return bool(self.organization_id) and scope[4:] == self.organization_id
        return False
```

- [ ] **Step 4: Update the defaults**

`shared/prompts/knowledge.py`:

```python
    scope: str = "global"
```

`chat_orchestrator/orchestrator/experts/handlers/context_expert/store_module.py`,
in `build_module_payload`:

```python
        "scope": "global",
```

- [ ] **Step 5: Run to verify pass**

```bash
cd chat_orchestrator && uv run pytest ../shared -q && uv run pytest tests/ -q
```

Expected: all pass. Some existing tests assert `scope == "sector"`; update those
assertions to `"global"` where they are asserting the *default*, and leave them
alone where they are deliberately exercising the legacy spelling.

- [ ] **Step 6: Commit**

```bash
git add shared/prompts/types.py shared/prompts/knowledge.py chat_orchestrator/orchestrator/experts/handlers/context_expert/store_module.py shared/tests/test_prompt_knowledge.py
git commit -m "refactor(context): rename the catch-all scope from sector to global

matches() accepts both spellings permanently -- it fails closed on an
unknown scope, so a row the rename missed would go silently dark."
```

---

## Phase 7 — Authoring

### Task 10: Admin page — source, audience, live preview, scope dropdown

**Files:**
- Modify: `anansi_app/nicegui_app/pages/knowledge_modules.py`
- Test: `anansi_app/tests/test_knowledge_modules_page.py`, `anansi_app/tests/test_knowledge_modules_dialog.py`

Note the house style: `anansi_app` tests either import the module's **pure
functions** (the conftest stubs `nicegui`) or assert on the page's **source text**
for UI wiring. Follow both patterns as the existing files do.

- [ ] **Step 1: Write the failing tests**

Append to `anansi_app/tests/test_knowledge_modules_page.py`:

```python
from nicegui_app.pages.knowledge_modules import (
    SCOPE_OPTIONS,
    body_is_editable,
    describe_audience,
    validate_module,
)


def test_a_doc_module_body_is_not_editable():
    assert body_is_editable("gdoc") is False
    assert body_is_editable("manual") is True


def test_global_is_a_valid_scope():
    validate_module(slug="s", title="T", summary="x", body="b", scope="global")


def test_sector_is_still_a_valid_scope():
    validate_module(slug="s", title="T", summary="x", body="b", scope="sector")


def test_an_unknown_scope_is_rejected():
    import pytest

    with pytest.raises(ValueError, match="scope"):
        validate_module(slug="s", title="T", summary="x", body="b", scope="universe")


def test_scope_options_offer_global_and_org_and_a_disabled_site():
    """A free-text box accepted site:FOO and produced a module that never
    fired -- nothing populates RequestScope.grid anywhere."""
    labels = {opt["value"]: opt for opt in SCOPE_OPTIONS}

    assert "global" in labels
    assert "org:" in labels
    assert labels["site:"]["disabled"] is True


def test_a_doc_module_requires_a_source_ref():
    import pytest

    with pytest.raises(ValueError, match="Google Doc or Sheet"):
        validate_module(
            slug="s", title="T", summary="x", body="", scope="global",
            require_body=False, source="gdoc", source_ref="",
        )


def test_a_doc_module_requires_an_audience():
    import pytest

    with pytest.raises(ValueError, match="audience"):
        validate_module(
            slug="s", title="T", summary="x", body="", scope="global",
            require_body=False, source="gdoc", source_ref="doc-1", doc_audience=None,
        )


def test_a_valid_doc_module_passes():
    validate_module(
        slug="s", title="T", summary="x", body="", scope="global",
        require_body=False, source="gdoc", source_ref="doc-1",
        doc_audience="acl_mirror",
    )


def test_describe_audience_warns_about_a_mirrored_customer_module():
    """It would provably resolve to nothing for a customer."""
    warning = describe_audience("acl_mirror", pinned_prompts=["customer.system"])

    assert warning is not None
    assert "customer" in warning.lower()


def test_describe_audience_is_quiet_for_a_staff_only_module():
    assert describe_audience("acl_mirror", pinned_prompts=["staff.system"]) is None


def test_describe_audience_is_quiet_for_a_published_module():
    assert describe_audience("published", pinned_prompts=["customer.system"]) is None
```

Append to `anansi_app/tests/test_knowledge_modules_dialog.py`:

```python
def test_the_dialog_offers_a_document_source():
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert "Google Doc or Sheet" in src


def test_the_preview_resolves_as_the_viewing_operator():
    """Preview must be a dry run of the real gate, not a second gate that
    could disagree with it. It previously passed no caller identity at all,
    so a document module would resolve under whatever the provider defaulted
    to rather than under the operator's own Drive access."""
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert "user_email=user_email" in src
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_knowledge_modules_page.py anansi_app/tests/test_knowledge_modules_dialog.py -q
```

Expected: `ImportError: cannot import name 'SCOPE_OPTIONS'`.

- [ ] **Step 3: Add the pure helpers**

In `anansi_app/nicegui_app/pages/knowledge_modules.py`, after the existing
constants:

```python
VALID_SCOPES_PREFIXED = ("site:", "org:")
LEGACY_GLOBAL_SCOPES = ("global", "sector")

# Free text let an operator type site:FOO and get a module that never fires --
# nothing populates RequestScope.grid anywhere in the codebase.
SCOPE_OPTIONS = [
    {
        "value": "global",
        "label": "Everywhere",
        "help": "Included in every conversation this prompt serves.",
        "disabled": False,
    },
    {
        "value": "org:",
        "label": "One organization",
        "help": "Included only when the caller belongs to this organization.",
        "disabled": False,
    },
    {
        "value": "site:",
        "label": "One grid",
        "help": "Not currently wired up — a grid-scoped module never matches.",
        "disabled": True,
    },
]

AUDIENCE_OPTIONS = {
    "acl_mirror": "Mirror the document's sharing (only people who can open it)",
    "published": "Publish to everyone this prompt serves",
}


def describe_audience(doc_audience: str, pinned_prompts: List[str]) -> "str | None":
    """A warning to show at attach time, or None.

    A mirrored module pinned to a customer-facing prompt resolves to nothing
    for customers -- their email is not in an internal document's ACL. Fail
    loudly here rather than silently at render.
    """
    if doc_audience != "acl_mirror":
        return None
    if not any(p.startswith("customer.") for p in pinned_prompts):
        return None
    return (
        "⚠️ This module mirrors the document's sharing, but it is attached to a "
        "customer-facing prompt. Customers are not in the document's sharing list, "
        "so it will contribute nothing for them. Choose \"Publish to everyone\" if "
        "customers are meant to see this content."
    )
```

- [ ] **Step 4: Extend `validate_module`**

Replace the existing `validate_module` with:

```python
def validate_module(
    slug: str,
    title: str,
    summary: str,
    body: str,
    scope: str = "global",
    mode: str = "pinned",
    require_body: bool = True,
    source: str = "manual",
    source_ref: str = "",
    doc_audience: "str | None" = None,
) -> None:
    """Reject a module that would fail silently at render time.

    require_body=False for a provider-backed module being edited: its body
    isn't stored here (see body_is_editable), so the field is legitimately
    empty and must not block saving a title/summary/scope/mode change.
    """
    if not slug or not title or (require_body and not body):
        raise ValueError("slug, title and body are required")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
    if mode == "on_demand" and not summary.strip():
        raise ValueError(
            "an on_demand module needs a summary: it is the only thing the model "
            "sees before deciding to fetch the body"
        )
    if scope not in LEGACY_GLOBAL_SCOPES and not scope.startswith(VALID_SCOPES_PREFIXED):
        raise ValueError("scope must be 'global', 'site:<name>' or 'org:<id>'")
    if source == "gdoc":
        if not source_ref.strip():
            raise ValueError("a document module needs a Google Doc or Sheet link")
        if doc_audience not in AUDIENCE_OPTIONS:
            raise ValueError(
                f"a document module needs an audience: {sorted(AUDIENCE_OPTIONS)}"
            )
```

- [ ] **Step 5: Run the pure-function tests**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_knowledge_modules_page.py -q
```

Expected: all pass.

- [ ] **Step 6: Fix the preview to use the operator's own identity**

In `_open_edit_dialog`, `preview_module_body` currently hardcodes `is_staff=True`.
Change the function to take the operator's email:

```python
async def preview_module_body(module: Any, provider: Any, user_email: str) -> str:
    """Resolve a provider module for display in the admin UI.

    Resolves against the viewing operator's own permissions. For a document
    module that means their own Drive access -- preview is a dry run of the
    real gate, not a second gate that could disagree with it. Anything else
    would show an operator content they cannot otherwise reach.
    """
    from shared.prompts.providers import ResolutionContext
    from shared.prompts.types import RequestScope

    # is_staff stays True and is accurate: /knowledge-modules is gated on
    # can_view_bot_admin (main.py:210), so anyone reaching this dialog is
    # staff. What was missing is user_email -- without it a document module
    # resolved under no identity at all.
    ctx = ResolutionContext(scope=RequestScope(), user_email=user_email, is_staff=True)
    try:
        body = await provider.resolve(module, ctx)
    except Exception as e:
        return f"Provider failed: {e}"
    return body or "Resolved to nothing for your permissions."
```

And at the call site inside `_run_preview`:

```python
                    preview_output.set_content(
                        await preview_module_body(existing, provider, user_email=user_email)
                    )
```

Delete the stale comment block above it that justified `is_staff=True`.

- [ ] **Step 7: Show the live document in the Preview pane**

For a provider-backed module the Edit/Preview toggle currently renders the stored
`body`, which for a document module is empty. Make the toggle resolve live. In
`_switch_view`:

```python
        async def _switch_view(e) -> None:
            if e.value == "Preview":
                if body_is_editable(source):
                    body_preview.set_content(body_input.value)
                else:
                    body_preview.set_content("_Resolving…_")
                    body_preview.set_content(await _resolved_body())
            body_input.set_visibility(e.value == "Edit")
            body_preview.set_visibility(e.value == "Preview")
```

and add, above it:

```python
        async def _resolved_body() -> str:
            """The live document, as this operator would actually receive it."""
            from orchestrator.services.jit_context_resolver import build_default_registry

            if existing is None:
                return "_Save the module first to preview its document._"
            provider = build_default_registry().get(source)
            if provider is None:
                return f"No '{source}' provider is available in this process."
            return await preview_module_body(existing, provider, user_email=user_email)
```

The dialog opens on Preview by default, so also resolve once at open time for a
provider-backed module rather than showing an empty pane.

- [ ] **Step 8: Add the source, document, audience and scope controls**

Replace the free-text `scope_input` with a select plus a conditional detail field,
and add the document controls. Insert after `summary_input`:

```python
        source_select = ui.select(
            {"manual": "Typed here", "gdoc": "Google Doc or Sheet"},
            value="gdoc" if source == "gdoc" else "manual",
            label="Source",
        ).classes("w-full")
        # The slug is the identity and the source decides the storage shape;
        # neither may drift on an existing module.
        source_select.set_enabled(existing is None)

        doc_row = ui.column().classes("w-full gap-2")
        with doc_row:
            doc_ref_input = ui.input(
                "Google Doc or Sheet link or ID",
                value=(existing.source_ref if existing else "") or "",
            ).classes("w-full")
            doc_tab_input = ui.input(
                "Sheet tab (optional — first tab if blank)",
                value=(existing.source_tab if existing else "") or "",
            ).classes("w-full")
            audience_select = ui.select(
                AUDIENCE_OPTIONS,
                value=(existing.doc_audience if existing else None) or "acl_mirror",
                label="Who may see this content",
            ).classes("w-full")
            if existing and existing.doc_audience_set_by:
                ui.label(
                    f"Audience last set by {existing.doc_audience_set_by}"
                ).classes("text-caption text-grey")
            audience_warning = ui.label("").classes("text-caption text-warning")
        doc_row.bind_visibility_from(source_select, "value", lambda v: v == "gdoc")

        scope_select = ui.select(
            {o["value"]: o["label"] for o in SCOPE_OPTIONS},
            value=_scope_kind(existing.scope if existing else "global"),
            label="Applies to",
        ).classes("w-full")
        scope_help = ui.label("").classes("text-caption text-grey")
        scope_detail = ui.input(
            "Organization ID", value=_scope_detail(existing.scope if existing else "")
        ).classes("w-full")

        def _on_scope_change() -> None:
            option = next(o for o in SCOPE_OPTIONS if o["value"] == scope_select.value)
            scope_help.set_text(option["help"])
            scope_detail.set_visibility(scope_select.value == "org:")

        scope_select.on_value_change(lambda _e: _on_scope_change())
        _on_scope_change()

        def _refresh_audience_warning() -> None:
            audience_warning.set_text(
                describe_audience(audience_select.value, list(prompts_select.value or [])) or ""
            )

        audience_select.on_value_change(lambda _e: _refresh_audience_warning())
```

Add the two scope helpers next to `describe_audience`:

```python
def _scope_kind(scope: str) -> str:
    """Which SCOPE_OPTIONS entry a stored scope string belongs to."""
    if scope.startswith("org:"):
        return "org:"
    if scope.startswith("site:"):
        return "site:"
    return "global"


def _scope_detail(scope: str) -> str:
    """The part after the prefix, for the detail field."""
    return scope.split(":", 1)[1] if ":" in scope else ""


def compose_scope(kind: str, detail: str) -> str:
    """Rebuild the stored scope string from the two controls."""
    detail = detail.strip()
    if kind == "global" or not detail:
        return "global"
    return f"{kind}{detail}"
```

**Ordering matters here.** `_refresh_audience_warning` closes over
`prompts_select`, which is created further down the existing function. Registering
the handler above is fine — the closure is not evaluated until the value changes —
but it must not be *called* before `prompts_select` exists, or it raises
`NameError`. So the first call goes immediately after that select is created:

```python
        prompts_select.on_value_change(lambda _e: _refresh_audience_warning())
        _refresh_audience_warning()
```

Two handlers become coroutines in this task — `_switch_view` (Step 7) and
`save()` (already async). NiceGUI awaits an async `on_value_change` handler, so
no wrapper is needed; just make sure `_switch_view` is declared `async def` and
every `await` inside it is reachable.

- [ ] **Step 9: Persist the new fields**

In `save()`, replace the validate call and the row construction:

```python
            chosen_source = source_select.value
            scope_value = compose_scope(scope_select.value, scope_detail.value)
            try:
                validate_module(
                    slug=slug_input.value.strip(),
                    title=title_input.value.strip(),
                    summary=summary_input.value.strip(),
                    body=body_input.value,
                    scope=scope_value,
                    mode=mode_select.value,
                    require_body=body_is_editable(chosen_source),
                    source=chosen_source,
                    source_ref=doc_ref_input.value,
                    doc_audience=audience_select.value if chosen_source == "gdoc" else None,
                )
            except ValueError as e:
                ui.notify(str(e), type="negative")
                return

            row = {
                "slug": slug_input.value.strip(),
                "title": title_input.value.strip(),
                "summary": summary_input.value.strip(),
                "tags": list(existing.tags) if existing else [],
                "scope": scope_value,
                "mode": mode_select.value,
                "source": chosen_source,
                "updated_by": user_email,
            }
            if chosen_source == "gdoc":
                file_id = extract_drive_id(doc_ref_input.value)
                if not file_id:
                    ui.notify("That doesn't look like a Google Doc or Sheet link", type="negative")
                    return
                # You cannot attach a document you cannot open yourself.
                from shared.utils.drive_permissions import user_can_access

                if not await user_can_access(file_id, user_email, strict=True):
                    ui.notify(
                        "You don't have access to that document, so you can't attach it.",
                        type="negative",
                    )
                    return
                row["source_ref"] = file_id
                row["source_tab"] = doc_tab_input.value.strip() or None
                row["doc_audience"] = audience_select.value
                # Only stamp attribution when the decision actually changes,
                # so an unrelated title edit doesn't reassign authorship.
                if not existing or existing.doc_audience != audience_select.value:
                    row["doc_audience_set_by"] = user_email
            if body_is_editable(chosen_source):
                row["body"] = body_input.value
```

Add the id extractor near the top of the file — the `/learn` path has an
equivalent, but `anansi_app` must not import from `chat_orchestrator`:

```python
import re

_DRIVE_ID_PATTERNS = (
    re.compile(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)"),
)
_BARE_DRIVE_ID = re.compile(r"^[a-zA-Z0-9_-]{25,60}$")


def extract_drive_id(text: str) -> "str | None":
    """The file id from a Docs/Sheets/Drive URL, or a bare id."""
    text = (text or "").strip()
    for pattern in _DRIVE_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return text if _BARE_DRIVE_ID.match(text) else None
```

Add tests for it to `test_knowledge_modules_page.py`:

```python
def test_extract_drive_id_reads_a_docs_url():
    from nicegui_app.pages.knowledge_modules import extract_drive_id

    assert extract_drive_id(
        "https://docs.google.com/document/d/1AbC_dEf-23456789012345678/edit"
    ) == "1AbC_dEf-23456789012345678"


def test_extract_drive_id_reads_a_sheets_url():
    from nicegui_app.pages.knowledge_modules import extract_drive_id

    assert extract_drive_id(
        "https://docs.google.com/spreadsheets/d/1AbC_dEf-23456789012345678/edit#gid=0"
    ) == "1AbC_dEf-23456789012345678"


def test_extract_drive_id_accepts_a_bare_id():
    from nicegui_app.pages.knowledge_modules import extract_drive_id

    assert extract_drive_id("1AbC_dEf-23456789012345678") == "1AbC_dEf-23456789012345678"


def test_extract_drive_id_rejects_nonsense():
    from nicegui_app.pages.knowledge_modules import extract_drive_id

    assert extract_drive_id("not a link") is None
    assert extract_drive_id("") is None
```

- [ ] **Step 10: Update the size label and read-only explanation**

`build_module_rows` already labels provider sources `"live"`. Add the tab to the
row caption so an operator can see which sheet tab a module reads. In
`_READONLY_BODY_EXPLANATIONS`:

```python
    "gdoc": "Body comes from the attached Google Doc or Sheet, fetched fresh at "
            "request time and filtered to what the caller may see.",
```

- [ ] **Step 11: Run the anansi_app suite**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests -q
```

Expected: all pass.

- [ ] **Step 12: Commit**

```bash
git add anansi_app/nicegui_app/pages/knowledge_modules.py anansi_app/tests/test_knowledge_modules_page.py anansi_app/tests/test_knowledge_modules_dialog.py
git commit -m "feat(context): attach a Google Doc or Sheet from the Context page

Source picker, audience choice with attribution, and a scope dropdown that
stops offering the grid scope nothing populates. Preview now resolves the
live document as the viewing operator, so it is a dry run of the real gate
rather than a second gate that could disagree with it."
```

### Task 11: `/learn` creates live doc-linked modules

**Files:**
- Modify: `chat_orchestrator/.../ingestion_expert/fetch_document.py`
- Modify: `chat_orchestrator/.../context_expert/store_module.py`
- Modify: `shared/prompts/library/experts.definitions.prompt:359-404`
- Test: `chat_orchestrator/tests/test_context_expert.py`

- [ ] **Step 1: Write the failing tests**

Append to `chat_orchestrator/tests/test_context_expert.py`:

```python
def test_a_typed_module_payload_is_unchanged():
    from orchestrator.experts.handlers.context_expert.store_module import (
        build_module_payload,
    )

    payload = build_module_payload(
        slug="s", title="T", summary="x", body="typed body",
        mode="on_demand", actor="tech@example.com",
    )

    assert payload["source"] == "manual"
    assert payload["body"] == "typed body"
    assert "source_ref" not in payload


def test_a_doc_linked_payload_stores_no_body():
    """The document is the source of truth; a stored copy would only drift."""
    from orchestrator.experts.handlers.context_expert.store_module import (
        build_module_payload,
    )

    payload = build_module_payload(
        slug="s", title="T", summary="x", body="ignored",
        mode="on_demand", actor="tech@example.com",
        source="gdoc", source_ref="doc-1", source_tab="Errors",
    )

    assert payload["source"] == "gdoc"
    assert payload["source_ref"] == "doc-1"
    assert payload["source_tab"] == "Errors"
    assert payload["doc_audience"] == "acl_mirror"
    assert payload["doc_audience_set_by"] == "tech@example.com"
    assert "body" not in payload


def test_a_doc_linked_payload_can_be_published():
    from orchestrator.experts.handlers.context_expert.store_module import (
        build_module_payload,
    )

    payload = build_module_payload(
        slug="s", title="T", summary="x", body="", mode="on_demand",
        actor="tech@example.com", source="gdoc", source_ref="doc-1",
        doc_audience="published",
    )

    assert payload["doc_audience"] == "published"


def test_a_spreadsheet_is_an_accepted_drive_type():
    from orchestrator.experts.handlers.ingestion_expert.fetch_document import (
        SUPPORTED_DRIVE_MIMES,
    )

    assert "application/vnd.google-apps.spreadsheet" in SUPPORTED_DRIVE_MIMES
```

- [ ] **Step 2: Run to verify failure**

```bash
cd chat_orchestrator && uv run pytest tests/test_context_expert.py -q -k "payload or spreadsheet"
```

Expected: FAIL — `build_module_payload() got an unexpected keyword argument 'source'`.

- [ ] **Step 3: Make the payload source-aware**

In `store_module.py`, replace `build_module_payload`:

```python
def build_module_payload(
    slug: str,
    title: str,
    summary: str,
    body: str,
    mode: str,
    actor: str,
    source: str = "manual",
    source_ref: str = "",
    source_tab: str = "",
    doc_audience: str = "acl_mirror",
) -> Dict[str, Any]:
    """A knowledge_modules row for a staff-authored module.

    A doc-linked module stores no body: the document is the source of truth
    and is fetched fresh at request time, so a stored copy could only drift.
    Its audience defaults to mirroring the document's own sharing.
    """
    payload: Dict[str, Any] = {
        "slug": slug,
        "title": title,
        "summary": summary,
        "tags": [],
        "scope": "global",
        "mode": mode,
        "source": source,
        "updated_by": actor,
    }
    if source == "gdoc":
        payload["source_ref"] = source_ref
        payload["source_tab"] = source_tab or None
        payload["doc_audience"] = doc_audience
        payload["doc_audience_set_by"] = actor
    else:
        payload["body"] = body
    return payload
```

And in `store_module`, pass the state through:

```python
    payload = build_module_payload(
        slug=slug,
        title=context.get_state("module_title") or "",
        summary=context.get_state("module_summary") or "",
        body=body,
        mode=context.get_state("module_mode") or resolve_mode(body),
        actor=actor,
        source=context.get_state("module_source") or "manual",
        source_ref=context.get_state("module_source_ref") or "",
        source_tab=context.get_state("module_source_tab") or "",
        doc_audience=context.get_state("module_doc_audience") or "acl_mirror",
    )
```

- [ ] **Step 4: Accept spreadsheets in the fetch step**

In `fetch_document.py`, add near the other constants:

```python
GSHEET_URL_PATTERN = re.compile(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)")

DOCUMENT_MIME = "application/vnd.google-apps.document"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
SUPPORTED_DRIVE_MIMES = (DOCUMENT_MIME, SPREADSHEET_MIME, "application/pdf")
```

Add `GSHEET_URL_PATTERN` to the loop in `extract_file_id`:

```python
    for pattern in [GDOC_URL_PATTERN, GSHEET_URL_PATTERN, GDRIVE_URL_PATTERN, GDRIVE_OPEN_PATTERN]:
```

In `_fetch_from_gdrive`'s mime dispatch, add a spreadsheet branch before the
DOCX branch:

```python
        elif mime_type == SPREADSHEET_MIME:
            await context.send_progress_to_user(f"Reading sheet: {title}")
            from shared.utils.gdrive_doc_fetcher import fetch_google_sheet_markdown

            content = await asyncio.to_thread(fetch_google_sheet_markdown, file_id, None)
            file_type = "google_sheet"
```

and extend the unsupported-type message:

```python
            return StepResult.failure(
                f"Unsupported file type: {mime_type}\n\n"
                f"Supported formats:\n"
                f"• Google Docs\n"
                f"• Google Sheets\n"
                f"• PDF files"
            )
```

Also set the source URL for a sheet:

```python
        if mime_type == DOCUMENT_MIME:
            source_url = f"https://docs.google.com/document/d/{file_id}/edit"
        elif mime_type == SPREADSHEET_MIME:
            source_url = f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
        else:
            source_url = f"https://drive.google.com/file/d/{file_id}/view"
```

- [ ] **Step 5: Run to verify pass**

```bash
cd chat_orchestrator && uv run pytest tests/test_context_expert.py -q
```

Expected: all pass.

- [ ] **Step 6: Add the live-link question to the workflow**

In `shared/prompts/library/experts.definitions.prompt`, update the
`context_expert` block (line 359). Change workflow steps 2 and 3, and extend the
State section:

```
1. [llm] understand_request - Parse what the user wants the bot to learn
2. [function:fetch_document] - Retrieve content from paste, Google Doc, Sheet, or file
3. [function:choose_doc_link_mode] - For a Drive source, ask whether to link it live or copy the text in now
4. [function:improve_content] - Quality check and iterative wording improvement (skipped for a live link)
5. [function:propose_module] - Draft slug, title and summary via LLM
6. [function:detect_module_duplicates] - Check for an existing module with the same slug, title or body
7. [function:select_prompts] - Ask which prompts should use this module
8. [function:prepare_module_approval] - Show the proposed module and ask for approval
9. [function:store_module] - On approval, write the module and its prompt pins
10. [llm] report_completion - Confirm what was saved and where it applies
```

And in the System Instructions, replace bullet 2:

```
2. Guide them to provide it (pasted text, a Google Doc, or a Google Sheet)
```

Add to the State section:

```
module_source: string - manual or gdoc
module_source_ref: string - Drive file id when linked live
module_source_tab: string - Sheet tab name, blank for the first tab
module_doc_audience: string - acl_mirror (default) or published
```

- [ ] **Step 7: Add the `choose_doc_link_mode` step handler**

Create `chat_orchestrator/orchestrator/experts/handlers/context_expert/choose_doc_link_mode.py`:

```python
"""Ask whether a Drive source should be linked live or copied in once.

A live link is the default and the point of the feature: the module reads
the document at request time, so edits to the document take effect without
anyone touching the module. A copy is a point-in-time snapshot that will
drift -- offered only because sometimes that is genuinely what you want.

A live link also skips improve_content: rewriting text that is discarded at
render time is waste, and the rewrite would misrepresent the document.
"""

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

LIVE_WORDS = {"live", "link", "linked", "1", "yes"}
COPY_WORDS = {"copy", "snapshot", "text", "2", "no"}

QUESTION = (
    "Do you want this to stay linked to the document?\n\n"
    "1. **Link it live** — the bot re-reads the document each time, so your "
    "edits take effect automatically. Only people who can open the document "
    "will see its content.\n"
    "2. **Copy the text in now** — a snapshot. Later edits to the document "
    "won't reach the bot.\n\n"
    "Reply `1` or `2`."
)


@register_step("choose_doc_link_mode")
async def choose_doc_link_mode(context: StepContext) -> StepResult:
    """Ask once, for a Drive source only. Pasted text passes straight through."""
    if context.get_state("source_type") != "gdrive":
        return StepResult()

    if not context.get_state("awaiting_link_mode"):
        return StepResult(
            state_updates={"awaiting_link_mode": True},
            needs_user_input=True,
            user_prompt=QUESTION,
        )

    answer = (context.user_input or "").strip().lower()
    if answer in COPY_WORDS:
        LOGGER.info("Doc will be copied in as a snapshot")
        return StepResult(
            state_updates={"module_source": "manual", "awaiting_link_mode": False},
            progress_message="Copying the text in as a snapshot.",
        )
    if answer in LIVE_WORDS:
        LOGGER.info(f"Doc {context.get_state('source_id')} will be linked live")
        return StepResult(
            state_updates={
                "module_source": "gdoc",
                "module_source_ref": context.get_state("source_id") or "",
                "module_source_tab": "",
                "module_doc_audience": "acl_mirror",
                "skip_improve_content": True,
                "awaiting_link_mode": False,
            },
            progress_message="Linking the document live.",
        )

    return StepResult(needs_user_input=True, user_prompt=f"Please reply 1 or 2.\n\n{QUESTION}")
```

Register it in `context_expert/__init__.py` alongside the others — add the import,
the `__all__` entry, and a line to the module docstring's step list.

- [ ] **Step 8: Make `improve_content` respect the skip flag**

At the top of `improve_content`'s handler body in
`chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/improve_content.py`:

```python
    if context.get_state("skip_improve_content"):
        # A live-linked document is the source of truth. Rewriting it here
        # would produce text that is thrown away at render time.
        return StepResult(progress_message="Using the document as written.")
```

- [ ] **Step 9: Add the handler tests**

Append to `chat_orchestrator/tests/test_context_expert.py`:

```python
import pytest


class _Ctx:
    """Minimal StepContext stand-in for the link-mode question."""

    def __init__(self, state, user_input=None):
        self._state, self.user_input = state, user_input

    def get_state(self, key, default=None):
        return self._state.get(key, default)


@pytest.mark.asyncio
async def test_pasted_text_is_never_asked_about_linking():
    from orchestrator.experts.handlers.context_expert.choose_doc_link_mode import (
        choose_doc_link_mode,
    )

    result = await choose_doc_link_mode(_Ctx({"source_type": "manual_input"}))

    assert result.needs_user_input is False


@pytest.mark.asyncio
async def test_a_drive_source_is_asked_once():
    from orchestrator.experts.handlers.context_expert.choose_doc_link_mode import (
        choose_doc_link_mode,
    )

    result = await choose_doc_link_mode(_Ctx({"source_type": "gdrive"}))

    assert result.needs_user_input is True
    assert result.state_updates["awaiting_link_mode"] is True


@pytest.mark.asyncio
async def test_choosing_live_records_the_file_id_and_skips_the_rewrite():
    from orchestrator.experts.handlers.context_expert.choose_doc_link_mode import (
        choose_doc_link_mode,
    )

    result = await choose_doc_link_mode(
        _Ctx({"source_type": "gdrive", "awaiting_link_mode": True, "source_id": "doc-9"}, "1")
    )

    assert result.state_updates["module_source"] == "gdoc"
    assert result.state_updates["module_source_ref"] == "doc-9"
    assert result.state_updates["module_doc_audience"] == "acl_mirror"
    assert result.state_updates["skip_improve_content"] is True


@pytest.mark.asyncio
async def test_choosing_a_snapshot_keeps_the_manual_source():
    from orchestrator.experts.handlers.context_expert.choose_doc_link_mode import (
        choose_doc_link_mode,
    )

    result = await choose_doc_link_mode(
        _Ctx({"source_type": "gdrive", "awaiting_link_mode": True, "source_id": "doc-9"}, "2")
    )

    assert result.state_updates["module_source"] == "manual"
    assert "skip_improve_content" not in result.state_updates


@pytest.mark.asyncio
async def test_an_unclear_answer_asks_again():
    from orchestrator.experts.handlers.context_expert.choose_doc_link_mode import (
        choose_doc_link_mode,
    )

    result = await choose_doc_link_mode(
        _Ctx({"source_type": "gdrive", "awaiting_link_mode": True}, "maybe")
    )

    assert result.needs_user_input is True
```

If `StepResult`'s constructor requires fields these tests do not set, read
`orchestrator/experts/step_context.py` and adjust the assertions to the real
attribute names rather than changing the handler to fit the test.

- [ ] **Step 10: Run the orchestrator suite**

```bash
cd chat_orchestrator && uv run pytest tests/ -q
```

Expected: all pass. `test_prompt_parity.py` may fail if
`shared/prompts/library/experts.definitions.prompt` changed — that file has a
committed checksum. Regenerate it the way the existing tooling does; check
`chat_orchestrator/tests/prompt_checksums.json` and the parity test's own error
message for the exact command. Do **not** edit the checksum by hand without
confirming the change is the intended one.

- [ ] **Step 11: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/context_expert/ chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/ shared/prompts/library/experts.definitions.prompt chat_orchestrator/tests/test_context_expert.py chat_orchestrator/tests/prompt_checksums.json
git commit -m "feat(learn): let /learn link a Google Doc or Sheet live

A linked module reads the document at request time, so edits take effect
without touching the module -- and it skips improve_content, since
rewriting text that is discarded at render would only misrepresent the
source. Sheets are now an accepted Drive type."
```

---

## Final verification

- [ ] **Step 1: Full test sweep**

```bash
cd chat_orchestrator && uv run pytest tests/ -q && uv run pytest ../shared -q
```

```bash
PYTHONPATH="$PWD:$PWD/mcp_servers" pytest mcp_servers/tests -q
```

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests -q
```

- [ ] **Step 2: Confirm no orphaned references**

```bash
cd chat_orchestrator && uv run grep -rn "gdoc_module_provider\|_with_resolved_body\|body_for(module" ../shared ../chat_orchestrator ../anansi_app ../mcp_servers
```

Expected: no output.

- [ ] **Step 3: Pre-commit**

```bash
pre-commit run --all-files
```

If it reports untracked files under any `tests/` directory, vet each for operator
data, then `git add -f` it explicitly and re-run until clean. A plain `git add`
is a silent no-op on those paths.

- [ ] **Step 4: Confirm the new test files actually committed**

```bash
git log --stat --oneline -12 | grep -E "test_drive_permissions|test_sheet_markdown"
```

Expected: both appear. If either is missing, it was dropped by `.gitignore` and
CI will never run it.

- [ ] **Step 5: Commit the plan itself**

```bash
git add -f docs/superpowers/plans/2026-08-20-doc-backed-context-modules.md
git commit -m "docs(context): implementation plan for doc-backed context modules"
```

---

## Manual verification (after the migration is applied)

Automated tests cover the logic; these confirm the wiring against real Drive.

- [ ] Apply `db/migrations/0018_doc_backed_modules.sql` by hand against `chat_db`. Confirm the closing `SELECT` reports `0` pre-existing gdoc rows (any non-zero row tightened from "everyone" to ACL-gated — check it was intended).
- [ ] On `/knowledge-modules`, create a module from a Google Doc you can open. Preview shows the live document.
- [ ] Create one from a Google Sheet with more than 200 rows. Preview shows a markdown table ending in the truncation footer.
- [ ] Attach a doc you cannot open. Save is refused.
- [ ] Set a mirrored module's prompts to include `customer.system`. The warning appears.
- [ ] Pin a mirrored module to `staff.system`. As a staff user with Drive access, ask the bot something it answers. Confirm the content appears; check the orchestrator log for the module slug in `jit_used`.
- [ ] Revoke your own Drive access to that document. Within the access TTL (60s) the content still resolves; after it, it stops. Confirm the "withheld" log line names the module and your email.
- [ ] Run `/learn` with a Google Sheet URL, choose "link it live", and confirm the stored row has `source='gdoc'`, a `source_ref`, `doc_audience='acl_mirror'`, and a NULL `body`.
