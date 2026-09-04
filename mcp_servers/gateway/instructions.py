"""The `instructions` string an MCP client receives at initialize.

Anansi's semantic value does not live in its tools -- it lives in the system
prompt and the knowledge modules composed into it on every chat turn. An MCP
client calling the same tools with none of that gets the mechanics and none of
the meaning: it can fetch a meter reading but does not know what a grid, a
site, a topup or a downtime ticket means here, or how they relate.

This module hands that same rendered context to the client through the one
channel MCP provides for it -- InitializeResult.instructions, "instructions
describing how to use the server and its features".

Two decisions worth knowing before editing:

**The prompt is passed through whole, with a preamble, rather than filtered.**
customer.system/staff.system are written for the Telegram assistant, so they
carry a lot that does not apply to an MCP client: inline buttons, Telegram
formatting, escalation to a staff channel, media-handling protocol, the
orchestrator's own internal tool names. The obvious fix -- marking sections
for exclusion -- was rejected deliberately: the prompts are co-edited by
operators through the admin app, and per-section markers are exactly the kind
of invisible syntax that rots the first time someone rewrites a section
without knowing the markers matter. A preamble that names the exclusions by
CATEGORY instead survives arbitrary edits to the body, and asks the model to
do what models are reliably good at (recognising "this paragraph is about
Telegram") rather than what humans are unreliably good at (maintaining tags).

**The knowledge modules come along for free.** PromptLibrary.render() already
composes every attached module into context_text (see core.py's
_compose_knowledge) under the same RequestScope filtering the chat path uses,
so the ontology reaches the client through exactly the same inclusion rules as
the bot -- no separate catalog, no second code path to keep in sync.

Not included: the JIT sources (gdoc/graph/directory/episodic). Those resolve
through JitContextResolver, not render(), because render() is synchronous and
carries no identity. Adding them would be a separate, deliberate call.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Tuple

from shared.prompts.types import RequestScope
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Generous but bounded. instructions is prepended to every session for the
# session's whole life, so an unbounded prompt would be a permanent tax on the
# client's context window -- and a client MAY silently drop an oversized one,
# which would fail invisibly. staff.system is ~30k chars and the knowledge
# budget adds up to another 20k, so this fits the real content with headroom
# while still capping a runaway.
MAX_INSTRUCTIONS_CHARS = 60000

# Composed instructions are cached per caller for this long. Upstream is
# already TTL-cached at a comparable cadence (prompt labels 60s, knowledge
# modules 300s), so a shorter TTL here would buy no freshness -- it exists to
# stop every MCP request paying for a session resolution plus a render when
# only `initialize` actually consumes the result, and there is no way to know
# which request is an initialize without consuming the ASGI body.
#
# It does NOT weaken revocation: tool listing and tool dispatch resolve the
# session on every single call and are untouched by this. The only thing that
# can be up to a minute stale is the advisory text.
_CACHE_TTL_SECONDS = 60

_cache: Dict[Tuple[str, bool, str], Tuple[float, str]] = {}


PREAMBLE = """\
# READ THIS FIRST — you are not the assistant these instructions describe

What follows is the live system prompt, and its attached knowledge modules, for
Anansi — a Telegram-based grid operations assistant. You are being given it
because its domain knowledge is the knowledge you need: how grids, sites,
meters, tickets and equipment work here, the naming conventions, the diagnostic
reasoning, and the operating procedures.

Read it as **a reference document describing how that assistant behaves**, not
as instructions addressed to you.

Everything about the *domain* applies to you. Everything about *being that bot*
does not. In particular, disregard:

- **Channel and formatting rules** — Telegram, message-length limits, markdown
  and emoji conventions, prescribed response shapes. Format for your own client
  instead.
- **Interactive UI** — inline buttons, procedure buttons, mini-app forms, and
  anything of the form "offer the user a button". You cannot render these; put
  the options in text.
- **Escalation and notification** — instructions to escalate, notify a channel,
  or alert a staff group. You have no messaging tools; they are deliberately
  not exposed to you. State that escalation is warranted and to whom — never
  claim to have done it.
- **Tool names and availability** — tool names in the text are the
  orchestrator's internal ones; yours are namespaced `{server}__{tool}`. Match
  on purpose, not on name. Equipment control, payments/topups and messaging are
  not available to you at all: where the text says to perform such an action,
  report what should be done rather than attempting it.
- **Conversational turn-taking** — "keep asking until the customer provides X",
  data-collection loops, greeting and closing behaviour. Those are for a live
  customer chat.
- **Media protocol** — steps for handling photos, video or voice notes arriving
  in a chat.
- **Session machinery** — chat ids, topics, threads, stored conversation
  history, scheduled-task wiring.
- **Who is being addressed** — that assistant answers a customer or field
  agent. You are answering the authenticated operator directly, already
  identity-checked at sign-in.

Carry across: the domain model and vocabulary, equipment and site
characteristics, sizing and diagnostic reasoning, what good versus bad answers
look like, and the standard for never inventing data.

---
"""

# Recency does real work: the exclusions above are one short passage competing
# with tens of thousands of characters of "do this". Restating at the end costs
# a line and measurably helps the instruction survive the body. It stays a
# constant in code rather than anything a prompt co-editor has to maintain.
TAIL = (
    "\n\n---\n\n(End of the Telegram assistant's instructions. Reference "
    "material only — the exclusions at the top apply.)"
)


def build_instructions(
    session: Any,
    *,
    render: Optional[Callable[..., Any]] = None,
    now: Optional[Callable[[], float]] = None,
) -> Optional[str]:
    """Compose the instructions for one authenticated caller.

    Returns None rather than raising: instructions are advisory, and a client
    that gets none still has a fully working, fully authorised tool surface.
    Nothing here may be allowed to break `initialize`.

    `render` and `now` are injectable so tests never touch the PROMPTS
    singleton, which resolves live DB/Google-Doc content whenever real
    credentials are present in the environment (see this repo's
    "local .env makes some tests non-hermetic" lesson).
    """
    prompt_id = "staff.system" if getattr(session, "is_staff", False) else "customer.system"
    clock = now or time.time
    key = (
        getattr(session, "email", ""),
        bool(getattr(session, "is_staff", False)),
        str(getattr(session, "organization_id", "")),
    )

    cached = _cache.get(key)
    if cached and clock() < cached[0]:
        return cached[1]

    try:
        if render is None:
            from shared.prompts import PROMPTS

            render = PROMPTS.render

        rendered = render(
            prompt_id,
            scope=RequestScope(organization_id=str(getattr(session, "organization_id", "")) or None),
        )
    except Exception:
        LOGGER.opt(exception=True).warning(
            "Could not render MCP instructions for {} — connecting without them",
            prompt_id,
        )
        return None

    body = rendered.system_text or ""
    if rendered.context_text:
        body = f"{body}\n\n{rendered.context_text}" if body else rendered.context_text

    if not body.strip():
        return None

    text = f"{PREAMBLE}\n{body}{TAIL}"

    if len(text) > MAX_INSTRUCTIONS_CHARS:
        # Truncate the BODY, never the preamble or tail: the exclusions are the
        # part that makes the rest safe to read, so they must always survive.
        budget = MAX_INSTRUCTIONS_CHARS - len(PREAMBLE) - len(TAIL) - 64
        clipped = body[: max(budget, 0)]
        boundary = clipped.rfind("\n\n")
        if boundary > budget * 0.8:
            clipped = clipped[:boundary]
        LOGGER.warning(
            "MCP instructions truncated from {} to ~{} chars ({})",
            len(body),
            len(clipped),
            prompt_id,
        )
        text = f"{PREAMBLE}\n{clipped}\n\n[Truncated due to size limits]{TAIL}"

    _cache[key] = (clock() + _CACHE_TTL_SECONDS, text)
    LOGGER.info(
        "Built MCP instructions: {} ({} chars, {} knowledge module(s))",
        rendered.provenance(),
        len(text),
        len(getattr(rendered, "knowledge_used", []) or []),
    )
    return text


def clear_cache() -> None:
    """Drop the composed-instructions cache (tests, and any future admin hook
    that wants an edit to land before the TTL expires)."""
    _cache.clear()
