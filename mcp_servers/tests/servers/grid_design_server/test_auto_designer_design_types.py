from unittest.mock import patch

from shared.grid_design import auto_designer


class _TableRepo:
    def __init__(self, tables, table):
        self.tables = tables
        self.table = table

    def _rows(self):
        return self.tables.setdefault(self.table, [])

    def list(self, *, active_only=True, filters=None, **_kwargs):
        rows = list(self._rows())
        if active_only:
            rows = [r for r in rows if r.get("active") is True]
        for key, value in (filters or {}).items():
            rows = [r for r in rows if r.get(key) == value]
        return [dict(r) for r in rows]

    def get(self, row_id):
        for row in self._rows():
            if row.get("id") == row_id:
                return dict(row)
        return None

    def update(self, row_id, changes):
        for row in self._rows():
            if row.get("id") == row_id:
                row.update(changes)
                return dict(row)
        return None

    def upsert(self, rows):
        by_id = {row.get("id"): row for row in self._rows()}
        for row in rows:
            existing = by_id.get(row.get("id"))
            if existing:
                existing.update(row)
            else:
                self._rows().append(dict(row))
        return len(rows)

    def soft_delete(self, row_id):
        for row in self._rows():
            if row.get("id") == row_id:
                row["active"] = False


def _repo_factory(tables):
    return lambda table: _TableRepo(tables, table)


def _base_tables():
    return {
        "designs": [
            {
                "id": "d1",
                "name": "Deye design",
                "inverter_type": "Deye SUN-30K",
                "battery_type": "Deye LV Bat 5kWh",
                "mppt_type": "Victron 250/85 MPPT",
                "pv_type": "JA455W Panel",
                "max_connections": 414,
                "initial_residential_connections": 372,
                "initial_business_connections": 42,
                "initial_3_phase_connections": 0,
                "number_of_poc_teams_to_install_meters": 1,
                "constrain_design_to_known_regulation": "Nigeria - DARES",
                "pue_hours_per_day": 3,
                "target_tariff_usd": 0.45,
                "active": True,
            }
        ],
        "components": [
            {
                "id": "inv-deye",
                "name": "Deye Hybrid Inverter SUN-30K-SG01HP3-EU-BM3",
                "active": True,
            },
            {
                "id": "bat-deye",
                "name": "Deye LFP Battery Module 5.12kWh for GE-Fx",
                "active": True,
            },
            {"id": "array-deye", "name": "20S2P JA 455W Solar array with combiner", "active": True},
            {"id": "meter-1p", "name": "LoRaWAN Smart Meter - Single Phase", "active": True},
            {"id": "tools", "name": "PoC Tools and PPE", "active": True},
        ],
        "unit_rental_prices": [
            {
                "item": "Deye SUN-30K",
                "engineering_item_name": "Deye Hybrid Inverter SUN-30K-SG01HP3-EU-BM3",
                "active": True,
            },
            {
                "item": "Deye LV Bat 5kWh",
                "engineering_item_name": "Deye LFP Battery Module 5.12kWh for GE-Fx",
                "active": True,
            },
        ],
        "design_rules": [
            {"name": "DARES kWp per conn", "value": 0.12},
            {"name": "kW Load per kWp", "value": 0.3},
            {"name": "Inverter VA per kW Load", "value": 1.8},
            {"name": "New Design kWh/kWp ratio", "value": 2.0},
            {"name": "DARES Minimum kWh required per residential connection", "value": 0.4},
            {"name": "PV Area sq.m. per kWp", "value": 6.0},
            {"name": "Max kWp for single phase grid", "value": 50},
        ],
        "wp_per_conn_lookup": [
            {"nonresidential_threshold": 0.0, "wp_per_conn": 120},
        ],
        "subassemblies": [
            {
                "id": "hybrid-ess",
                "description": "Deye GE-F60 Hybrid ESS with SUN-30K",
                "assembly_class": "Hybrid ESS",
                "design_types": "Deye",
                "main_component": "inv-deye",
                "spec1_name": "Power",
                "spec1_value": "30.0",
                "spec1_unit": "kVA",
                "active": True,
            },
            {
                "id": "battery-min",
                "description": "Deye Minimum LFP Battery for GE-F60 with SUN-30K",
                "assembly_class": "Battery",
                "design_types": "Deye",
                "main_component": "bat-deye",
                "spec1_name": "Energy",
                "spec1_value": "35.84",
                "spec1_unit": "kWh",
                "active": True,
            },
            {
                "id": "battery-add",
                "description": "Deye Additional LFP Battery for GE-F60",
                "assembly_class": "Battery",
                "design_types": "Deye",
                "main_component": "bat-deye",
                "spec1_name": "Energy",
                "spec1_value": "5.12",
                "spec1_unit": "kWh",
                "active": True,
            },
            {
                "id": "array",
                "description": "MPPT (+Panels) 20S2P JA 455W Solar array with combiner",
                "assembly_class": "MPPT (+Panels)",
                "design_types": "Deye",
                "main_component": "array-deye",
                "spec1_name": "Power",
                "spec1_value": "18.2",
                "spec1_unit": "kWp",
                "active": True,
            },
            {
                "id": "meter",
                "description": "LoRaWAN Smart Meter - Single Phase",
                "assembly_class": "Metering",
                "design_types": "Victron and Deye",
                "main_component": "meter-1p",
                "active": True,
            },
            {
                "id": "tools",
                "description": "PoC Tools and PPE",
                "assembly_class": "Metering",
                "design_types": "Victron and Deye",
                "main_component": "tools",
                "active": True,
            },
        ],
        "subassembly_components": [],
        "design_subassemblies": [],
    }


def test_subassembly_supports_design_family_uses_design_types():
    assert auto_designer.subassembly_supports_design_family({"design_types": "Deye"}, "deye")
    assert auto_designer.subassembly_supports_design_family(
        {"design_types": "Victron and Deye"}, "deye"
    )
    assert auto_designer.subassembly_supports_design_family(
        {"design_types": "Victron and Deye"}, "victron"
    )
    assert not auto_designer.subassembly_supports_design_family({"design_types": "Victron"}, "deye")
    assert not auto_designer.subassembly_supports_design_family({}, "deye")


def test_auto_design_deye_uses_hybrid_ess_and_deye_solar_array():
    tables = _base_tables()
    with (
        patch.object(auto_designer, "Repository", side_effect=_repo_factory(tables)),
        patch("shared.grid_design.data.Repository", side_effect=_repo_factory(tables)),
        patch("shared.grid_design.exchange_rate.get_usd_to_ngn", return_value=1500.0),
    ):
        result = auto_designer.auto_design("d1", technology_family="deye")

    assert result["ok"] is True
    active_sub_ids = {
        row["subassembly"] for row in tables["design_subassemblies"] if row.get("active") is True
    }
    assert "hybrid-ess" in active_sub_ids
    assert "battery-min" in active_sub_ids
    assert "battery-add" in active_sub_ids
    assert "array" in active_sub_ids
    assert tables["designs"][0]["kwp"] > 0
    assert tables["designs"][0]["kva"] == 30.0


def test_auto_design_deye_reports_family_specific_missing_subassembly():
    tables = _base_tables()
    tables["subassemblies"] = [row for row in tables["subassemblies"] if row["id"] != "hybrid-ess"]
    with (
        patch.object(auto_designer, "Repository", side_effect=_repo_factory(tables)),
        patch("shared.grid_design.data.Repository", side_effect=_repo_factory(tables)),
    ):
        result = auto_designer.auto_design("d1", technology_family="deye")

    assert result == {
        "ok": False,
        "error": "No deye-compatible inverter/hybrid ESS subassembly found.",
    }
