"""Tests for handler._resolve_email_lookup_fallback (Phase 4 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md, "Identity over
the API channel").

Before Phase 4, _handle_webhook / _handle_webhook_async trusted a
caller-supplied user_email unconditionally whenever the auth-DB lookup
missed. Since every "api" auth_method caller shares one API_KEY, that made
the key an impersonation oracle: any holder could set user_email to any
address and be treated as that account for the rest of the conversation,
with that account's org/staff permissions. This is the gate that closes it
-- see also tests/test_identity_trusted_caller.py for the header-level check
that produces the identity_trusted flag this function consumes.
"""

from __future__ import annotations

from unittest.mock import patch

from handler import _resolve_email_lookup_fallback


class TestResolveEmailLookupFallback:
    def test_trusted_caller_with_email_is_honored(self):
        result = _resolve_email_lookup_fallback(
            "someone@example.com", True, source="api", user_id="u1"
        )

        assert result == "someone@example.com"

    def test_untrusted_caller_with_email_is_rejected(self):
        result = _resolve_email_lookup_fallback(
            "someone@example.com", False, source="api", user_id="u1"
        )

        assert result is None

    def test_no_email_supplied_is_rejected_regardless_of_trust(self):
        assert _resolve_email_lookup_fallback(None, True, source="api", user_id="u1") is None
        assert _resolve_email_lookup_fallback(None, False, source="api", user_id="u1") is None

    def test_empty_string_email_is_rejected(self):
        # Falsy but not None -- WebhookRequest.user_email could plausibly be "".
        assert _resolve_email_lookup_fallback("", True, source="api", user_id="u1") is None

    def test_untrusted_rejection_is_logged_as_a_warning(self):
        with patch("handler.LOGGER") as mock_logger:
            _resolve_email_lookup_fallback(
                "attacker@example.com", False, source="api", user_id="u1"
            )

        mock_logger.warning.assert_called_once()
        logged_text = mock_logger.warning.call_args.args[0]
        assert "untrusted" in logged_text
        assert "api" in logged_text
        assert "u1" in logged_text

    def test_trusted_success_does_not_log_a_warning(self):
        with patch("handler.LOGGER") as mock_logger:
            _resolve_email_lookup_fallback("someone@example.com", True, source="api", user_id="u1")

        mock_logger.warning.assert_not_called()

    def test_no_email_supplied_does_not_log_a_warning(self):
        # Nothing to reject -- this is the ordinary "not registered" case,
        # not a rejected impersonation attempt, so it shouldn't look like one.
        with patch("handler.LOGGER") as mock_logger:
            _resolve_email_lookup_fallback(None, False, source="api", user_id="u1")

        mock_logger.warning.assert_not_called()
