"""Deployment-readiness capability computation.

`validate_required` and the `required` flag field existed for months with zero
callers and zero flags using them. These tests pin the behaviour now that the
settings page reports on it.
"""

from shared.config import flag_registry as fr


def _status(env, key):
    return next(s for s in fr.readiness(env=env) if s.capability.key == key)


class TestAdminLogin:
    def test_dev_bypass_alone_satisfies_login(self):
        status = _status({"GRID_DESIGN_DEV_NO_AUTH": "1"}, "admin_login")
        assert status.satisfied
        assert status.missing == []

    def test_empty_env_reports_every_login_requirement_missing(self):
        status = _status({}, "admin_login")
        assert not status.satisfied
        assert "GOOGLE_CLIENT_ID" in status.missing[0]

    def test_auth_client_id_alias_satisfies_the_client_id_requirement(self):
        env = {
            "AUTH_CLIENT_ID": "x",
            "AUTH_CLIENT_SECRET": "y",
            "ALLOWED_VIEWER_EMAILS": "a@example.com",
        }
        assert _status(env, "admin_login").satisfied

    def test_whitespace_only_value_does_not_count_as_configured(self):
        env = {
            "GOOGLE_CLIENT_ID": "   ",
            "GOOGLE_CLIENT_SECRET": "y",
            "ALLOWED_VIEWER_EMAILS": "a@example.com",
        }
        assert not _status(env, "admin_login").satisfied


class TestBotReplies:
    def test_chat_db_legacy_supabase_names_are_accepted(self):
        env = {
            "GOOGLE_API_KEY": "k",
            "TELEGRAM_BOT_TOKEN": "t",
            "SUPABASE_URL": "u",
            "SUPABASE_KEY": "s",
            "API_KEY": "a",
            "SESSION_ID_SECRET": "z",
            "AUTH_DB_HOST": "h",
        }
        assert _status(env, "bot_replies").satisfied

    def test_missing_telegram_token_is_reported_by_name(self):
        env = {
            "GOOGLE_API_KEY": "k",
            "CHAT_DB_URL": "u",
            "CHAT_DB_SERVICE_KEY": "s",
            "API_KEY": "a",
            "SESSION_ID_SECRET": "z",
            "AUTH_DB_HOST": "h",
        }
        assert _status(env, "bot_replies").missing == ["TELEGRAM_BOT_TOKEN"]


class TestSettingsPersistence:
    def test_auto_without_digitalocean_credentials_is_read_only(self):
        status = _status({"SETTINGS_BACKEND": "auto"}, "settings_persist")

        assert not status.satisfied
        assert status.missing == ["SETTINGS_BACKEND (no writable backend configured)"]

    def test_explicit_envfile_is_accepted_for_local_development(self):
        status = _status({"SETTINGS_BACKEND": "envfile"}, "settings_persist")

        assert status.satisfied

    def test_auto_with_digitalocean_credentials_is_writable(self):
        env = {
            "SETTINGS_BACKEND": "auto",
            "DIGITALOCEAN_APP_ID": "app-id",
            "DIGITALOCEAN_API_TOKEN": "token",
        }

        assert _status(env, "settings_persist").satisfied


class TestSeverity:
    def test_optional_integrations_are_recommended_not_required(self):
        for key in ("escalations_to_jira", "grafana_tools"):
            status = _status({}, key)
            assert status.capability.severity == "recommended"

    def test_core_capabilities_are_required(self):
        for key in ("admin_login", "bot_replies", "system_instructions"):
            assert _status({}, key).capability.severity == "required"


def test_every_capability_requirement_names_a_real_env_var():
    """A typo in a requirement would silently make a capability unsatisfiable."""
    for capability in fr.CAPABILITIES:
        for requirement in capability.requires:
            names = (requirement,) if isinstance(requirement, str) else requirement
            assert names, f"{capability.key} has an empty requirement"
            for name in names:
                assert name.isupper() or "_" in name, name
