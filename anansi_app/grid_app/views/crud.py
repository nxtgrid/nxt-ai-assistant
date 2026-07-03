"""Generic list / detail / form renderers shared by every entity.

Driven entirely by ``EntitySpec`` metadata — adding or changing a table needs no
new view code. Navigation is encoded in query params:

    ?page=<bare>                      -> list
    ?page=<bare>&id=<pk>              -> detail
    ?page=<bare>&id=<pk>&edit=1       -> edit form
    ?page=<bare>&new=1               -> create form
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from grid_app.entities import ColumnSpec, EntitySpec, get_entity, reverse_relations
from grid_app.entities.virtual import VIRTUAL, VirtualCol, make_fetch, preload_for
from grid_app.lib import perms
from grid_app.lib.ids import new_id

PAGE_SIZE = 25
_RELATED_PAGE_SIZE = 50

# For these entities the 🔎/✏️ pair is replaced with a "→ Design" button that
# navigates to the owning design — they have no meaningful standalone detail page.
_VIA_DESIGN: set[str] = {"design_subassemblies", "bom_items"}

# Entities rendered as an in-place editable grid (st.data_editor) instead of
# the standard paginated list. Toggled per-session with an "Edit mode" switch.
_EDITABLE_GRID_ENTITIES: set[str] = {"wp_per_conn_lookup"}


# ── navigation ────────────────────────────────────────────────────────────────
def nav(**params: Any) -> None:
    """Replace query params and rerun (drops keys whose value is None)."""
    st.query_params.clear()
    for k, v in params.items():
        if v is not None:
            st.query_params[k] = str(v)
    st.rerun()


# ── ref helpers ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=60, show_spinner=False)
def _ref_rows(target_bare: str) -> list[tuple[str, str]]:
    """(id, label) pairs for a referenced table, active rows only."""
    spec = get_entity(target_bare)
    if not spec:
        return []
    label_col = spec.label_column
    rows = spec.repo().list(active_only=True, order_by=label_col)
    return [(r.get("id", ""), str(r.get(label_col) or r.get("id", ""))) for r in rows]


def _ref_label(target_bare: str, value: str | None) -> str:
    if not value:
        return "—"
    for rid, label in _ref_rows(target_bare):
        if rid == value:
            return str(label)
    return value  # dangling ref — show raw id


def _display(spec: EntitySpec, row: dict, col) -> str:
    val = row.get(col.name)
    if val is None or val == "":
        return "—"
    if col.ref:
        return _ref_label(col.ref, str(val))
    if col.widget == "bool":
        return "✅" if val in (True, "TRUE", "true", 1, "1") else "—"
    if col.widget == "datetime" and isinstance(val, str) and "T" in val:
        return val[:10]  # show YYYY-MM-DD only in list/detail
    return str(val)


# ── editable grid (for small lookup tables) ───────────────────────────────────
def _render_editable_grid(spec: EntitySpec, rows: list[dict]) -> None:
    import pandas as pd

    edit_cols = [c for c in spec.columns if c.editable and c.name != "active"]
    row_ids = [r["id"] for r in rows]
    df = pd.DataFrame([{c.name: r.get(c.name) for c in edit_cols} for r in rows])

    col_cfg: dict[str, Any] = {}
    for c in edit_cols:
        if c.widget == "number":
            col_cfg[c.name] = st.column_config.NumberColumn(c.label)
        else:
            col_cfg[c.name] = st.column_config.TextColumn(c.label)

    edit_mode = perms.can_edit(spec.bare) and st.toggle("Edit mode", key=f"grid_edit_{spec.bare}")

    if not edit_mode:
        st.dataframe(df, column_config=col_cfg, use_container_width=True, hide_index=True)
        return

    edited = st.data_editor(
        df,
        column_config=col_cfg,
        use_container_width=True,
        hide_index=True,
        key=f"editor_{spec.bare}",
    )
    if st.button("💾 Save changes", type="primary", key=f"save_{spec.bare}"):
        repo = spec.repo()
        saved = 0
        for i in range(len(df)):
            changes = {
                c.name: edited.iloc[i][c.name]
                for c in edit_cols
                if edited.iloc[i][c.name] != df.iloc[i][c.name]
            }
            if changes:
                repo.update(row_ids[i], changes)
                saved += 1
        st.toast(f"Saved {saved} row(s)")
        st.rerun()


# ── sort / filter helpers ────────────────────────────────────────────────────


def _sort_rows(rows: list[dict], col_name: str | None, desc: bool, spec: EntitySpec) -> list[dict]:
    if not col_name:
        return rows
    c = spec.col(col_name)
    numeric = c and c.widget == "number"

    def key_fn(r):
        v = r.get(col_name)
        if v is None or v == "":
            return (1, 0.0) if numeric else (1, "")
        if numeric:
            try:
                return (0, float(v))
            except (TypeError, ValueError):
                return (1, 0.0)
        return (0, str(v).lower())

    return sorted(rows, key=key_fn, reverse=desc)


def _active_filters(spec: EntitySpec, list_cols: list) -> dict[str, Any]:
    """Read current filter widget values from session_state; return only non-empty ones."""
    active: dict[str, Any] = {}
    for c in list_cols:
        if c.widget == "number":
            lo_s = st.session_state.get(f"flo_{spec.bare}_{c.name}", "")
            hi_s = st.session_state.get(f"fhi_{spec.bare}_{c.name}", "")
            try:
                lo = float(lo_s) if lo_s else None
            except ValueError:
                lo = None
            try:
                hi = float(hi_s) if hi_s else None
            except ValueError:
                hi = None
            if lo is not None or hi is not None:
                active[c.name] = (lo, hi)
        elif c.widget == "enum":
            sel = st.session_state.get(f"fenum_{spec.bare}_{c.name}", [])
            if sel:
                active[c.name] = sel
        elif c.widget == "bool":
            choice = st.session_state.get(f"fbool_{spec.bare}_{c.name}", "All")
            if choice != "All":
                active[c.name] = choice == "Yes"
        else:
            val = st.session_state.get(f"ftxt_{spec.bare}_{c.name}", "")
            if val:
                active[c.name] = val
    return active


def _apply_filters(rows: list[dict], active: dict[str, Any], spec: EntitySpec) -> list[dict]:
    if not active:
        return rows
    out = []
    for r in rows:
        ok = True
        for col_name, fval in active.items():
            c = spec.col(col_name)
            rval = r.get(col_name)
            if c and c.widget == "number":
                lo, hi = fval
                try:
                    n = float(rval) if rval not in (None, "") else None
                except (TypeError, ValueError):
                    n = None
                if n is None or (lo is not None and n < lo) or (hi is not None and n > hi):
                    ok = False
                    break
            elif c and c.widget == "enum":
                if str(rval) not in fval:
                    ok = False
                    break
            elif c and c.widget == "bool":
                actual = rval in (True, "TRUE", "true", 1, "1")
                if actual != fval:
                    ok = False
                    break
            else:  # text, ref, datetime
                if str(fval).lower() not in str(rval or "").lower():
                    ok = False
                    break
        if ok:
            out.append(r)
    return out


def _render_filter_bar(spec: EntitySpec, list_cols: list) -> int:
    """Render per-column filter widgets. Returns count of active filters."""
    n_active = len(_active_filters(spec, list_cols))
    label = f"🔍 Filters ({n_active} active)" if n_active else "🔍 Filters"
    with st.expander(label, expanded=n_active > 0):
        n_cols = min(len(list_cols), 4)
        cols = st.columns(n_cols)
        for i, c in enumerate(list_cols):
            with cols[i % n_cols]:
                if c.widget == "number":
                    st.text_input(
                        f"Min {c.label}", key=f"flo_{spec.bare}_{c.name}", placeholder="min…"
                    )
                    st.text_input(
                        f"Max {c.label}", key=f"fhi_{spec.bare}_{c.name}", placeholder="max…"
                    )
                elif c.widget == "enum":
                    st.multiselect(c.label, c.options or [], key=f"fenum_{spec.bare}_{c.name}")
                elif c.widget == "bool":
                    st.selectbox(c.label, ["All", "Yes", "No"], key=f"fbool_{spec.bare}_{c.name}")
                else:
                    st.text_input(
                        c.label, placeholder="contains…", key=f"ftxt_{spec.bare}_{c.name}"
                    )
        if n_active and st.button("✕ Clear filters", key=f"clr_{spec.bare}"):
            for c in list_cols:
                for suffix in ("flo", "fhi", "fenum", "fbool", "ftxt"):
                    st.session_state.pop(f"{suffix}_{spec.bare}_{c.name}", None)
            st.rerun()
    return n_active


# ── list view ─────────────────────────────────────────────────────────────────
_LIST_CSS = """
<style>
/* Page scrolls horizontally if columns don't fit */
section[data-testid="stMain"],
div[data-testid="stMainBlockContainer"] {
    overflow-x: auto !important;
}
/* Row: keep columns on one line */
div[data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important;
}
/* Only apply table min-width to header + data rows (positions 5+).
   Open col ~70px (1/25×1750), data cols ~210px (3/25×1750 ≈ 30 chars). */
div[data-testid="stVerticalBlock"] > div[data-testid="stLayoutWrapper"]:nth-child(n+5) div[data-testid="stHorizontalBlock"] {
    min-width: 1750px !important;
}
div[data-testid="stVerticalBlock"] > div[data-testid="stLayoutWrapper"]:nth-child(n+5) div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
    flex-shrink: 0 !important;
    overflow-wrap: break-word !important;
    word-break: break-word !important;
}
/* Header row (always nth-child 5 in the list stVerticalBlock): bold text */
div[data-testid="stVerticalBlock"] > div[data-testid="stLayoutWrapper"]:nth-child(5) p,
div[data-testid="stVerticalBlock"] > div[data-testid="stLayoutWrapper"]:nth-child(5) button p {
    font-weight: 700 !important;
}
/* Alternate row shading: data rows start at nth-child(6), shade every other */
div[data-testid="stVerticalBlock"] > div[data-testid="stLayoutWrapper"]:nth-child(n+6):nth-child(even) {
    background-color: rgba(0, 0, 0, 0.03) !important;
    border-radius: 4px !important;
}
</style>
"""


def render_list(spec: EntitySpec) -> None:
    st.markdown(_LIST_CSS, unsafe_allow_html=True)
    can_write = perms.can_edit(spec.bare)
    top = st.columns([6, 2, 2])
    top[0].subheader(f"{spec.icon} {spec.label}")
    if can_write and top[2].button("➕ New", use_container_width=True, type="primary"):
        nav(page=spec.bare, new=1)

    repo = spec.repo()
    rows = repo.list(active_only=True)  # sorted client-side below

    query = top[1].text_input(
        "Search", key=f"search_{spec.bare}", label_visibility="collapsed", placeholder="Search…"
    )
    if query:
        q = query.lower()
        rows = [r for r in rows if any(q in str(v).lower() for v in r.values())]

    if spec.bare in _EDITABLE_GRID_ENTITIES:
        st.caption(f"{len(rows)} record(s)")
        if not rows:
            st.info("No records yet.")
            return
        _render_editable_grid(spec, rows)
        return

    list_cols = spec.list_columns()

    # per-column filters (renders widgets; apply after)
    _render_filter_bar(spec, list_cols)
    rows = _apply_filters(rows, _active_filters(spec, list_cols), spec)

    # sort — default to label_column asc on first visit
    sort_key = f"sort_{spec.bare}"
    if sort_key not in st.session_state:
        lc = spec.label_column
        st.session_state[sort_key] = (lc if lc != "id" else None, False)
    sort_col, sort_desc = st.session_state[sort_key]
    rows = _sort_rows(rows, sort_col, sort_desc, spec)

    st.caption(f"{len(rows)} record(s)")
    if not rows:
        st.info("No records found.")
        return

    page_key = f"page_{spec.bare}"
    page = st.session_state.get(page_key, 0)
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages - 1)
    window = rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    # Virtual columns marked in_list=True — batch-load FK data for the page window
    vlist_cols = [vc for vc in VIRTUAL.get(spec.bare, []) if vc.in_list]
    # Trim physical cols to keep total ≤ 8
    max_phys = max(2, 8 - len(vlist_cols))
    list_cols = list_cols[:max_phys]
    list_fetch = preload_for(spec.bare, window) if vlist_cols else None

    def _vval(vc: VirtualCol, r: dict) -> str:
        try:
            v = vc.compute(r, list_fetch)
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

    all_list_cols = list_cols + vlist_cols  # virtual cols appended after physical

    # column header buttons — click to sort asc → desc → off (physical only)
    head = st.columns([1] + [3] * len(all_list_cols))
    head[0].markdown("**Open**")
    for i, c in enumerate(list_cols):
        icon = (" ▲" if not sort_desc else " ▼") if sort_col == c.name else ""
        if head[i + 1].button(
            f"{c.label}{icon}", key=f"shdr_{spec.bare}_{c.name}", use_container_width=True
        ):
            if sort_col == c.name and not sort_desc:
                st.session_state[sort_key] = (c.name, True)
            elif sort_col == c.name and sort_desc:
                st.session_state[sort_key] = (None, False)
            else:
                st.session_state[sort_key] = (c.name, False)
            st.session_state[page_key] = 0
            st.rerun()
    for i, vc in enumerate(vlist_cols):
        head[len(list_cols) + i + 1].markdown(f"**{vc.label}**")

    for r in window:
        cells = st.columns([1] + [3] * len(all_list_cols))
        if cells[0].button("🔎", key=f"open_{spec.bare}_{r['id']}"):
            nav(page=spec.bare, id=r["id"])
        for i, c in enumerate(list_cols):
            cells[i + 1].write(_display(spec, r, c))
        for i, vc in enumerate(vlist_cols):
            cells[len(list_cols) + i + 1].write(_vval(vc, r))

    if pages > 1:
        nav_cols = st.columns([1, 2, 1])
        if nav_cols[0].button("← Prev", disabled=page == 0, key=f"prev_{spec.bare}"):
            st.session_state[page_key] = page - 1
            st.rerun()
        nav_cols[1].markdown(
            f"<div style='text-align:center'>Page {page + 1} / {pages}</div>",
            unsafe_allow_html=True,
        )
        if nav_cols[2].button("Next →", disabled=page >= pages - 1, key=f"next_{spec.bare}"):
            st.session_state[page_key] = page + 1
            st.rerun()


# ── related-list helper (used by detail view) ────────────────────────────────────
def _render_related_list(parent_id: str, child_spec: EntitySpec, fk_col: ColumnSpec) -> None:
    all_rows = child_spec.repo().list(active_only=True, filters={fk_col.name: parent_id})
    if not all_rows:
        return

    # drop the FK column itself — it just echoes the parent's id
    list_cols = [c for c in child_spec.list_columns() if c.name != fk_col.name]
    if not list_cols:
        list_cols = child_spec.list_columns()

    # On the design's own detail page, keep normal open/edit buttons; elsewhere
    # route design_subassemblies and bom_items to their owning design.
    via_design = child_spec.bare in _VIA_DESIGN and st.query_params.get("page") != "designs"
    can_write = perms.can_edit(child_spec.bare)
    rel_key = f"{child_spec.bare}_{fk_col.name}_{parent_id}"

    with st.expander(
        f"{child_spec.icon} {child_spec.label} ({len(all_rows)})", expanded=len(all_rows) <= 5
    ):
        # ── search bar ────────────────────────────────────────────────────────
        query = st.text_input(
            "search",
            placeholder="Search by name, qty, type, price…",
            key=f"relsearch_{rel_key}",
            label_visibility="collapsed",
        )

        # ── filter ────────────────────────────────────────────────────────────
        if query:
            ql = query.lower()
            rows: list[dict] = []
            for r in all_rows:
                if any(ql in str(v).lower() for v in r.values()):
                    rows.append(r)
                    continue
                for c in list_cols:
                    if c.ref and ql in _display(child_spec, r, c).lower():
                        rows.append(r)
                        break
        else:
            rows = all_rows

        # ── reset page when search changes ────────────────────────────────────
        page_key = f"relpage_{rel_key}"
        prev_q_key = f"relpq_{rel_key}"
        if st.session_state.get(prev_q_key) != query:
            st.session_state[page_key] = 0
            st.session_state[prev_q_key] = query

        total = len(rows)
        page = st.session_state.get(page_key, 0)
        pages = max(1, (total + _RELATED_PAGE_SIZE - 1) // _RELATED_PAGE_SIZE)
        page = min(page, pages - 1)
        window = rows[page * _RELATED_PAGE_SIZE : (page + 1) * _RELATED_PAGE_SIZE]

        # ── header ────────────────────────────────────────────────────────────
        head = st.columns([1, 1] + [3] * len(list_cols))
        if can_write and head[0].button(
            "➕", key=f"relnew_{rel_key}", help=f"New {child_spec.label}", use_container_width=True
        ):
            nav(page=child_spec.bare, new=1, **{fk_col.name: parent_id})
        head[1].markdown("**Design**" if via_design else ("**Edit**" if can_write else ""))
        for i, c in enumerate(list_cols):
            head[i + 2].markdown(f"**{c.label}**")

        # ── rows ──────────────────────────────────────────────────────────────
        for r in window:
            cells = st.columns([1, 1] + [3] * len(list_cols))
            if via_design:
                design_id = r.get("design")
                if design_id and cells[1].button(
                    "→ Design", key=f"reldes_{rel_key}_{r['id']}", use_container_width=True
                ):
                    nav(page="designs", id=design_id)
            else:
                if cells[0].button("🔎", key=f"rel_{rel_key}_{r['id']}"):
                    nav(page=child_spec.bare, id=r["id"])
                if can_write and cells[1].button("✏️", key=f"reledit_{rel_key}_{r['id']}"):
                    nav(page=child_spec.bare, id=r["id"], edit=1)
            for i, c in enumerate(list_cols):
                cells[i + 2].write(_display(child_spec, r, c))

        # ── pagination controls ───────────────────────────────────────────────
        if pages > 1:
            pcols = st.columns([1, 3, 1])
            if pcols[0].button("← Prev", disabled=page == 0, key=f"relprev_{rel_key}"):
                st.session_state[page_key] = page - 1
                st.rerun()
            match_label = f" · {total} match" if query else ""
            pcols[1].markdown(
                f"<div style='text-align:center'>Page {page + 1} / {pages}{match_label}</div>",
                unsafe_allow_html=True,
            )
            if pcols[2].button("Next →", disabled=page >= pages - 1, key=f"relnext_{rel_key}"):
                st.session_state[page_key] = page + 1
                st.rerun()
        elif query and total < len(all_rows):
            st.caption(f"{total} of {len(all_rows)} match")


# ── detail view ─────────────────────────────────────────────────────────────────
def render_detail(spec: EntitySpec, row: dict) -> None:
    title = row.get(spec.label_column) or row.get("id")
    can_write = perms.can_edit(spec.bare)
    bar = st.columns([5, 1.4, 1.4, 1.4])
    bar[0].subheader(f"{spec.icon} {title}")
    if bar[1].button("← Back", use_container_width=True):
        nav(page=spec.bare)
    if can_write and bar[2].button("✏️ Edit", use_container_width=True):
        nav(page=spec.bare, id=row["id"], edit=1)
    if can_write and bar[3].button("🗑️ Delete", use_container_width=True):
        spec.repo().soft_delete(row["id"])
        st.toast("Deleted")
        nav(page=spec.bare)

    # entity-specific actions (compute engines that WRITE) — editors only.
    from grid_app.actions import actions_for

    acts = actions_for(spec.bare) if can_write else []
    if acts:
        st.markdown("##### Actions")
        acols = st.columns(len(acts))
        for i, (label, fn) in enumerate(acts):
            if acols[i].button(label, key=f"act_{spec.bare}_{i}", use_container_width=True):
                with st.spinner(f"{label}…"):
                    fn(row)
                st.rerun()

    st.divider()
    non_image = [c for c in spec.columns if c.widget != "image"]
    image_cols = [c for c in spec.columns if c.widget == "image"]

    left, right = st.columns(2)
    for i, c in enumerate(non_image):
        target = left if i % 2 == 0 else right
        with target:
            if c.ref:
                rc = st.columns([2, 3])
                rc[0].markdown(f"**{c.label}**")
                if row.get(c.name) and rc[1].button(
                    _ref_label(c.ref, row.get(c.name)), key=f"reflink_{c.name}"
                ):
                    nav(page=c.ref, id=row.get(c.name))
                if not row.get(c.name):
                    rc[1].write("—")
            else:
                st.markdown(f"**{c.label}**  \n{_display(spec, row, c)}")

    for c in image_cols:
        val = row.get(c.name)
        st.markdown(f"**{c.label}**")
        if val and str(val).startswith("http"):
            st.image(val, width=400)
        else:
            st.write("—")

    # ── virtual (computed) columns ────────────────────────────────────────────
    vcols = VIRTUAL.get(spec.bare, [])
    if vcols:
        fetch = make_fetch()
        computed: list[tuple[VirtualCol, Any]] = []
        for vc in vcols:
            try:
                val = vc.compute(row, fetch)
            except Exception:
                val = None
            computed.append((vc, val))
        # Only show if at least one has a value
        non_null = [(vc, v) for vc, v in computed if v not in (None, "", 0.0)]
        if non_null:
            st.divider()
            st.markdown("##### Computed")
            vimg = [(vc, v) for vc, v in non_null if vc.widget == "image"]
            vnon = [(vc, v) for vc, v in non_null if vc.widget != "image"]
            left2, right2 = st.columns(2)
            for i, (vc, v) in enumerate(vnon):
                with left2 if i % 2 == 0 else right2:
                    if vc.widget == "number":
                        st.markdown(f"**{vc.label}**  \n{v:,.4g}")
                    elif vc.widget == "bool":
                        st.markdown(f"**{vc.label}**  \n{'✅' if v else '—'}")
                    else:
                        st.markdown(f"**{vc.label}**  \n{v}")
            for vc, v in vimg:
                st.markdown(f"**{vc.label}**")
                if str(v).startswith("http"):
                    st.image(v, width=400)
                else:
                    st.write(v)

    # ── related lists ──────────────────────────────────────────────────────────
    relations = reverse_relations(spec)
    if relations:
        st.divider()
        st.markdown("##### Related")
        for child_spec, fk_col in relations:
            if child_spec.group == "Field Ops" and not st.session_state.get(
                "show_field_ops", False
            ):
                continue
            _render_related_list(row["id"], child_spec, fk_col)


# ── form view (create / edit) ────────────────────────────────────────────────────
def _widget_input(c, current: Any):
    label = c.label
    if c.widget == "ref":
        opts = [("", "—")] + _ref_rows(c.ref)
        ids = [o[0] for o in opts]
        labels = {o[0]: o[1] for o in opts}
        idx = ids.index(current) if current in ids else 0
        return (
            st.selectbox(
                label, ids, index=idx, format_func=lambda x: labels.get(x, x), key=f"f_{c.name}"
            )
            or None
        )
    if c.widget == "enum":
        opts = [""] + (c.options or [])
        idx = opts.index(current) if current in opts else 0
        return st.selectbox(label, opts, index=idx, key=f"f_{c.name}") or None
    if c.widget == "bool":
        return st.checkbox(
            label, value=bool(current) and current not in ("FALSE", "false", "0"), key=f"f_{c.name}"
        )
    if c.widget == "number":
        val = None
        try:
            val = float(current) if current not in (None, "") else None
        except (TypeError, ValueError):
            val = None
        return st.number_input(label, value=val, key=f"f_{c.name}", format="%g")
    if c.widget == "textarea":
        return st.text_area(label, value=str(current or ""), key=f"f_{c.name}") or None
    if c.widget == "image":
        return st.text_input(f"{label} (URL)", value=str(current or ""), key=f"f_{c.name}") or None
    return st.text_input(label, value=str(current or ""), key=f"f_{c.name}") or None


def render_form(spec: EntitySpec, row: dict | None) -> None:
    # Defense-in-depth: a non-editor reaching ?new=1 / ?edit=1 via a crafted URL
    # gets the read-only view instead of an editable form.
    if not perms.can_edit(spec.bare):
        st.warning("🔒 You don't have edit access to this table.")
        if row is not None:
            render_detail(spec, row)
        elif st.button("← Back"):
            nav(page=spec.bare)
        return

    is_new = row is None
    st.subheader(f"{spec.icon} {'New' if is_new else 'Edit'} {spec.label}")
    row = row or {}
    # Pre-populate FK fields passed as query params (e.g. from a related-list ➕ button).
    if is_new:
        for c in spec.columns:
            if c.widget == "ref" and st.query_params.get(c.name):
                row[c.name] = st.query_params[c.name]

    with st.form(f"form_{spec.bare}", clear_on_submit=False):
        values: dict[str, Any] = {}
        for c in spec.columns:
            if not c.editable:
                continue
            values[c.name] = _widget_input(c, row.get(c.name))
        submitted = st.form_submit_button("💾 Save", type="primary")

    if st.button("← Cancel"):
        nav(page=spec.bare, id=row.get("id"))

    if submitted:
        repo = spec.repo()
        # coerce booleans/numbers already typed by widgets; drop empties on create
        if is_new:
            rec = {k: v for k, v in values.items() if v is not None}
            rec["id"] = new_id()
            rec.setdefault("active", True)
            saved = repo.insert(rec)
            st.toast("Created")
            nav(page=spec.bare, id=saved.get("id", rec["id"]))
        else:
            repo.update(row["id"], values)
            st.toast("Saved")
            nav(page=spec.bare, id=row["id"])
