"""Tests for Telegram markdown utilities."""

from shared.utils.telegram_markdown import (
    convert_github_to_telegram_markdown,
    escape_markdown,
    sanitize_for_telegram,
    strip_markdown,
)


class TestSlashCommandProtection:
    """Tests for slash command protection from underscore escaping."""

    def test_slash_command_with_underscores(self):
        """Slash commands with underscores should be preserved."""
        result = convert_github_to_telegram_markdown("/equipment_history is useful")
        assert "/equipment_history" in result
        # Ensure no protection markers leaked through
        assert "⟦CMD" not in result
        assert "__PROTECTED" not in result

    def test_multiple_commands(self):
        """Multiple slash commands should all be preserved."""
        result = convert_github_to_telegram_markdown("/first_cmd and /second_cmd")
        assert "/first_cmd" in result
        assert "/second_cmd" in result

    def test_mixed_content(self):
        """Slash commands and regular identifiers should be handled correctly."""
        result = convert_github_to_telegram_markdown("Run /meter_reading for grid_name")
        assert "/meter_reading" in result
        # grid_name should have escaped underscore (outside slash command)
        assert "grid\\_name" in result

    def test_command_with_multiple_underscores(self):
        """Commands with multiple underscores should be preserved."""
        result = convert_github_to_telegram_markdown("Try /very_long_command_name")
        assert "/very_long_command_name" in result

    def test_command_at_line_start(self):
        """Command at line start should be preserved."""
        result = convert_github_to_telegram_markdown("/start_here\nsome text")
        assert "/start_here" in result

    def test_command_at_line_end(self):
        """Command at line end should be preserved."""
        result = convert_github_to_telegram_markdown("use /end_command")
        assert "/end_command" in result


class TestUnderscoreEscaping:
    """Tests for underscore escaping in regular text."""

    def test_identifier_underscore_escaped(self):
        """Underscores in identifiers should be escaped."""
        result = convert_github_to_telegram_markdown("The grid_name value")
        assert "grid\\_name" in result

    def test_double_underscore_identifier(self):
        """Double underscores should be escaped."""
        result = convert_github_to_telegram_markdown("__init__ method")
        # Multiple underscores should be escaped
        assert "__" not in result or "\\_" in result


class TestMarkdownConversion:
    """Tests for markdown conversion."""

    def test_bold_conversion(self):
        """GitHub bold (**text**) should convert to Telegram bold (*text*)."""
        result = convert_github_to_telegram_markdown("This is **bold** text")
        assert "*bold*" in result
        assert "**" not in result

    def test_header_conversion(self):
        """Headers should convert to bold."""
        result = convert_github_to_telegram_markdown("### Header")
        assert "*Header*" in result
        assert "###" not in result

    def test_bullet_conversion(self):
        """Asterisk bullets should convert to dashes."""
        result = convert_github_to_telegram_markdown("* item 1\n* item 2")
        assert "- item 1" in result
        assert "- item 2" in result


class TestSanitizeForTelegram:
    """Tests for the main sanitize_for_telegram function."""

    def test_truncation(self):
        """Long messages should be truncated."""
        long_text = "a" * 5000
        result = sanitize_for_telegram(long_text, max_length=4096)
        assert len(result) <= 4096
        assert "truncated" in result

    def test_no_truncation_when_disabled(self):
        """No truncation when max_length is None."""
        long_text = "a" * 5000
        result = sanitize_for_telegram(long_text, max_length=None)
        assert len(result) == 5000


class TestEscapeMarkdown:
    """Tests for escape_markdown function."""

    def test_escape_underscore(self):
        """Underscores should be escaped."""
        result = escape_markdown("some_text")
        assert "\\_" in result

    def test_escape_asterisk(self):
        """Asterisks should be escaped."""
        result = escape_markdown("*text*")
        assert "\\*" in result

    def test_escape_backtick(self):
        """Backticks should be escaped."""
        result = escape_markdown("`code`")
        assert "\\`" in result


class TestStripMarkdown:
    """Tests for strip_markdown function."""

    def test_strip_bold(self):
        """Bold markers should be removed."""
        result = strip_markdown("*bold* and **also bold**")
        assert "*" not in result
        assert "bold" in result

    def test_strip_italic(self):
        """Italic markers should be removed."""
        result = strip_markdown("_italic_")
        assert "_" not in result
        assert "italic" in result

    def test_strip_links(self):
        """Links should have URL removed, text kept."""
        result = strip_markdown("[link text](https://example.com)")
        assert "link text" in result
        assert "https://example.com" not in result
