"""Tests for Telegram inline button utilities."""

import os
import unittest
from unittest.mock import patch

from shared.utils.telegram_buttons import (
    CALLBACK_PREFIX,
    DUPLICATE_OPTIONS,
    DUPLICATE_OPTIONS_RESUMABLE,
    MAX_CALLBACK_DATA_LENGTH,
    PROCEDURE_CALLBACK_PREFIX,
    RESUME_OPTIONS,
    build_decision_keyboard,
    build_procedure_keyboard,
    get_options_for_duplicate_decision,
    get_options_for_resume_decision,
    is_inline_buttons_enabled,
    is_procedure_buttons_enabled,
    parse_callback_data,
    parse_procedure_buttons,
)


class TestBuildDecisionKeyboard(unittest.TestCase):
    """Test build_decision_keyboard function."""

    def test_builds_keyboard_with_correct_structure(self):
        """Test that keyboard has correct Telegram structure."""
        decision_id = "12345678-1234-1234-1234-123456789012"
        options = [
            {"label": "Option 1", "action": "action1"},
            {"label": "Option 2", "action": "action2"},
        ]

        result = build_decision_keyboard(decision_id, options)

        self.assertIn("inline_keyboard", result)
        self.assertEqual(len(result["inline_keyboard"]), 2)

        # Each button should be in its own row
        self.assertEqual(len(result["inline_keyboard"][0]), 1)
        self.assertEqual(len(result["inline_keyboard"][1]), 1)

    def test_callback_data_uses_full_decision_id(self):
        """Test that callback data uses the full decision UUID."""
        decision_id = "abcdefgh-1234-5678-90ab-cdef12345678"
        options = [{"label": "Test", "action": "test_action"}]

        result = build_decision_keyboard(decision_id, options)

        callback_data = result["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(callback_data, f"{CALLBACK_PREFIX}:{decision_id}:test_action")

    def test_callback_data_within_telegram_limit(self):
        """Test that callback data never exceeds 64 bytes."""
        decision_id = "12345678-1234-1234-1234-123456789012"

        # Test with longest action name we use
        long_options = [{"label": "Test", "action": "start_fresh"}]

        result = build_decision_keyboard(decision_id, long_options)
        callback_data = result["inline_keyboard"][0][0]["callback_data"]

        self.assertLessEqual(
            len(callback_data.encode("utf-8")),
            MAX_CALLBACK_DATA_LENGTH,
            f"Callback data '{callback_data}' exceeds 64-byte limit",
        )

    def test_predefined_options_within_limit(self):
        """Test that all predefined option sets produce valid callback data."""
        decision_id = "12345678-1234-1234-1234-123456789012"

        for options in [DUPLICATE_OPTIONS, DUPLICATE_OPTIONS_RESUMABLE, RESUME_OPTIONS]:
            result = build_decision_keyboard(decision_id, options)

            for row in result["inline_keyboard"]:
                for button in row:
                    callback_data = button["callback_data"]
                    byte_len = len(callback_data.encode("utf-8"))
                    self.assertLessEqual(
                        byte_len,
                        MAX_CALLBACK_DATA_LENGTH,
                        f"Callback data '{callback_data}' exceeds 64-byte limit ({byte_len} bytes)",
                    )


class TestParseCallbackData(unittest.TestCase):
    """Test parse_callback_data function."""

    def test_parses_valid_decision_callback_data(self):
        """Test parsing valid decision callback data."""
        result = parse_callback_data("pd:abc12345:run_new")

        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "pd")
        self.assertEqual(result["id_prefix"], "abc12345")
        self.assertEqual(result["action"], "run_new")

    def test_parses_valid_procedure_callback_data(self):
        """Test parsing valid procedure callback data."""
        result = parse_callback_data("pc:1")

        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "pc")
        self.assertEqual(result["choice"], "1")

    def test_parses_procedure_callback_with_higher_number(self):
        """Test parsing procedure callback with choice number > 1."""
        result = parse_callback_data("pc:3")

        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "pc")
        self.assertEqual(result["choice"], "3")

    def test_returns_none_for_empty_string(self):
        """Test that empty string returns None."""
        self.assertIsNone(parse_callback_data(""))

    def test_returns_none_for_invalid_format(self):
        """Test that invalid format returns None."""
        # Too few parts for pd
        self.assertIsNone(parse_callback_data("pd:abc12345"))
        # Single part
        self.assertIsNone(parse_callback_data("pd"))

    def test_returns_none_for_wrong_prefix(self):
        """Test that wrong prefix returns None."""
        result = parse_callback_data("xx:abc12345:action")
        self.assertIsNone(result)

    def test_handles_action_with_underscores(self):
        """Test parsing action names with underscores."""
        result = parse_callback_data("pd:abc12345:start_fresh")

        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "start_fresh")


class TestIsInlineButtonsEnabled(unittest.TestCase):
    """Test is_inline_buttons_enabled function."""

    def test_returns_false_by_default(self):
        """Test that feature is disabled by default."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove the env var if it exists
            if "INLINE_BUTTONS_ENABLED" in os.environ:
                del os.environ["INLINE_BUTTONS_ENABLED"]
            self.assertFalse(is_inline_buttons_enabled())

    def test_returns_true_when_enabled(self):
        """Test that feature is enabled when env var is 'true'."""
        with patch.dict(os.environ, {"INLINE_BUTTONS_ENABLED": "true"}):
            self.assertTrue(is_inline_buttons_enabled())

    def test_case_insensitive(self):
        """Test that 'TRUE', 'True', etc. also work."""
        for value in ["TRUE", "True", "tRuE"]:
            with patch.dict(os.environ, {"INLINE_BUTTONS_ENABLED": value}):
                self.assertTrue(is_inline_buttons_enabled())

    def test_returns_false_for_other_values(self):
        """Test that non-'true' values return False."""
        for value in ["false", "False", "1", "yes", "enabled"]:
            with patch.dict(os.environ, {"INLINE_BUTTONS_ENABLED": value}):
                self.assertFalse(is_inline_buttons_enabled())


class TestOptionHelpers(unittest.TestCase):
    """Test option helper functions."""

    def test_get_options_for_duplicate_non_resumable(self):
        """Test duplicate options without resume."""
        options = get_options_for_duplicate_decision(is_resumable=False)
        self.assertEqual(options, DUPLICATE_OPTIONS)
        self.assertEqual(len(options), 2)

    def test_get_options_for_duplicate_resumable(self):
        """Test duplicate options with resume."""
        options = get_options_for_duplicate_decision(is_resumable=True)
        self.assertEqual(options, DUPLICATE_OPTIONS_RESUMABLE)
        self.assertEqual(len(options), 3)

    def test_get_options_for_resume_decision(self):
        """Test resume options."""
        options = get_options_for_resume_decision()
        self.assertEqual(options, RESUME_OPTIONS)
        self.assertEqual(len(options), 3)


class TestParseProcedureButtons(unittest.TestCase):
    """Test parse_procedure_buttons function."""

    def test_parses_valid_buttons_block(self):
        """Test parsing a valid [BUTTONS] block."""
        response = """Here's what I can help you with:

[BUTTONS]
1. Check meter status
2. Create support ticket
3. Talk to a human
[/BUTTONS]

Let me know your choice!"""

        clean_text, keyboard, choices = parse_procedure_buttons(response)

        self.assertIsNotNone(keyboard)
        self.assertEqual(len(keyboard["inline_keyboard"]), 3)
        self.assertEqual(
            choices, ["Check meter status", "Create support ticket", "Talk to a human"]
        )
        self.assertNotIn("[BUTTONS]", clean_text)
        self.assertNotIn("[/BUTTONS]", clean_text)

    def test_returns_none_for_no_buttons_block(self):
        """Test that no buttons block returns None."""
        response = "Just a normal response without buttons."

        clean_text, keyboard, choices = parse_procedure_buttons(response)

        self.assertIsNone(keyboard)
        self.assertIsNone(choices)
        self.assertEqual(clean_text, response)

    def test_rejects_single_option(self):
        """Test that single option is rejected (need 2-4)."""
        response = """[BUTTONS]
1. Only one option
[/BUTTONS]"""

        clean_text, keyboard, choices = parse_procedure_buttons(response)

        self.assertIsNone(keyboard)

    def test_rejects_too_many_options(self):
        """Test that more than 4 options is rejected."""
        response = """[BUTTONS]
1. Option one
2. Option two
3. Option three
4. Option four
5. Option five
[/BUTTONS]"""

        clean_text, keyboard, choices = parse_procedure_buttons(response)

        self.assertIsNone(keyboard)

    def test_callback_data_format(self):
        """Test that callback data uses pc: prefix."""
        response = """[BUTTONS]
1. First
2. Second
[/BUTTONS]"""

        clean_text, keyboard, choices = parse_procedure_buttons(response)

        self.assertIsNotNone(keyboard)
        callback_data = keyboard["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(callback_data.startswith(f"{PROCEDURE_CALLBACK_PREFIX}:"))

    def test_handles_empty_response(self):
        """Test handling of empty response."""
        clean_text, keyboard, choices = parse_procedure_buttons("")

        self.assertIsNone(keyboard)
        self.assertEqual(clean_text, "")

    def test_case_insensitive_tags(self):
        """Test that [buttons] tags are case-insensitive."""
        response = """[buttons]
1. First
2. Second
[/Buttons]"""

        clean_text, keyboard, choices = parse_procedure_buttons(response)

        self.assertIsNotNone(keyboard)

    def test_bracketless_tags_multiline(self):
        """Test BUTTONS ... /BUTTONS without brackets (tag/slash-tag format)."""
        response = """Here are your options:

BUTTONS
Get OPS-2148 details
Filter by grid
Start new task
/BUTTONS"""

        clean_text, keyboard, choices = parse_procedure_buttons(response)

        self.assertIsNotNone(keyboard)
        self.assertEqual(len(keyboard["inline_keyboard"]), 3)
        self.assertEqual(choices, ["Get OPS-2148 details", "Filter by grid", "Start new task"])
        self.assertNotIn("BUTTONS", clean_text)
        self.assertNotIn("/BUTTONS", clean_text)

    def test_bracketless_single_line_strips_tags(self):
        """Test that single-line BUTTONS block strips tags even when it can't build buttons."""
        response = "Some text BUTTONS Get OPS-2148 details Filter by grid Start new task /BUTTONS"

        clean_text, keyboard, choices = parse_procedure_buttons(response)

        # Can't build buttons from single line, but tags must be stripped
        self.assertIsNone(keyboard)
        self.assertNotIn("BUTTONS", clean_text)
        self.assertNotIn("/BUTTONS", clean_text)
        self.assertIn("Some text", clean_text)

    def test_invalid_count_strips_tags(self):
        """Test that tags are stripped even when option count is invalid."""
        response = """Info here.

[BUTTONS]
1. Only one option
[/BUTTONS]

More text."""

        clean_text, keyboard, choices = parse_procedure_buttons(response)

        self.assertIsNone(keyboard)
        self.assertNotIn("[BUTTONS]", clean_text)
        self.assertNotIn("[/BUTTONS]", clean_text)
        self.assertIn("Info here.", clean_text)
        self.assertIn("More text.", clean_text)

    def test_bullet_options_stripped(self):
        """Test that leading dashes/bullets are stripped from option labels."""
        response = """BUTTONS
- Check status
- Create ticket
- Talk to human
/BUTTONS"""

        clean_text, keyboard, choices = parse_procedure_buttons(response)

        self.assertIsNotNone(keyboard)
        self.assertEqual(choices, ["Check status", "Create ticket", "Talk to human"])


class TestBuildProcedureKeyboard(unittest.TestCase):
    """Test build_procedure_keyboard function."""

    def test_builds_keyboard_from_options(self):
        """Test building keyboard from option list."""
        options = ["Check status", "Create ticket"]

        keyboard = build_procedure_keyboard(options)

        self.assertIn("inline_keyboard", keyboard)
        self.assertEqual(len(keyboard["inline_keyboard"]), 2)

    def test_rejects_invalid_option_count(self):
        """Test that invalid option counts raise ValueError."""
        with self.assertRaises(ValueError):
            build_procedure_keyboard(["Only one"])

        with self.assertRaises(ValueError):
            build_procedure_keyboard(["1", "2", "3", "4", "5"])


class TestIsProcedureButtonsEnabled(unittest.TestCase):
    """Test is_procedure_buttons_enabled function."""

    def test_returns_false_by_default(self):
        """Test that feature is disabled by default."""
        with patch.dict(os.environ, {}, clear=True):
            if "PROCEDURE_BUTTONS_ENABLED" in os.environ:
                del os.environ["PROCEDURE_BUTTONS_ENABLED"]
            self.assertFalse(is_procedure_buttons_enabled())

    def test_returns_true_when_enabled(self):
        """Test that feature is enabled when env var is 'true'."""
        with patch.dict(os.environ, {"PROCEDURE_BUTTONS_ENABLED": "true"}):
            self.assertTrue(is_procedure_buttons_enabled())


if __name__ == "__main__":
    unittest.main()
