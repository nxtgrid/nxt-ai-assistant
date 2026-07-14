"""Auto-design — Python port of Apps Script `createDesign` + `dsRowObject`.

Given a Design record's input parameters, selects inverter/battery/MPPT/PV
subassemblies (plus cabin, meters, comms, etc.) from the catalogue using the
Design Rules, sizes the system, writes the computed fields back onto the Design,
and (re)creates its Design Subassemblies.

Depends on the external "Sizing DB" lookups now held in `unit_rental_prices`
(maps a friendly type -> engineering component name) and `wp_per_conn_lookup`.
Returns {"ok": bool, "error"|"design_id": ...}.
"""

from __future__ import annotations

from datetime import datetime, timezone

from shared.grid_design.data import index_by, load, num, truthy
from shared.grid_design.db import Repository
from shared.grid_design.ids import new_id

TECHNOLOGY_FAMILIES = ("victron", "deye")


def normalize_design_family(value: str | None) -> str:
    """Normalize user/tool-facing technology family names.

    `design_type` is already used by this module for lifecycle modes such as
    Initial/Resize, so the vendor/architecture selector is intentionally called
    technology_family.
    """
    raw = str(value or "victron").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "victron": "victron",
        "victron_container": "victron",
        "deye": "deye",
        "ess": "deye",
        "hybrid_ess": "deye",
        "deye_hybrid_ess": "deye",
    }
    family = aliases.get(raw)
    if family not in TECHNOLOGY_FAMILIES:
        raise ValueError(f"Unsupported technology_family '{value}'. Use 'victron' or 'deye'.")
    return family


def _design_type_tokens(value: str | None) -> set[str]:
    raw = str(value or "").strip().lower()
    if not raw or raw == "none":
        return set()
    return {part.strip() for part in raw.replace("&", " and ").split("and") if part.strip()}


def subassembly_supports_design_family(subassembly: dict, family: str | None) -> bool:
    tokens = _design_type_tokens(subassembly.get("design_types"))
    return normalize_design_family(family) in tokens


def _infer_design_family(design: dict) -> str:
    fields = (
        design.get("inverter_type"),
        design.get("battery_type"),
        design.get("mppt_type"),
        design.get("pv_type"),
        design.get("pv_inverter_type"),
    )
    joined = " ".join(str(value or "") for value in fields).lower()
    return "deye" if "deye" in joined else "victron"


def _build_ds_row(design_id: str, sub: dict, qty: float, sub_components: list[dict]) -> dict:
    in_sub = [
        c for c in sub_components if c.get("subassembly") == sub["id"] and truthy(c.get("active"))
    ]

    def spec_sum(unit: str) -> float:
        return float(
            qty * sum(num(c.get("spec1_value")) for c in in_sub if c.get("spec1_unit") == unit)
        )

    return {
        "id": new_id(),
        "design": design_id,
        "subassembly": sub["id"],
        "qty": qty,
        "subassembly_image": sub.get("assembly_reference_image"),
        "named": f"{qty} x {sub.get('description', '')}",
        "class": sub.get("assembly_class"),
        "spec1_name": sub.get("spec1_name"),
        "spec1_value": sub.get("spec1_value"),
        "spec1_unit": sub.get("spec1_unit"),
        "spec2_name": sub.get("spec2_name"),
        "spec2_value": sub.get("spec2_value"),
        "spec2_unit": sub.get("spec2_unit"),
        "spec3_name": sub.get("spec3_name"),
        "spec3_value": sub.get("spec3_value"),
        "spec3_unit": sub.get("spec3_unit"),
        "kwp": spec_sum("kWp"),
        "kwh": spec_sum("kWh"),
        "kva": spec_sum("kVA"),
        "active": True,
    }


def auto_design(
    design_id: str, design_type: str = "Initial", technology_family: str | None = None
) -> dict:
    target = Repository("designs").get(design_id)
    if not target:
        return {"ok": False, "error": f"Design {design_id} not found."}

    components = [c for c in load("components") if truthy(c.get("active"))]
    component_map = {c.get("name"): c for c in components}
    rentals = load("unit_rental_prices")
    rental_map = index_by(rentals, "item")
    rules = {r.get("name"): r.get("value") for r in load("design_rules")}
    wp_lookup = load("wp_per_conn_lookup")
    active_subs = [s for s in load("subassemblies") if truthy(s.get("active"))]
    sub_components = load("subassembly_components")

    def rule(name: str, default: float = 0.0) -> float:
        return float(num(rules.get(name), default))

    def comp_id(type_name: str):
        item = rental_map.get(type_name)
        if not item:
            return {"error": f"No rentable item: {type_name}"}
        comp = component_map.get(item.get("engineering_item_name"))
        if not comp:
            return {"error": f"No active component: {item.get('engineering_item_name')}"}
        return comp["id"]

    d = target
    res = int(num(d.get("initial_residential_connections")))
    non_res = int(num(d.get("initial_business_connections")))
    max_conns = int(num(d.get("max_connections")))
    three_phase = int(num(d.get("initial_3_phase_connections")))
    poc_teams = int(num(d.get("number_of_poc_teams_to_install_meters"), 1)) or 1
    constrain = d.get("constrain_design_to_known_regulation") or "None"

    inverter_type = d.get("inverter_type")
    pv_inverter_type = d.get("pv_inverter_type")
    mppt_type = d.get("mppt_type")
    batt_type = d.get("battery_type")
    pv_type = d.get("pv_type")
    family = normalize_design_family(technology_family or _infer_design_family(d))

    use_pv_inverters = rule("Use PV Inverters for Initial Designs?") == 1
    ratio_conn = 0 if (res + non_res) == 0 else non_res / (res + non_res)
    # The sheet's .find() relies on rows being ordered by descending threshold
    # (it takes the highest threshold the ratio meets); DB order isn't guaranteed,
    # so sort explicitly to preserve that semantics.
    wp_lookup = sorted(
        wp_lookup, key=lambda it: num(it.get("nonresidential_threshold")), reverse=True
    )
    wp_rule = next(
        (it for it in wp_lookup if ratio_conn >= num(it.get("nonresidential_threshold"))), None
    )
    if not wp_rule:
        return {"ok": False, "error": "Wp/conn rule lookup failed (load wp_per_conn_lookup)."}

    override = num(d.get("wp_per_conn_override"))
    kwp_per_conn = (override / 1000) if override != 0 else (num(wp_rule.get("wp_per_conn")) / 1000)
    # Design's own value (user override via the form/API) wins over the
    # Design Rules default.
    daily_gen = num(d.get("daily_generation_potential_kwh_kwp")) or rule(
        "Nominal kWh/kWp/day generation potential", 3.5
    )

    min_total_kwp = num(d.get("target_kwp")) or max(
        (max_conns * rule("DARES kWp per conn")) if constrain == "Nigeria - DARES" else 0,
        kwp_per_conn * (res + non_res)
        + num(d.get("anchor_load_kw")) * num(d.get("pue_hours_per_day")) / (daily_gen or 3.5),
    )
    if family == "deye":
        phases = 3
    elif truthy(d.get("force_3_phase")) or min_total_kwp > rule(
        "Max kWp for single phase grid", 50
    ):
        phases = 3
    else:
        phases = 1

    required_types = [
        ("inverter", inverter_type),
        ("battery", batt_type),
    ]
    if family == "victron":
        required_types.extend(
            [
                ("mppt", mppt_type),
                ("pv", pv_type),
            ]
        )
    for label, t in required_types:
        r = comp_id(t)
        if isinstance(r, dict):
            return {"ok": False, "error": r["error"]}
    inverter_c = comp_id(inverter_type)
    batt_c = comp_id(batt_type)
    mppt_c = comp_id(mppt_type) if family == "victron" else None
    pv_c = comp_id(pv_type) if family == "victron" else None

    pv_inverter_c = None
    if pv_inverter_type and use_pv_inverters:
        r = comp_id(pv_inverter_type)
        if isinstance(r, dict):
            return {"ok": False, "error": r["error"]}
        pv_inverter_c = r

    # Inverter sizing
    if family == "deye":
        batt_inv_candidates = [
            s
            for s in active_subs
            if subassembly_supports_design_family(s, family)
            and s.get("main_component") == inverter_c
            and (
                "nverter" in (s.get("assembly_class") or "")
                or "Hybrid ESS" in (s.get("assembly_class") or "")
            )
        ]
    else:
        batt_inv_candidates = [
            s
            for s in active_subs
            if subassembly_supports_design_family(s, family)
            and s.get("main_component") == inverter_c
            and inverter_type in (s.get("description") or "")
            and "nverter" in (s.get("assembly_class") or "")
        ]
    batt_inv_candidates.sort(key=lambda s: num(s.get("spec1_value")), reverse=True)
    if not batt_inv_candidates:
        return {
            "ok": False,
            "error": f"No {family}-compatible inverter/hybrid ESS subassembly found.",
        }
    batt_inv = batt_inv_candidates[0]
    unit_inv_kva = num(batt_inv.get("spec1_value"))
    if unit_inv_kva <= 0:
        return {
            "ok": False,
            "error": (
                f"Battery Inverter subassembly '{batt_inv.get('description', '')}' has no "
                "kVA rating (spec1_value) — fix the catalogue entry."
            ),
        }

    pv_inv = None
    unit_pv_inv_kva = 0.0
    if pv_inverter_c:
        pv_inv = next(
            (
                s
                for s in active_subs
                if s.get("main_component") == pv_inverter_c
                and pv_inverter_type in (s.get("description") or "")
                and "nverter" in (s.get("assembly_class") or "")
                and num(s.get("spec3_value")) == phases
            ),
            None,
        )
        if pv_inv:
            unit_pv_inv_kva = num(pv_inv.get("spec1_value"))

    realistic_load = rule("kW Load per kWp", 0.3) * min_total_kwp
    inv_kva_unq = rule("Inverter VA per kW Load", 1.8) * realistic_load
    pv_inv_va_unq = 0.0
    if unit_pv_inv_kva > 0 and inv_kva_unq >= unit_inv_kva:
        frac = rule("Max Initial PV Inverter VA Fraction", 0.5)
        if frac * inv_kva_unq >= 0.8 * unit_pv_inv_kva:
            pv_inv_va_unq = frac * inv_kva_unq
    pv_inverters = int(pv_inv_va_unq // unit_pv_inv_kva) if unit_pv_inv_kva > 0 else 0
    batt_inverters = max(1, round((inv_kva_unq - pv_inverters * unit_pv_inv_kva) / unit_inv_kva))
    kva_actual = (
        batt_inverters * unit_inv_kva
        if family == "deye"
        else (batt_inverters * unit_inv_kva) / phases
    )

    # Battery selection
    target_kwh = num(d.get("target_kwh")) or max(
        min_total_kwp * rule("New Design kWh/kWp ratio", 2.0),
        res * rule("DARES Minimum kWh required per residential connection")
        if constrain == "Nigeria - DARES"
        else 0,
    )
    all_batts = [
        s
        for s in active_subs
        if subassembly_supports_design_family(s, family)
        and s.get("main_component") == batt_c
        and "Battery" in (s.get("assembly_class") or "")
    ]
    all_batts.sort(key=lambda s: num(s.get("spec1_value")), reverse=True)
    remaining_kwh, total_kwh = target_kwh, 0.0
    chosen_batts = []
    for s in all_batts:
        unit = num(s.get("spec1_value"))
        count = int(remaining_kwh // unit) if unit else 0
        if count > 0:
            chosen_batts.append((s, count))
            total_kwh += count * unit
            remaining_kwh -= count * unit

    # MPPT selection
    mppt_power_needed = min_total_kwp - (pv_inverters * unit_pv_inv_kva)
    if family == "deye":
        all_mppts = [
            s
            for s in active_subs
            if subassembly_supports_design_family(s, family)
            and "MPPT" in (s.get("assembly_class") or "")
        ]
    else:
        all_mppts = [
            s
            for s in active_subs
            if subassembly_supports_design_family(s, family)
            and mppt_c in str(s.get("main_component"))
            and pv_c in str(s.get("main_component"))
            and "MPPT" in (s.get("assembly_class") or "")
        ]
    all_mppts.sort(key=lambda s: num(s.get("spec1_value")), reverse=True)
    remaining_power, total_mppt_units, mppt_kwp = mppt_power_needed, 0, 0.0
    chosen_mppts = []
    mppt_max = rule("MPPT Max Count per Cerbo", 25)
    for s in all_mppts:
        unit = num(s.get("spec1_value"))
        count = min(int(remaining_power // unit) if unit else 0, int(mppt_max) - total_mppt_units)
        if count > 0:
            chosen_mppts.append((s, count))
            mppt_kwp += count * unit
            remaining_power -= count * unit
            total_mppt_units += count

    kwp_actual = round(100 * (pv_inverters * unit_pv_inv_kva + mppt_kwp)) / 100
    pv_area = kwp_actual * rule("PV Area sq.m. per kWp", 6.0)
    x_rate = 0.0
    from shared.grid_design.exchange_rate import get_usd_to_ngn

    x_rate = get_usd_to_ngn() or num(d.get("usd_to_ngn"))
    today = datetime.now(timezone.utc).isoformat()

    # Write computed fields back onto the design (named, not positional)
    design_update = {
        "name": f"{d.get('name', '')} at {1000 * kwp_per_conn}Wp/conn",
        "phases": "3-phase" if phases == 3 else "1-phase",
        "target_kwp": min_total_kwp,
        "target_kwh": target_kwh,
        "daily_generation_potential_kwh_kwp": daily_gen,
        "pv_area_sqm": pv_area,
        "usd_to_ngn": x_rate,
        "xrate_updated_at": today,
        "design_checks": "Not checked yet",
        "kwp": kwp_actual,
        "kva": kva_actual,
        "kwh": total_kwh,
        "monthly_revenue_at_expected_cuf_and_tariff": num(d.get("target_tariff_usd")) * x_rate,
    }
    Repository("designs").update(design_id, design_update)

    # (Re)create design subassemblies
    sub_rows: list[dict] = []

    def add_sub(sub, qty):
        if sub and qty > 0:
            sub_rows.append(_build_ds_row(design_id, sub, qty, sub_components))

    if pv_inverters > 0:
        add_sub(pv_inv, pv_inverters)
    add_sub(batt_inv, batt_inverters)
    for s, c in chosen_batts:
        add_sub(s, c)
    for s, c in chosen_mppts:
        add_sub(s, c)

    if design_type != "Resize" and family == "deye":
        for sub in active_subs:
            if (
                subassembly_supports_design_family(sub, family)
                and "Hybrid ESS" in (sub.get("assembly_class") or "")
                and sub.get("id") != batt_inv.get("id")
                and num(sub.get("spec1_value")) == 0
            ):
                add_sub(sub, 1)
    elif design_type != "Resize":
        cabin = next(
            (
                s
                for s in active_subs
                if subassembly_supports_design_family(s, family)
                and "Cabin" in (s.get("description") or "")
                and num(s.get("spec3_value")) == phases
                and batt_c in str(s.get("main_component"))
            ),
            None,
        )
        add_sub(cabin, 1)
        add_sub(
            next(
                (
                    s
                    for s in active_subs
                    if (rules.get("Cabin Arrester Name") or "Cabin Lightning")
                    in (s.get("description") or "")
                ),
                None,
            ),
            1,
        )
        add_sub(
            next(
                (
                    s
                    for s in active_subs
                    if "Feeder" in (s.get("description") or "")
                    and num(s.get("spec3_value")) == phases
                ),
                None,
            ),
            1,
        )
        add_sub(
            next(
                (
                    s
                    for s in active_subs
                    if "LoRaWAN" in (s.get("description") or "")
                    and "Base" in (s.get("description") or "")
                ),
                None,
            ),
            1,
        )
        if chosen_batts:
            add_sub(
                next(
                    (
                        s
                        for s in active_subs
                        if (rules.get("Battery Cooling Name") or "Cooling enclosure")
                        in (s.get("description") or "")
                    ),
                    None,
                ),
                1,
            )

    # Meters
    s_meter = next(
        (
            s
            for s in active_subs
            if (rules.get("1-ph Meter Name") or "Smart Meter - Single")
            in (s.get("description") or "")
        ),
        None,
    )
    t_meter = next(
        (
            s
            for s in active_subs
            if (rules.get("3-ph Meter Name") or "Smart Meter - Three")
            in (s.get("description") or "")
        ),
        None,
    )
    total_single = 3 + (res + non_res) - three_phase
    if total_single > 0:
        add_sub(s_meter, total_single)
    if three_phase > 0:
        add_sub(t_meter, three_phase)
        add_sub(
            next(
                (
                    s
                    for s in active_subs
                    if (rules.get("DCU Name") or "DCU v1/v2") in (s.get("description") or "")
                ),
                None,
            ),
            1,
        )
    if poc_teams > 0:
        add_sub(
            next(
                (
                    s
                    for s in active_subs
                    if (rules.get("PoC Tools Name") or "PoC Tools") in (s.get("description") or "")
                ),
                None,
            ),
            poc_teams,
        )

    # Replace existing design subassemblies (improvement over append-only original)
    repo = Repository("design_subassemblies")
    for ex in repo.list(active_only=True, filters={"design": design_id}):
        repo.soft_delete(ex["id"])
    repo.upsert(sub_rows)

    return {
        "ok": True,
        "design_id": design_id,
        "subassemblies": len(sub_rows),
        "kwp": kwp_actual,
        "kwh": total_kwh,
        "kva": kva_actual,
    }
