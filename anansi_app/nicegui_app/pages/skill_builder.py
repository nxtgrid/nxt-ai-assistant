"""Skill builder page (Phase 4 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md).

Interactive, chat-driven authoring surface for user-designed skills: the
author has a normal conversation (full tool access, as if talking to the
bot directly) via chat_orchestrator's POST /chat -- one user message = one
step. There is no artificial read-only/write restriction *during* design;
the per-step "allow this step to make changes" and "also return this
response" controls are authoring metadata for later replay (Phase 5), held
in this page's own state and only baked into the stored steps shape at Save.

Rewind archives a step and everything after it (see
services/skill_builder_service.py's archive_from_message_index) and
repopulates the input box with that step's text -- no branching, no undo;
see the plan's "Decisions already made".

Sends every message with source="api" and the logged-in user's real email,
authenticated by both API_KEY (any "api" caller) and IDENTITY_ASSERTION_KEY
(this caller specifically may assert user_email -- see
chat_orchestrator/orchestrator/api/app.py's is_identity_trusted_caller).
Without IDENTITY_ASSERTION_KEY configured, sending is disabled with an
explicit banner rather than failing on the first click with a 403 that
looks like a bug.

Not yet implemented (scoped out of this first pass, tracked in the plan doc's
Phase 4 implementation note): attachment rendering and per-step token counts.
Neither is persisted at chat_messages granularity today -- only text and
tool names invoked survive a page reload.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests
from nicegui import run, ui

from nicegui_app.services_access import get_skill_builder_service

# Mirrors chat_orchestrator/orchestrator/experts/skill_step_bindings.py's
# _OUTPUT_BINDING_RE. Duplicated rather than imported -- anansi_app and
# chat_orchestrator are separately deployed packages with no shared import
# path outside shared/ (see skill_validation.py's own docstring for the
# same reasoning about a comparably small regex).
_OUTPUT_BINDING_RE = re.compile(r"(?:→|->)\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*\Z")


def _parse_output_binding(instruction: str) -> Tuple[str, Optional[str]]:
    match = _OUTPUT_BINDING_RE.search(instruction or "")
    if not match:
        return instruction, None
    return instruction[: match.start()].rstrip(), match.group(1)


def _orchestrator_base_url() -> str:
    # Base URL, no trailing path -- callers append /chat, /skills/validate,
    # etc. themselves. (scripts/broadcast_scheduler.py's CHAT_ORCHESTRATOR_URL
    # usage is the correct precedent for this; some other call sites in this
    # app assume the var itself already ends in /chat, which is inconsistent
    # with how the var is actually documented in flag_registry.py.)
    return os.getenv("CHAT_ORCHESTRATOR_URL", "http://localhost:8000").rstrip("/")


def _orchestrator_headers() -> Dict[str, str]:
    return {
        "X-Api-Key": os.getenv("API_KEY", ""),
        "X-Identity-Assertion-Key": os.getenv("IDENTITY_ASSERTION_KEY", ""),
        "Content-Type": "application/json",
    }


def _identity_configured() -> bool:
    return bool(os.getenv("IDENTITY_ASSERTION_KEY", ""))


def _error_detail(e: requests.HTTPError) -> str:
    try:
        body = e.response.json()
        return body.get("message") or body.get("error") or str(e)
    except Exception:
        return str(e)


def _send_chat_message(*, message: str, user_id: str, user_email: str) -> Dict[str, Any]:
    """POST /chat -- see this module's docstring for the auth story."""
    resp = requests.post(
        f"{_orchestrator_base_url()}/chat",
        headers=_orchestrator_headers(),
        json={
            "message": message,
            "user_id": user_id,
            "user_email": user_email,
            "source": "api",
            "metadata": {},
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def _validate_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    resp = requests.post(
        f"{_orchestrator_base_url()}/skills/validate",
        headers=_orchestrator_headers(),
        json={"steps": steps},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("errors", [])


def _summarize_steps(steps: List[Dict[str, Any]], title: str) -> str:
    resp = requests.post(
        f"{_orchestrator_base_url()}/skills/summarize",
        headers=_orchestrator_headers(),
        json={"steps": steps, "title": title},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("summary", "")


def _group_into_steps(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group a flat, chronological chat_messages list into one entry per
    user message: {"user_message": <row>, "response_messages": [<row>...]}.

    "One user message = one step" (see the plan's Phase 4 Work section).
    Any row before the first user message is dropped rather than crashing
    the page -- shouldn't happen for a builder session, but a stray system
    row must not break rendering.
    """
    steps: List[Dict[str, Any]] = []
    for row in messages:
        if row.get("role") == "user":
            steps.append({"user_message": row, "response_messages": []})
        elif steps:
            steps[-1]["response_messages"].append(row)
    return steps


def _step_response_text(step: Dict[str, Any]) -> str:
    parts = [
        m.get("content")
        for m in step["response_messages"]
        if m.get("role") == "model" and m.get("content")
    ]
    return "\n\n".join(parts)


def _step_tool_names(step: Dict[str, Any]) -> List[str]:
    names = []
    for m in step["response_messages"]:
        fc = m.get("function_call")
        if fc and fc.get("name"):
            names.append(fc["name"])
    return names


def _derive_steps_payload(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the skills.steps-shaped payload from the current transcript +
    per-step flags -- see
    chat_orchestrator/orchestrator/experts/skill_validation.py's module
    docstring for the canonical shape. Used for /skills/validate,
    /skills/summarize, and Save, so all three always see the same steps.
    """
    steps = []
    step_count = len(state["steps"])
    for index, step in enumerate(state["steps"]):
        instruction = step["user_message"].get("content") or ""
        _read_text, output_var = _parse_output_binding(instruction)
        flags = state["flags"].get(index, {"allow_write": False, "is_response_step": False})
        is_last = index == step_count - 1
        steps.append(
            {
                "index": index,
                "name": output_var or f"step_{index + 1}",
                "instruction": instruction,
                "output_var": output_var,
                "allow_write": flags["allow_write"],
                # The final step is always an implicit response step even if
                # not flagged -- see the plan's "Run-mode output" section.
                "is_response_step": is_last or flags["is_response_step"],
            }
        )
    return steps


async def render(user: dict[str, Any]) -> None:
    user_email = user.get("email", "unknown")
    db = get_skill_builder_service()

    state: Dict[str, Any] = {
        "draft_id": str(uuid.uuid4()),
        "session_id": None,
        "steps": [],
        "flags": {},  # step index -> {"allow_write": bool, "is_response_step": bool}
        "validation_errors": [],
        "sending": False,
    }

    def _user_id() -> str:
        # Per-draft, not per-user: a stable user_id would make every builder
        # session for one staff member collapse into a single
        # never-ending chat_orchestrator session (see generate_session_id).
        # A fresh draft_id per page load gives each "New skill" attempt its
        # own session.
        return f"{user_email}:{state['draft_id']}"

    ui.label("🧩 Skill Builder").classes("text-h5")
    ui.label(
        "Chat normally to build a skill step by step. Each message you send becomes one "
        "step; rewind any step to redo it and everything after."
    ).classes("text-caption")

    if not _identity_configured():
        ui.label(
            "⚠️ IDENTITY_ASSERTION_KEY is not configured on chat_orchestrator. The builder "
            "cannot send messages until it is set -- see chat_orchestrator/.env.example."
        ).classes("text-negative")
        return

    if not await run.io_bound(db.is_configured):
        ui.label("⚠️ Database not configured. Check CHAT_DB_URL and CHAT_DB_SERVICE_KEY.").classes(
            "text-negative"
        )
        return

    transcript = ui.column().classes("w-full gap-3")

    with ui.row().classes("w-full items-end gap-2"):
        message_input = ui.textarea("Next step").classes("flex-grow").props("autogrow")
        send_button = ui.button("Send", icon="send")

    with ui.row().classes("w-full justify-end"):
        save_button = ui.button("💾 Save as skill", color="primary")
    save_button.set_visibility(False)

    async def _refresh_transcript() -> None:
        if not state["session_id"]:
            state["steps"] = []
        else:
            messages = await run.io_bound(lambda: db.get_builder_messages(state["session_id"]))
            state["steps"] = _group_into_steps(messages)

        for index in range(len(state["steps"])):
            state["flags"].setdefault(index, {"allow_write": False, "is_response_step": False})

        derived = _derive_steps_payload(state)
        if derived:
            try:
                state["validation_errors"] = await run.io_bound(lambda: _validate_steps(derived))
            except Exception as e:
                state["validation_errors"] = []
                ui.notify(f"Could not validate steps: {e}", type="warning")
        else:
            state["validation_errors"] = []

        _rebuild_transcript()
        save_button.set_visibility(bool(state["steps"]))

    def _rebuild_transcript() -> None:
        transcript.clear()
        errors_by_step: Dict[int, List[Dict[str, Any]]] = {}
        for err in state["validation_errors"]:
            errors_by_step.setdefault(err["step_index"], []).append(err)

        with transcript:
            for index, step in enumerate(state["steps"]):
                _render_step(index, step, errors_by_step.get(index, []))

    def _render_step(index: int, step: Dict[str, Any], errors: List[Dict[str, Any]]) -> None:
        is_last = index == len(state["steps"]) - 1
        flags = state["flags"][index]

        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label(f"Step {index + 1}").classes("text-bold")
                ui.button("↩ Rewind to here", on_click=lambda i=index: _rewind(i)).props(
                    "flat dense color=warning"
                )

            ui.label(step["user_message"].get("content") or "").classes("text-body1")

            response_text = _step_response_text(step)
            if response_text:
                ui.markdown(response_text).classes("text-caption")

            tool_names = _step_tool_names(step)
            if tool_names:
                ui.label(f"🔧 Tools used: {', '.join(tool_names)}").classes("text-caption")

            with ui.row().classes("items-center gap-4"):
                write_switch = ui.switch(
                    "Allow this step to make changes", value=flags["allow_write"]
                )
                write_switch.on_value_change(
                    lambda e, i=index: state["flags"][i].__setitem__("allow_write", e.value)
                )
                response_switch = ui.switch(
                    "Also return this response", value=(is_last or flags["is_response_step"])
                )
                if is_last:
                    # Always true for the last step (see the plan's
                    # "Run-mode output" rule) -- disabled, not hidden, so the
                    # author isn't left wondering why toggling it does
                    # nothing.
                    response_switch.disable()
                else:
                    response_switch.on_value_change(
                        lambda e, i=index: state["flags"][i].__setitem__(
                            "is_response_step", e.value
                        )
                    )

            for err in errors:
                color = "negative" if err["severity"] == "error" else "warning"
                ui.label(f"⚠️ {err['message']}").classes(f"text-{color} text-caption")

    async def _send() -> None:
        text = message_input.value.strip() if message_input.value else ""
        if not text or state["sending"]:
            return
        state["sending"] = True
        send_button.disable()
        try:
            result = await run.io_bound(
                lambda: _send_chat_message(message=text, user_id=_user_id(), user_email=user_email)
            )
        except requests.HTTPError as e:
            ui.notify(f"Send failed: {_error_detail(e)}", type="negative")
        except Exception as e:
            ui.notify(f"Send failed: {e}", type="negative")
        else:
            if result.get("session_id"):
                state["session_id"] = result["session_id"]
            message_input.value = ""
            await _refresh_transcript()
        finally:
            state["sending"] = False
            send_button.enable()

    async def _rewind(index: int) -> None:
        step = state["steps"][index]
        from_index = step["user_message"]["message_index"]
        text = step["user_message"].get("content") or ""

        side_effects = [
            f"🔧 {name}" for name in _step_tool_names(step) if not name.startswith(("get_", "list_", "search_", "check_", "fetch_"))
        ]

        await run.io_bound(
            lambda: db.archive_from_message_index(state["session_id"], from_index)
        )
        message_input.value = text
        await _refresh_transcript()

        if side_effects:
            ui.notify(
                "Rewound. Note: this step already ran " + ", ".join(side_effects) + " -- "
                "that isn't undone. Edit and resend to continue.",
                type="warning",
                multi_line=True,
                timeout=10000,
            )
        else:
            ui.notify("Rewound. Edit and resend to continue.", type="info")

    send_button.on_click(_send)

    async def _open_save_dialog() -> None:
        derived = _derive_steps_payload(state)
        blocking = [e for e in state["validation_errors"] if e["severity"] == "error"]
        if blocking:
            ui.notify("Fix validation errors before saving.", type="negative")
            return

        with ui.dialog() as dialog, ui.card().classes("w-[32rem] gap-2"):
            ui.label("Save as skill").classes("text-h6")
            title_input = ui.input("Title").classes("w-full")
            summary_input = ui.textarea("Summary").classes("w-full").props("autogrow")
            staff_switch = ui.switch("Staff only", value=True)

            async def _prefill_summary() -> None:
                title = title_input.value.strip() if title_input.value else ""
                if not title:
                    return
                try:
                    summary_input.value = await run.io_bound(
                        lambda: _summarize_steps(derived, title)
                    )
                except Exception as e:
                    ui.notify(f"Could not auto-generate a summary: {e}", type="warning")

            title_input.on("blur", _prefill_summary)

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")

                async def _confirm_save() -> None:
                    result = await run.io_bound(
                        lambda: db.save_skill(
                            title_input.value or "",
                            summary_input.value or "",
                            derived,
                            staff_switch.value,
                            user_email,
                        )
                    )
                    if result.get("success"):
                        ui.notify(f"Saved '{result['skill']['title']}'.", type="positive")
                        dialog.close()
                    else:
                        ui.notify(result.get("error") or "Save failed", type="negative")

                ui.button("Save", on_click=_confirm_save, color="primary")

        dialog.open()

    save_button.on_click(_open_save_dialog)

    await _refresh_transcript()
