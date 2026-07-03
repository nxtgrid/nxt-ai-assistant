"""Virtual (computed) column definitions — mirrors AppSheet App Formula columns.

Each VirtualCol has a compute(row, fetch) -> value function where:
  fetch(table_bare, pk) -> dict | None  (cached within a single render call)

make_fetch()        — for detail view (lazy, per-row caching)
make_batch_fetch()  — for list view (pre-loaded in bulk, no N+1 queries)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

Fetch = Callable[[str, str | None], dict | None]


@dataclass
class VirtualCol:
    name: str
    label: str
    widget: str = "text"  # text | number | image | bool
    compute: Callable = None
    in_list: bool = False  # include in list view (only free/pre-loaded cols)


# ── Registry ──────────────────────────────────────────────────────────────────

VIRTUAL: dict[str, list[VirtualCol]] = {}


def _reg(table: str, *cols: VirtualCol) -> None:
    VIRTUAL.setdefault(table, []).extend(cols)


# ── Fetch factories ───────────────────────────────────────────────────────────


def make_fetch() -> Fetch:
    """Per-render fetch with in-process cache. Use for detail views."""
    _cache: dict[tuple, dict | None] = {}

    def fetch(table: str, pk: str | None) -> dict | None:
        if not pk:
            return None
        key = (table, str(pk))
        if key not in _cache:
            from grid_app.entities import get_entity

            spec = get_entity(table)
            _cache[key] = spec.repo().get(str(pk)) if spec else None
        return _cache[key]

    return fetch


def make_batch_fetch(preload: dict[str, dict[str, dict]]) -> Fetch:
    """Fetch backed by a pre-loaded dict. Use for list views.
    preload = {table_bare: {pk: row}}
    """
    _extra: dict[tuple, dict | None] = {}

    def fetch(table: str, pk: str | None) -> dict | None:
        if not pk:
            return None
        if table in preload:
            return preload[table].get(str(pk))
        key = (table, str(pk))
        if key not in _extra:
            from grid_app.entities import get_entity

            spec = get_entity(table)
            _extra[key] = spec.repo().get(str(pk)) if spec else None
        return _extra[key]

    return fetch


def preload_for(table_bare: str, rows: list[dict]) -> Fetch:
    """Batch-load all FK data needed for virtual cols in a list of rows."""
    from grid_app.entities import get_entity

    vcols = VIRTUAL.get(table_bare, [])
    if not vcols:
        return make_fetch()

    # Collect FK columns needed: (fk_col_in_row, target_table)
    needs: dict[str, str] = _FK_NEEDS.get(table_bare, {})
    preload: dict[str, dict[str, dict]] = {}
    for fk_col, target_table in needs.items():
        pks = [str(r[fk_col]) for r in rows if r.get(fk_col)]
        if pks:
            spec = get_entity(target_table)
            if spec:
                preload[target_table] = spec.repo().get_many(pks)

    return make_batch_fetch(preload)


# FK columns needed per table for batch pre-loading (list view)
_FK_NEEDS: dict[str, dict[str, str]] = {
    "bom_items": {"item": "components"},
    "subassemblies": {"main_component": "components"},
    "design_subassemblies": {"subassembly": "subassemblies"},
    "subassembly_components": {"component": "components", "subassembly": "subassemblies"},
    "job_subassemblies": {"subassembly": "subassemblies"},
    "job_procedures": {"procedure": "procedures"},
    "job_steps": {"step_reference": "procedure_steps", "job_procedure": "job_procedures"},
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _fmt(v) -> str | None:
    if v is None:
        return None
    n = _num(v)
    if n is not None:
        return f"{n:,.2f}"
    return str(v)


# ── bom_items ─────────────────────────────────────────────────────────────────


def _comp(row, fetch):
    return fetch("components", row.get("item")) or {}


def _bom_ctype(row, fetch):
    ct = _comp(row, fetch).get("component_type") or ""
    return "-> Main Energy Asset" if ct == "Main Energy Asset" else ct or None


def _ddp(row, fetch, use_contingency: bool):
    if _bom_ctype(row, fetch) == "Tools":
        return 0.0
    qty_col = "qty_with_contingency" if use_contingency else "qty"
    qty = _num(row.get(qty_col))
    ddp = _num(_comp(row, fetch).get("ddp_cost"))
    return round(qty * ddp, 4) if qty is not None and ddp is not None else None


def _proj(row, fetch, use_contingency: bool):
    if _bom_ctype(row, fetch) == "Tools":
        return 0.0
    qty_col = "qty_with_contingency" if use_contingency else "qty"
    qty = _num(row.get(qty_col))
    proj = _num(_comp(row, fetch).get("projected_cost"))
    return round(qty * proj, 4) if qty is not None and proj is not None else None


_reg(
    "bom_items",
    VirtualCol("_item_name", "Item Name", compute=lambda r, f: _comp(r, f).get("name")),
    VirtualCol("_item_unit", "Item Unit", compute=lambda r, f: _comp(r, f).get("counting_unit")),
    VirtualCol("_comp_type", "Component Type", compute=_bom_ctype),
    VirtualCol(
        "_ddp_cont",
        "DDP Cost (w/ contingency)",
        widget="number",
        in_list=True,
        compute=lambda r, f: _ddp(r, f, True),
    ),
    VirtualCol(
        "_ddp_no_cont",
        "DDP Cost (no contingency)",
        widget="number",
        compute=lambda r, f: _ddp(r, f, False),
    ),
    VirtualCol(
        "_proj_cont",
        "Projected Cost (w/ contingency)",
        widget="number",
        in_list=True,
        compute=lambda r, f: _proj(r, f, True),
    ),
    VirtualCol(
        "_proj_no_cont",
        "Projected Cost (no contingency)",
        widget="number",
        compute=lambda r, f: _proj(r, f, False),
    ),
)


# ── components ────────────────────────────────────────────────────────────────
# ddp_cost and projected_cost are already physical columns — no virtual needed.


# ── subassemblies ─────────────────────────────────────────────────────────────


def _main_comp_name(row, fetch):
    # main_component may be a comma-separated multi-ref (AppSheet quirk) — use first
    raw = row.get("main_component") or ""
    pk = raw.split(",")[0].strip() if raw else None
    return (fetch("components", pk) or {}).get("name")


_reg(
    "subassemblies",
    VirtualCol("_main_comp_name", "Main Component Name", in_list=True, compute=_main_comp_name),
    VirtualCol(
        "_subassembly_name",
        "Subassembly Name",
        compute=lambda r, f: (
            r.get("id", "")[:3]
            + " "
            + (r.get("description") or "")
            + " ("
            + (_main_comp_name(r, f) or "?")
            + " assembly)"
        )
        if r.get("description")
        else None,
    ),
)


# ── design_subassemblies ──────────────────────────────────────────────────────


def _ds_design_types(row, fetch):
    return (fetch("subassemblies", row.get("subassembly")) or {}).get("design_types")


_reg(
    "design_subassemblies",
    VirtualCol("_design_types", "Subassembly Design Types", in_list=True, compute=_ds_design_types),
)


# ── subassembly_components ────────────────────────────────────────────────────

_reg(
    "subassembly_components",
    VirtualCol(
        "_item_name",
        "Component Name",
        in_list=True,
        compute=lambda r, f: (f("components", r.get("component")) or {}).get("name"),
    ),
    VirtualCol(
        "_sub_design_types",
        "Subassembly Design Types",
        compute=lambda r, f: (f("subassemblies", r.get("subassembly")) or {}).get("design_types"),
    ),
)


# ── designs ───────────────────────────────────────────────────────────────────

_reg(
    "designs",
    VirtualCol(
        "_design_name",
        "Design Name",
        in_list=True,
        compute=lambda r, f: (r.get("id", "")[:3] + " " + r.get("name", ""))
        if r.get("name")
        else None,
    ),
)


# ── procedure_steps ───────────────────────────────────────────────────────────

_reg(
    "procedure_steps",
    VirtualCol(
        "_label",
        "Label",
        in_list=True,
        compute=lambda r, f: (
            str(int(r["step_order"])) + ". " + r["name"]
            if r.get("step_order") is not None and r.get("name")
            else r.get("name")
        ),
    ),
)


# ── job_procedures ────────────────────────────────────────────────────────────

_reg(
    "job_procedures",
    VirtualCol(
        "_label",
        "Label",
        in_list=True,
        compute=lambda r, f: (
            str(r.get("sequence_in_job", ""))
            + ". "
            + ((f("procedures", r.get("procedure")) or {}).get("name") or "")
        ).strip(". ")
        or None,
    ),
)


# ── job_subassemblies ─────────────────────────────────────────────────────────


def _job_sub_label(row, fetch):
    sub = fetch("subassemblies", row.get("subassembly")) or {}
    comp_name = _main_comp_name(sub, fetch) if sub else None
    qty = row.get("qty")
    return f"{qty}x {comp_name}" if qty is not None and comp_name else None


_reg(
    "job_subassemblies",
    VirtualCol("_label", "Label", in_list=True, compute=_job_sub_label),
    VirtualCol(
        "_subassy_photo",
        "Subassembly Photo",
        widget="image",
        compute=lambda r, f: (f("subassemblies", r.get("subassembly")) or {}).get(
            "assembly_reference_image"
        ),
    ),
)


# ── job_steps ─────────────────────────────────────────────────────────────────


def _step_ref(row, fetch):
    return fetch("procedure_steps", row.get("step_reference")) or {}


def _step_image(row, fetch):
    proof = row.get("proof")
    if proof and str(proof).startswith("http"):
        return proof
    return _step_ref(row, fetch).get("image")


def _step_proc_name(row, fetch):
    jp = fetch("job_procedures", row.get("job_procedure")) or {}
    proc = fetch("procedures", jp.get("procedure")) or {}
    return proc.get("name")


_reg(
    "job_steps",
    VirtualCol("_step_detail", "Step Detail", compute=lambda r, f: _step_ref(r, f).get("detail")),
    VirtualCol("_step_image", "Step Image", widget="image", compute=_step_image),
    VirtualCol("_proc_name", "Procedure Name", compute=_step_proc_name),
)


# ── grids ─────────────────────────────────────────────────────────────────────


def _jobs_last_n(row, fetch, days: int) -> int | None:
    grid_id = row.get("id")
    if not grid_id:
        return None
    from grid_app.entities import get_entity

    spec = get_entity("jobs")
    if not spec:
        return None
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = spec.repo().list(
        active_only=True,
        filters={"grid": grid_id},
    )
    return sum(
        1 for r in rows if r.get("status") != "⃠ Cancelled" and (r.get("created_at") or "") >= cutoff
    )


_reg(
    "grids",
    VirtualCol(
        "_jobs_90d",
        "Jobs (last 90 days)",
        widget="number",
        compute=lambda r, f: _jobs_last_n(r, f, 90),
    ),
)


# ── unit_rental_prices ────────────────────────────────────────────────────────


def _rental_component(row, fetch):
    name = row.get("engineering_item_name")
    if not name:
        return None
    from grid_app.entities import get_entity

    spec = get_entity("components")
    if not spec:
        return None
    matches = spec.repo().list(active_only=True, filters={"name": name}, limit=1)
    return matches[0].get("id") if matches else None


_reg(
    "unit_rental_prices",
    VirtualCol("_component_id", "Linked Component ID", compute=_rental_component),
)
