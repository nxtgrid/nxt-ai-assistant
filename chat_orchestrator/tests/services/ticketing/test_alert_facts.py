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

    def test_ignores_urgent_word_elsewhere_in_subject(self):
        assert (
            derive_severity("! Warning: DCU 862406008 needs urgent attention in Belel !")
            == "warning"
        )

    def test_ignores_warning_word_elsewhere_in_subject(self):
        assert (
            derive_severity("! Urgent: Battery fault, disregard prior warning !")
            == "urgent"
        )

    def test_bare_word_with_no_marker_is_unclassified(self):
        assert derive_severity("This is urgent, please check Belel") == ""


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

    def test_solar_charger_mppt_with_model_between_id_and_bracket(self):
        subject = (
            "! Urgent: Turn off Combiner: ALERT - 'Okpokunou': "
            "'#26 - Charger terminal overheated' on "
            "'Solar Charger - MPPT PNXG ARTN4.50/100/10 [27]' !"
        )
        kind, key, label = derive_component(subject, "")
        assert kind == "mppt"
        assert key == "PNXG#27"
        assert label == "MPPT PNXG#27"

    def test_same_charger_id_different_instance_is_a_different_component(self):
        first = derive_component("Solar Charger - MPPT PNXG ARTN4.50/100/10 [27]", "")
        second = derive_component("Solar Charger - MPPT PNXG ARTN4.50/100/10 [28]", "")
        assert first[1] != second[1]

    def test_prose_mppt_mention_does_not_swallow_a_later_real_mention(self):
        subject = "! Warning: MPPT performance issue on MPPT B1 [5] !"
        kind, key, label = derive_component(subject, "")
        assert kind == "mppt"
        assert key == "B1#5"
        assert label == "MPPT B1#5"

    def test_mppt_id_without_any_bracket(self):
        subject = "! Warning: MPPT IYYY in Ogheye seems to perform lower than other MPPTs !"
        kind, key, label = derive_component(subject, "")
        assert kind == "mppt"
        assert key == "IYYY"
        assert label == "MPPT IYYY"

    def test_prose_after_mppt_is_not_treated_as_an_id(self):
        kind, key, label = derive_component("! Warning: MPPT performance drop detected !", "")
        assert (kind, key, label) == ("", "", "")

    def test_solar_charger_bracket_only_is_an_mppt(self):
        """finding 2: 'Solar Charger [278]' has no MPPT word at all and
        previously parsed as component-less."""
        kind, key, label = derive_component("Solar Charger [278]", "")
        assert kind == "mppt"
        assert key == "278"
        assert label == "MPPT 278"

    def test_solar_charger_with_token_and_model_is_an_mppt(self):
        kind, key, label = derive_component(
            "Solar Charger - VT6Y ARTN4.4/-141/32 House 4 [8]", ""
        )
        assert kind == "mppt"
        assert key == "VT6Y#8"
        assert label == "MPPT VT6Y#8"

    def test_solar_charger_bracket_instance_differs_by_id(self):
        first = derive_component("Solar Charger [278]", "")
        second = derive_component("Solar Charger [279]", "")
        assert first[1] == "278"
        assert second[1] == "279"

    def test_solar_charger_without_bracket_or_mppt_word_returns_blank(self):
        kind, key, label = derive_component("Solar Charger needs servicing", "")
        assert (kind, key, label) == ("", "", "")

    def test_literal_mppt_word_still_wins_over_solar_charger_pattern(self):
        """A subject naming both 'Solar Charger' and 'MPPT' (the common VRM
        shape) must still resolve through _MPPT_PATTERN so its key stays
        TOKEN#instance -- unchanged behavior, just confirming the new
        Solar Charger check (tried after MPPT) does not shadow it."""
        kind, key, label = derive_component(
            "Solar Charger - MPPT PNXG ARTN4.50/100/10 [27]", ""
        )
        assert kind == "mppt"
        assert key == "PNXG#27"


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

    def test_masks_vrm_device_clause_structurally(self):
        """finding 1: component_key is synthesized (TOKEN#instance) and never
        appears literally in the subject, so the old literal-removal did
        nothing and the device token + location word leaked into the hash."""
        a = normalize_subject(
            "! Urgent: ALERT - 'Akinsolu': '#67 - No BMS' on "
            "'Solar Charger - MPPT KBUA ARTN4.4/-176/5 Cabin [5]' !"
        )
        b = normalize_subject(
            "! Urgent: ALERT - 'Akinsolu': '#67 - No BMS' on "
            "'Solar Charger - MPPT 65SQ ARTN4.4/-141/32 House [0]' !"
        )
        assert a == b
        assert "kbua" not in a
        assert "65sq" not in b
        assert "cabin" not in a
        assert "house" not in b

    def test_device_clause_masking_preserves_a_differing_fault(self):
        no_bms = normalize_subject(
            "! Urgent: ALERT - 'Akinsolu': '#67 - No BMS' on "
            "'Solar Charger - MPPT KBUA ARTN4.4/-176/5 Cabin [5]' !"
        )
        low_voltage = normalize_subject(
            "! Urgent: ALERT - 'Akinsolu': '#68 - Low voltage' on "
            "'Solar Charger - MPPT KBUA ARTN4.4/-176/5 Cabin [5]' !"
        )
        assert no_bms != low_voltage

    def test_bracketless_mppt_mention_still_masked_without_a_provided_key(self):
        """The guarded MPPT mask must fire even when the caller doesn't pass
        component_key -- e.g. the correlator's replay/duplicate paths that
        normalize a raw subject before a component is known."""
        a = normalize_subject(
            "! Warning: MPPT A3 in Kudi seems to perform lower than other MPPTs !"
        )
        b = normalize_subject(
            "! Warning: MPPT A7 in Kudi seems to perform lower than other MPPTs !"
        )
        assert a == b
        assert "a3" not in a
        assert "a7" not in b


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


class TestRealProductionStormSignatures:
    """Regression coverage for the plan's findings 1 and 2 (the 2026-08-08
    Akinsolu 'No BMS' storm and the Ogbinbiri 'Solar Charger' pair). Drives
    derive_component + derive_signature together, the same way
    enrich_alert_facts does, so this exercises the real pipeline rather than
    normalize_subject/derive_component in isolation."""

    _AKINSOLU_NO_BMS_SUBJECTS = [
        "! Urgent: ALERT - 'Akinsolu': '#67 - No BMS' on "
        "'Solar Charger - MPPT KBUA ARTN4.4/-176/5 Cabin [5]' !",
        "! Urgent: ALERT - 'Akinsolu': '#67 - No BMS' on "
        "'Solar Charger - MPPT 65SQ ARTN4.4/-141/32 House [0]' !",
        "! Urgent: ALERT - 'Akinsolu': '#67 - No BMS' on "
        "'Solar Charger - MPPT JD65 ARTN4.4/-176/5 Cabin [3]' !",
        "! Urgent: ALERT - 'Akinsolu': '#67 - No BMS' on "
        "'Solar Charger - MPPT RH2W ARTN4.4/-176/5 Cabin [6]' !",
        "! Urgent: ALERT - 'Akinsolu': '#67 - No BMS' on "
        "'Solar Charger - MPPT QI11 ARTN4.4/+27/24 Church [2]' !",
        "! Urgent: ALERT - 'Akinsolu': '#67 - No BMS' on "
        "'Solar Charger - MPPT LQLA ARTN4.4/27/24 Church [1]' !",
    ]

    _OGBINBIRI_NO_BMS_SUBJECTS = [
        "! Urgent: ALERT - 'Ogbinbiri': '#67 - No BMS' on 'Solar Charger [278]' !",
        "! Urgent: ALERT - 'Ogbinbiri': '#67 - No BMS' on 'Solar Charger [279]' !",
    ]

    def _signature_for(self, subject: str, grid_name: str) -> str:
        kind, key, _ = derive_component(subject)
        return derive_signature(
            grid_name=grid_name, component_kind=kind, subject=subject, component_key=key
        )

    def test_six_akinsolu_no_bms_mppt_alerts_share_one_signature(self):
        signatures = {
            self._signature_for(subject, "Akinsolu")
            for subject in self._AKINSOLU_NO_BMS_SUBJECTS
        }
        assert len(signatures) == 1

    def test_akinsolu_no_bms_alerts_keep_six_distinct_component_keys(self):
        keys = {derive_component(subject)[1] for subject in self._AKINSOLU_NO_BMS_SUBJECTS}
        assert len(keys) == len(self._AKINSOLU_NO_BMS_SUBJECTS)

    def test_two_ogbinbiri_solar_charger_alerts_share_one_signature(self):
        signatures = {
            self._signature_for(subject, "Ogbinbiri")
            for subject in self._OGBINBIRI_NO_BMS_SUBJECTS
        }
        assert len(signatures) == 1

    def test_ogbinbiri_solar_chargers_keep_distinct_component_keys(self):
        keys = {derive_component(subject)[1] for subject in self._OGBINBIRI_NO_BMS_SUBJECTS}
        assert keys == {"278", "279"}

    def test_a_different_fault_on_the_same_device_still_differs(self):
        no_bms = self._signature_for(self._AKINSOLU_NO_BMS_SUBJECTS[0], "Akinsolu")
        low_voltage = self._signature_for(
            "! Urgent: ALERT - 'Akinsolu': '#68 - Low voltage' on "
            "'Solar Charger - MPPT KBUA ARTN4.4/-176/5 Cabin [5]' !",
            "Akinsolu",
        )
        assert no_bms != low_voltage


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
