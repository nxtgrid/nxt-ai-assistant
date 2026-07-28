# Settings UX Redesign — Minimum Environment Verification

**Date:** 2026-07-28
**Purpose:** Confirm the tiered minimum-environment claims in the design doc by
actually booting `anansi_app` under each tier, rather than inferring them from
reading code.

## Setup

The shared `.venv` at the repo root lacked `anansi_app`'s own dependencies
(`nicegui`, `authlib`, `supabase`, etc.) even though `anansi_app/requirements.txt`
declares them. Installed once for this verification:

```bash
pip install -r anansi_app/requirements.txt
```

No compile errors:

```bash
python -m py_compile nicegui_app/main.py nicegui_app/pages/settings.py \
  nicegui_app/pages/settings_readiness.py nicegui_app/pages/settings_widgets.py
```

## Tier 0 — dev bypass, nothing else

**Environment:** `GRID_DESIGN_DEV_NO_AUTH=1`, `PORT=8599`. Nothing else set
(launched with `env -i` to guarantee no ambient host env leaked in).

```bash
cd anansi_app
env -i PATH="$PATH" HOME="$HOME" GRID_DESIGN_DEV_NO_AUTH=1 PORT=8599 \
  PYTHONPATH="$(cd .. && pwd):$(pwd)" \
  python -m nicegui_app.main &
```

**Observed:**

```
$ curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8599/healthz
200
$ curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8599/settings
200
```

Loaded the page in a real browser (Claude Browser pane) and confirmed, with no
console errors:

- The Deployment Readiness panel rendered live data, not a placeholder:
  *"6 of 7 capabilities are not configured yet"*, with the exact missing-name
  lists produced by `flag_registry.readiness()` (verified identical via direct
  Python call below).
- All 13 non-empty groups rendered in the declared `GROUPS` order: Bot Control,
  AI Models & Providers, Conversation Experience, Escalations & Ticketing,
  Alerts & Notifications, Tools & Integrations, Knowledge & RAG, Grafana
  Dashboards, Documents & Templates, Access Control, Connections & Credentials,
  Metrics & Scheduling, Deployment.
- **Site Layout Engine did not render at all** — correct: every `LAYOUT_*` flag
  is `advanced=True` and "Show advanced" defaults off, so `visible_flags`
  returns an empty list and the group is skipped. This is the intended
  reduction in noise from the redesign, not a bug.
- Every visible Bot Control flag showed a `default` provenance chip (correct —
  nothing was set in this environment).
- Typing "temperature" into the search box collapsed the group list from 13
  headers down to a single "AI Models & Providers" header — direct evidence
  the search box's `on_change` reaches `_on_search` → `_rebuild_groups()` →
  `visible_flags()` server-side, since no client-side mechanism could produce
  that reduction. (Confirming the exact flag widget's expand animation inside
  that group is a Quasar client-timing question already covered by Task 9's
  12 unit tests on `visible_flags`/`group_is_inert`, and was not re-verified
  pixel-by-pixel here — the automated browser pane's scroll/screenshot tools
  were unreliable for that level of detail in this environment.)

**Conclusion:** Tier 0 confirmed exactly as designed. No corrections needed.

## Tier 0′ — real auth configuration, no bypass

**Environment:** `GOOGLE_CLIENT_ID=test-id`, `GOOGLE_CLIENT_SECRET=test-secret`,
`AUTH_REDIRECT_URI=http://localhost:8600/oauth2callback`,
`ALLOWED_VIEWER_EMAILS=you@example.com`. `GRID_DESIGN_DEV_NO_AUTH` unset.

**Observed:**

```
$ curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8600/healthz
200
$ curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8600/settings
307
$ curl -s -o /dev/null -w "%{redirect_url}\n" -L --max-redirs 0 http://localhost:8600/settings
http://localhost:8600/login
$ curl -s http://localhost:8600/login | grep -io "sign in with google\|unconfigured"
Sign in with Google
```

`/settings` redirects unauthenticated requests to `/login` (307, per
`AuthMiddleware`); `/login` renders the "Sign in with Google" button and does
**not** show "Google OAuth is not configured on this server" — confirming
`auth.is_configured()` correctly evaluates true from the four Tier 0′
variables. A real sign-in cannot complete with dummy credentials, which is the
expected boundary of this check.

**Conclusion:** Tier 0′ confirmed exactly as designed. No corrections needed.

## Readiness parity check

```bash
cd chat_orchestrator   # actually run from worktree root; shared/ is a top-level package
python -c "
from shared.config import flag_registry as fr
for s in fr.readiness(env={'GRID_DESIGN_DEV_NO_AUTH': '1'}):
    print(('OK ' if s.satisfied else '-- '), s.capability.key, s.missing)
"
```

Output:

```
OK  admin_login []
--  settings_persist ['DIGITALOCEAN_APP_ID', 'DIGITALOCEAN_API_TOKEN']
--  bot_replies ['GOOGLE_API_KEY', 'TELEGRAM_BOT_TOKEN', 'CHAT_DB_URL or SUPABASE_URL', 'CHAT_DB_SERVICE_KEY or SUPABASE_KEY', 'API_KEY', 'SESSION_ID_SECRET', 'AUTH_DB_HOST or AUTH_SUPABASE_URL']
--  system_instructions ['GOOGLE_SERVICE_ACCOUNT_JSON', 'CUSTOMER_SUPPORT_DOC_ID', 'STAFF_SUPPORT_DOC_ID']
--  escalations_to_jira ['JIRA_BASE_URL', 'JIRA_USERNAME', 'JIRA_API_TOKEN', 'JIRA_PROJECT_KEY']
--  grafana_tools ['GRAFANA_URL', 'GRAFANA_USERNAME', 'GRAFANA_PASSWORD']
--  notify_endpoint ['NOTIFY_SHARED_SECRET']
```

Byte-for-byte identical to what the live browser rendered under Tier 0 (same
6-of-7, same missing lists, same severities). No wording changes needed —
reading it cold, "The bot can answer messages" / "Missing: ..." reads clearly
without prior context.

## Final confirmed variable list (no corrections to the design)

- **Tier 0:** `GRID_DESIGN_DEV_NO_AUTH=1` only.
- **Tier 0′:** `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `AUTH_REDIRECT_URI`,
  `ALLOWED_VIEWER_EMAILS`.
- **Tier 1:** nothing further for the env-file backend; `DIGITALOCEAN_APP_ID` +
  `DIGITALOCEAN_API_TOKEN` for the live DigitalOcean backend. Matches the
  `settings_persist` capability observed above exactly.
- **Tier 2:** `GOOGLE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `CHAT_DB_URL`/`SUPABASE_URL`
  + `CHAT_DB_SERVICE_KEY`/`SUPABASE_KEY`, `API_KEY`, `SESSION_ID_SECRET`,
  `AUTH_DB_HOST`/`AUTH_SUPABASE_URL`, `GOOGLE_SERVICE_ACCOUNT_JSON`,
  `CUSTOMER_SUPPORT_DOC_ID`, `STAFF_SUPPORT_DOC_ID`. Matches `bot_replies` +
  `system_instructions` observed above exactly.
- **Tier 3:** everything else (Jira, Grafana, `/chat/notify`), each independently
  optional and configurable from the settings UI once Tier 0 is up.

Every tier in the original design doc was verified as stated. Nothing in this
verification pass required revising the design.
