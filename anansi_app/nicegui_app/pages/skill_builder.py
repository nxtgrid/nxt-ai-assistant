"""The chat-driven skill builder widget (originally Phase 4 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md; no longer a page
of its own since P3's Task 9 -- see below).

Interactive, chat-driven authoring surface for user-designed skills: the
author has a normal conversation (full tool access, as if talking to the
bot directly) via chat_orchestrator's POST /chat -- one user message = one
step. There is no artificial read-only/write restriction *during* design;
the per-step "allow this step to make changes" and "also return this
response" controls are authoring metadata for later replay (Phase 5), held
in the caller's state dict and only baked into the stored steps shape at
Save (see _derive_steps_payload).

render_builder is the reusable piece: it renders the transcript, input,
send and rewind controls into the current container and returns the
mutable state dict a caller reads from -- nicegui_app/pages/skills.py's
"New workflow" modal is the only caller now. There is no standalone page or
`render` function anymore: /skill-builder (main.py) redirects to /skills
rather than routing here, and the module-level Save-as-skill dialog that
used to wrap this for that route was removed with it -- see git history
(P3 Task 9) if that flow needs to be revived.

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
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    """POST /chat -- see this module's docstring for the auth story.

    metadata.skill_builder_staff_auth opts this session into
    resolve_auth.py's skill_builder_staff_auth branch, which grants
    is_staff + STAFF_ORG_ID scope without consulting public.accounts (the
    bot's own Telegram/customer-onboarding auth DB -- a different identity
    system than the Google-OAuth-gated NiceGUI admin app this page lives
    in, which most bot-admin emails were never added to). Only takes effect
    when paired with the server-verified _identity_trusted signal
    IDENTITY_ASSERTION_KEY already proves for this caller -- see
    resolve_auth.py's branch docstring; the flag alone grants nothing.
    """
    resp = requests.post(
        f"{_orchestrator_base_url()}/chat",
        headers=_orchestrator_headers(),
        json={
            "message": message,
            "user_id": user_id,
            "user_email": user_email,
            "source": "api",
            "metadata": {"skill_builder_staff_auth": True},
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


def _summarize_steps(steps: List[Dict[str, Any]], title: str = "") -> str:
    """POST /skills/summarize -- see skill_summary.py. Called automatically
    after every step change (render_builder's _refresh_transcript) so the
    Summary field stays current without the author re-triggering it by hand."""
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


# Response text an escalation produces when a tool failed. A step that
# captured one of these saved an apology, not a result -- saving that skill
# bakes the apology in permanently.
_FAILURE_MARKERS = (
    "#nxtaction",
    "unable to retrieve",
    "something went wrong on our end",
)


def _step_had_tool_error(step: Dict[str, Any]) -> bool:
    """Whether this step's tools failed rather than returning data.

    Two signals, either sufficient: an explicit error on a recorded tool
    result, or escalation text in the response. Adapted to the real
    response_messages shape (see _step_tool_names/_group_into_steps) rather
    than the plan's assumed flat "tool_calls" list -- a tool invocation and
    its result are two entries in response_messages: the row carrying
    function_call names the call, and (per skill_builder_service.py's
    _MESSAGE_COLUMNS) tool_result on either that row or a later one carries
    the outcome, which the orchestrator swallows and escalates from rather
    than surfacing to the builder as a distinct error field.
    """
    for m in step["response_messages"]:
        result = m.get("tool_result")
        if isinstance(result, dict) and result.get("error"):
            return True
    if "escalate_to_support" in _step_tool_names(step):
        return True
    text = _step_response_text(step).lower()
    return any(marker in text for marker in _FAILURE_MARKERS)


# Cap on how much of a step's response text feeds the auto-summary prompt
# (skill_summary.py's _build_summary_prompt) -- long enough to name what was
# actually retrieved, short enough that a handful of steps' worth still fits
# comfortably in one summarization call. Not a display truncation; the full
# response stays visible in the transcript via _step_response_text.
_RESULT_PREVIEW_CHARS = 500


def _derive_steps_payload(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the skills.steps-shaped payload from the current transcript +
    per-step flags -- see
    chat_orchestrator/orchestrator/experts/skill_validation.py's module
    docstring for the canonical shape. Used for /skills/validate,
    /skills/summarize, and Save, so all three always see the same steps.

    Appends whatever's left of state["initial_steps"] beyond the live
    transcript -- the "pending tail" (steps from a reopened workflow not yet
    re-run this session) -- verbatim, renumbered only. This is what makes
    "open Edit, Save without touching anything" reproduce the stored steps
    byte-for-byte, and what lets a step kind this builder can't produce
    (e.g. a P3 "function" step) survive an edit untouched instead of being
    dropped.
    """
    live_count = len(state["steps"])
    pending_tail = state.get("initial_steps", [])[live_count:]
    step_count = live_count + len(pending_tail)

    steps = []
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
                # The final step (of the combined live + pending sequence)
                # is always an implicit response step even if not flagged --
                # see the plan's "Run-mode output" section.
                "is_response_step": is_last or flags["is_response_step"],
                "had_tool_error": _step_had_tool_error(step),
                # Builder-only context for /skills/summarize (item b) -- what
                # the step's tools actually returned, not just the intent
                # the instruction states. Ignored by /skills/validate.
                "result_preview": _step_response_text(step)[:_RESULT_PREVIEW_CHARS],
            }
        )

    for offset, stored_step in enumerate(pending_tail):
        index = live_count + offset
        is_last = index == step_count - 1
        kept = dict(stored_step)
        kept["index"] = index
        kept["is_response_step"] = is_last or kept.get("is_response_step", False)
        steps.append(kept)

    return steps


async def render_builder(
    user_email: str,
    user_id: str,
    initial_steps: Optional[List[Dict[str, Any]]] = None,
    on_summary_update: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Render the step builder (transcript, input, send, rewind) into the
    current container.

    Returns the mutable `state` dict the caller reads after the user is
    done -- it stays live via the closures below, so a caller that reads
    `state["steps"]` later (e.g. on its own Save click) sees every update,
    not a snapshot from when this function returned. Extracted from
    `render` unchanged so the same widget serves both the standalone page
    and the skills modal (Phase 3) -- the transcript, send and rewind
    behaviour is deliberately untouched.

    `user_id` is the caller's to build, not derived here: it must be fresh
    per builder session (a bare email would make every session for one
    staff member collapse into a single never-ending chat_orchestrator
    session -- see generate_session_id), and each caller knows its own
    freshness boundary (a page load vs. a modal open).

    `initial_steps` is accepted for the editor modal's future "resume an
    existing skill's steps" use and is not wired to anything yet -- neither
    this phase nor the modal built on top of it (Task 8) populates it, so a
    skill's steps still can't be reopened for further chat-driven editing
    once saved. Tracked as a known gap, not silently solved here.

    `on_summary_update`, if given, is called with the freshly auto-generated
    summary (item b) every time it changes -- after every send/rewind, as
    long as the caller hasn't set `state["summary_user_edited"]` (the
    caller's job: flip it the moment the author types into the summary
    field themselves, so this can't clobber a hand edit on the next step).
    A plain sync callback, not a widget binding: the only caller
    (nicegui_app/pages/skills.py) just needs to copy a string into its own
    textarea, which is simpler done directly than through NiceGUI's
    polling-based bind_value_from.
    """
    db = get_skill_builder_service()

    state: Dict[str, Any] = {
        "session_id": None,
        "steps": [],
        "flags": {},  # step index -> {"allow_write": bool, "is_response_step": bool}
        "validation_errors": [],
        "sending": False,
        "summary": "",
        "summary_user_edited": False,
    }

    if not _identity_configured():
        ui.label(
            "⚠️ IDENTITY_ASSERTION_KEY is not configured on chat_orchestrator. The builder "
            "cannot send messages until it is set -- see chat_orchestrator/.env.example."
        ).classes("text-negative")
        return state

    if not await run.io_bound(db.is_configured):
        ui.label("⚠️ Database not configured. Check CHAT_DB_URL and CHAT_DB_SERVICE_KEY.").classes(
            "text-negative"
        )
        return state

    transcript = ui.column().classes("w-full gap-3")

    with ui.row().classes("w-full items-end gap-2"):
        message_input = ui.textarea("Next step").classes("flex-grow").props("autogrow")
        send_button = ui.button("Send", icon="send")

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

            # Auto-regenerate the summary (item b) unless the author has
            # already taken over editing it by hand -- see this function's
            # docstring on on_summary_update. Skipped entirely (no network
            # call) once summary_user_edited, not just left unapplied.
            if not state["summary_user_edited"]:
                try:
                    state["summary"] = await run.io_bound(lambda: _summarize_steps(derived))
                except Exception as e:
                    ui.notify(f"Could not auto-generate summary: {e}", type="warning")
                else:
                    if on_summary_update:
                        on_summary_update(state["summary"])
        else:
            state["validation_errors"] = []

        _rebuild_transcript()

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
                lambda: _send_chat_message(message=text, user_id=user_id, user_email=user_email)
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

    await _refresh_transcript()
    return state
