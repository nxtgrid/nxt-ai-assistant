"""Context admin page: CRUD for context modules.

A context module is named, addressable content a prompt can be given.
Attaching one to a prompt inlines its body into that prompt in full --
there is no summary-only tier any more, because "attached but not actually
present" is not what attaching a module reads as. Selection is explicit per
prompt -- see the Context tab on the Prompts page.

Modules are grouped by where their body comes from:

* **Built-in** -- directory, entity-graph and episodic. Defined by the code,
  generated fresh per request and filtered to what the caller may see. You
  choose which prompts use them; you cannot create, delete or write one.
* **Curated** -- a body typed directly into this admin UI. This app is the
  source of truth for it.
* **External** -- an attached Google Doc or Sheet. The content lives in
  Drive, not here; this app only mirrors it (fetched fresh at request time,
  filtered to what the caller may see), which is why it gets its own group
  rather than sharing Curated's "this app owns it" framing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, List, Tuple

from nicegui import ui

from shared.prompts.knowledge import INLINE_BUDGET_CHARS, SINGLETON_SOURCES, KnowledgeModule
from shared.prompts.skills import SKILL_CATALOG, SKILL_PIN_PREFIX, skill_prompt_id

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


def slugify(text: str) -> str:
    """Kebab-case identifier -- same shape as chat_orchestrator's
    normalize_slug (context_expert/propose_module.py, the /learn flow) and
    the Skills editor's _slugify (skill_builder_service.py), but never
    raises: this one runs live on every keystroke of a Title field while
    creating a module, where "nothing survived" just means "nothing to
    suggest yet," not an error worth interrupting typing over.
    """
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def next_auto_slug(current_slug: str, last_auto_slug: str, title: str) -> "str | None":
    """The slug to auto-fill from a Title edit, or None to leave it alone.

    Auto-fills only while the Slug field still holds exactly what autofill
    itself last wrote there. The moment an operator types their own slug,
    ``current_slug`` diverges from ``last_auto_slug`` and every subsequent
    call returns None for good -- autofill can never overwrite a deliberate
    choice, even if they later change their mind about the Title too.

    Only meaningful while creating a module: the dialog never wires this up
    for an existing one, whose slug field is locked (it's the identity;
    see slug_input.set_enabled below).
    """
    if current_slug != last_auto_slug:
        return None
    return slugify(title)


def draft_gdoc_module(
    slug: str, title: str, summary: str, file_id: str, source_tab: "str | None",
    doc_audience: "str | None",
) -> KnowledgeModule:
    """A throwaway KnowledgeModule for previewing a document before Save.

    Resolving a module means handing a KnowledgeModule to a provider
    (see shared/prompts/providers_gdoc.py's GDocProvider.resolve) -- but a
    module being created doesn't have a real row or id yet. This builds one
    from the current form values instead, good for exactly one preview
    resolve and never persisted: an empty id is harmless because nothing
    downstream of resolve() reads it.

    Slug/title fall back to placeholders rather than staying blank --
    KnowledgeModule requires both non-empty, and this draft only exists to
    be resolved, never saved, so a placeholder cannot leak into real data.
    """
    return KnowledgeModule(
        id="",
        slug=slug or "preview",
        title=title or "(untitled)",
        summary=summary,
        body=None,
        source="gdoc",
        source_ref=file_id,
        source_tab=source_tab or None,
        doc_audience=doc_audience,
    )


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

# The list groups by where a body comes from, which is the only axis a
# module still varies on. "Built-in" is the code-defined singletons; "External"
# is an attached Google Doc/Sheet (content lives in Drive, not here); anything
# else an operator wrote directly is "Curated".
BUILT_IN_LABEL = "Built-in"
CURATED_LABEL = "Curated"
EXTERNAL_LABEL = "External"
GROUP_ORDER = [BUILT_IN_LABEL, CURATED_LABEL, EXTERNAL_LABEL]

SOURCE_LABELS = {
    "manual": "typed here",
    "gdoc": "Google Doc or Sheet",
    "directory": "built-in · directory",
    "graph": "built-in · knowledge graph",
    "episodic": "built-in · episodic memory",
    "ingested": "ingested",
}

# Same disclosure-triangle convention as the Prompts and Settings pages:
# pointing right while collapsed, down once expanded.
DISCLOSURE_ICONS = 'expand-icon="keyboard_arrow_right" expanded-icon="keyboard_arrow_down"'


# Sources whose body is produced at render time rather than stored.
PROVIDER_SOURCES = ("gdoc", "graph", "directory", "episodic")

# Exactly one of each exists; deleting it only makes the capability
# unreachable, so the UI refuses. Sourced from shared.prompts.knowledge so
# this list can't drift from the one ensure_singleton_modules bootstraps --
# that drift is exactly how episodic ended up missing from this guard before.

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


def singleton_creation_warnings(results: "dict[str, str]") -> List[str]:
    """Human-readable warnings for any singleton row ensure_singleton_modules
    could not create -- most likely migration 0017_context_module_providers.sql
    not yet applied against this database. 'exists' and 'created' are both
    success; anything else is a failure worth surfacing to whoever is looking
    at this page, since it is the only place that failure would ever appear.
    """
    return [
        f"Couldn't create the '{source}' context module: {outcome}"
        for source, outcome in sorted(results.items())
        if outcome not in ("exists", "created")
    ]


@dataclass(frozen=True)
class ModuleRow:
    slug: str
    title: str
    tags: List[str]
    scope: str
    chars: int
    source: str = "manual"
    size_label: str = ""
    # Prompt ids using this module. Shown on the row because a module
    # attached to nothing reaches no conversation, and until it was surfaced
    # here the only way to find that out was to open every module in turn --
    # which is exactly how the built-in modules sat unattached unnoticed.
    used_by: List[str] = field(default_factory=list)
    # Not shown in _render_row -- only used to make a module findable via
    # the search box (see filter_context_rows). Same reasoning as chars
    # above: already in memory here, so carrying it costs nothing new.
    summary: str = ""
    body: str = ""


def describe_usage(used_by: List[str]) -> str:
    """The row's usage line. Explicit about "nothing", never blank."""
    if not used_by:
        return "⚠️ not used by any prompt"
    return f"used by: {', '.join(used_by)}"


def resolve_pin_label(pin_id: str, skill_titles: "dict[str, str]") -> str:
    """A prompt id shows as-is; a skill:<uuid> id shows its title (falling
    back to the raw id if the skill's title isn't known -- e.g. a skill
    deleted after the pin was made; prompt_knowledge_overrides has no FK on
    skill ids to clean this up automatically, matching how a retired
    prompt id's stale pin already behaved before skills existed)."""
    if pin_id.startswith(SKILL_PIN_PREFIX):
        skill_id = pin_id[len(SKILL_PIN_PREFIX):]
        return f"🎬 {skill_titles.get(skill_id, skill_id)}"
    return pin_id


def resolve_pins_to_save(prompt_ids: List[str], skill_ids: List[str]) -> List[str]:
    """The full id list for one set_prompt_pins call -- prompts and skills
    share prompt_knowledge_overrides' key space, so writing them via two
    separate calls would have the second call's diff (current - selected)
    delete the first call's pins (see knowledge.py's diff_prompt_pins).
    This must always be a single call with the union."""
    return list(prompt_ids) + [skill_prompt_id(sid) for sid in skill_ids]


def build_module_rows(
    modules: List[Any],
    pins: "dict[str, List[str]] | None" = None,
    skill_titles: "dict[str, str] | None" = None,
) -> List[ModuleRow]:
    """Rows for the list. ``pins`` is module_id -> prompt/skill ids, fetched
    once. ``skill_titles`` is skill_id -> title, for resolving a skill pin
    to something readable (see resolve_pin_label).

    Optional so callers that only need identity/size (and every test that
    predates usage display) keep working; omitting it renders every module
    as unattached, which is why the page itself always passes it.
    """
    pins = pins or {}
    skill_titles = skill_titles or {}
    rows = []
    for m in sorted(modules, key=lambda m: m.slug):
        body = m.body or ""
        chars = len(body)
        source = getattr(m, "source", "manual")
        rows.append(
            ModuleRow(
                slug=m.slug, title=m.title, tags=list(m.tags), scope=m.scope,
                chars=chars, source=source,
                # A provider body has no size until it resolves, and it
                # resolves differently per caller -- a number here would be
                # a fiction.
                size_label="live" if source in PROVIDER_SOURCES else f"{chars} chars",
                summary=m.summary,
                body=body,
                used_by=[resolve_pin_label(pid, skill_titles) for pid in pins.get(m.id, [])],
            )
        )
    return rows


def group_label(source: str) -> str:
    """Which section of the list a module belongs in.

    Built-in is defined by SINGLETON_SOURCES, not by a hardcoded list here,
    so a provider added later lands in the right section without a second
    place to remember to update. External is specifically 'gdoc': content
    that lives in Drive and is only ever mirrored here, as distinct from
    Curated, which this app itself is the source of truth for.
    """
    if source in SINGLETON_SOURCES:
        return BUILT_IN_LABEL
    if source == "gdoc":
        return EXTERNAL_LABEL
    return CURATED_LABEL


def group_module_rows(rows: List[ModuleRow]) -> List[Tuple[str, List[ModuleRow]]]:
    """Bucket rows into Built-in then Curated, as ``(label, rows)``.

    Each bucket stays slug-sorted because ``rows`` already is (see
    ``build_module_rows``).
    """
    by_group: "defaultdict[str, List[ModuleRow]]" = defaultdict(list)
    for row in rows:
        by_group[group_label(row.source)].append(row)

    return [(label, by_group[label]) for label in GROUP_ORDER if label in by_group]


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
    require_body: bool = True,
    source: str = "manual",
    source_ref: str = "",
    doc_audience: "str | None" = None,
    taken_slugs: "frozenset[str] | None" = None,
) -> None:
    """Reject a module that would fail silently at render time.

    require_body=False for a provider-backed module being edited: its body
    isn't stored here (see body_is_editable), so the field is legitimately
    empty and must not block saving a title/summary/scope/mode change.

    taken_slugs, when given, is every slug already in use by another
    module: a new module with a colliding slug is rejected here, by name,
    rather than surfacing whatever raw error the database's UNIQUE
    constraint produces on insert. Omitted (None) by an edit to an existing
    module -- its slug field is locked, so it can never newly collide, and
    checking it against its own slug would reject every save.
    """
    if not slug or not title or (require_body and not body):
        raise ValueError("slug, title and body are required")
    if taken_slugs is not None and slug in taken_slugs:
        raise ValueError(
            f"the slug '{slug}' is already used by another module — choose a different one"
        )
    if not summary.strip():
        raise ValueError(
            "a summary is required: it is how you and anyone else recognise this "
            "module in the picker without opening it"
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
        "Facts the bot is told directly — the context it works from. A module "
        "attached to a prompt is inlined into that prompt in full. Built-in modules "
        "are generated by the system per request and filtered to what the caller may "
        "see; curated ones are what you write here or attach from Google Drive. "
        "Attach modules to prompts here or from the Context tab of any prompt."
    ).classes("text-caption")

    store = KnowledgeStore.from_env()
    if not store._client:  # noqa: SLF001 -- readiness check, same as the Prompts page
        ui.label(
            "⚠️ Context storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY). "
            "Modules can't be listed or saved."
        ).classes("text-warning")
        return

    # Self-healing bootstrap: directory/graph/episodic have no other way to
    # come into existence (see SINGLETON_SOURCES above), so every page load
    # ensures they're there. Cheap once they exist -- ensure_singleton_modules
    # is a no-op past the first successful run.
    for message in singleton_creation_warnings(store.ensure_singleton_modules(actor=user_email)):
        ui.notify(message, type="warning")

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
        SKILL_CATALOG.invalidate()
        # One query for every row's usage, not one per row.
        skill_titles = {s.id: s.title for s in SKILL_CATALOG.all_skills(active_only=False)}
        rows = build_module_rows(store.all_modules(), store.all_prompt_pins(), skill_titles)
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
                    f"{row.slug} · {SOURCE_LABELS.get(row.source, row.source)} · "
                    f"{row.scope} · {row.size_label}"
                ).classes("text-caption")
                ui.label(describe_usage(row.used_by)).classes(
                    "text-caption" + ("" if row.used_by else " text-warning")
                )
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
        is_built_in = source in SINGLETON_SOURCES
        ui.label("Edit module" if existing else "New module").classes("text-h6")
        if is_built_in:
            # Every field below except the prompt picker is either fixed by
            # the code or generated per request. Say that once, at the top,
            # rather than leaving an operator to work it out from a screen
            # of disabled inputs.
            ui.label(
                "Built-in module. Its content is generated by the system for each "
                "request and filtered to what that caller may see, so there is "
                "nothing to write here — but you choose which prompts use it, and "
                "you can preview what it resolves to for you below."
            ).classes("text-caption text-grey")

        slug_input = ui.input("Slug", value=existing.slug if existing else "").classes("w-full")
        slug_input.set_enabled(existing is None)  # slug is the identity; don't let it drift
        title_input = ui.input("Title", value=existing.title if existing else "").classes(
            "w-full"
        )
        if existing is None:
            # Live "slug follows title" convenience -- same idea as the
            # Skills editor's auto-derived slug (SkillBuilderService.
            # _slugify) and /learn's normalize_slug, applied here instead of
            # hidden since this dialog shows the Slug field itself. Stops
            # the moment the operator edits Slug directly (next_auto_slug).
            _last_auto_slug = [""]

            def _on_title_change(_e) -> None:
                new_slug = next_auto_slug(
                    slug_input.value, _last_auto_slug[0], title_input.value
                )
                if new_slug is not None:
                    slug_input.value = new_slug
                    _last_auto_slug[0] = new_slug

            title_input.on_value_change(_on_title_change)
        summary_input = ui.input("Summary", value=existing.summary if existing else "").classes(
            "w-full"
        )

        # A built-in's source is neither of the two an operator can pick, so
        # offer its real name rather than mislabelling it "Typed here" --
        # which is what a two-option picker defaulting to `manual` did.
        source_options = {"manual": "Typed here", "gdoc": "Google Doc or Sheet"}
        if is_built_in:
            source_options = {source: SOURCE_LABELS.get(source, source)}
        source_select = ui.select(
            source_options,
            value=source if is_built_in else ("gdoc" if source == "gdoc" else "manual"),
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

        # No mode picker: a module attached to a prompt is inlined in full,
        # full stop. Say so where the picker used to be, so the absence reads
        # as a decision rather than something missing.
        ui.label(
            "Attached to a prompt, this module's content is inlined into that "
            f"prompt in full (up to a shared {INLINE_BUDGET_CHARS:,}-character budget "
            "across everything one prompt uses)."
        ).classes("text-caption text-grey")

        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Body").classes("text-caption")
            # Defaults to Preview -- opening a module is almost always to
            # read it; Edit is one click away for the times it isn't.
            view_toggle = ui.toggle(["Edit", "Preview"], value="Preview").props("dense")
        # max-height + overflow-y here is load-bearing, not cosmetic: a
        # built-in's resolved body (directory, in particular -- see
        # providers_directory.py) can be an uncapped, unbroken wall of text
        # -- every grid, org and staff/test account in the system, one
        # paragraph, no line breaks. With only min-height, that content grew
        # this box without bound, and the dialog's own scroll (the ui.card()
        # above already has max-height + overflow-y: auto) wasn't enough to
        # keep the rest of the form -- notably Save/Cancel -- reachable or
        # even legible. Bounding both panes to the same height so switching
        # Edit/Preview doesn't jump the dialog's size.
        _BODY_PANE_STYLE = "max-height: 24rem; overflow-y: auto"
        body_input = (
            ui.codemirror(
                value=existing_body,
                language="Markdown",
                theme="vscodeLight",
                line_wrapping=True,
            )
            .classes("w-full")
            .style(_BODY_PANE_STYLE)
        )
        body_input.set_visibility(False)
        body_preview = (
            ui.markdown(existing_body)
            .classes("w-full")
            .style(f"min-height: 16rem; {_BODY_PANE_STYLE}; border: 1px solid #e0e0e0; padding: 0.5rem;")
        )

        async def _resolved_body() -> str:
            """The live body, as this operator would actually receive it.

            For a document module this always resolves the *current form
            values* (whether or not the module is already saved) rather
            than only a saved row -- an operator changing the doc link, tab
            or audience before saving should see what that change would
            actually produce, and a brand-new module has no saved row to
            resolve at all yet. Every other provider source (graph/
            directory/episodic) has no editable fields here, so those still
            resolve the saved ``existing`` row unchanged.

            Imported from `shared`, not from the orchestrator: this page runs
            in anansi_app, whose image ships only anansi_app/ and shared/
            (see anansi_app/Dockerfile). This import used to name
            orchestrator.services.jit_context_resolver, which raised
            ModuleNotFoundError here in production -- and because it runs
            while the dialog is still being built, below, the exception
            escaped before dialog.open() and the Edit button did nothing at
            all for every built-in and document-backed module.

            Never raises: a provider that cannot be built is reported as
            missing, and preview_module_body catches a failing resolve.
            """
            current_source = source_select.value
            if current_source == "gdoc":
                file_id = extract_drive_id(doc_ref_input.value)
                if not file_id:
                    return "_Paste a Google Doc or Sheet link above to preview it._"
                module = draft_gdoc_module(
                    slug=slug_input.value.strip() or (existing.slug if existing else ""),
                    title=title_input.value.strip() or (existing.title if existing else ""),
                    summary=summary_input.value.strip(),
                    file_id=file_id,
                    source_tab=doc_tab_input.value,
                    doc_audience=audience_select.value,
                )
            elif existing is None:
                return "_Save the module first to preview it._"
            else:
                module = existing
            try:
                from shared.prompts.providers import build_default_registry

                provider = build_default_registry().get(current_source)
            except Exception as e:  # noqa: BLE001 -- a broken preview must not block editing
                return f"Could not build the context providers: {e}"
            if provider is None:
                return f"No '{current_source}' provider is available in this process."
            return await preview_module_body(module, provider, user_email=user_email)

        async def _refresh_preview() -> None:
            """Re-resolve the Preview pane for the current form values.

            Safe to call whether Preview is showing or not -- it just keeps
            the pane warm, so switching to it (or pasting a new link while
            already on it) doesn't need a second trigger. A no-op for an
            editable source: Preview there just mirrors body_input, which
            _switch_view already handles the moment the toggle is clicked.
            """
            if body_is_editable(source_select.value):
                return
            body_preview.set_content("_Resolving…_")
            body_preview.set_content(await _resolved_body())

        async def _switch_view(e) -> None:
            if e.value == "Preview":
                if body_is_editable(source_select.value):
                    body_preview.set_content(body_input.value)
                else:
                    body_preview.set_content("_Resolving…_")
                    body_preview.set_content(await _resolved_body())
            body_input.set_visibility(e.value == "Edit")
            body_preview.set_visibility(e.value == "Preview")

        view_toggle.on_value_change(_switch_view)

        readonly_explanation = ui.label("").classes("text-xs text-gray-500")

        def _apply_source_view() -> None:
            """Keep the body pane's read-only state in sync with the
            selected source. Re-run on every source change, not just at
            dialog build -- for a new module the dropdown starts on "Typed
            here" and the operator can switch it to "Google Doc or Sheet"
            before ever touching Save, and the body pane must stop looking
            editable (with the explanation appearing) the moment they do.

            This used to read a `source` variable frozen at dialog-open
            time, which never noticed a later change here -- that was the
            whole bug: the Edit button stayed usable and no explanation
            showed for a Google-Doc source chosen after the dialog opened.
            """
            if body_is_editable(source_select.value):
                body_input.props(remove="readonly").classes(remove="opacity-60")
                readonly_explanation.set_visibility(False)
            else:
                body_input.props("readonly").classes("opacity-60")
                readonly_explanation.set_text(
                    _READONLY_BODY_EXPLANATIONS.get(source_select.value, "")
                )
                readonly_explanation.set_visibility(True)

        async def _on_source_change(_e) -> None:
            _apply_source_view()
            await _refresh_preview()

        source_select.on_value_change(_on_source_change)
        _apply_source_view()

        if not body_is_editable(source_select.value):
            # The dialog opens on Preview by default -- resolve once now so
            # the pane isn't just showing the (always-empty) stored body.
            body_preview.set_content(await _resolved_body())

        # A pasted link fires on_value_change on every keystroke (see
        # ui.input's own docs); blur only fires once typing/pasting is
        # actually done, so this can't hammer the Drive API per keystroke.
        doc_ref_input.on("blur", _refresh_preview, [])
        doc_tab_input.on("blur", _refresh_preview, [])

        from nicegui_app.pages.knowledge_picker import PickerRow, render_entity_select

        existing_prompt_pins = {
            pid for pid in existing_pins if not pid.startswith(SKILL_PIN_PREFIX)
        }
        prompt_rows = [
            PickerRow(
                slug=pid, title=pid, chars=0,
                checked=(pid in existing_prompt_pins),
                summary=PROMPTS.spec(pid).description,
            )
            for pid in sorted(PROMPTS.ids())
        ]

        def _refresh_audience_warning() -> None:
            audience_warning.set_text(
                describe_audience(audience_select.value, get_selected_prompts()) or ""
            )

        get_selected_prompts = render_entity_select(
            prompt_rows,
            label="Used by these prompts",
            on_change=_refresh_audience_warning,
        )

        existing_skill_pins = {
            pid[len(SKILL_PIN_PREFIX):]
            for pid in existing_pins
            if pid.startswith(SKILL_PIN_PREFIX)
        }
        all_skills = SKILL_CATALOG.all_skills(active_only=False)
        skill_rows = [
            PickerRow(
                slug=s.id, title=s.title, chars=0,
                checked=(s.id in existing_skill_pins),
                summary=f"/{s.slug} · {s.status}",
            )
            for s in sorted(all_skills, key=lambda s: s.title)
        ]
        get_selected_skills = render_entity_select(
            skill_rows, label="Used by these skills"
        )

        async def _on_audience_change(_e) -> None:
            _refresh_audience_warning()
            # Audience decides whether the document resolves at all (see
            # GDocProvider.visible_to), so a change here can flip Preview
            # from real content to "withheld" or back -- keep it live too.
            await _refresh_preview()

        audience_select.on_value_change(_on_audience_change)
        _refresh_audience_warning()

        async def save() -> None:
            chosen_source = source_select.value
            scope_value = compose_scope(scope_select.value, scope_detail.value)
            # Only a new module can collide -- an existing one's slug field
            # is locked (see slug_input.set_enabled above), so it can never
            # newly clash and this stays None, skipping the check entirely.
            taken_slugs = (
                frozenset(m.slug for m in store.all_modules()) if existing is None else None
            )
            try:
                validate_module(
                    slug=slug_input.value.strip(),
                    title=title_input.value.strip(),
                    summary=summary_input.value.strip(),
                    body=body_input.value,
                    scope=scope_value,
                    require_body=body_is_editable(chosen_source),
                    source=chosen_source,
                    source_ref=doc_ref_input.value,
                    doc_audience=audience_select.value if chosen_source == "gdoc" else None,
                    taken_slugs=taken_slugs,
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
                # `mode` is deliberately absent: nothing reads it any more
                # (see shared/prompts/knowledge.py) and the column has a
                # NOT NULL DEFAULT, so leaving it out keeps this working
                # whether or not 0029 has been applied.
                "source": chosen_source,
                "updated_by": user_email,
            }
            if chosen_source == "gdoc":
                file_id = extract_drive_id(doc_ref_input.value)
                if not file_id:
                    ui.notify("That doesn't look like a Google Doc or Sheet link", type="negative")
                    return
                # You cannot attach a document you cannot open yourself.
                from shared.utils.drive_permissions import check_access

                access = await check_access(file_id, user_email, strict=True)
                if not access.allowed:
                    ui.notify(access.reason, type="negative")
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
                    module_id,
                    resolve_pins_to_save(get_selected_prompts(), get_selected_skills()),
                    actor=user_email,
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
