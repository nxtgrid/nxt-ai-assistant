"""Resolve an authenticated email into a scoped gateway session.

AuthService.get_user_permissions is transport-neutral — the Telegram-specific
entry points (resolve_permissions_from_chat, get_org_id_for_telegram_user) all
funnel into it after resolving an email. That makes email the reuse seam for a
non-Telegram transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet


class SessionDenied(Exception):
    """The caller could not be granted a scoped session."""


@dataclass(frozen=True)
class GatewaySession:
    """One authenticated caller's resolved scope."""

    email: str
    user_id: str
    organization_id: str
    organization_short_name: str | None
    grid_names: FrozenSet[str]
    is_staff: bool


async def resolve_session(email: str, auth_service) -> GatewaySession:
    """Build a GatewaySession, or raise SessionDenied.

    Fails closed on empty organization_ids: AuthService returns that (rather
    than raising) for an email with no row in public.accounts, and forwarding it
    would send organization_id=None to servers that never filter by org.
    """
    permissions = await auth_service.get_user_permissions(email)

    if not permissions.organization_ids:
        raise SessionDenied(f"No organization resolved for {email}")

    organization_id = str(permissions.organization_ids[0])
    is_staff = bool(permissions.is_staff)

    grid_names = await auth_service.get_grid_names_for_organization(
        organization_id, include_all=is_staff
    )

    return GatewaySession(
        email=email,
        user_id=str(permissions.user_id),
        organization_id=organization_id,
        organization_short_name=getattr(permissions, "organization_short_name", None),
        grid_names=frozenset(grid_names),
        is_staff=is_staff,
    )
