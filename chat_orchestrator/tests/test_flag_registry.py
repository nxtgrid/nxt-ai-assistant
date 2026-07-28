"""Sync tests for the central flag registry and settings backends.

These guard the "single source of truth" guarantee: if someone adds/edits a flag
in ``shared/config/flag_registry.py`` but forgets to regenerate the example file
or drifts the settings service, one of these tests fails with a clear fix.
"""

from pathlib import Path

from shared.config import flag_registry as fr
from shared.config.settings_backends import (
    DigitalOceanBackend,
    EnvFileBackend,
    get_backend,
)

GENERATED_EXAMPLE = Path(fr.__file__).with_name("flags.env.example")


# --------------------------------------------------------------------------- #
# Registry integrity
# --------------------------------------------------------------------------- #
class TestRegistryIntegrity:
    def test_every_default_coerces_to_its_type(self):
        for name, flag in fr.FLAGS.items():
            value = flag.coerce(flag.default_str)
            if flag.type is fr.FlagType.BOOL:
                assert isinstance(value, bool), name
            elif flag.type is fr.FlagType.INT:
                assert isinstance(value, int), name
            elif flag.type is fr.FlagType.FLOAT:
                assert isinstance(value, float), name
            else:  # STR / JSON -> verbatim string
                assert isinstance(value, str), name

    def test_scopes_are_valid(self):
        valid = {fr.SCOPE_GLOBAL, fr.SERVICE_BOT}
        for name, flag in fr.FLAGS.items():
            assert flag.scope in valid, f"{name} has unexpected scope {flag.scope}"

    def test_coerce_falls_back_to_default_when_unset(self):
        assert fr.get("MAX_TOOL_ROUNDS", env={}) == 5
        assert fr.get("JIRA_ENABLED", env={}) is True
        assert fr.get("VERIFICATION_ENABLED", env={}) is False

    def test_env_override_is_typed(self):
        assert fr.get("MAX_TOOL_ROUNDS", env={"MAX_TOOL_ROUNDS": "9"}) == 9
        assert fr.get("JIRA_ENABLED", env={"JIRA_ENABLED": "false"}) is False
        assert fr.get("LAYOUT_POLE_SPACING_M", env={"LAYOUT_POLE_SPACING_M": "30"}) == 30.0

    def test_urgent_live_output_timeout_defaults_to_three_seconds(self):
        assert fr.get("URGENT_ALERT_LIVE_OUTPUT_TIMEOUT_SECONDS", env={}) == 3


# --------------------------------------------------------------------------- #
# Generated example file is current
# --------------------------------------------------------------------------- #
def test_generated_env_example_is_current():
    assert GENERATED_EXAMPLE.exists(), (
        "shared/config/flags.env.example missing — run "
        "`python -m shared.config.flag_registry > shared/config/flags.env.example`"
    )
    on_disk = GENERATED_EXAMPLE.read_text(encoding="utf-8")
    assert on_disk == fr.render_env_example(), (
        "flags.env.example is stale. Regenerate with: "
        "`python -m shared.config.flag_registry > shared/config/flags.env.example`"
    )


def test_documented_flags_appear_in_example():
    text = GENERATED_EXAMPLE.read_text(encoding="utf-8")
    for name, flag in fr.FLAGS.items():
        if flag.document:
            assert f"{name}=" in text, f"{name} should be documented in flags.env.example"
        else:
            assert f"{name}=" not in text, f"{name} should be excluded from flags.env.example"


def test_registry_has_no_jira_alert_profile_flags():
    assert not [name for name in fr.FLAGS if name.startswith("JIRA" + "_ALERT_")]


def test_generated_env_example_has_no_jira_alert_profile_block():
    rendered = fr.render_env_example()
    assert "JIRA" + "_ALERT_" not in rendered


def test_alert_settings_expose_only_operational_deployment_choices():
    visible = {flag.name for flag in fr.FLAGS.values() if flag.show_in_settings}
    assert "JIRA_PROJECT_KEY" in visible
    assert "ALERT_CORRELATION_ENABLED" in visible
    assert "NOTIFY_TICKETS_BACKEND" in visible
    assert {
        name
        for name in visible
        if name.startswith("ALERT_CORRELATION_")
        and name != "ALERT_CORRELATION_ENABLED"
    } == set()
    assert not {name for name in visible if name.startswith("JIRA" + "_ALERT_")}


# --------------------------------------------------------------------------- #
# Settings-service consistency (guards the migration away from hardcoded sets)
# --------------------------------------------------------------------------- #
class TestSettingsServiceConsistency:
    # The exact read-only set the settings service historically hardcoded.
    HISTORICAL_DO_NOT_SAVE = {
        "ESCALATION_TELEGRAM_CHAT_ID",
        "DEBUG_TELEGRAM_CHAT_ID",
        "EMBEDDING_MODEL",
        "GEMINI_MAX_OUTPUT_TOKENS",
        "GEMINI_LITE_MAX_OUTPUT_TOKENS",
        "CUSTOMER_SUPPORT_DOC_ID",
        "STAFF_SUPPORT_DOC_ID",
        "TROUBLESHOOTING_PROCEDURES_DOC_ID",
        "GRAFANA_PANELS_METADATA",
        "GRAFANA_AVAILABLE_DASHBOARDS",
    }

    def test_non_editable_covers_historical_read_only(self):
        assert self.HISTORICAL_DO_NOT_SAVE <= fr.non_editable_settings()

    def test_editable_flags_are_not_read_only(self):
        read_only = fr.non_editable_settings()
        for name in ("JIRA_ENABLED", "VERIFICATION_ENABLED", "GRAFANA_URL", "MAX_TOOL_ROUNDS"):
            assert name not in read_only

    def test_service_specific_routing(self):
        ss = fr.service_specific_settings()
        # Bot-scoped flags route to anansi-bot; everything in the map is non-global.
        assert ss["VERIFICATION_ENABLED"] == fr.SERVICE_BOT
        assert ss["LAYOUT_POLE_SPACING_M"] == fr.SERVICE_BOT
        assert all(v != fr.SCOPE_GLOBAL for v in ss.values())
        # Global flags are absent from the routing map.
        assert "JIRA_ENABLED" not in ss
        assert "MAX_TOOL_ROUNDS" not in ss

    def test_settings_defaults_excludes_routing_only_flags(self):
        defaults = fr.settings_defaults(env={})
        for hidden in ("DEFAULT_TIMEZONE", "STAFF_ORG_ID", "SETTINGS_BACKEND"):
            assert hidden not in defaults
        # but real UI flags are present and typed
        assert defaults["JIRA_ENABLED"] is True
        assert defaults["MAX_TOOL_ROUNDS"] == 5

    def test_lpp_template_flags_are_settings_visible(self):
        # Regression: the settings page renders these as editable text inputs
        # (Templates section). They MUST be show_in_settings=True so the page
        # loads their real DO values; with show_in_settings=False the overlay
        # dropped them, the inputs rendered blank, and the next Save wiped the
        # Drive IDs from the live spec — silently breaking the LPP workflow.
        defaults = fr.settings_defaults(env={})
        for k in ("LPP_TEMPLATE_ID", "LPP_OUTPUT_FOLDER_ID", "QGIS_TEMPLATE_FILE_ID"):
            assert k in defaults, f"{k} must be settings-visible"
            assert fr.FLAGS[k].show_in_settings is True
            assert fr.FLAGS[k].editable is True

    def test_provider_and_model_flags_are_editable_for_picker_guardrails(self):
        defaults = fr.settings_defaults(env={})
        for k in (
            "LLM_PROVIDER",
            "GEMINI_MODEL",
            "GEMINI_FALLBACK_MODEL",
            "INTENT_ROUTER_MODEL",
            "VERIFICATION_MODEL",
            "OPENROUTER_PROVIDER_ORDER",
            "OPENROUTER_ALLOW_FALLBACKS",
            "OPENROUTER_REQUIRE_PARAMETERS",
        ):
            assert k in defaults, f"{k} must be settings-visible"
            assert fr.FLAGS[k].show_in_settings is True
            assert fr.FLAGS[k].editable is True
        assert "OPENROUTER_MODEL" not in defaults
        assert fr.FLAGS["OPENROUTER_MODEL"].show_in_settings is False


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
class TestBackends:
    def test_envfile_round_trip(self, tmp_path):
        path = tmp_path / "settings.env"
        backend = EnvFileBackend(path=str(path))
        ok, err = backend.update({"JIRA_ENABLED": False, "MAX_TOOL_ROUNDS": 7})
        assert ok and err is None
        contents = path.read_text(encoding="utf-8")
        assert "JIRA_ENABLED=false" in contents
        assert "MAX_TOOL_ROUNDS=7" in contents

    def test_envfile_writes_editable_model_settings(self, tmp_path):
        path = tmp_path / "settings.env"
        backend = EnvFileBackend(path=str(path))
        backend.update({"GEMINI_MODEL": "gemini-2.5-flash", "JIRA_ENABLED": False})
        contents = path.read_text(encoding="utf-8")
        assert "GEMINI_MODEL=gemini-2.5-flash" in contents
        assert "JIRA_ENABLED=false" in contents

    def test_envfile_get_all_reads_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JIRA_ENABLED", "false")
        backend = EnvFileBackend(path=str(tmp_path / "none.env"))
        assert backend.get_all().get("JIRA_ENABLED") == "false"

    def test_get_backend_explicit_envfile(self, monkeypatch):
        monkeypatch.setenv("SETTINGS_BACKEND", "envfile")
        assert isinstance(get_backend(), EnvFileBackend)

    def test_get_backend_explicit_digitalocean(self, monkeypatch):
        monkeypatch.setenv("SETTINGS_BACKEND", "digitalocean")
        assert isinstance(get_backend(), DigitalOceanBackend)

    def test_get_backend_auto_without_do_creds(self, monkeypatch):
        monkeypatch.setenv("SETTINGS_BACKEND", "auto")
        monkeypatch.delenv("DIGITALOCEAN_APP_ID", raising=False)
        monkeypatch.delenv("DIGITALOCEAN_API_TOKEN", raising=False)
        assert isinstance(get_backend(), EnvFileBackend)

    def test_get_backend_auto_with_do_creds(self, monkeypatch):
        monkeypatch.setenv("SETTINGS_BACKEND", "auto")
        monkeypatch.setenv("DIGITALOCEAN_APP_ID", "abc123")
        monkeypatch.setenv("DIGITALOCEAN_API_TOKEN", "tok")
        assert isinstance(get_backend(), DigitalOceanBackend)

    def test_do_backend_spec_routing(self):
        """Global vs service-specific flags land in the right spec block."""
        backend = DigitalOceanBackend(app_id="x", api_token="y")
        spec = {"envs": [], "services": [{"name": fr.SERVICE_BOT, "envs": []}]}
        backend._apply_to_spec(
            spec,
            {
                "JIRA_ENABLED": False,  # global
                "VERIFICATION_ENABLED": True,  # anansi-bot
                "GEMINI_MODEL": "gemini-2.5-flash",  # editable via picker
            },
        )
        global_keys = {e["key"] for e in spec["envs"]}
        bot_keys = {e["key"] for e in spec["services"][0]["envs"]}
        assert "JIRA_ENABLED" in global_keys
        assert "VERIFICATION_ENABLED" in bot_keys
        assert "GEMINI_MODEL" in global_keys
        assert "GEMINI_MODEL" not in bot_keys


# --------------------------------------------------------------------------- #
# Required-flag validation (fail-loud startup helper)
# --------------------------------------------------------------------------- #
def test_validate_required_reports_missing():
    # No flags are required today, so an empty env is valid.
    assert fr.validate_required(env={}) == []


# --------------------------------------------------------------------------- #
# UI metadata (settings-page groups, labels, choices, bounds)
# --------------------------------------------------------------------------- #
class TestFlagUIMetadata:
    def test_group_ids_are_all_known(self):
        known = {group.id for group in fr.GROUPS}
        for name, flag in fr.FLAGS.items():
            assert flag.group in known, f"{name} has unknown group {flag.group!r}"

    def test_groups_have_unique_ids_and_are_ordered(self):
        ids = [group.id for group in fr.GROUPS]
        assert len(ids) == len(set(ids))
        assert ids[0] == "bot_control", "Bot Control must render first"

    def test_flags_in_group_returns_registration_order(self):
        names = [flag.name for flag in fr.flags_in_group("bot_control")]
        assert names[0] == "BOT_ENABLED"

    def test_display_label_falls_back_to_name(self):
        assert fr.FLAGS["BOT_ENABLED"].display_label == "BOT_ENABLED"

    def test_choices_always_contain_the_default(self):
        for name, flag in fr.FLAGS.items():
            if flag.choices:
                assert flag.default_str in flag.choices, (
                    f"{name} default {flag.default_str!r} is not among {flag.choices}"
                )

    def test_depends_on_targets_an_existing_bool_flag(self):
        for name, flag in fr.FLAGS.items():
            if flag.depends_on is None:
                continue
            target = fr.FLAGS.get(flag.depends_on)
            assert target is not None, f"{name} depends on unknown flag {flag.depends_on}"
            assert target.type is fr.FlagType.BOOL, (
                f"{name} depends on {flag.depends_on}, which is not a boolean"
            )

    def test_numeric_defaults_are_inside_declared_bounds(self):
        for name, flag in fr.FLAGS.items():
            if flag.type not in (fr.FlagType.INT, fr.FlagType.FLOAT):
                continue
            value = flag.coerce(flag.default_str)
            if flag.minimum is not None:
                assert value >= flag.minimum, f"{name} default below minimum"
            if flag.maximum is not None:
                assert value <= flag.maximum, f"{name} default above maximum"

    def test_read_only_flags_explain_where_to_set_them(self):
        for name, flag in fr.FLAGS.items():
            if not flag.editable and flag.show_in_settings:
                assert flag.set_via, f"{name} is read-only but has no set_via hint"
