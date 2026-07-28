from nicegui_app.pages.settings_widgets import (
    RenderMode,
    render_mode,
    secret_placeholder,
    validate,
)
from shared.config import flag_registry as fr


class TestRenderMode:
    def test_bool_renders_a_switch(self):
        assert render_mode(fr.FLAGS["BOT_ENABLED"]) is RenderMode.SWITCH

    def test_enum_renders_a_select_not_a_text_box(self):
        assert render_mode(fr.FLAGS["TICKET_BACKEND_OVERRIDE"]) is RenderMode.SELECT

    def test_secret_renders_masked(self):
        assert render_mode(fr.FLAGS["GRAFANA_PASSWORD"]) is RenderMode.SECRET

    def test_read_only_wins_over_type(self):
        assert render_mode(fr.FLAGS["DEFAULT_TIMEZONE"]) is RenderMode.READ_ONLY

    def test_json_renders_a_textarea(self):
        assert render_mode(fr.FLAGS["MCP_DISABLED_TOOLS"]) is RenderMode.TEXTAREA

    def test_email_lists_render_as_chips(self):
        assert render_mode(fr.FLAGS["ALLOWED_VIEWER_EMAILS"]) is RenderMode.MULTI_SELECT


class TestSecretPlaceholder:
    def test_set_secret_shows_a_masked_marker_and_no_value(self):
        text = secret_placeholder(True)
        assert "set" in text.lower()
        assert "•" in text

    def test_unset_secret_says_so(self):
        assert secret_placeholder(False) == "not set"


class TestValidate:
    def test_enum_rejects_an_unlisted_value(self):
        error = validate(fr.FLAGS["TICKET_BACKEND_OVERRIDE"], "jra")
        assert error is not None and "auto" in error

    def test_enum_accepts_a_listed_value(self):
        assert validate(fr.FLAGS["TICKET_BACKEND_OVERRIDE"], "internal") is None

    def test_number_below_minimum_is_rejected(self):
        assert validate(fr.FLAGS["METRICS_SCHEDULE_HOUR"], -1) is not None

    def test_number_above_maximum_is_rejected(self):
        assert validate(fr.FLAGS["METRICS_SCHEDULE_HOUR"], 24) is not None

    def test_number_inside_bounds_is_accepted(self):
        assert validate(fr.FLAGS["METRICS_SCHEDULE_HOUR"], 9) is None

    def test_invalid_json_is_rejected(self):
        assert validate(fr.FLAGS["MCP_DISABLED_TOOLS"], "[not json") is not None

    def test_valid_json_is_accepted(self):
        assert validate(fr.FLAGS["MCP_DISABLED_TOOLS"], '["jira:create_issue"]') is None
