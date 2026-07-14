"""Framework-agnostic grid detail actions for the NiceGUI UI.

Mirrors the registry in ``grid_app/actions.py`` (which calls ``st.*`` directly)
but returns a ``(ok, message)`` tuple so the NiceGUI page can surface it via
``ui.notify``. The heavy lifting lives in the ``grid_app.services.*`` compute
engines, called identically to the Streamlit path.
"""

from __future__ import annotations

from typing import Callable


def _auto_design(row: dict) -> tuple[bool, str]:
    from shared.grid_design.auto_designer import auto_design

    res = auto_design(row["id"])
    if res.get("ok"):
        return True, (
            f"Auto-designed: {res['subassemblies']} subassemblies · "
            f"{res['kwp']} kWp / {res['kwh']} kWh / {res['kva']} kVA"
        )
    return False, res.get("error", "Auto-design failed")


def _check_design(row: dict) -> tuple[bool, str]:
    from shared.grid_design.design_validator import check_design

    return True, check_design(row["id"])


def _generate_bom(row: dict) -> tuple[bool, str]:
    from shared.grid_design.bom_generator import generate_bom

    res = generate_bom(row["id"], bom_type="Design")
    if res.get("ok"):
        return True, (
            f"BOM generated: {res['items']} items · "
            f"₦{res['total_cost_ngn']:,.0f} (rate {res['exchange_rate']})"
        )
    return False, res.get("error", "BOM generation failed")


def _generate_bom_job(row: dict) -> tuple[bool, str]:
    from shared.grid_design.bom_generator import generate_bom

    res = generate_bom(row["id"], bom_type="Job")
    if res.get("ok"):
        return True, f"Job BOM generated: {res['items']} items"
    return False, res.get("error", "BOM generation failed")


def _recompute_costs(row: dict) -> tuple[bool, str]:
    from shared.grid_design.cost_projection import recompute_component_costs

    res = recompute_component_costs()
    return True, (
        f"Recomputed costs: {res['components_updated']} components updated "
        f"from {res['items_with_history']} items with purchase history "
        f"(rate {res['exchange_rate']})"
    )


ActionFn = Callable[[dict], "tuple[bool, str]"]

_REGISTRY: dict[str, list[tuple[str, ActionFn]]] = {
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


def actions_for(bare: str) -> list[tuple[str, ActionFn]]:
    return _REGISTRY.get(bare, [])
