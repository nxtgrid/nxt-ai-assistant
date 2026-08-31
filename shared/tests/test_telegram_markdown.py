"""Tests for Telegram markdown utilities."""

import re

from shared.utils.telegram_markdown import (
    balance_markdown_entities,
    convert_github_to_telegram_markdown,
    escape_markdown,
    sanitize_for_telegram,
    strip_markdown,
)


class TestSlashCommandProtection:
    """Slash commands must survive conversion and reach the reader intact.

    Their underscores are escaped rather than left raw: to Telegram a bare "_"
    is a live italic delimiter, so an odd count fails the whole message and an
    even one italicises the text between two commands. The backslash is
    consumed when Telegram parses the message, so what the reader sees -- and
    taps -- is the command exactly as written.
    """

    def test_slash_command_with_underscores(self):
        """Slash commands with underscores should reach the reader intact."""
        result = convert_github_to_telegram_markdown("/equipment_history is useful")
        assert strip_markdown(result) == "/equipment_history is useful"
        # No underscore is left free to open an italic entity.
        assert re.search(r"(?<!\\)_", result) is None
        # Ensure no protection markers leaked through
        assert "⟦CMD" not in result
        assert "__PROTECTED" not in result

    def test_multiple_commands(self):
        """Multiple slash commands should all be preserved."""
        result = convert_github_to_telegram_markdown("/first_cmd and /second_cmd")
        assert strip_markdown(result) == "/first_cmd and /second_cmd"
        # Two raw underscores would have italicised the "and" between them.
        assert re.search(r"(?<!\\)_", result) is None

    def test_mixed_content(self):
        """Slash commands and regular identifiers should be handled correctly."""
        result = convert_github_to_telegram_markdown("Run /meter_reading for grid_name")
        assert strip_markdown(result) == "Run /meter_reading for grid_name"
        # grid_name should have escaped underscore (outside slash command)
        assert "grid\\_name" in result

    def test_command_with_multiple_underscores(self):
        """Commands with multiple underscores should be preserved."""
        result = convert_github_to_telegram_markdown("Try /very_long_command_name")
        assert strip_markdown(result) == "Try /very_long_command_name"
        assert re.search(r"(?<!\\)_", result) is None

    def test_command_at_line_start(self):
        """Command at line start should be preserved."""
        result = convert_github_to_telegram_markdown("/start_here\nsome text")
        assert strip_markdown(result) == "/start_here\nsome text"

    def test_command_at_line_end(self):
        """Command at line end should be preserved."""
        result = convert_github_to_telegram_markdown("use /end_command")
        assert strip_markdown(result) == "use /end_command"

    def test_url_underscores_do_not_open_an_entity(self):
        """A bare URL's underscore is a delimiter to Telegram just the same."""
        result = convert_github_to_telegram_markdown("see https://example.com/a_b now")
        assert strip_markdown(result) == "see https://example.com/a_b now"
        assert re.search(r"(?<!\\)_", result) is None


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


class TestEntityBalancing:
    """Tests for balance_markdown_entities.

    Telegram's Markdown v1 parser rejects the whole message with
    "can't parse entities" as soon as one delimiter opens an entity that never
    closes. Text assembled from external systems routinely carries such a
    delimiter -- a summary truncated mid-sentence keeps its opening "[" but
    loses the closing "]" -- so every delimiter that cannot pair has to be
    escaped before the message is handed to Telegram.
    """

    def test_unmatched_bracket_is_escaped(self):
        """A "[" with no closing "]" must not reach Telegram unescaped."""
        result = balance_markdown_entities('[In reply to the bot: "I have located...')
        assert result.startswith("\\[")

    def test_truncated_summary_in_a_digest_line_is_escaped(self):
        """The real-world shape: a bullet whose summary was cut mid-bracket."""
        line = '- *OPS-1001*: [In reply to the bot: "I have located your transa...'
        result = balance_markdown_entities(line)
        assert "\\[" in result
        # The legitimate bold pair around the ticket key must survive.
        assert "*OPS-1001*" in result

    def test_complete_link_is_left_alone(self):
        """A well-formed inline link is a valid entity and must not be escaped."""
        result = balance_markdown_entities("see [the docs](https://example.com/a_b)")
        assert result == "see [the docs](https://example.com/a_b)"

    def test_odd_asterisk_is_escaped(self):
        """An unpaired bold marker is escaped, paired ones are kept."""
        result = balance_markdown_entities("*bold* and a stray * marker")
        assert "*bold*" in result
        assert "\\*" in result

    def test_even_asterisks_are_untouched(self):
        result = balance_markdown_entities("*one* and *two*")
        assert result == "*one* and *two*"

    def test_odd_underscore_is_escaped(self):
        result = balance_markdown_entities("a stray _ marker")
        assert "\\_" in result

    def test_unterminated_backtick_is_escaped(self):
        result = balance_markdown_entities("here is a `code span that never ends")
        assert "\\`" in result

    def test_complete_code_span_is_left_alone(self):
        """Delimiters inside a code span are literal and must not be escaped."""
        result = balance_markdown_entities("run `foo [bar_baz*` now")
        assert result == "run `foo [bar_baz*` now"

    def test_already_escaped_text_is_unchanged(self):
        """Balancing must be idempotent -- chunks get balanced more than once."""
        once = balance_markdown_entities('[unclosed and a stray * here')
        twice = balance_markdown_entities(once)
        assert once == twice

    def test_empty_bold_pair_is_escaped(self):
        """"**" with nothing between it is an empty entity Telegram rejects."""
        result = balance_markdown_entities("a ** b")
        assert "\\*\\*" in result

    def test_no_delimiters_is_a_no_op(self):
        text = "plain text with (parens) and 123 numbers"
        assert balance_markdown_entities(text) == text


class TestConverterProducesParsableOutput:
    """The converter is the single choke point every Telegram sender goes through."""

    def test_truncated_bracket_survives_conversion_escaped(self):
        """The reported failure: a digest of truncated ticket summaries."""
        digest = (
            "Here are the open tickets:\n"
            '* **OPS-1001**: [In reply to the bot: "I have located your transa...\n'
            '* **OPS-1002**: [In reply to the bot: "I am sorry to hear about th...\n'
        )
        result = convert_github_to_telegram_markdown(digest)
        # Every bracket is neutralised...
        assert "\\[" in result
        assert re.search(r"(?<!\\)\[", result) is None
        # ...while the bold ticket keys still render.
        assert "*OPS-1001*" in result

    def test_sanitize_truncation_cannot_sever_an_entity(self):
        """Truncating to max_length must not leave a half-open entity behind."""
        text = "x" * 4080 + " *bold text that gets cut in half*"
        result = sanitize_for_telegram(text, max_length=4096)
        assert re.search(r"(?<!\\)\*", result) is None
