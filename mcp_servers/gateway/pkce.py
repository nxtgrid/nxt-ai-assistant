"""PKCE (RFC 7636) — code_verifier / code_challenge generation and check.

Only S256 is supported. The spec allows "plain" but every real client
(including Claude Code) uses S256; supporting plain would just be an
unused, weaker code path to maintain.
"""

from __future__ import annotations

import base64
import hashlib
import secrets


class PkceInvalid(Exception):
    """The presented code_verifier does not match the stored challenge."""


def _b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_verifier() -> str:
    """A fresh, high-entropy verifier — used by the gateway itself for the
    server-to-server Google leg, where the gateway plays the client role.
    """
    return _b64url_no_pad(secrets.token_bytes(32))


def verifier_to_challenge(verifier: str) -> str:
    """S256: BASE64URL(SHA256(verifier)), no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url_no_pad(digest)


def verify_challenge(verifier: str, challenge: str) -> None:
    """Raise PkceInvalid unless verifier hashes to challenge.

    Constant-time comparison — this is a security boundary, not just a
    correctness check.
    """
    computed = verifier_to_challenge(verifier)
    if not secrets.compare_digest(computed, challenge):
        raise PkceInvalid("code_verifier does not match code_challenge")
