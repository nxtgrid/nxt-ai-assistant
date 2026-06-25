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


# --------------------------------------------------------------------------- #
# Settings-service consistency (guards the migration away from hardcoded sets)
# --------------------------------------------------------------------------- #
class TestSettingsServiceConsistency:
    # The exact read-only set the settings service historically hardcoded.
    HISTORICAL_DO_NOT_SAVE = {
        "ESCALATION_TELEGRAM_CHAT_ID",
        "DEBUG_TELEGRAM_CHAT_ID",
        "GEMINI_MODEL",
        "GEMINI_FALLBACK_MODEL",
        "VERIFICATION_MODEL",
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
        for hidden in ("LPP_TEMPLATE_ID", "DEFAULT_TIMEZONE", "STAFF_ORG_ID", "SETTINGS_BACKEND"):
            assert hidden not in defaults
        # but real UI flags are present and typed
        assert defaults["JIRA_ENABLED"] is True
        assert defaults["MAX_TOOL_ROUNDS"] == 5


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

    def test_envfile_drops_read_only(self, tmp_path):
        path = tmp_path / "settings.env"
        backend = EnvFileBackend(path=str(path))
        backend.update({"GEMINI_MODEL": "evil-model", "JIRA_ENABLED": False})
        contents = path.read_text(encoding="utf-8")
        assert "GEMINI_MODEL" not in contents  # read-only, filtered
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
                "GEMINI_MODEL": "nope",  # read-only -> dropped
            },
        )
        global_keys = {e["key"] for e in spec["envs"]}
        bot_keys = {e["key"] for e in spec["services"][0]["envs"]}
        assert "JIRA_ENABLED" in global_keys
        assert "VERIFICATION_ENABLED" in bot_keys
        assert "GEMINI_MODEL" not in global_keys and "GEMINI_MODEL" not in bot_keys


# --------------------------------------------------------------------------- #
# Required-flag validation (fail-loud startup helper)
# --------------------------------------------------------------------------- #
def test_validate_required_reports_missing():
    # No flags are required today, so an empty env is valid.
    assert fr.validate_required(env={}) == []
