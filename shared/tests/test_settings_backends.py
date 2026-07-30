"""Tests for DigitalOceanBackend's env-var merge logic.

Regression coverage for a real gap: flags registered ``secret=True`` in
shared.config.flag_registry (e.g. GRAFANA_PASSWORD, OPENROUTER_API_KEY) were
being written to the DigitalOcean app spec without ``type: SECRET`` -- both
for brand-new keys and for existing ones that were previously saved as
GENERAL -- so DO stored their values in plaintext despite the registry
declaring them sensitive.
"""

from __future__ import annotations

from shared.config.settings_backends import DigitalOceanBackend


def test_merge_env_vars_marks_new_secret_flag_as_type_secret():
    result = DigitalOceanBackend._merge_env_vars([], {"GRAFANA_PASSWORD": "hunter2"})

    assert result == [
        {
            "key": "GRAFANA_PASSWORD",
            "value": "hunter2",
            "scope": "RUN_TIME",
            "type": "SECRET",
        }
    ]


def test_merge_env_vars_upgrades_existing_general_secret_to_type_secret():
    existing = [
        {"key": "OPENROUTER_API_KEY", "value": "old-key", "scope": "RUN_TIME"},
    ]

    result = DigitalOceanBackend._merge_env_vars(existing, {"OPENROUTER_API_KEY": "new-key"})

    assert result == [
        {
            "key": "OPENROUTER_API_KEY",
            "value": "new-key",
            "scope": "RUN_TIME",
            "type": "SECRET",
        }
    ]


def test_merge_env_vars_does_not_mark_non_secret_flag_as_secret():
    result = DigitalOceanBackend._merge_env_vars([], {"BOT_ENABLED": "true"})

    assert result == [{"key": "BOT_ENABLED", "value": "true", "scope": "RUN_TIME"}]
