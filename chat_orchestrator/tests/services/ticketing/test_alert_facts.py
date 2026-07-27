"""Table-driven tests for alert_facts.py, using real alert subjects/text
lifted from the n8n "Build Alert Actions1" node (see
docs/superpowers/plans/2026-07-27-smart-alert-correlation-notify.md,
"Current architecture (n8n side)").

The core property under test: MPPT A3 and MPPT A7 firing on the same grid
must produce the *same* signature (so they surface as amend candidates for
each other) but *different* component keys (so the deterministic duplicate
check -- which compares the full (signature, component_key) pair, not
signature alone -- correctly treats them as distinct affected components
rather than a re-fire of the same one).
"""

from __future__ import annotations

from orchestrator.services.ticketing.alert_facts import (
    AlertFacts,
    derive_component,
    derive_severity,
    derive_signature,
    enrich_alert_facts,
    normalize_subject,
)


class TestDeriveSeverity:
    def test_urgent(self):
        assert derive_severity("! Urgent: Inverter Fault reported in Kudi !") == "urgent"

    def test_warning(self):
        assert derive_severity("! Warning: FS delivery in Kudi seems to be lower !") == "warning"

    def test_neither(self):
        assert derive_severity("Some other subject") == ""

    def test_case_insensitive(self):
        assert derive_severity("URGENT: something") == "urgent"


class TestDeriveComponent:
    def test_mppt_from_subject_and_text(self):
        subject = "! Warning: MPPT A3 in Kudi seems to perform lower than other MPPTs !"
        text = "mppt A3 [Kudi] performance dropped"
        kind, key, label = derive_component(subject, text)
        assert kind == "mppt"
        assert key == "A3"
        assert label == "MPPT A3"

    def test_different_mppt_key(self):
        subject = "! Warning: MPPT A7 in Kudi seems to perform lower than other MPPTs !"
        text = "mppt A7 [Kudi] performance dropped"
        kind, key, label = derive_component(subject, text)
        assert kind == "mppt"
        assert key == "A7"
        assert label == "MPPT A7"

    def test_dcu_nine_digit(self):
        subject = "! Warning: DCU 123456789 in Kudi could have a problem, causing Meter Issues !"
        text = "dcu 123456789 reporting fault grid Kudi"
        kind, key, label = derive_component(subject, text)
        assert kind == "dcu"
        assert key == "123456789"
        assert label == "DCU 123456789"

    def test_base_station_sixteen_hex(self):
        subject = "! Warning: Base Station a1b2c3d4e5f60718 in Kudi could have a problem !"
        text = "dcu a1b2c3d4e5f60718 reporting fault grid Kudi"
        kind, key, label = derive_component(subject, text)
        assert kind == "base_station"
        assert key == "a1b2c3d4e5f60718"
        assert label == "Base Station a1b2c3d4e5f60718"

    def test_no_match_returns_blank(self):
        subject = "! Urgent: Inverter Fault reported in Kudi, could be causing Grid outage !"
        kind, key, label = derive_component(subject, "")
        assert (kind, key, label) == ("", "", "")

    def test_finds_pattern_in_text_when_absent_from_subject(self):
        subject = "! Warning: MPPT performance drop detected !"
        text = "mppt B1 [Kudi] below threshold"
        kind, key, label = derive_component(subject, text)
        assert kind == "mppt"
        assert key == "B1"


class TestNormalizeSubject:
    def test_strips_marker_and_trailing_bang(self):
        result = normalize_subject("! Warning: FS delivery seems low !")
        assert result == "fs delivery seems low"

    def test_strips_urgent_marker(self):
        result = normalize_subject("! Urgent: Inverter Fault reported !")
        assert result == "inverter fault reported"

    def test_masks_percentages(self):
        result = normalize_subject("! Warning: delivery below 75% threshold !")
        assert "75" not in result
        assert "#" in result

    def test_masks_numbers(self):
        a = normalize_subject("! Warning: dropped by 20% today !")
        b = normalize_subject("! Warning: dropped by 35% today !")
        assert a == b

    def test_component_key_removed_when_provided(self):
        a = normalize_subject(
            "! Warning: MPPT A3 in Kudi seems to perform lower than other MPPTs !",
            component_key="A3",
        )
        b = normalize_subject(
            "! Warning: MPPT A7 in Kudi seems to perform lower than other MPPTs !",
            component_key="A7",
        )
        assert a == b
        assert "a3" not in a
        assert "a7" not in b

    def test_collapses_whitespace(self):
        result = normalize_subject("! Warning:   too   many   spaces  !")
        assert "  " not in result


class TestDeriveSignature:
    def test_same_grid_same_subject_shape_different_mppt_key_same_signature(self):
        sig_a3 = derive_signature(
            grid_name="Kudi",
            component_kind="mppt",
            subject="! Warning: MPPT A3 in Kudi seems to perform lower than other MPPTs !",
            component_key="A3",
        )
        sig_a7 = derive_signature(
            grid_name="Kudi",
            component_kind="mppt",
            subject="! Warning: MPPT A7 in Kudi seems to perform lower than other MPPTs !",
            component_key="A7",
        )
        assert sig_a3 == sig_a7

    def test_different_grid_different_signature(self):
        sig_kudi = derive_signature(
            grid_name="Kudi", component_kind="mppt", subject="MPPT A3 low", component_key="A3"
        )
        sig_other = derive_signature(
            grid_name="OtherGrid",
            component_kind="mppt",
            subject="MPPT A3 low",
            component_key="A3",
        )
        assert sig_kudi != sig_other

    def test_different_component_kind_different_signature(self):
        sig_mppt = derive_signature(
            grid_name="Kudi", component_kind="mppt", subject="low", component_key="A3"
        )
        sig_dcu = derive_signature(
            grid_name="Kudi", component_kind="dcu", subject="low", component_key="A3"
        )
        assert sig_mppt != sig_dcu

    def test_different_subject_shape_different_signature(self):
        sig_low_production = derive_signature(
            grid_name="Kudi", component_kind="mppt", subject="MPPT performance low"
        )
        sig_offline = derive_signature(
            grid_name="Kudi", component_kind="mppt", subject="MPPT went offline"
        )
        assert sig_low_production != sig_offline

    def test_deterministic(self):
        args = dict(grid_name="Kudi", component_kind="mppt", subject="low", component_key="A3")
        assert derive_signature(**args) == derive_signature(**args)

    def test_is_short_hex_string(self):
        sig = derive_signature(grid_name="Kudi", component_kind="mppt", subject="low")
        assert len(sig) == 16
        int(sig, 16)  # raises ValueError if not hex


class TestEnrichAlertFacts:
    def test_fills_severity_component_and_signature(self):
        alert = AlertFacts(
            subject="! Warning: MPPT A3 in Kudi seems to perform lower than other MPPTs !",
            details="mppt A3 [Kudi] performance dropped",
        )

        enriched = enrich_alert_facts(alert, grid_name="Kudi")

        assert enriched.severity == "warning"
        assert enriched.component_kind == "mppt"
        assert enriched.component_key == "A3"
        assert enriched.component_label == "MPPT A3"
        assert enriched.signature
        assert enriched.fired_at

    def test_preserves_explicitly_supplied_fields(self):
        """n8n (post-Task-13 cutover) supplies these directly -- enrichment
        must not clobber caller-supplied values."""
        alert = AlertFacts(
            subject="! Warning: MPPT A3 in Kudi !",
            severity="urgent",  # caller says urgent even though subject says Warning
            component_kind="mppt",
            component_key="A3",
            component_label="MPPT A3 (custom)",
            fired_at="2026-07-01T00:00:00Z",
        )

        enriched = enrich_alert_facts(alert, grid_name="Kudi")

        assert enriched.severity == "urgent"
        assert enriched.component_label == "MPPT A3 (custom)"
        assert enriched.fired_at == "2026-07-01T00:00:00Z"

    def test_same_grid_different_mppt_produce_same_signature_via_enrich(self):
        a3 = enrich_alert_facts(
            AlertFacts(
                subject="! Warning: MPPT A3 in Kudi seems to perform lower than other MPPTs !",
                details="mppt A3 [Kudi]",
            ),
            grid_name="Kudi",
        )
        a7 = enrich_alert_facts(
            AlertFacts(
                subject="! Warning: MPPT A7 in Kudi seems to perform lower than other MPPTs !",
                details="mppt A7 [Kudi]",
            ),
            grid_name="Kudi",
        )

        assert a3.signature == a7.signature
        assert a3.component_key != a7.component_key
