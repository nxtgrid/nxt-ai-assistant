"""PKCE (RFC 7636): the client proves it holds the same secret across the
authorize and token calls, which is what makes a public client's loopback
redirect safe without a client_secret.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.pkce import PkceInvalid, generate_verifier, verifier_to_challenge, verify_challenge


def test_challenge_derivation_matches_rfc7636_example():
    # RFC 7636 Appendix B's worked example.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert verifier_to_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generated_verifier_round_trips():
    verifier = generate_verifier()
    challenge = verifier_to_challenge(verifier)
    verify_challenge(verifier, challenge)  # must not raise


def test_wrong_verifier_is_rejected():
    challenge = verifier_to_challenge(generate_verifier())
    with pytest.raises(PkceInvalid):
        verify_challenge("wrong-verifier-entirely", challenge)


def test_generated_verifiers_are_not_reused():
    assert generate_verifier() != generate_verifier()
