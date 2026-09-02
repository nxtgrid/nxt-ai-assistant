"""One admin-app chat turn against chat_orchestrator's POST /chat.

Same transport the skill builder already uses (see pages/skill_builder.py's
module docstring for the full auth story): an "api" auth_method caller
holding both API_KEY and IDENTITY_ASSERTION_KEY, which returns the whole turn
in the HTTP body rather than sending it to Telegram.

Kept free of NiceGUI so it is unit-testable -- anansi_app's test conftest
fakes `nicegui`, so anything importing `ui` can be imported but not exercised.

The call is server-to-server (this NiceGUI backend -> the orchestrator over
the DO private URL), not browser-to-orchestrator, so CORS never applies.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

DEFAULT_TIMEOUT_SECONDS = 120


class ChatTurnError(RuntimeError):
    """The orchestrator answered, but not with a usable turn."""


def orchestrator_base_url() -> str:
    """Base URL with no trailing path -- callers append "/chat" themselves.

    anansi_app/.env.example ships CHAT_ORCHESTRATOR_URL ending in "/chat"
    while the DO spec sets it to the bare service URL
    (${chat-orchestrator.PRIVATE_URL}). Tolerate both rather than producing
    "/chat/chat" in local dev -- skill_builder.py's _orchestrator_base_url
    flags this same inconsistency but does not handle it.
    """
    raw = os.getenv("CHAT_ORCHESTRATOR_URL", "http://localhost:8000").rstrip("/")
    if raw.endswith("/chat"):
        raw = raw[: -len("/chat")]
    return raw or "http://localhost:8000"


def orchestrator_headers() -> Dict[str, str]:
    return {
        "X-Api-Key": os.getenv("API_KEY", ""),
        "X-Identity-Assertion-Key": os.getenv("IDENTITY_ASSERTION_KEY", ""),
        "Content-Type": "application/json",
    }


def identity_configured() -> bool:
    """Whether this deployment can assert the logged-in user's email at all.

    Without IDENTITY_ASSERTION_KEY the orchestrator's is_identity_trusted_caller
    fails closed, resolve_auth ignores the admin_app_auth opt-in, and the
    session silently degrades to an unscoped customer. Better to disable
    sending and say so.
    """
    return bool(os.getenv("IDENTITY_ASSERTION_KEY", ""))


@dataclass(frozen=True)
class ChatTurn:
    """One model turn, as handler.py's direct-API path returns it."""

    text: str
    session_id: str
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    choices: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[str] = field(default_factory=list)
    scope: Dict[str, Any] = field(default_factory=dict)

    def scope_label(self) -> str:
        """"Staff · yourorg", "7", or "" -- shown in the widget header so a
        mis-scoped session is visible rather than silent."""
        organization = str(self.scope.get("organization") or "").strip()
        if self.scope.get("is_staff"):
            return f"Staff · {organization}" if organization else "Staff"
        return organization


def build_payload(
    *,
    message: str,
    user_email: str,
    tab_nonce: str,
    entity_context: Optional[Dict[str, Any]] = None,
    is_bot_admin: bool = False,
) -> Dict[str, Any]:
    """The POST /chat body for one turn.

    `user_id` carries `tab_nonce` because generate_session_id hashes user_id
    for DM-shaped sessions: a bare email would collapse every tab, every day,
    into one never-ending session. Same trick pages/skills.py uses to give
    each builder run its own session.

    `metadata.admin_app_auth` opts into resolve_auth's admin_app_auth branch,
    which resolves the org from this email first and only falls back to the
    staff org for a bot-admin whose email is absent from public.accounts. The
    flag alone grants nothing -- the branch also requires the server-computed
    `_identity_trusted` signal that IDENTITY_ASSERTION_KEY proves.
    """
    payload: Dict[str, Any] = {
        "message": message,
        "user_id": f"anansi-app:{user_email}:{tab_nonce}",
        "user_email": user_email,
        "source": "web",
        "metadata": {
            "admin_app_auth": True,
            "admin_app_bot_admin": bool(is_bot_admin),
            # Becomes chat_sessions.title via get_or_create_session, so these
            # rows are recognisable in the Chats viewer.
            "chat_title": f"Anansi App — {user_email}",
        },
    }
    if entity_context:
        payload["entity_context"] = entity_context
    return payload


def parse_response(body: Dict[str, Any]) -> ChatTurn:
    """Read handler.py's direct-API response dict into a ChatTurn.

    A 200 with success=False is the BOT_ENABLED=false path, not an HTTP error,
    so it has to be checked explicitly.
    """
    if not body.get("success", False):
        raise ChatTurnError(
            body.get("message") or body.get("error") or "Chat request failed"
        )
    return ChatTurn(
        text=body.get("message") or "",
        session_id=body.get("session_id") or "",
        attachments=[a for a in (body.get("attachments") or []) if isinstance(a, dict)],
        choices=[c for c in (body.get("choices") or []) if isinstance(c, dict)],
        tool_calls=[str(name) for name in (body.get("tool_calls") or [])],
        scope=body.get("scope") or {},
    )


def send_turn(
    *,
    message: str,
    user_email: str,
    tab_nonce: str,
    entity_context: Optional[Dict[str, Any]] = None,
    is_bot_admin: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> ChatTurn:
    """Blocking. Callers on the event loop must wrap this in run.io_bound."""
    response = requests.post(
        f"{orchestrator_base_url()}/chat",
        headers=orchestrator_headers(),
        json=build_payload(
            message=message,
            user_email=user_email,
            tab_nonce=tab_nonce,
            entity_context=entity_context,
            is_bot_admin=is_bot_admin,
        ),
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_response(response.json())


def error_detail(exc: requests.HTTPError) -> str:
    """The orchestrator's own message from a failed response, when it has one."""
    try:
        body = exc.response.json()
        return str(body.get("message") or body.get("error") or exc)
    except Exception:
        return str(exc)
