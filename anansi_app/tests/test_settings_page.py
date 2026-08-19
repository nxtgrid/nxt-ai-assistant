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
        names = [f.name for f in page.visible_flags("models", _pending(), True, "fast-tier")]
        assert "MODEL_FAST" in names

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

    def test_a_group_is_not_inert_when_only_one_flag_depends_on_an_unrelated_toggle(self):
        """"conversation" hosts 15 flags; only ACTIVE_THREAD_WINDOW_MINUTES
        depends_on THREAD_DISENTANGLEMENT_ENABLED, a toggle that belongs to a
        different feature entirely (the other two dependent flags in this
        group -- VERIFICATION_DOC_ID and LOOP_DETECTION_THRESHOLD -- each
        depend on their own, still different, toggles). That one flag being
        off must not hide the whole conversation section.
        """
        pending = _pending(THREAD_DISENTANGLEMENT_ENABLED=False)
        assert page.group_is_inert("conversation", pending) is False


class TestGroupExpansionPresentation:
    def test_every_group_is_closed_without_a_search(self):
        for group in fr.groups():
            presentation = page.group_expansion_presentation(query="", inert=False)
            assert presentation.expanded is False, group.id

    def test_a_search_opens_matching_active_groups(self):
        presentation = page.group_expansion_presentation(query="model", inert=False)
        assert presentation.expanded is True

    def test_an_inert_group_stays_closed_during_search(self):
        presentation = page.group_expansion_presentation(query="grafana", inert=True)
        assert presentation.expanded is False

    def test_closed_and_open_icons_point_right_and_down(self):
        presentation = page.group_expansion_presentation(query="", inert=False)
        assert presentation.expand_icon == "chevron_right"
        assert presentation.expanded_icon == "expand_more"


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
        "MODEL_THINKING",
        "MODEL_FAST",
        "MODEL_LITE",
        "FALLBACK_MODEL",
        "OPENROUTER_MODEL",
        "OPENROUTER_PROVIDER_ORDER",
        "OPENROUTER_ALLOW_FALLBACKS",
        "OPENROUTER_REQUIRE_PARAMETERS",
    }
    leaked = sorted(
        name for name in fr.FLAGS if name not in permitted and f'"{name}"' in source
    )
    assert leaked == [], f"page still hardcodes {leaked}; move it to the registry"
