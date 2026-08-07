"""Tests for orchestrator.api.app's is_identity_trusted_caller /
get_identity_assertion_key (Phase 4 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md, "Identity over
the API channel").

IDENTITY_ASSERTION_KEY is a secret distinct from API_KEY: holding the
general API_KEY authenticates a caller as auth_method="api", but must not by
itself let that caller assert an arbitrary user_email (see
handler._resolve_email_lookup_fallback and
tests/test_resolve_email_lookup_fallback.py for the consumer of the flag
this produces). Only a caller that also presents the separate
X-Identity-Assertion-Key header, matching IDENTITY_ASSERTION_KEY, is trusted
for that.
"""

from __future__ import annotations

from types import SimpleNamespace

from orchestrator.api.app import get_identity_assertion_key, is_identity_trusted_caller


def _request(headers: dict) -> SimpleNamespace:
    """Minimal stand-in for fastapi.Request -- only .headers.get(...) is used."""
    return SimpleNamespace(headers=headers)


class TestGetIdentityAssertionKey:
    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("IDENTITY_ASSERTION_KEY", "s3cret")

        assert get_identity_assertion_key() == "s3cret"

    def test_defaults_to_empty_string(self, monkeypatch):
        monkeypatch.delenv("IDENTITY_ASSERTION_KEY", raising=False)

        assert get_identity_assertion_key() == ""


class TestIsIdentityTrustedCaller:
    def test_matching_header_is_trusted(self, monkeypatch):
        monkeypatch.setenv("IDENTITY_ASSERTION_KEY", "s3cret")

        request = _request({"X-Identity-Assertion-Key": "s3cret"})

        assert is_identity_trusted_caller(request) is True

    def test_mismatched_header_is_not_trusted(self, monkeypatch):
        monkeypatch.setenv("IDENTITY_ASSERTION_KEY", "s3cret")

        request = _request({"X-Identity-Assertion-Key": "wrong"})

        assert is_identity_trusted_caller(request) is False

    def test_missing_header_is_not_trusted(self, monkeypatch):
        monkeypatch.setenv("IDENTITY_ASSERTION_KEY", "s3cret")

        request = _request({})

        assert is_identity_trusted_caller(request) is False

    def test_unconfigured_key_fails_closed_even_with_a_header(self, monkeypatch):
        # No IDENTITY_ASSERTION_KEY set at all -- a deployment that never
        # configures this must never honor a caller-supplied user_email,
        # not even from a caller that guesses/sends an empty-string match.
        monkeypatch.delenv("IDENTITY_ASSERTION_KEY", raising=False)

        request = _request({"X-Identity-Assertion-Key": ""})

        assert is_identity_trusted_caller(request) is False

    def test_empty_header_against_configured_key_is_not_trusted(self, monkeypatch):
        monkeypatch.setenv("IDENTITY_ASSERTION_KEY", "s3cret")

        request = _request({"X-Identity-Assertion-Key": ""})

        assert is_identity_trusted_caller(request) is False
