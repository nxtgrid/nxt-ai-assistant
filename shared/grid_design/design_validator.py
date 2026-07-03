"""Design validation — Python port of Apps Script `checkDesign`.

Runs the engineering sanity checks and returns an emoji-coded report
(🧿 start · 🛑 issue · ⚠️ warning), also persisted to designs.design_checks.
Mirrors the original's active checks; checks the original had commented out remain
commented here so parity is auditable.

Note: the original referenced design["Residential Connections"] /
["Business Connections"] (AppSheet virtual columns). The exported sheet stores the
inputs as initial_residential_connections / initial_business_connections, so we use
those.
"""

from __future__ import annotations

import math

from shared.grid_design.data import index_by, load, num
from shared.grid_design.db import Repository

PV_INVERTER = "PV Inverter (+Panels)"
INVERTER_CHARGER = "Inverter Charger"
HYBRID_INVERTER = "Hybrid Inverter (MPPT and AC Charger + Inverter)"
CABIN = "Energy Cabin or Cabinet"
AC_BUS = "AC Bus"
DC_BUS = "DC Bus"
LIGHTNING = "Lightning Arresters"
GROUNDING = "Grounding"


def _rule(rules: dict[str, dict], name: str) -> float:
    r = rules.get(name)
    return num(r.get("value")) if r else 0.0


def check_design(design_id: str) -> str:
    checks = "🧿"
    design = Repository("designs").get(design_id)
    if not design:
        return checks + "\n🛑 Issue: design not found"
    rules = index_by(load("design_rules"), "name")
    subs = Repository("design_subassemblies").list(active_only=True, filters={"design": design_id})

    def by_class(*classes):
        return [s for s in subs if s.get("class") in classes]

    # Check #1: PV inverter kVA must not exceed inverter-charger kVA
    pv_kva = sum(num(s.get("kva")) for s in by_class(PV_INVERTER))
    ic_kva = sum(num(s.get("kva")) for s in by_class(INVERTER_CHARGER, HYBRID_INVERTER))
    if pv_kva > ic_kva:
        checks += f"\n🛑 Issue: {PV_INVERTER} kVA {pv_kva} > {INVERTER_CHARGER} kVA {ic_kva}"

    # Check #2: BESS/PV ratios
    kwp, kva, kwh = num(design.get("kwp")), num(design.get("kva")), num(design.get("kwh"))
    target_kwp, target_kwh = num(design.get("target_kwp")), num(design.get("target_kwh"))
    if kwp < target_kwp:
        checks += f"\n⚠️ Warning: kWp {kwp} lower than target {target_kwp}"
    if kwh < target_kwh:
        checks += f"\n⚠️ Warning: kWh {kwh} lower than target {target_kwh}"
    if kwp and kwp * 1.85 < kwh:
        ratio = round(100 * kwh / kwp) / 100
        bound = "2" if kwp * 2 < kwh else "1.85"
        checks += f"\n⚠️ Warning: BESS {kwh}kWh to PV {kwp}kWp ratio {ratio} more than {bound}"
    if kwh < kwp:
        checks += f"\n🛑 Issue: BESS {kwh}kWh too low w.r.t. PV {kwp}kWp"

    # Check #3: generation vs load
    anchor = num(design.get("anchor_load_kw"))
    res_kw = num(design.get("initial_residential_connections")) * _rule(rules, "kWp per home")
    biz_kw = num(design.get("initial_business_connections")) * _rule(rules, "kWp per business")
    total_load = anchor * 1.5 + res_kw + biz_kw
    if kva < 1.25 * 0.9 * target_kwp / 2 or kva < 1.25 * 0.9 * total_load:
        checks += (
            f"\n⚠️ Warning: kVA {kva} might be lower than needed to supply expected "
            f"load, with target kWp {target_kwp}kW and load from connections {total_load}kW"
        )
    if kwp < 1.8 * total_load:
        checks += f"\n⚠️ Warning: kWp {kwp} too low for estimated load power {total_load}"

    # Check #4: energy enclosure count
    cabins = len(by_class(CABIN))
    if cabins == 0:
        checks += "\n🛑 Issue: no Energy enclosure"
    if cabins > 1:
        checks += "\n⚠️ Warning: More than one Energy enclosure"

    # Check #6/7: PV lightning arrester coverage (arresters named with "12.5m")
    pv_arresters = sum(
        num(s.get("qty")) for s in by_class(LIGHTNING) if "12.5m" in (s.get("named") or "")
    )
    area_pv = num(design.get("pv_area_sqm"))
    area_per = _rule(rules, "Approx. Sq.m Covered by one PV Lightning Arrester")
    if area_per and pv_arresters * area_per < area_pv:
        need = max(math.ceil(area_pv / area_per), 1)
        checks += f"\n⚠️ Warning: Insufficient PV Lightning Arresters, estimated need {need}"

    # Check #8 (grounding/AC-bus/DC-bus) was commented out in the original — left out
    # here for parity; the rule lookups remain available via `rules`.

    Repository("designs").update(design_id, {"design_checks": checks})
    return checks
