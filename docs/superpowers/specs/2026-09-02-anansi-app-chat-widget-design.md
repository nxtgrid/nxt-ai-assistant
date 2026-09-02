# Anansi App Chat Widget — Design

**Date:** 2026-09-02
**Status:** Ready to plan
**Plan:** `docs/superpowers/plans/2026-09-02-anansi-app-chat-widget.md`

## Goal

A pop-over chat panel anchored bottom-right on every anansi_app NiceGUI page. It talks to the
same bot brain a Telegram personal chat reaches, carries the current page's data item and any
highlighted text into the model's context, persists to `chat_db` exactly like a Telegram chat,
and starts a fresh session on every tab refresh.

## What is already true

Verified in the tree at `80777b8a` (branch `fix/episodic-grid-distillation-and-preview`).

| Fact | Evidence |
|---|---|
| A NiceGUI page in this app already drives the bot over HTTP | `anansi_app/nicegui_app/pages/skill_builder.py:169` `_send_chat_message` → `POST {CHAT_ORCHESTRATOR_URL}/chat` |
| That call is authenticated by two headers | `skill_builder.py:149-154` — `X-Api-Key` (`API_KEY`) + `X-Identity-Assertion-Key` (`IDENTITY_ASSERTION_KEY`) |
| `POST /chat` delegates to `handle_webhook`, which for `X-Api-Key` returns the full body synchronously | `api/app.py:779-792`, `api/app.py:773-776` `result = await async_main(body)` |
| The synchronous path returns a rich envelope | `handler.py:2555-2564` — `message`, `session_id`, `attachments`, `choices`, `tool_calls`, `tokens` |
| `attachments` carries base64 tool images; `choices` carries inline-keyboard buttons | `models/envelope.py:88-125` `attachments_from_tool_results`, `:122-142` `choices_from_reply_markup` |
| `CHAT_ORCHESTRATOR_URL` is the service-internal URL in prod (no ingress hop) | `.do/app.example.yaml:497-499` `value: "${chat-orchestrator.PRIVATE_URL}"` |
| `API_KEY` and `IDENTITY_ASSERTION_KEY` are app-level, so anansi-app already has both | `.do/app.example.yaml:26-27,43-44` |
| The widget's HTTP call is server→server (NiceGUI backend → orchestrator), so CORS never applies | `skill_builder.py:182` runs in the Python process, not the browser |
| **anansi_app may not import `orchestrator`** | `anansi_app/tests/test_no_orchestrator_imports.py` — AST scan over every non-test `.py`; the image ships only `anansi_app/` + `shared/` (`anansi_app/Dockerfile:22-23`) |
| `nicegui` is stubbed at `sys.modules` level for anansi_app tests | `anansi_app/tests/conftest.py` — `SimpleNamespace(run=…, ui=…)`; there is **no** `app` attribute |
| NiceGUI version in the venv is 3.15.0; `ui.chat_message`, `ui.page_sticky`, `ui.markdown`, `ui.spinner` all exist | `.venv/…/nicegui/__init__.py`; verified by import |
| `ui.chat_message` opens its own slot (`with self:`) around each text part, so children can be nested | `.venv/…/nicegui/elements/chat_message.py:59-61` |
| `WebhookRequest` already accepts `entity_context` | `orchestrator/models/schemas.py:199` |
| `EntityContext` has typed id fields plus free-form `additional_context: Dict[str, Any]` | `orchestrator/models/schemas.py:66-76` |
| The graph already renders `entity_context` into an `[Entity Context]` block prepended to the user turn | `graphs/conversation_graph.py:1145-1150`, `:1179-1196` `_format_entity_context` |
| `source: "web"` is accepted in production | `handler.py:591` `allowed_sources = ["telegram", "roam", "web", "api"]` |
| A DM session id is `{source}_dm_{sha256(f"dm:{user_id}:{secret}")[:16]}` | `orchestrator/utils/session_id.py:66-71` |
| The skill builder already mints a fresh session per builder run by varying `user_id` | `pages/skills.py:465` `builder_user_id = f"{user_email}:{uuid.uuid4()}"` |
| `app.storage.client` dies on page reload **and on navigation**; `app.storage.tab` survives both and dies with the tab | `nicegui/storage.py` — `Storage.client` docstring, `Storage.tab` |
| `app.storage.tab` raises unless the client already has a socket connection | `nicegui/storage.py` `Storage.tab` — `if not client.has_socket_connection: raise RuntimeError(...)` |
| Every admin page renders inside one sync context manager | `nicegui_app/layout.py:192-242` `frame()` |
| `perms.can_view_bot_admin(email)` is the Google-OAuth allowlist gate used by every admin route | `grid_app/lib/perms.py:88-92`; `nicegui_app/main.py:79-84` etc. |
| `_get_user_permissions_direct` derives staff purely from the account's own org | `shared/auth/auth_service.py:342` `is_staff = organization_id == STAFF_ORG_ID` |
| That lookup misses for most admin emails — they live in a different identity system | `pages/skill_builder.py:172-180`; `tests/experts/test_resolve_auth_skill_builder.py` module docstring |
| Today's fallback for that miss is unconditional staff | `graphs/nodes/resolve_auth.py:91-118` `skill_builder_staff_auth` branch |
| `parse_command` (slash commands **and** natural-language triggers) is gated to Telegram only | `graphs/nodes/parse_command.py:35-40` |
| `get_chat_contexts` selects every `chat_messages` row regardless of source | `services/supabase_reader.py` `get_chat_contexts` — no source filter |
| The Chats page already filters one class of session out by policy | `pages/chat.py:146-156` — escalation group excluded because "this page is for customer conversations" |

## Answers to the five questions

### (a) Markdown — no converter is needed

**What is stored in `chat_messages.content` is standard/GitHub markdown, not Telegram markdown.**

The graph returns `final_state["final_response"]` untouched (`services/webhook_processor.py:139,166`), and
`save_history` persists exactly those message objects. The Telegram dialect conversion happens strictly
later, at the transport layer: `telegram_transport.py:151-167` `_convert_to_telegram_markdown`, called from
`_send_telegram_response` at send time. Nothing in `chat_orchestrator/orchestrator/graphs/` calls
`convert_github_to_telegram_markdown`.

So the widget renders the same string the DB holds — **no two-way converter, no round-trip.**

This is not a guess; it is already in production. The `/conversations` Chats viewer renders
`chat_messages.content` through `markdown.markdown(text, extensions=["fenced_code", "nl2br", "sane_lists"])`
(`rendering/conversation_html.py:143`). Same column, same renderer, working today. (That function is
confusingly named `escape_markdown` with a docstring claiming it "parses Telegram markdown" — the code
underneath is a plain GitHub-markdown parse. Left alone here; renaming it is unrelated churn.)

**Rendering choice:** `with ui.chat_message(...): ui.markdown(text)`. `ChatMessage.__init__` opens its own
slot, so nested children work, and `ui.markdown` gives real markdown rather than the HTML-escaping
`text=` path.

**Two cosmetic wrinkles, documented and deliberately not fixed:**

1. Both system prompts tell the model to prefer lists over tables because tables render badly *in Telegram*
   (`customer.system.prompt:262`, `staff.system.prompt:298`). Web could render tables fine; the instruction
   merely makes web output plainer than it needs to be.
2. `experts.definitions.prompt:154` tells the LPP expert its output "will be sent as markdown to Telegram".

Neither produces invalid markdown. Changing prompt text to be surface-aware is a separate piece of work.

### (b) Page + selection context

Mirrors the screenshot: a row of removable chips above the input, one per attached context item.

**Two sources:**

- **Page context** — what the current page is showing. A new `nicegui_app/page_context.py` defines a
  `PageContext` dataclass; each page calls `set_page_context(...)` as it renders. Stored in
  `app.storage.client`, whose lifetime (one page load) is exactly the lifetime of "what this page is
  showing".
- **Selection** — read at send time with `ui.run_javascript("window.getSelection().toString()")`.

**Summary inline, detail on demand — via tools that already exist.** The page block carries identifiers
(ticket refs, grid ids/names, org) plus a short human summary and one `detail_hint` line naming how to go
deeper. The bot already has ticket and grid MCP tools; it drills in by calling them. No new endpoint, no
"fetch page detail" round trip.

**Transport: the existing `entity_context` field — zero orchestrator changes on this path.**
`PageContext.to_entity_context()` projects onto the typed slots (`grid_id`, `site_id`, `customer_id`,
`meter_id`) where they fit, and onto `additional_context` otherwise. `_format_entity_context` already
renders all of it into an `[Entity Context]` block prepended to the user turn.

**Caps** (enforced client-side, before the request): page summary ≤ 2000 chars, selection ≤ 4000 chars,
≤ 12 summary lines. A list page contributes a count plus the first 10 identifiers, never full rows.

### (c) Auth — email first, staff only as a named, logged fallback

The ask: use the logged-in email to find the right org; staff should land on the staff org automatically;
keep it consistent now so a future non-staff viewer cannot leak data by accident.

That is achievable, but the plain email branch cannot be used as-is: `get_user_permissions` looks the email
up in `public.accounts`, which is the *bot's* onboarding DB — a different identity system from the Google
OAuth allowlist gating this app. A miss there returns empty `organization_ids` and `is_staff=False`, i.e. the
session silently runs as an unscoped customer. That silent degradation is the leak risk, and it is exactly
what today's `skill_builder_staff_auth` branch papers over by forcing staff unconditionally.

**New `admin_app_auth` branch in `resolve_auth`, gated on `_identity_trusted` the same way:**

1. Look the email up in `accounts` first. **If found, use those permissions verbatim** — real org, real
   `is_staff`. A genuine staff account already has `organization_id == STAFF_ORG_ID`, so staff get the staff
   org automatically, and a future customer-org viewer gets exactly their own org. This is the consistency
   the ask is about.
2. **Only if the email is absent from `accounts`**, fall back to `STAFF_ORG_ID`, and only when the app
   asserts the user passed `perms.can_view_bot_admin()`. Log a warning naming the email so the gap is
   visible and closable by adding the account.
3. Never fall through to empty-org/non-staff.

The widget header shows the resolved org, so a mis-scoped session is visible rather than silent.

Strictly tighter than the existing skill-builder branch. That branch is **not** changed here — migrating it
onto `admin_app_auth` is a clean follow-up, out of scope.

### (d) Persistence and session lifetime

Persistence needs no new code: `save_user_message` and `save_history` run inside the graph for every
source. Rows land in `chat_sessions` / `chat_messages` identically to a Telegram turn.

**Session identity.** `source: "web"` and `user_id = f"anansi-app:{email}:{nonce}"`, where `nonce` is a
`uuid4` minted per tab-session — the same trick `skills.py:465` uses for the builder. The orchestrator
derives `web_dm_<hash>` and returns it; the widget caches the returned value.

**Lifetime — refresh resets, navigation does not.** The literal rule ("each tab refresh starts a new
session") is implementable exactly, but only if navigation is handled separately: `ui.navigate.to()` is a
full page load, so `app.storage.client` alone would also reset the chat on every sidebar click, wiping the
conversation each time the user moves pages. Instead:

- session + transcript live in `app.storage.tab` (survives navigation, dies with the tab);
- on mount, `performance.getEntriesByType('navigation')[0].type === 'reload'` distinguishes a refresh from a
  navigation, and a refresh mints a new nonce.

Net: refresh → new session; navigate within the tab → same session; close the tab → gone. A "New chat"
button gives the same reset on demand. If the JS probe fails, the session is kept (fail toward continuity).

**Chats viewer.** Web sessions would otherwise flood `/conversations`, which is explicitly the
customer-conversation page. Sessions whose `session_id` starts with `web_` are hidden behind an
"Include app chats" switch, defaulting off — the same policy shape as the existing escalation-group filter.

### (e) No streaming

The `X-Api-Key` path returns the whole turn in the response body. Spinner plus disabled input while in
flight, 120 s timeout, matching `skill_builder._send_chat_message`.

## Also in scope, for Telegram parity

- **Tool images.** Render `attachments[].data_b64` inline; Telegram gets these as separate `sendPhoto`
  calls, the envelope hands them back in the body.
- **Choices.** `choices[]` render as buttons; clicking sends the label as an ordinary user message — the
  same thing a Telegram user typing the answer instead of tapping would produce.
- **Commands.** `parse_command`'s gate widens from `source != "telegram"` to
  `source not in ("telegram", "web")`, so slash commands and natural-language triggers work. Without this,
  a staff user's `/` command is sent to the model as literal prose.

## Out of scope

- Streaming; media upload from the widget.
- Resolving `pending_decisions` through the callback path (`_handle_callback_query` needs a Telegram
  `callback_query` update). Choice clicks degrade to plain text.
- Telegram `web_app` button choices (Mini App URLs) — rendered as external links, not actioned.
- Customer impersonation ("show me what customer X sees").
- Migrating `skill_builder_staff_auth` onto the new `admin_app_auth` branch.
- Making prompt formatting rules surface-aware.

## Risks

| Risk | Mitigation |
|---|---|
| `anansi_app` cannot import `orchestrator`, so `EntityContext`/`UserContext` are unavailable | Build plain dicts; `test_no_orchestrator_imports.py` enforces it automatically |
| `nicegui` is a stub in tests, so UI code is untestable | All logic lives in pure modules (`page_context.py`, `chat_client.py`); the widget module holds only wiring |
| `from nicegui import app` at module top would `AttributeError` under the conftest stub | `page_context.py` imports `app` lazily inside its two storage functions |
| A `[BUTTONS]` block would leak as literal text on the web path | `PROCEDURE_BUTTONS_ENABLED` is unset in the DO spec (defaults `false`); noted, not handled |
| Page context inflating token cost | Hard char/line caps, applied before the request |
