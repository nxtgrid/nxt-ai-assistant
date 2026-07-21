"""Grid Design CRUD pages (NiceGUI port of grid_app/views/crud.py).

Metadata-driven list / detail / form, driven by ``EntitySpec``. Navigation uses
the same query-param scheme as the Streamlit app so links stay compatible:

    /grid/<bare>                       -> list
    /grid/<bare>?id=<pk>               -> detail
    /grid/<bare>?id=<pk>&edit=1        -> edit form
    /grid/<bare>?new=1                 -> create form
    /grid/<bare>?new=1&<fk>=<pk>       -> create form, FK pre-filled

The list view uses ``ui.aggrid`` (sort / column-filter / pagination come for
free, replacing the Streamlit manual filter+sort+paginate code). Reads run off
the event loop via ``run.io_bound``. RBAC edit gating reuses ``grid_app.lib.perms``
(email passed explicitly — no Streamlit session_state).
"""

from __future__ import annotations

import html as html_module
from functools import partial
from typing import Any
from urllib.parse import quote, urlencode

from grid_app.entities import ColumnSpec, EntitySpec, get_entity, reverse_relations
from grid_app.entities.virtual import VIRTUAL, VirtualCol, make_fetch, preload_for
from grid_app.lib import perms
from nicegui import run, ui

from nicegui_app.pages import grid_actions
from shared.grid_design import settings as grid_settings
from shared.grid_design.ids import new_id

_RELATED_LIMIT = 50


# ── navigation ─────────────────────────────────────────────────────────────────
def grid_nav(bare: str, **params: Any) -> None:
    clean = {k: str(v) for k, v in params.items() if v is not None}
    target = f"/grid/{bare}"
    if clean:
        target += "?" + urlencode(clean)
    ui.navigate.to(target)


def _open_link_html(bare: str, row_id: str) -> str:
    """Real ``<a href>`` for an "Open" grid cell (rendered via ``html_columns``).

    NOT a ``grid.on("rowClicked", ...)`` handler: AG Grid 34's ``addGlobalListener``
    forwards every event through NiceGUI's generic emitter, which always attaches
    the event's ``context`` (AG Grid's internal, circular dependency-injection
    container) to the payload. ``JSON.stringify`` throws on that circular
    reference client-side, so the event never reaches the Python handler — clicks
    silently do nothing. A plain link needs no websocket round-trip at all.
    """
    href = f"/grid/{quote(bare)}?id={quote(str(row_id))}"
    return f'<a href="{html_module.escape(href, quote=True)}">🔎 Open</a>'


# ── value formatting (ports crud._display / _ref_label, no Streamlit) ───────────
def _ref_label_map(target_bare: str) -> dict[str, str]:
    spec = get_entity(target_bare)
    if not spec:
        return {}
    label_col = spec.label_column
    rows = spec.repo().list(active_only=True, order_by=label_col)
    return {r.get("id", ""): str(r.get(label_col) or r.get("id", "")) for r in rows}


def _display(row: dict, col: ColumnSpec, ref_maps: dict[str, dict[str, str]]) -> str:
    val = row.get(col.name)
    if val is None or val == "":
        return "—"
    if col.ref:
        return ref_maps.get(col.ref, {}).get(str(val), str(val))
    if col.widget == "bool":
        return "✅" if val in (True, "TRUE", "true", 1, "1") else "—"
    if col.widget == "datetime" and isinstance(val, str) and "T" in val:
        return val[:10]
    return str(val)


def _vval(vc: VirtualCol, r: dict, fetch) -> str:
    try:
        v = vc.compute(r, fetch)
    except Exception:
        v = None
    if v is None:
        return "—"
    if vc.widget == "number":
        try:
            return f"{float(v):,.4g}"
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def _db_configured() -> bool:
    return grid_settings.is_db_configured()


# ── dispatcher ──────────────────────────────────────────────────────────────────
async def render(user: dict, bare: str, params: dict[str, str]) -> None:
    email = user.get("email", "")
    spec = get_entity(bare)
    if spec is None:
        ui.label("Unknown entity.").classes("text-negative")
        return
    if not perms.can_view_grid(email):
        ui.label("🔒 You don't have access to this section.").classes("text-lg text-bold")
        return
    if not _db_configured():
        ui.label("⚠️ Database not configured. Set CHAT_DB_URL and CHAT_DB_SERVICE_KEY.").classes(
            "text-negative"
        )
        return

    if params.get("new"):
        await _render_form(spec, None, params, email)
    elif params.get("id"):
        row = await run.io_bound(lambda: spec.repo().get(params["id"]))
        if not row:
            ui.label("Record not found.").classes("text-negative")
            ui.button("← Back", on_click=lambda: grid_nav(spec.bare)).props("flat")
            return
        if params.get("edit"):
            await _render_form(spec, row, params, email)
        else:
            await _render_detail(spec, row, email)
    else:
        await _render_list(spec, email)


# ── list view (ui.aggrid) ───────────────────────────────────────────────────────
async def _render_list(spec: EntitySpec, email: str) -> None:
    can_write = perms.can_edit(spec.bare, email)

    with ui.row().classes("items-center justify-between w-full"):
        ui.label(f"{spec.icon} {spec.label}").classes("text-h5")
        if can_write:
            ui.button("➕ New", on_click=lambda: grid_nav(spec.bare, new=1)).props("color=primary")

    rows = await run.io_bound(lambda: spec.repo().list(active_only=True))
    ui.label(f"{len(rows)} record(s)").classes("text-caption")
    if not rows:
        ui.label("No records found.").classes("text-italic")
        return

    list_cols = spec.list_columns()
    vlist_cols = [vc for vc in VIRTUAL.get(spec.bare, []) if vc.in_list]
    max_phys = max(2, 8 - len(vlist_cols))
    list_cols = list_cols[:max_phys]

    # Pre-resolve ref label maps once for all ref columns present.
    ref_maps: dict[str, dict[str, str]] = {}
    for c in list_cols:
        if c.ref and c.ref not in ref_maps:
            ref_maps[c.ref] = await run.io_bound(partial(_ref_label_map, c.ref))

    list_fetch = await run.io_bound(lambda: preload_for(spec.bare, rows)) if vlist_cols else None

    col_defs = (
        [{"headerName": "", "field": "_open", "sortable": False, "filter": False, "width": 90}]
        + [
            {"headerName": c.label, "field": c.name, "sortable": True, "filter": True}
            for c in list_cols
        ]
        + [
            {"headerName": vc.label, "field": vc.name, "sortable": True, "filter": True}
            for vc in vlist_cols
        ]
    )

    row_data = []
    for r in rows:
        entry: dict[str, Any] = {"_id": r["id"], "_open": _open_link_html(spec.bare, r["id"])}
        for c in list_cols:
            entry[c.name] = _display(r, c, ref_maps)
        for vc in vlist_cols:
            entry[vc.name] = _vval(vc, r, list_fetch)
        row_data.append(entry)

    search = ui.input("🔍 Search").classes("w-64")
    grid = (
        ui.aggrid(
            {
                "columnDefs": col_defs,
                "rowData": row_data,
                "pagination": True,
                "paginationPageSize": 25,
                "defaultColDef": {"resizable": True, "flex": 1, "minWidth": 120},
            },
            html_columns=[0],
        )
        .classes("w-full")
        .style("height: 600px")
    )
    search.on_value_change(
        lambda: grid.run_grid_method("setGridOption", "quickFilterText", search.value)
    )


# ── detail view ─────────────────────────────────────────────────────────────────
async def _render_detail(spec: EntitySpec, row: dict, email: str) -> None:
    can_write = perms.can_edit(spec.bare, email)
    title = row.get(spec.label_column) or row.get("id")

    with ui.row().classes("items-center justify-between w-full"):
        ui.label(f"{spec.icon} {title}").classes("text-h5")
        with ui.row().classes("gap-2"):
            ui.button("← Back", on_click=lambda: grid_nav(spec.bare)).props("flat")
            if can_write:
                ui.button(
                    "✏️ Edit", on_click=lambda: grid_nav(spec.bare, id=row["id"], edit=1)
                ).props("flat")
                ui.button("🗑️ Delete", on_click=lambda: _confirm_delete(spec, row)).props(
                    "flat color=negative"
                )

    # entity-specific write actions (editors only)
    acts = grid_actions.actions_for(spec.bare) if can_write else []
    if acts:
        ui.label("Actions").classes("text-bold q-mt-sm")
        with ui.row().classes("gap-2 flex-wrap"):
            for label, fn in acts:
                ui.button(label, on_click=lambda f=fn, la=label: _run_action(f, la, row)).props(
                    "outline"
                )

    ui.separator()

    # Pre-resolve ref labels for ref columns.
    ref_cols = [c for c in spec.columns if c.ref]
    ref_maps: dict[str, dict[str, str]] = {}
    for c in ref_cols:
        if c.ref not in ref_maps:
            ref_maps[c.ref] = await run.io_bound(partial(_ref_label_map, c.ref))

    non_image = [c for c in spec.columns if c.widget != "image"]
    image_cols = [c for c in spec.columns if c.widget == "image"]

    with ui.grid(columns=2).classes("w-full gap-2"):
        for c in non_image:
            with ui.column().classes("gap-0"):
                ui.label(c.label).classes("text-bold text-caption")
                if c.ref and row.get(c.name):
                    ref_id = row.get(c.name)
                    ui.link(
                        ref_maps.get(c.ref, {}).get(str(ref_id), str(ref_id)),
                        f"/grid/{c.ref}?id={ref_id}",
                    )
                else:
                    ui.label(_display(row, c, ref_maps))

    for c in image_cols:
        ui.label(c.label).classes("text-bold text-caption")
        val = row.get(c.name)
        if val and str(val).startswith("http"):
            ui.image(str(val)).classes("w-96")
        else:
            ui.label("—")

    await _render_computed(spec, row)
    await _render_related(spec, row, email)


async def _render_computed(spec: EntitySpec, row: dict) -> None:
    vcols = VIRTUAL.get(spec.bare, [])
    if not vcols:
        return
    fetch = make_fetch()
    computed = []
    for vc in vcols:
        try:
            val = vc.compute(row, fetch)
        except Exception:
            val = None
        if val not in (None, "", 0.0):
            computed.append((vc, val))
    if not computed:
        return
    ui.separator()
    ui.label("Computed").classes("text-bold q-mt-sm")
    with ui.grid(columns=2).classes("w-full gap-2"):
        for vc, v in computed:
            if vc.widget == "image":
                continue
            with ui.column().classes("gap-0"):
                ui.label(vc.label).classes("text-bold text-caption")
                if vc.widget == "number":
                    ui.label(f"{v:,.4g}")
                elif vc.widget == "bool":
                    ui.label("✅" if v else "—")
                else:
                    ui.label(str(v))
    for vc, v in computed:
        if vc.widget == "image" and str(v).startswith("http"):
            ui.label(vc.label).classes("text-bold text-caption")
            ui.image(str(v)).classes("w-96")


async def _render_related(spec: EntitySpec, row: dict, email: str) -> None:
    relations = reverse_relations(spec)
    if not relations:
        return
    ui.separator()
    ui.label("Related").classes("text-bold q-mt-sm")
    for child_spec, fk_col in relations:
        await _render_related_list(row["id"], child_spec, fk_col, email)


async def _render_related_list(
    parent_id: str, child_spec: EntitySpec, fk_col: ColumnSpec, email: str
) -> None:
    all_rows = await run.io_bound(
        lambda: child_spec.repo().list(active_only=True, filters={fk_col.name: parent_id})
    )
    if not all_rows:
        return

    list_cols = [c for c in child_spec.list_columns() if c.name != fk_col.name]
    if not list_cols:
        list_cols = child_spec.list_columns()
    can_write = perms.can_edit(child_spec.bare, email)

    ref_maps: dict[str, dict[str, str]] = {}
    for c in list_cols:
        if c.ref and c.ref not in ref_maps:
            ref_maps[c.ref] = await run.io_bound(partial(_ref_label_map, c.ref))

    label = f"{child_spec.icon} {child_spec.label} ({len(all_rows)})"
    with ui.expansion(label, value=len(all_rows) <= 5).classes("w-full"):
        if can_write:
            ui.button(
                f"➕ New {child_spec.label}",
                on_click=lambda: grid_nav(child_spec.bare, new=1, **{fk_col.name: parent_id}),
            ).props("flat dense")
        col_defs = [
            {"headerName": "", "field": "_open", "sortable": False, "filter": False, "width": 90}
        ] + [
            {"headerName": c.label, "field": c.name, "sortable": True, "filter": True}
            for c in list_cols
        ]
        row_data = []
        for r in all_rows[:_RELATED_LIMIT]:
            entry: dict[str, Any] = {
                "_id": r["id"],
                "_open": _open_link_html(child_spec.bare, r["id"]),
            }
            for c in list_cols:
                entry[c.name] = _display(r, c, ref_maps)
            row_data.append(entry)
        ui.aggrid(
            {
                "columnDefs": col_defs,
                "rowData": row_data,
                "defaultColDef": {"resizable": True, "flex": 1, "minWidth": 110},
            },
            html_columns=[0],
        ).classes("w-full").style("height: 300px")

        if len(all_rows) > _RELATED_LIMIT:
            ui.label(f"Showing first {_RELATED_LIMIT} of {len(all_rows)}.").classes("text-caption")


async def _run_action(fn, label: str, row: dict) -> None:
    ui.notify(f"{label}…")
    try:
        ok, message = await run.io_bound(lambda: fn(row))
    except Exception as e:  # noqa: BLE001 - surface engine failure to operator
        ui.notify(f"{label} failed: {e}", type="negative")
        return
    ui.notify(message, type="positive" if ok else "negative", multi_line=True)


def _confirm_delete(spec: EntitySpec, row: dict) -> None:
    with ui.dialog() as dialog, ui.card():
        ui.label(f"Delete “{row.get(spec.label_column) or row.get('id')}”?").classes("text-bold")

        async def do_delete() -> None:
            await run.io_bound(lambda: spec.repo().soft_delete(row["id"]))
            dialog.close()
            ui.notify("Deleted", type="positive")
            grid_nav(spec.bare)

        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Delete", on_click=do_delete).props("color=negative")
    dialog.open()


# ── form view (create / edit) ────────────────────────────────────────────────────
async def _render_form(
    spec: EntitySpec, row: dict | None, params: dict[str, str], email: str
) -> None:
    # Defense-in-depth: a non-editor reaching ?new=1 / ?edit=1 gets read-only.
    if not perms.can_edit(spec.bare, email):
        ui.label("🔒 You don't have edit access to this table.").classes("text-warning")
        if row is not None:
            await _render_detail(spec, row, email)
        else:
            ui.button("← Back", on_click=lambda: grid_nav(spec.bare)).props("flat")
        return

    is_new = row is None
    ui.label(f"{spec.icon} {'New' if is_new else 'Edit'} {spec.label}").classes("text-h5")
    row = dict(row or {})
    if is_new:
        for c in spec.columns:
            if c.widget == "ref" and params.get(c.name):
                row[c.name] = params[c.name]

    # Build ref options for ref columns.
    ref_opts: dict[str, dict[str, str]] = {}
    for c in spec.columns:
        if c.editable and c.widget == "ref" and c.ref not in ref_opts:
            ref_opts[c.ref] = await run.io_bound(partial(_ref_label_map, c.ref))

    inputs: dict[str, Any] = {}
    with ui.column().classes("w-full gap-2").style("max-width: 640px"):
        for c in spec.columns:
            if not c.editable:
                continue
            inputs[c.name] = _widget_input(c, row.get(c.name), ref_opts)

    async def save() -> None:
        values = {name: _read_value(inputs[name]) for name in inputs}
        repo = spec.repo()
        if is_new:
            rec = {k: v for k, v in values.items() if v is not None}
            rec["id"] = new_id()
            rec.setdefault("active", True)
            saved = await run.io_bound(lambda: repo.insert(rec))
            ui.notify("Created", type="positive")
            grid_nav(spec.bare, id=saved.get("id", rec["id"]))
        else:
            await run.io_bound(lambda: repo.update(row["id"], values))
            ui.notify("Saved", type="positive")
            grid_nav(spec.bare, id=row["id"])

    with ui.row().classes("gap-2 q-mt-md"):
        ui.button("💾 Save", on_click=save).props("color=primary")
        ui.button(
            "← Cancel",
            on_click=lambda: grid_nav(spec.bare, id=row.get("id")),
        ).props("flat")


def _widget_input(c: ColumnSpec, current: Any, ref_opts: dict[str, dict[str, str]]):
    if c.widget == "ref":
        ref_map = {"": "—", **ref_opts.get(c.ref, {})}
        value = current if current in ref_map else ""
        return ui.select(ref_map, value=value, label=c.label, with_input=True).classes("w-full")
    if c.widget == "enum":
        enum_opts = [""] + (c.options or [])
        value = current if current in enum_opts else ""
        return ui.select(enum_opts, value=value, label=c.label).classes("w-full")
    if c.widget == "bool":
        checked = bool(current) and current not in ("FALSE", "false", "0")
        return ui.switch(c.label, value=checked)
    if c.widget == "number":
        try:
            value = float(current) if current not in (None, "") else None
        except (TypeError, ValueError):
            value = None
        return ui.number(c.label, value=value).classes("w-full")
    if c.widget == "textarea":
        return ui.textarea(c.label, value=str(current or "")).classes("w-full")
    if c.widget == "image":
        return ui.input(f"{c.label} (URL)", value=str(current or "")).classes("w-full")
    return ui.input(c.label, value=str(current or "")).classes("w-full")


def _read_value(widget) -> Any:
    """Normalize a widget value back to a storable scalar (empty string -> None)."""
    val = getattr(widget, "value", None)
    if isinstance(val, str):
        stripped = val.strip()
        return stripped or None
    return val
