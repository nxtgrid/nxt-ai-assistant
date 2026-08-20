"""Context admin page: CRUD for curated context modules.

A context module is named, addressable content a prompt can deliberately pin
(inlined in full) or leave on-demand (name + summary only, fetched via the
get_knowledge_module MCP tool when the model decides it's relevant). Selection
is explicit per prompt -- see the Context tab on the Prompts page.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, List, Tuple

from nicegui import ui

VALID_MODES = {"pinned", "on_demand"}
VALID_SOURCES = {"manual", "gdoc", "ingested"}

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

_DRIVE_ID_PATTERNS = (
    re.compile(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)"),
)
_BARE_DRIVE_ID = re.compile(r"^[a-zA-Z0-9_-]{25,60}$")


def extract_drive_id(text: str) -> "str | None":
    """The file id from a Docs/Sheets/Drive URL, or a bare id.

    Anansi_app must not import from chat_orchestrator, so this duplicates
    (rather than reuses) the equivalent extractor in the /learn handler's
    fetch_document.py.
    """
    text = (text or "").strip()
    for pattern in _DRIVE_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return text if _BARE_DRIVE_ID.match(text) else None


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

MODE_LABELS = {"pinned": "Pinned", "on_demand": "On-demand"}
MODE_ORDER = ["pinned", "on_demand"]

# Same disclosure-triangle convention as the Prompts and Settings pages:
# pointing right while collapsed, down once expanded.
DISCLOSURE_ICONS = 'expand-icon="keyboard_arrow_right" expanded-icon="keyboard_arrow_down"'


# Sources whose body is produced at render time rather than stored.
PROVIDER_SOURCES = ("gdoc", "graph", "directory", "episodic")

# Exactly one of each exists; deleting it only makes the capability
# unreachable, so the UI refuses.
SINGLETON_SOURCES = ("graph", "directory")

# Shown under a non-editable body field, keyed by source.
_READONLY_BODY_EXPLANATIONS = {
    "gdoc": "Body comes from the attached Google Doc or Sheet, fetched fresh at "
            "request time and filtered to what the caller may see.",
    "graph": "Body is generated from the knowledge graph at request time, "
             "filtered to what the caller may see.",
    "directory": "Body lists the grids, organizations and people the "
                 "caller may see, at request time.",
    "episodic": "Body is the stored distillation for the grid or "
                "organization in scope.",
}


def body_is_editable(source: str) -> bool:
    """Only a stored body can be edited here."""
    return source not in PROVIDER_SOURCES


def module_is_deletable(source: str) -> bool:
    return source not in SINGLETON_SOURCES


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


def group_module_rows(rows: List[ModuleRow]) -> List[Tuple[str, List[ModuleRow]]]:
    """Bucket rows by mode -- pinned, then on-demand -- as ``(label, rows)``.

    Each bucket stays slug-sorted because ``rows`` already is (see
    ``build_module_rows``).
    """
    by_mode: "defaultdict[str, List[ModuleRow]]" = defaultdict(list)
    for row in rows:
        by_mode[row.mode].append(row)

    order = [m for m in MODE_ORDER if m in by_mode]
    order += sorted(m for m in by_mode if m not in MODE_LABELS)

    return [(MODE_LABELS.get(m, m), by_mode[m]) for m in order]


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


def prompt_option_label(prompt_id: str, description: str, max_len: int = 70) -> str:
    """Dropdown label: the id plus a truncated purpose, not the id alone."""
    description = description.strip()
    if len(description) > max_len:
        description = description[: max_len - 1].rstrip() + "…"
    return f"{prompt_id} — {description}" if description else prompt_id


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


async def render(user_email: str) -> None:
    from shared.prompts.knowledge import KnowledgeStore

    ui.label("🧠 Context").classes("text-h5")
    ui.label(
        "Curated facts the bot is told directly — the context it works from. Pinned "
        "modules are inlined into a prompt in full; on-demand modules contribute only "
        "their summary, and the model fetches the body with a tool when it decides "
        "it's relevant. Attach modules to prompts here or from the Context tab of any "
        "prompt."
    ).classes("text-caption")

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
    # can_view_bot_admin, so anyone reaching this dialog is staff. What was
    # missing is user_email -- without it a document module resolved under
    # no identity at all.
    ctx = ResolutionContext(scope=RequestScope(), user_email=user_email, is_staff=True)
    try:
        body = await provider.resolve(module, ctx)
    except Exception as e:
        return f"Provider failed: {e}"
    return body or "Resolved to nothing for your permissions."


def _render_row(row: ModuleRow, store: Any, refresh, user_email: str) -> None:
    with ui.card().classes("w-full q-my-xs"):
        with ui.row().classes("items-center justify-between w-full no-wrap"):
            with ui.column().classes("gap-0").style("flex: 3"):
                ui.label(row.title).classes("text-bold")
                ui.label(
                    f"{row.slug} · {row.source} · {row.scope} · {row.mode} · {row.size_label}"
                ).classes("text-caption")
                if row.tags:
                    ui.label(f"tags (legacy): {', '.join(row.tags)}").classes("text-caption")
            ui.button(
                "Edit",
                on_click=lambda: _open_edit_dialog(row.slug, store, refresh, user_email),
            ).props("flat dense")


async def _open_edit_dialog(
    slug: "str | None", store: Any, refresh, user_email: str
) -> None:
    from shared.prompts import PROMPTS

    existing = None
    if slug:
        existing = next((m for m in store.all_modules() if m.slug == slug), None)
    existing_pins = store.prompts_pinning(existing.id) if existing else []
    source = getattr(existing, "source", "manual") if existing else "manual"
    # existing.body is None for a provider-backed module -- "" if existing
    # else "" would miss that, since it only substitutes when existing
    # itself is falsy.
    existing_body = (existing.body or "") if existing else ""

    with ui.dialog() as dialog, ui.card().classes("w-full").style(
        "max-width: 700px; max-height: calc(100dvh - 32px); overflow-y: auto"
    ):
        ui.label("Edit module" if existing else "New module").classes("text-h6")
        slug_input = ui.input("Slug", value=existing.slug if existing else "").classes("w-full")
        slug_input.set_enabled(existing is None)  # slug is the identity; don't let it drift
        title_input = ui.input("Title", value=existing.title if existing else "").classes(
            "w-full"
        )
        summary_input = ui.input("Summary", value=existing.summary if existing else "").classes(
            "w-full"
        )

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

        mode_select = ui.select(
            sorted(VALID_MODES), value=existing.mode if existing else "pinned", label="Mode"
        ).classes("w-full")
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Body").classes("text-caption")
            # Defaults to Preview -- opening a module is almost always to
            # read it; Edit is one click away for the times it isn't.
            view_toggle = ui.toggle(["Edit", "Preview"], value="Preview").props("dense")
        body_input = ui.codemirror(
            value=existing_body,
            language="Markdown",
            theme="vscodeLight",
            line_wrapping=True,
        ).classes("w-full")
        body_input.set_visibility(False)
        body_preview = (
            ui.markdown(existing_body)
            .classes("w-full")
            .style("min-height: 16rem; border: 1px solid #e0e0e0; padding: 0.5rem;")
        )

        async def _resolved_body() -> str:
            """The live document, as this operator would actually receive it."""
            from orchestrator.services.jit_context_resolver import build_default_registry

            if existing is None:
                return "_Save the module first to preview its document._"
            provider = build_default_registry().get(source)
            if provider is None:
                return f"No '{source}' provider is available in this process."
            return await preview_module_body(existing, provider, user_email=user_email)

        async def _switch_view(e) -> None:
            if e.value == "Preview":
                if body_is_editable(source):
                    body_preview.set_content(body_input.value)
                else:
                    body_preview.set_content("_Resolving…_")
                    body_preview.set_content(await _resolved_body())
            body_input.set_visibility(e.value == "Edit")
            body_preview.set_visibility(e.value == "Preview")

        view_toggle.on_value_change(_switch_view)

        if not body_is_editable(source):
            body_input.props("readonly").classes("opacity-60")
            ui.label(_READONLY_BODY_EXPLANATIONS[source]).classes("text-xs text-gray-500")
            # The dialog opens on Preview by default -- resolve once now so
            # the pane isn't just showing the (always-empty) stored body.
            body_preview.set_content(await _resolved_body())

        prompt_options = {
            pid: prompt_option_label(pid, PROMPTS.spec(pid).description)
            for pid in sorted(PROMPTS.ids())
        }
        prompts_select = ui.select(
            prompt_options,
            value=list(existing_pins),
            multiple=True,
            label="Used by these prompts",
        ).classes("w-full").props("use-chips")

        def _refresh_audience_warning() -> None:
            audience_warning.set_text(
                describe_audience(audience_select.value, list(prompts_select.value or [])) or ""
            )

        audience_select.on_value_change(lambda _e: _refresh_audience_warning())
        prompts_select.on_value_change(lambda _e: _refresh_audience_warning())
        _refresh_audience_warning()

        async def save() -> None:
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
            # A provider body is never stored -- the field is read-only and
            # left blank in the UI, so sending it here would overwrite a
            # real NULL with an empty string for no reason. Omit the key
            # entirely so the column is left untouched.
            if body_is_editable(chosen_source):
                row["body"] = body_input.value
            try:
                if existing:
                    store._client.table("knowledge_modules").update(row).eq(
                        "slug", row["slug"]
                    ).execute()
                    module_id = existing.id
                else:
                    result = store._client.table("knowledge_modules").insert(row).execute()
                    module_id = result.data[0]["id"]
                store.set_prompt_pins(
                    module_id, list(prompts_select.value or []), actor=user_email
                )
                ui.notify("Saved", type="positive")
                dialog.close()
                refresh()
            except Exception as e:
                ui.notify(f"Save failed: {e}", type="negative")

        with ui.row().classes("justify-end w-full gap-2 q-mt-sm"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save).props("color=primary")

    dialog.open()
