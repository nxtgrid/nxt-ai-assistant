import sys
from types import SimpleNamespace

sys.modules.setdefault("nicegui", SimpleNamespace(run=SimpleNamespace(), ui=SimpleNamespace()))

from nicegui_app.pages import settings as page

from shared.config import flag_registry as fr


def _pending(**overrides):
    values = {name: flag.coerce(None) for name, flag in fr.FLAGS.items()}
    values.update(overrides)
    return values


class TestVisibleFlags:
    def test_advanced_flags_are_hidden_by_default(self):
        names = [f.name for f in page.visible_flags("layout", _pending(), False, "")]
        assert "LAYOUT_POWER_FACTOR" not in names

    def test_advanced_flags_appear_when_requested(self):
        names = [f.name for f in page.visible_flags("layout", _pending(), True, "")]
        assert "LAYOUT_POWER_FACTOR" in names

    def test_search_matches_the_env_var_name(self):
        names = [f.name for f in page.visible_flags("ticketing", _pending(), True, "prefix")]
        assert names == ["INTERNAL_TICKET_PREFIX"]

    def test_search_matches_the_description(self):
        names = [f.name for f in page.visible_flags("bot_control", _pending(), True, "telegram")]
        assert "BOT_ENABLED" in names

    def test_search_matches_the_human_label(self):
        names = [f.name for f in page.visible_flags("models", _pending(), True, "main model")]
        assert "GEMINI_MODEL" in names

    def test_search_is_case_insensitive(self):
        assert page.visible_flags("bot_control", _pending(), True, "LOG_LEVEL")
        assert page.visible_flags("bot_control", _pending(), True, "log_level")

    def test_a_flag_whose_dependency_is_off_is_hidden(self):
        pending = _pending(rag__enabled=False)
        names = [f.name for f in page.visible_flags("knowledge", pending, True, "")]
        assert names == ["rag__enabled"]

    def test_a_flag_whose_dependency_is_on_is_shown(self):
        pending = _pending(rag__enabled=True)
        names = [f.name for f in page.visible_flags("knowledge", pending, True, "")]
        assert "rag__top_k" in names


class TestGroupIsInert:
    def test_grafana_group_is_inert_when_the_server_is_disabled(self):
        assert page.group_is_inert("grafana", _pending(GRAFANA_ENABLED=False)) is True

    def test_grafana_group_is_active_when_the_server_is_enabled(self):
        assert page.group_is_inert("grafana", _pending(GRAFANA_ENABLED=True)) is False

    def test_a_group_with_no_shared_dependency_is_never_inert(self):
        assert page.group_is_inert("bot_control", _pending()) is False


def test_page_contains_no_hardcoded_flag_names():
    """Adding a flag to the registry must require no edit to this page.

    The Grafana dashboard/panel picker is a genuine bespoke widget and is the
    only permitted exception.
    """
    import inspect

    source = inspect.getsource(page)
    permitted = {
        "LOG_LEVEL",  # options can come from a live service call, not just flag.choices
        "GRAFANA_ENABLED_DASHBOARDS",
        "GRAFANA_ENABLED_PANELS",
        "GRAFANA_SYNC_HOUR",
        "GRAFANA_FORCE_FULL_REINDEX",
        "GRAFANA_PANELS_METADATA",
        "GRAFANA_AVAILABLE_DASHBOARDS",
        "GRAFANA_URL",
        "GRAFANA_USERNAME",
        "GRAFANA_FOLDER_NAME",
        "GRAFANA_PANEL_DESCRIPTION_PROMPT",
        "LLM_PROVIDER",
        "GEMINI_MODEL",
        "GEMINI_FALLBACK_MODEL",
        "GEMINI_DEEP_THINKING_MODEL",
        "INTENT_ROUTER_MODEL",
        "VERIFICATION_MODEL",
        "OPENROUTER_MODEL",
        "OPENROUTER_PROVIDER_ORDER",
        "OPENROUTER_ALLOW_FALLBACKS",
        "OPENROUTER_REQUIRE_PARAMETERS",
    }
    leaked = sorted(
        name for name in fr.FLAGS if name not in permitted and f'"{name}"' in source
    )
    assert leaked == [], f"page still hardcodes {leaked}; move it to the registry"
