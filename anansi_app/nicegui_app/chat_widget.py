"""Bottom-right pop-over chat panel, mounted on every admin page.

Reaches the same LangGraph conversation graph a Telegram personal chat does,
over chat_orchestrator's POST /chat (see chat_client.py). This module is
wiring: the request/response contract lives in chat_client.py and the page
context model in page_context.py, both unit-tested. `nicegui` is faked in
anansi_app's tests, so nothing here that touches `ui` is exercisable -- keep
logic out of `mount()`.

Session lifetime. The session lives in app.storage.tab, not
app.storage.client: `ui.navigate.to()` is a full page load, so client storage
(discarded on reload AND navigation) would wipe the conversation on every
sidebar click. A refresh still starts a fresh session --
`performance.getEntriesByType('navigation')[0].type` distinguishes the two.
Close the tab and the session is gone.

Markdown. `chat_messages.content` holds the model's own GitHub-flavoured
markdown; the Telegram dialect conversion happens later and elsewhere
(telegram_transport._convert_to_telegram_markdown, at send time), and never
touches this path. So `ui.markdown` renders the stored string directly -- no
converter in either direction. The /conversations viewer already does the
same thing with the same column (rendering/conversation_html.py).

`from nicegui import app` is function-local throughout, for the same reason
as in page_context.py: the test conftest fakes `nicegui` with no `app`.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from grid_app.lib import perms

_TAB_KEY = "anansi_chat"


def should_show_chat(email: str) -> bool:
    """Who gets the widget.

    Bot admins only for now, matching the surfaces it is mounted on.
    Widening it is a one-line change now that resolve_auth's admin_app_auth
    branch resolves the org from the email instead of assuming staff -- a
    grid-only viewer would be scoped to their own org, not ours.
    """
    return bool(perms.can_view_bot_admin(email))


def should_start_new_session(
    navigation_type: str, stored: Optional[Dict[str, Any]]
) -> bool:
    """A refresh starts a new session; navigating within the tab keeps it.

    Anything other than an explicit "reload" keeps the session, so a failed
    JS probe fails toward continuity rather than silently discarding a
    conversation. The New chat button covers the manual case.
    """
    if not stored or not stored.get("nonce"):
        return True
    return navigation_type == "reload"


def new_tab_session() -> Dict[str, Any]:
    """A fresh per-tab session.

    `nonce` rides in the request's user_id because generate_session_id hashes
    user_id for DM-shaped sessions -- without it every tab, every day, would
    collapse into one never-ending session.
    """
    return {"nonce": uuid.uuid4().hex, "session_id": None, "messages": []}


def attachment_data_url(attachment: Dict[str, Any]) -> Optional[str]:
    """An <img> source for a tool image, or None when there is nothing to show.

    Telegram receives these as separate sendPhoto calls; the direct-API
    envelope hands the same bytes back base64-encoded in the response body
    (models/envelope.py's attachments_from_tool_results).
    """
    data = attachment.get("data_b64")
    if data:
        return f"data:{attachment.get('mime_type') or 'image/png'};base64,{data}"
    return attachment.get("url") or None
