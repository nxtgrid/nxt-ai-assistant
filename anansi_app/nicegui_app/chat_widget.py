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

import requests
from grid_app.lib import perms
from nicegui import run, ui

from nicegui_app import chat_client, page_context

_TAB_KEY = "anansi_chat"
_SELECTION_EVENT = "anansi_chat_selection"

# Report whether this page arrived by refresh or by navigation. Wrapped in
# defaults because the Navigation Timing entry is absent in some contexts.
_NAV_TYPE_JS = "(performance.getEntriesByType('navigation')[0] || {}).type || 'navigate'"

# Mirror the page's live text selection into Python so the chip appears as
# soon as the user highlights something -- reading the selection at send time
# would be too late, since clicking into the chat input clears it.
_SELECTION_HOOK_JS = """
(() => {
  if (window.__anansiSelectionHook) return;
  window.__anansiSelectionHook = true;
  let last = '';
  document.addEventListener('selectionchange', () => {
    const sel = window.getSelection();
    const text = (sel ? sel.toString() : '').trim();
    // The empty selection a click inside the chat input produces must not
    // clear what the user highlighted on the page beforehand.
    if (text && text !== last) {
      last = text;
      emitEvent('anansi_chat_selection', text.slice(0, 4000));
    }
  });
})();
"""

_SCROLL_TRANSCRIPT_JS = (
    "const e = document.querySelector('.anansi-chat-transcript');"
    " if (e) e.scrollTop = e.scrollHeight;"
)


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


def mount(user: Dict[str, Any]) -> None:
    """Mount the pop-over launcher and panel into the current page.

    Called once per page render from layout.frame(). Everything is built
    synchronously; the tab-scoped session is resolved in a one-shot timer
    because app.storage.tab raises until the client's socket is connected.
    """
    email = (user.get("email") or "").lower()
    if not should_show_chat(email):
        return

    state: Dict[str, Any] = {
        # Replaced by _init() with the real app.storage.tab entry. Until then
        # this stand-in keeps every handler total.
        "store": new_tab_session(),
        "selection": "",
        "include_page": True,
        "sending": False,
        "scope": "",
    }

    with ui.page_sticky(position="bottom-right", x_offset=18, y_offset=18):
        with ui.column().classes("items-end gap-2"):
            panel = ui.card().classes("shadow-8").style("width: 26rem; max-width: 92vw;")
            ui.button(icon="forum", on_click=lambda: _toggle()).props(
                "fab color=primary"
            ).tooltip("Ask the assistant")

    with panel:
        with ui.row().classes("items-center justify-between w-full no-wrap"):
            ui.label("Assistant").classes("text-subtitle2 text-bold")
            scope_label = ui.label("").classes("text-caption text-grey-7")
            with ui.row().classes("items-center gap-0 no-wrap"):
                ui.button(icon="restart_alt", on_click=lambda: _new_chat()).props(
                    "flat dense round"
                ).tooltip("New chat")
                ui.button(icon="close", on_click=lambda: _toggle(False)).props(
                    "flat dense round"
                )

        # Fixed height, deliberately NOT a flex child: an overflow-y:auto child
        # inside a flex card gets min-height:auto and is crushed to zero height
        # with its rows still in the DOM -- the exact failure that made the
        # Context page's picker look empty. A fixed height sidesteps it.
        transcript = (
            ui.column()
            .classes("w-full gap-2 anansi-chat-transcript")
            .style("height: 22rem; overflow-y: auto; padding-right: 4px;")
        )

        chips = ui.row().classes("w-full items-center gap-1").style("flex-wrap: wrap;")

        with ui.row().classes("w-full items-end gap-1 no-wrap"):
            message_input = (
                ui.textarea(placeholder="Ask about this page…")
                .classes("flex-grow")
                .props("dense outlined autogrow")
            )
            send_button = ui.button(icon="send", on_click=lambda: _send()).props(
                "round dense color=primary"
            )

        notice = ui.label("").classes("text-caption text-negative")

    panel.set_visibility(False)

    if not chat_client.identity_configured():
        notice.text = (
            "IDENTITY_ASSERTION_KEY is not set on this deployment, so chat is "
            "disabled — see chat_orchestrator/.env.example."
        )
        send_button.disable()
        message_input.disable()

    # ── rendering ────────────────────────────────────────────────────────────
    def _scroll_to_bottom() -> None:
        ui.run_javascript(_SCROLL_TRANSCRIPT_JS)

    def _render_entry(entry: Dict[str, Any]) -> None:
        role = entry.get("role")
        if role == "error":
            ui.label(entry.get("text") or "").classes("text-caption text-negative")
            return
        with ui.chat_message(sent=(role == "user")):
            ui.markdown(entry.get("text") or "")
            for attachment in entry.get("attachments") or []:
                source = attachment_data_url(attachment)
                if not source:
                    continue
                ui.image(source).classes("w-full rounded-borders")
                if attachment.get("caption"):
                    ui.label(attachment["caption"]).classes("text-caption")
            tool_calls = entry.get("tool_calls") or []
            if tool_calls:
                ui.label("🛠 " + ", ".join(tool_calls)).classes("text-caption text-grey-7")
        for choice in entry.get("choices") or []:
            label = choice.get("label") or ""
            value = str(choice.get("value") or "")
            if value.startswith("http"):
                # A web_app button: a Telegram Mini App URL. Offer the link
                # rather than pretending a click continues this conversation.
                ui.link(label, value, new_tab=True).classes("text-caption")
            else:
                ui.button(label, on_click=lambda _=None, t=label: _send(t)).props(
                    "outline dense no-caps"
                )

    def _render_transcript() -> None:
        transcript.clear()
        with transcript:
            if not state["store"]["messages"]:
                ui.label("Ask about what you're looking at.").classes(
                    "text-caption text-grey-6"
                )
            for entry in state["store"]["messages"]:
                _render_entry(entry)
        _scroll_to_bottom()

    def _render_chips() -> None:
        chips.clear()
        page = page_context.get_page_context() if state["include_page"] else None
        with chips:
            if page is not None:
                _chip(page.chip_label(), "description", _drop_page)
            if state["selection"]:
                _chip("Selected text", "format_quote", _drop_selection)
            if page is None and not state["selection"]:
                ui.label("No context attached").classes("text-caption text-grey-6")

    def _chip(text: str, icon: str, on_remove) -> None:
        ui.chip(
            text,
            icon=icon,
            removable=True,
            on_value_change=lambda _=None: on_remove(),
        ).props("outline dense")

    def _drop_page() -> None:
        state["include_page"] = False
        _render_chips()

    def _drop_selection() -> None:
        state["selection"] = ""
        _render_chips()

    # ── behaviour ────────────────────────────────────────────────────────────
    def _toggle(visible: Optional[bool] = None) -> None:
        panel.set_visibility(not panel.visible if visible is None else visible)
        if panel.visible:
            _render_chips()
            _scroll_to_bottom()

    def _new_chat() -> None:
        # One _store() call, held in a local: the fallback branch returns a
        # fresh throwaway dict each time, so writing and reading through two
        # separate calls would hand back two different sessions.
        fresh = new_tab_session()
        _store()[_TAB_KEY] = fresh
        state["store"] = fresh
        state["scope"] = ""
        scope_label.text = ""
        state["include_page"] = True
        _render_transcript()
        _render_chips()

    def _store() -> Dict[str, Any]:
        """The tab-scoped storage dict, or a per-render stand-in.

        app.storage.tab raises until the client's socket is connected, and
        `frame()` runs before that, so every access is guarded.
        """
        try:
            from nicegui import app

            return app.storage.tab
        except Exception:
            return {_TAB_KEY: state["store"]}

    async def _send(text: Optional[str] = None) -> None:
        body = (text if text is not None else message_input.value or "").strip()
        if not body or state["sending"]:
            return

        state["sending"] = True
        send_button.disable()
        message_input.disable()
        message_input.value = ""

        store = state["store"]
        page = page_context.get_page_context() if state["include_page"] else None
        entity_context = page_context.to_entity_context(page, state["selection"])

        store["messages"].append({"role": "user", "text": body})
        _render_transcript()
        with transcript:
            with ui.row().classes("items-center gap-2"):
                ui.spinner(size="sm")
                ui.label("Thinking…").classes("text-caption text-grey-7")
        _scroll_to_bottom()

        try:
            turn = await run.io_bound(
                lambda: chat_client.send_turn(
                    message=body,
                    user_email=email,
                    tab_nonce=store["nonce"],
                    entity_context=entity_context,
                    # Everyone reaching this widget passed can_view_bot_admin
                    # (should_show_chat), which is what this flag asserts.
                    is_bot_admin=True,
                )
            )
        except requests.HTTPError as exc:
            store["messages"].append(
                {"role": "error", "text": chat_client.error_detail(exc)}
            )
        except chat_client.ChatTurnError as exc:
            store["messages"].append({"role": "error", "text": str(exc)})
        except Exception as exc:  # noqa: BLE001 -- surface anything to the operator
            store["messages"].append(
                {"role": "error", "text": f"Could not reach the assistant: {exc}"}
            )
        else:
            store["session_id"] = turn.session_id or store["session_id"]
            state["scope"] = turn.scope_label()
            store["messages"].append(
                {
                    "role": "model",
                    "text": turn.text,
                    "attachments": turn.attachments,
                    "choices": turn.choices,
                    "tool_calls": turn.tool_calls,
                }
            )
            # The highlighted text has been sent. Drop it so the next turn
            # cannot silently re-attach a stale selection.
            state["selection"] = ""
        finally:
            state["sending"] = False
            send_button.enable()
            message_input.enable()
            scope_label.text = state["scope"]
            _render_transcript()
            _render_chips()

    def _remember_selection(event) -> None:
        state["selection"] = str(getattr(event, "args", "") or "")[
            : page_context.MAX_SELECTION_CHARS
        ]
        if panel.visible:
            _render_chips()

    ui.on(_SELECTION_EVENT, _remember_selection)
    message_input.on("keydown.enter", lambda: _send())

    async def _init() -> None:
        """Resolve the tab-scoped session once the client is connected."""
        try:
            navigation_type = str(await ui.run_javascript(_NAV_TYPE_JS, timeout=3.0))
        except Exception:
            navigation_type = "navigate"

        store = _store()
        stored = store.get(_TAB_KEY)
        if should_start_new_session(navigation_type, stored):
            stored = new_tab_session()
            store[_TAB_KEY] = stored
        state["store"] = stored

        _render_transcript()
        _render_chips()
        ui.run_javascript(_SELECTION_HOOK_JS)

    ui.timer(0.1, _init, once=True)
