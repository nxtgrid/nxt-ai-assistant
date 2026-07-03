"""Entity row actions — buttons surfaced on the detail view that invoke the
compute engines. Kept here (not in the generic views) so the views stay
metadata-only and engines are wired per entity.
"""

from __future__ import annotations

from typing import Callable

import streamlit as st


def _auto_design(row: dict) -> None:
    from grid_app.services.auto_designer import auto_design

    res = auto_design(row["id"])
    if res.get("ok"):
        st.success(
            f"Auto-designed: {res['subassemblies']} subassemblies · "
            f"{res['kwp']} kWp / {res['kwh']} kWh / {res['kva']} kVA"
        )
    else:
        st.error(res.get("error", "Auto-design failed"))


def _check_design(row: dict) -> None:
    from grid_app.services.design_validator import check_design

    report = check_design(row["id"])
    st.info(report)


def _generate_bom(row: dict) -> None:
    from grid_app.services.bom_generator import generate_bom

    res = generate_bom(row["id"], bom_type="Design")
    if res.get("ok"):
        st.success(
            f"BOM generated: {res['items']} items · "
            f"₦{res['total_cost_ngn']:,.0f} (rate {res['exchange_rate']})"
        )
    else:
        st.error(res.get("error", "BOM generation failed"))


def _generate_bom_job(row: dict) -> None:
    from grid_app.services.bom_generator import generate_bom

    res = generate_bom(row["id"], bom_type="Job")
    if res.get("ok"):
        st.success(f"Job BOM generated: {res['items']} items")
    else:
        st.error(res.get("error", "BOM generation failed"))


def _recompute_costs(row: dict) -> None:
    from grid_app.services.cost_projection import recompute_component_costs

    res = recompute_component_costs()
    st.success(
        f"Recomputed costs: {res['components_updated']} components updated "
        f"from {res['items_with_history']} items with purchase history "
        f"(rate {res['exchange_rate']})"
    )


_REGISTRY: dict[str, list[tuple[str, Callable[[dict], None]]]] = {
    "designs": [
        ("⚙️ Auto-Design", _auto_design),
        ("🔍 Check Design", _check_design),
        ("🧾 Generate BOM", _generate_bom),
    ],
    "jobs": [
        ("🧾 Generate Job BOM", _generate_bom_job),
    ],
    "components": [
        ("💲 Recompute Costs from Purchases", _recompute_costs),
    ],
    "purchases": [
        ("💲 Recompute Component Costs", _recompute_costs),
    ],
}


def actions_for(bare: str) -> list[tuple[str, Callable[[dict], None]]]:
    return _REGISTRY.get(bare, [])
