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


def _apply_instruction_edit(stored_step: Dict[str, Any], new_text: str) -> Dict[str, Any]:
    """Write an in-place instruction edit onto a *pending* stored step.

    Mutates and returns the same dict object held in `state["initial_steps"]`,
    exactly as the mock switch already does -- that object identity is the
    whole mechanism: `_derive_steps_payload` passes the pending tail through
    verbatim, so an edit made here is already in the payload Save builds, with
    no separate "dirty steps" bookkeeping to keep in sync.

    Keeps `output_var` in step with the text's own `-> {{var}}` write clause.
    Not cosmetic: skill_validation.py's Pass 1 errors on an llm step whose
    stored `output_var` disagrees with (or outlives) its instruction, so an
    edit that touches the clause has to move both or the step silently
    becomes un-activatable. `name` follows `output_var` only while there is
    one -- it is display-only in validation, so a step that loses its clause
    keeps the name it already had rather than churning to a positional
    `step_N` that no longer means anything.

    A `kind="function"` step's write comes from its handler's return value
    rather than clause parsing (same Pass 1), so its `output_var` is left
    alone however the label text changes.
    """
    stored_step["instruction"] = new_text
    if (stored_step.get("kind") or "llm") == "function":
        return stored_step
    _read_text, output_var = _parse_output_binding(new_text)
    stored_step["output_var"] = output_var
    if output_var:
        stored_step["name"] = output_var
    return stored_step


def _delete_pending_step(
    initial_steps: List[Dict[str, Any]], live_count: int, offset: int
) -> List[Dict[str, Any]]:
    """Drop pending step `offset` -- an index into the pending *tail*, not
    into `initial_steps` -- mutating and returning the same list object.

    Scoped to the tail because everything before `live_count` has already
    been re-run this session and belongs to the chat transcript, which this
    list cannot edit; Rewind is the only way back through those. An
    out-of-range offset is a no-op rather than an IndexError, so a click
    landing on a card that a concurrent rebuild has already moved can't
    take the modal down.
    """
    absolute = live_count + offset
    if offset < 0 or absolute >= len(initial_steps):
        return initial_steps
    del initial_steps[absolute]
    return initial_steps


def _move_pending_step(
    initial_steps: List[Dict[str, Any]], live_count: int, offset: int, delta: int
) -> List[Dict[str, Any]]:
    """Move pending step `offset` by `delta` places *within the pending
    tail*, mutating and returning `initial_steps`.

    Clamped to the tail for the same reason `_delete_pending_step` is: a
    pending step can never be reordered above an already-re-run one, because
    the live steps' order is the chat session's own history, not a list this
    page owns.
    """
    target = offset + delta
    tail_len = len(initial_steps) - live_count
    if offset < 0 or offset >= tail_len or target < 0 or target >= tail_len:
        return initial_steps
    step = initial_steps.pop(live_count + offset)
    initial_steps.insert(live_count + target, step)
    return initial_steps


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

    `initial_steps` is the stored `skills.steps` list when this builder is
    reopened to edit an existing workflow (`[]` for a brand-new one). It is
    captured once into `state["initial_steps"]` at mount and never mutated;
    "how much of it is still pending" is a *derived* value
    (`state["initial_steps"][len(state["steps"]):]`), not tracked state --
    slicing by the live step count is what makes this self-correcting under
    Rewind for free (archiving live steps back to zero re-expands the
    pending tail to the full original list with no bookkeeping needed).
    Each pending step renders as a greyed, *editable* card
    (`_render_pending_step`) until its instruction is actually (re-)sent, at
    which point it graduates into a normal live step sourced from the real
    transcript exactly like any other. See `_derive_steps_payload` for how a
    still-pending tail is preserved verbatim into the saved payload.

    That split is the editor's whole model, and it is worth stating plainly
    because the UI used not to: there are two ways to change a reopened
    workflow, and they cost very different things.

    - *Editing* a pending card -- its text, its order, whether it exists at
      all -- runs nothing. It mutates the stored dict in place and Save
      writes it. This is the ordinary case (a typo, a reworded instruction,
      a step that is no longer wanted) and it works on any step, at any
      position, in any order.
    - *Running* a step is a real execution against live tools, and it is
      strictly sequential from the top, because each step's tools read the
      earlier steps' results out of the same chat session. So only the first
      pending step is ever runnable; there is no "just re-run step 9". The
      first pending card is the only one that carries a run button, and the
      compose box states which step number a send will occupy.

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
        "initial_steps": initial_steps or [],
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

    # Three stacked containers, in this DOM order: what has already run,
    # the compose box, then what has not run yet. The box sits *at the
    # cursor* between the two halves rather than below everything. When it
    # was last -- and auto-filled with the FIRST pending step's text under
    # the label "Next step" -- it read as a step appended after the last
    # card while holding the beginning of the workflow, which is the exact
    # opposite of what it does.
    live_column = ui.column().classes("w-full gap-3")

    with ui.column().classes("w-full gap-1 q-my-sm"):
        compose_caption = ui.label("").classes("text-caption text-grey-7")
        with ui.row().classes("w-full items-end gap-2"):
            message_input = ui.textarea("Run a step").classes("flex-grow").props("autogrow")
            send_button = ui.button("Send", icon="send")

    pending_column = ui.column().classes("w-full gap-3")

    async def _refresh_transcript() -> None:
        if not state["session_id"]:
            state["steps"] = []
        else:
            messages = await run.io_bound(lambda: db.get_builder_messages(state["session_id"]))
            state["steps"] = _group_into_steps(messages)

        for index in range(len(state["steps"])):
            state["flags"].setdefault(index, {"allow_write": False, "is_response_step": False})

        derived = _derive_steps_payload(state)
        state["validation_errors"] = await _validated(derived)

        # Auto-regenerate the summary (item b) unless the author has
        # already taken over editing it by hand -- see this function's
        # docstring on on_summary_update. Skipped entirely (no network
        # call) once summary_user_edited, not just left unapplied.
        if derived and not state["summary_user_edited"]:
            try:
                state["summary"] = await run.io_bound(lambda: _summarize_steps(derived))
            except Exception as e:
                ui.notify(f"Could not auto-generate summary: {e}", type="warning")
            else:
                if on_summary_update:
                    on_summary_update(state["summary"])

        _rebuild_transcript()

    async def _validated(derived: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """/skills/validate for a derived payload, or [] when it is empty or
        the call fails. Split out of _refresh_transcript so an in-place edit
        to a pending card can refresh validation on its own, without the DB
        re-read and the summarization round-trip a real send needs."""
        if not derived:
            return []
        try:
            return await run.io_bound(lambda: _validate_steps(derived))
        except Exception as e:
            ui.notify(f"Could not validate steps: {e}", type="warning")
            return []

    async def _revalidate(*, force_rebuild: bool = False) -> None:
        """Re-validate after an edit that changed the steps without running
        anything, and redraw if the findings moved.

        Not optional bookkeeping: skills.py's Save reads
        `state["validation_errors"]` as it stands when deciding whether the
        workflow may go `active`, so an edit that leaves them stale is an
        edit that can promote a broken skill (or block a fixed one).

        Redraws only when the findings actually changed, because this runs
        on every textarea blur and an unconditional rebuild would tear down
        and re-mount fifteen cards -- losing scroll position -- every time
        the author tabbed out of one.
        """
        before = state["validation_errors"]
        state["validation_errors"] = await _validated(_derive_steps_payload(state))
        if force_rebuild or state["validation_errors"] != before:
            _rebuild_transcript()

    def _rebuild_transcript() -> None:
        live_column.clear()
        pending_column.clear()
        errors_by_step: Dict[int, List[Dict[str, Any]]] = {}
        for err in state["validation_errors"]:
            errors_by_step.setdefault(err["step_index"], []).append(err)

        live_count = len(state["steps"])
        pending_tail = state["initial_steps"][live_count:]
        total = live_count + len(pending_tail)

        with live_column:
            for index, step in enumerate(state["steps"]):
                _render_step(index, step, errors_by_step.get(index, []))

        with pending_column:
            # Only meaningful for a reopened workflow -- initial_steps is
            # always [] for a brand-new one, so pending_tail is always []
            # there too and none of this renders.
            if pending_tail:
                ui.label(
                    f"Saved steps -- {len(pending_tail)} of {total} not re-run in this "
                    "session"
                ).classes("text-bold text-grey-7")
                ui.label(
                    "Edit, reorder or delete any of these in place and press Save: "
                    "nothing runs, and the rest is stored exactly as it stands. "
                    "Running is the separate path, and it only goes in order from "
                    "the top -- a step's tools need the earlier steps' results in "
                    "the same session, so there is no way to run one from the middle "
                    "on its own."
                ).classes("text-caption text-grey-6")

            for offset, stored_step in enumerate(pending_tail):
                _render_pending_step(
                    live_count + offset,
                    stored_step,
                    offset=offset,
                    tail_len=len(pending_tail),
                    total=total,
                    errors=errors_by_step.get(live_count + offset, []),
                )

        _set_compose_caption(live_count, len(pending_tail), total)

    def _set_compose_caption(live_count: int, pending_count: int, total: int) -> None:
        """Say what pressing Send will actually do, above the box.

        The box used to be labelled "Next step" and sat below the last
        pending card, so its position claimed it appended to the end while
        its auto-filled contents were the *first* pending step. What it
        really does is run the step at the live cursor -- which, for a
        reopened workflow, consumes the saved card at that same position
        (see _derive_steps_payload: the pending tail is sliced by the live
        step count, so a send shortens it from the front).
        """
        if pending_count:
            compose_caption.set_text(
                f"Send runs a step for real as step {live_count + 1} of {total}, "
                f"replacing saved step {live_count + 1} below."
            )
        elif live_count:
            compose_caption.set_text(
                f"Send runs a new step {live_count + 1} for real and appends it."
            )
        else:
            compose_caption.set_text(
                "Describe this workflow's first step. Send runs it for real."
            )

    def _edit_pending_instruction(stored_step: Dict[str, Any], text: str) -> None:
        """Per-keystroke write-through for a pending card's textarea.

        Deliberately sync and deliberately not revalidating: this fires on
        every character, and the findings are refreshed once on blur
        instead (see the card's "blur" handler and _revalidate).
        """
        _apply_instruction_edit(stored_step, text)

    async def _move_step(offset: int, delta: int) -> None:
        _move_pending_step(state["initial_steps"], len(state["steps"]), offset, delta)
        await _revalidate(force_rebuild=True)

    async def _delete_step(offset: int) -> None:
        live_count = len(state["steps"])
        pending_tail = state["initial_steps"][live_count:]
        if not 0 <= offset < len(pending_tail):
            return
        removed_name = pending_tail[offset].get("name") or ""
        _delete_pending_step(state["initial_steps"], live_count, offset)
        await _revalidate(force_rebuild=True)
        label = f" ({removed_name})" if removed_name else ""
        ui.notify(
            f"Deleted step {live_count + offset + 1}{label}. Nothing is written "
            "until you press Save.",
            type="info",
        )

    def _render_pending_step(
        index: int,
        stored_step: Dict[str, Any],
        *,
        offset: int,
        tail_len: int,
        total: int,
        errors: List[Dict[str, Any]],
    ) -> None:
        """A step from initial_steps not yet re-run in this edit session.

        Editable in place rather than inert. The card's textarea writes
        straight onto `stored_step` (`_apply_instruction_edit`), which is
        the same dict object `_derive_steps_payload` copies into the Save
        payload -- so fixing a step's wording, dropping one, or resequencing
        two costs nothing and runs nothing. That is the common edit, and
        before this it was the one thing the editor could not do: the only
        way to change step 9 was to re-run steps 1 through 9 against live
        tools, because a re-run is a real execution and executions are
        sequential.

        Running stays sequential and stays explicit. Only the first pending
        card carries "Run this step" (`is_up_next`); the rest have no run
        affordance at all, since running them out of order would hand their
        tools a session with none of the earlier steps' results in it.

        The mock toggle (Task 5.3 of
        docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md) is
        unchanged: this builder has no way to CREATE a `kind:"function"`
        step (that is the converter script's job, Phase 7), but a converted
        skill's pending steps land here, and an author reviewing one needs
        to control whether it runs mocked without leaving the page. Gated on
        `stored_step.get("mutates")`, which only a converted step's dict
        carries (stamped by the converter from the handler's real
        StepContract.mutates -- nothing in this app can look that up itself;
        anansi_app and chat_orchestrator are separately deployed packages,
        see this module's docstring) -- a non-mutating step shows no switch,
        since mock mode is meaningless for it.
        """
        is_up_next = offset == 0
        kind = stored_step.get("kind") or "llm"

        card_classes = "w-full bg-grey-2"
        if is_up_next:
            card_classes += " border-l-4 border-primary"
        with ui.card().classes(card_classes):
            with ui.row().classes("items-center justify-between w-full"):
                label = f"Step {index + 1} of {total} · saved"
                if is_up_next:
                    label += " · next to run"
                ui.label(label).classes("text-bold text-grey-7")

                with ui.row().classes("items-center gap-1"):
                    up_button = ui.button(
                        icon="arrow_upward", on_click=lambda o=offset: _move_step(o, -1)
                    ).props("flat dense round color=grey-7")
                    up_button.tooltip("Move earlier")
                    if offset == 0:
                        up_button.disable()

                    down_button = ui.button(
                        icon="arrow_downward", on_click=lambda o=offset: _move_step(o, 1)
                    ).props("flat dense round color=grey-7")
                    down_button.tooltip("Move later")
                    if offset >= tail_len - 1:
                        down_button.disable()

                    delete_button = ui.button(
                        icon="delete_outline", on_click=lambda o=offset: _delete_step(o)
                    ).props("flat dense round color=negative")
                    delete_button.tooltip("Delete this step")

            instruction_input = (
                ui.textarea(value=stored_step.get("instruction") or "")
                .classes("w-full")
                .props("autogrow outlined dense")
            )
            instruction_input.on_value_change(
                lambda e, s=stored_step: _edit_pending_instruction(s, e.value or "")
            )
            # Re-validate when the author leaves the field, not per
            # keystroke: /skills/validate is a network round-trip, and Save
            # reads the cached findings (skills.py's can_promote_to_active),
            # so they have to be current by the time focus moves on -- but
            # not forty times while one sentence is typed.
            instruction_input.on("blur", lambda: _revalidate())

            if kind == "function":
                handler_name = stored_step.get("handler") or "a handler"
                ui.label(
                    f"Runs '{handler_name}' directly -- this text is the step's "
                    "label, not the action it takes."
                ).classes("text-caption text-grey-6")

            preview = (stored_step.get("result_preview") or "").strip()
            if preview:
                ui.label(f"Previously retrieved: {preview[:200]}").classes(
                    "text-caption text-grey-5"
                )

            if stored_step.get("mutates"):
                # Default True (mock ON) when the converter hasn't stamped
                # an explicit value -- "safe by default" matches every other
                # mock-mode default in this plan (a mutating step is
                # presumed unsafe to run for real until an author
                # deliberately opts in).
                handler_name = stored_step.get("handler") or "this step"
                mock_switch = ui.switch(
                    f"Mock '{handler_name}' (no real side effect)",
                    value=stored_step.get("mock", True),
                )
                mock_switch.on_value_change(
                    lambda e, s=stored_step: s.__setitem__("mock", e.value)
                )

            # Findings for a pending step were computed but never drawn
            # before this: errors_by_step only reached _render_step, so a
            # broken saved step showed a clean card and then blocked
            # activation from skills.py with no on-card cause to look at.
            for err in errors:
                color = "negative" if err["severity"] == "error" else "warning"
                ui.label(f"⚠️ {err['message']}").classes(f"text-{color} text-caption")

            if is_up_next:
                run_button = ui.button(
                    "▶ Run this step", on_click=lambda s=stored_step: _send(s.get("instruction"))
                ).props("flat dense color=primary")
                run_button.tooltip(
                    f"Runs this text for real as step {index + 1} and replaces this "
                    "card with the live result."
                )

    def _render_step(index: int, step: Dict[str, Any], errors: List[Dict[str, Any]]) -> None:
        pending_tail = state["initial_steps"][len(state["steps"]):]
        is_last = index == len(state["steps"]) - 1 and not pending_tail
        flags = state["flags"][index]
        total = len(state["steps"]) + len(pending_tail)

        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                # "of {total}" so a live card and a saved card below it are
                # obviously positions in one numbered list rather than two
                # separate lists that happen to both start at 1.
                ui.label(f"Step {index + 1} of {total} · ran this session").classes("text-bold")
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

    async def _send(text: Optional[str] = None) -> None:
        """Run one step for real.

        `text=None` (the Send button, and NiceGUI's own click dispatch --
        `helpers.expects_arguments` ignores defaulted parameters, so the
        click event is never passed in as `text`) sends whatever is in the
        compose box. An explicit string is a pending card's "Run this step",
        which sends that card's current text -- edits included, since the
        card's textarea writes onto the same dict the button reads.
        """
        raw = message_input.value if text is None else text
        text = raw.strip() if raw else ""
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
                "that isn't undone. The text is in the compose box; edit and send "
                "to continue.",
                type="warning",
                multi_line=True,
                timeout=10000,
            )
        else:
            ui.notify(
                "Rewound. The text is in the compose box; edit and send to continue.",
                type="info",
            )

    send_button.on_click(_send)

    await _refresh_transcript()
    return state
