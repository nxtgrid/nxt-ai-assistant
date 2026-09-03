"""Exchange a Google-verified email for a gateway bearer token.

The OAuth dance itself is anansi_app/nicegui_app/auth.py's, unchanged — same
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET and the same /oauth2callback path, so
the existing Google OAuth client registration keeps working. This module owns
only what happens once an email is verified.

is_authorized is REQUIRED, not defaulted. grid_app.lib.perms — the shared RBAC
whitelist this should delegate to — lives under anansi_app/, a sibling project
tree mcp_servers' own sys.path cannot reach; a lazy cross-project import would
either fail at runtime or silently depend on how the process happened to be
launched. The HTTP layer that wires the real OAuth callback to this function
(a follow-on, not in this plan — see "Deferred") is what imports perms.is_authorized
and passes it in explicitly.
"""

from __future__ import annotations

from typing import Callable

from gateway.session import SessionDenied, resolve_session
from gateway.tokens import issue_token


class SignInRejected(Exception):
    """The verified email may not be issued a gateway token."""


async def mint_token_for_email(
    email: str,
    secret: str,
    auth_service,
    is_authorized: Callable[[str], bool],
) -> str:
    """Issue a bearer token, or raise SignInRejected.

    Rejecting here rather than at first tool call means a user finds out at
    sign-in time, instead of seeing an empty tool list with no explanation.
    """
    if not is_authorized(email):
        raise SignInRejected(f"{email} is not authorized for this application")

    try:
        await resolve_session(email, auth_service)
    except SessionDenied as exc:
        raise SignInRejected(f"{email} maps to no organization") from exc

    return issue_token(email, secret)
