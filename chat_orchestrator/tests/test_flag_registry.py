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
        # Every flag is app-level today: DigitalOcean's per-service env block
        # required an exact service name match, and the one non-global scope
        # ("anansi-bot") pointed at a service that no longer exists once the
        # app split into chat-orchestrator/tools-service/anansi-app, so saves
        # for those flags were silently dropped. Global envs are inherited by
        # every service, which is what all of them actually need.
        for name, flag in fr.FLAGS.items():
            assert flag.scope == fr.SCOPE_GLOBAL, f"{name} has unexpected scope {flag.scope}"

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

    def test_always_file_escalation_as_ticket_defaults_to_false(self):
        assert fr.get("ALWAYS_FILE_ESCALATION_AS_TICKET", env={}) is False


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
    # Matched as whole lines, not substrings: "API_KEY=" is also a suffix of
    # "OPENROUTER_API_KEY=", which a naive `in` check would false-positive on.
    lines = set(GENERATED_EXAMPLE.read_text(encoding="utf-8").splitlines())
    for name, flag in fr.FLAGS.items():
        expected_value = "" if flag.secret else flag.default_str
        present = f"{name}={expected_value}" in lines
        if flag.document:
            assert present, f"{name} should be documented in flags.env.example"
        else:
            assert not present, f"{name} should be excluded from flags.env.example"


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
    # GEMINI_MAX_OUTPUT_TOKENS was historically read-only for no discoverable
    # reason and was made editable by the settings-ux-redesign (it disagreed
    # with the orchestrator's own bounds, so the settings page couldn't reach
    # the documented behaviour) -- it is deliberately absent from this set now.
    HISTORICAL_DO_NOT_SAVE = {
        "ESCALATION_TELEGRAM_CHAT_ID",
        "DEBUG_TELEGRAM_CHAT_ID",
        "EMBEDDING_MODEL",
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
        # No flag opts into a specific DigitalOcean service today (see
        # test_scopes_are_valid) — every flag is global, so the routing map
        # this feeds DigitalOceanBackend._apply_to_spec with is empty.
        assert fr.service_specific_settings() == {}

    def test_settings_defaults_includes_deployment_flags_as_read_only(self):
        # DEFAULT_TIMEZONE / STAFF_ORG_ID / SETTINGS_BACKEND / SETTINGS_FILE were
        # hidden entirely (show_in_settings=False), so an operator debugging why
        # settings weren't persisting couldn't see which backend was active. The
        # settings-ux-redesign made them visible, read-only Deployment entries.
        defaults = fr.settings_defaults(env={})
        for visible_read_only in (
            "DEFAULT_TIMEZONE",
            "STAFF_ORG_ID",
            "SETTINGS_BACKEND",
            "SETTINGS_FILE",
        ):
            assert visible_read_only in defaults
            assert fr.FLAGS[visible_read_only].editable is False
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
        """All writable flags land in the app-level spec block (see test_scopes_are_valid)."""
        backend = DigitalOceanBackend(app_id="x", api_token="y")
        spec = {"envs": [], "services": [{"name": "chat-orchestrator", "envs": []}]}
        backend._apply_to_spec(
            spec,
            {
                "JIRA_ENABLED": False,
                "VERIFICATION_ENABLED": True,
                "GEMINI_MODEL": "gemini-2.5-flash",
                "LLM_PROVIDER": "openrouter",
            },
        )
        global_keys = {e["key"] for e in spec["envs"]}
        service_keys = {e["key"] for e in spec["services"][0]["envs"]}
        assert global_keys == {"JIRA_ENABLED", "VERIFICATION_ENABLED", "GEMINI_MODEL", "LLM_PROVIDER"}
        assert service_keys == set()


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


# --------------------------------------------------------------------------- #
# Newly registered flags: registering must not change runtime behaviour
# --------------------------------------------------------------------------- #
class TestNewlyRegisteredFlagsMatchTheirConsumers:
    """Registering a flag must not change runtime behaviour.

    Each expected value here was read from the module that actually consumes the
    variable. If a consumer's default changes, this test fails and the registry
    gets updated with it -- which is the drift these assertions exist to stop.
    """

    EXPECTED = {
        "AGENT_MAX_ACTIONS_PER_WAKE": 10,
        "AGENT_MAX_TOOL_ROUNDS": 5,
        "LOOP_DETECTION_ENABLED": True,
        "LOOP_DETECTION_THRESHOLD": 2,
        "MULTI_SITE_MAX_CONCURRENCY": 5,
        "STARTUP_RECOVERY_ENABLED": True,
        "JIRA_SWEEP_ENABLED": True,
        "JIRA_ISSUE_TYPE": "Task",
        "METRICS_TIMEZONE": "UTC",
        "AFTER_HOURS_START_HOUR": 19,
        "GEMINI_THINKING_BUDGET": 4096,
        "GEMINI_AGENT_PRO_MODEL": "gemini-2.5-pro",
        "THREAD_CLASSIFIER_MODEL": "gemini-2.5-flash-lite",
        "GOOGLE_SEARCH_GROUNDING": True,
        "GRAFANA_ACTIONS_ENABLED": False,
        "GRAFANA_QUERY_TIMEOUT": 180,
        "GRAFANA_METADATA_TIMEOUT": 30,
        "GRAFANA_VARIABLE_TIMEOUT": 60,
        "ORGANIZATION_NAME": "the operator",
        "DOC_CODE_PREFIX": "DOC",
        "STAFF_ORG_NAME": "Staff",
        "MANAGED_GENERATION_COLUMN": "is_generation_managed_by_nxt_grid",
        "LAYOUT_KW_PER_HOUSEHOLD": 0.0,
        "LAYOUT_MAX_BRIDGE_DISTANCE_M": 200.0,
        "LAYOUT_PATH_REDUNDANCY_DISTANCE_M": 22.5,
        "LAYOUT_PATH_WEIGHT_PENALTY": 3.0,
        "LAYOUT_PLANT_CONNECT_DISTANCE_M": 150.0,
        "LAYOUT_PLANT_CONNECT_K": 5,
        "LAYOUT_POWER_FACTOR": 0.95,
        "LAYOUT_ROAD_CLIP_BUFFER_M": 100.0,
        "LAYOUT_WATERWAY_BUFFER_M": 200.0,
    }

    def test_each_new_flag_keeps_its_consumer_default(self):
        for name, expected in self.EXPECTED.items():
            assert name in fr.FLAGS, f"{name} is not registered"
            assert fr.get(name, env={}) == expected, name

    def test_every_mcp_server_has_a_write_gate(self):
        for server in fr.MCP_SERVER_NAMES:
            name = f"{server.upper()}_ACTIONS_ENABLED"
            assert name in fr.FLAGS, f"{name} missing -- write gating is invisible"
            assert fr.FLAGS[name].group == "tools"

    def test_after_hours_timezone_defaults_to_empty_not_utc(self):
        # The consumer falls back to DEFAULT_TIMEZONE at read time; baking "UTC"
        # into the registry would silently override a deployment's own timezone.
        assert fr.get("AFTER_HOURS_TIMEZONE", env={}) == ""


# --------------------------------------------------------------------------- #
# Registry values must match the orchestrator that actually runs
# --------------------------------------------------------------------------- #
class TestRegistryMatchesOrchestratorDefaults:
    """The settings page must not display a value the orchestrator ignores."""

    def test_fallback_model_matches_the_orchestrator(self):
        assert fr.get("GEMINI_FALLBACK_MODEL", env={}) == "gemini-2.5-flash-lite"

    def test_deep_thinking_model_matches_the_orchestrator(self):
        assert fr.get("GEMINI_DEEP_THINKING_MODEL", env={}) == "gemini-pro-latest"

    def test_temperature_is_editable(self):
        # Rendered read-only at 0.2 while the orchestrator treats empty as "use
        # the model default" -- an operator could not reach the documented
        # behaviour from the UI at all.
        assert fr.FLAGS["GEMINI_TEMPERATURE"].editable is True

    def test_main_max_output_tokens_is_editable(self):
        assert fr.FLAGS["GEMINI_MAX_OUTPUT_TOKENS"].editable is True
