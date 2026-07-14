# Anansi Admin App

A NiceGUI web application for administering the Anansi chat bot. Provides chat history browsing, settings management, MCP server toggles, broadcast scheduling, and live bot status — all behind Google OAuth authentication.

## Features

- **Google OAuth Authentication** — Secure login with work Google accounts; whitelist-based access
- **Conversation Browser** — View all bot conversations (groups and direct messages) with tool call inspection
- **Settings Management** — Edit environment variables, MCP server toggles, and feature flags live without redeployment
- **Broadcast Scheduler** — Schedule and manage Telegram broadcast messages to org groups
- **Bot Status Dashboard** — Live view of which grids are online/offline via VRM integration
- **Statistics** — Daily message metrics and activity tracking
- **Grid Design & BOMs** — Metadata-driven CRUD over the grid design / bill-of-materials tables (`gd_*`), in a separate **Grid Design** sidebar section with its own whitelist-based view/edit access control

## Architecture

This is a **separate service** from the chat_orchestrator, designed to:
- Run independently without affecting the production bot
- Read the Chat DB (Supabase) and read/write app settings via the DigitalOcean API
- Deploy on minimal infrastructure (co-launches the broadcast scheduler as a background process; no separate worker service needed)

```
anansi_app/
├── nicegui_app/
│   ├── main.py                    # App entry point, routes, /healthz
│   ├── auth.py                    # Google OAuth (Authlib) + whitelist gating
│   ├── layout.py                  # Sidebar shell, nav, live bot-status dot
│   ├── services_access.py         # Cached accessors over services/
│   └── pages/
│       ├── chat.py                # Conversation browser
│       ├── documents.py           # RAG knowledgebase management
│       ├── agents.py              # Persistent agents management
│       ├── settings.py            # Env var editor + MCP toggles (registry-driven)
│       ├── broadcast.py           # Broadcast compose/templates/scheduled/history
│       ├── grid.py                # Grid Design list/detail/form (ui.aggrid)
│       └── grid_actions.py        # Grid detail-view compute-engine actions
├── rendering/
│   └── conversation_html.py       # Pure message-HTML builders (used by pages/chat.py)
├── grid_app/                      # Vendored grid metadata/entities/RBAC (framework-agnostic)
└── services/
    ├── supabase_reader.py         # Read-only Chat DB queries
    ├── settings_service.py        # Read/write env vars via DO API
    ├── broadcast_service.py       # Broadcast send/schedule/templates
    └── bot_status_service.py      # DigitalOcean deployment status
```

## Quick Start

### Local Development

1. **Install dependencies:**
```bash
cd anansi_app
pip install -r requirements.txt
```

2. **Configure environment:**
```bash
cp .env.example .env
# Edit .env with your credentials (see Google OAuth Setup section below)
```

3. **Run the app:**
```bash
python -m nicegui_app.main
```

4. **Open browser:**
```
http://localhost:8501
```

## Google OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials) and create an **OAuth 2.0 Client ID** (application type: Web application).
2. Add your redirect URI under "Authorized redirect URIs" — e.g. `http://localhost:8501/oauth2callback` for local dev, or your production URL.
3. Copy the **Client ID** and **Client Secret** into `.env`:
   ```bash
   AUTH_CLIENT_ID=your-client-id.apps.googleusercontent.com
   AUTH_CLIENT_SECRET=your-client-secret
   AUTH_REDIRECT_URI=http://localhost:8501/oauth2callback
   GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com   # alias
   GOOGLE_CLIENT_SECRET=your-client-secret                      # alias
   ```
4. Add your Google account email to `ALLOWED_VIEWER_EMAILS` and run `python -m nicegui_app.main`.

For local testing without Google OAuth, set `GRID_DESIGN_DEV_NO_AUTH=1` — this bypasses auth entirely and grants full access. **Never set this in production.**

## Deployment

### DigitalOcean App Platform (Recommended)

Deploy as part of the `anansi` app spec (see `.do/app.example.yaml`). The admin app runs as a separate service alongside the chat orchestrator. See [DEPLOY_NICEGUI.md](DEPLOY_NICEGUI.md) for the full deploy/rollback runbook.

1. **Add to your app spec** — copy the `anansi-app` service block from `.do/app.example.yaml` into your live spec.
2. **Set env vars** — at minimum `AUTH_CLIENT_ID`, `AUTH_CLIENT_SECRET`, `AUTH_COOKIE_SECRET`, `SUPABASE_URL`, `SUPABASE_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_VIEWER_EMAILS`, and `DIGITALOCEAN_API_TOKEN`.
3. **Update OAuth redirect URIs** in Google Cloud Console to include `https://your-app.example.com/oauth2callback`.
4. **Health check** — the service serves `/healthz`; the live DO spec's `health_check.http_path` must point there.

**Cost:** ~$5/month (basic-xxs instance)

## Access Control

The admin app implements two-tier access control:

### 1. Explicit Whitelist
Configured in `.env` / environment variables:
```bash
ALLOWED_VIEWER_EMAILS=alice@example.com,bob@example.com
```

### 2. Staff Organization (Future Enhancement)
Users whose `organization_id` matches `STAFF_ORG_ID` in the accounts table will be automatically granted access.

### 3. Grid Design Section

The app hosts a second navigation area, **Grid Design**, alongside the existing pages
(now grouped under **Bot Admin**). It has its own four-whitelist access model — a user
must appear in at least one list to reach the app at all:

| Env var | Grants |
|---|---|
| `ALLOWED_VIEWER_EMAILS` | Bot Admin **+** grid **view** (no grid edit) |
| `GRID_DESIGN_ALLOWED_USERS` | grid **view-only** |
| `GRID_DESIGN_EDITORS` | edit every grid table **except** Purchases (BoS) |
| `GRID_PROCUREMENT_EDITORS` | edit **only** Purchases (BoS) |

- **Grid view** = membership in the union of all four lists. The **Bot Admin** section
  stays restricted to `ALLOWED_VIEWER_EMAILS`.
- **Edit rights are strictly separated**: procurement editors touch only Purchases;
  design editors touch everything else; admins and view-only users edit nothing.
- Gates are enforced both in the rendered UI and on every route dispatch (crafted
  `?id=…&edit=1` URLs are blocked server-side).
- The grid code is vendored at `grid_app/` with its entity metadata at `db/entities.json`.
  `GRID_DESIGN_DEV_NO_AUTH=1` bypasses OAuth and grants all permissions for **local dev only**.

## Environment Variables

Required configuration (see `.env.example`):

```bash
# Google OAuth
AUTH_CLIENT_ID=                # From Google Cloud Console
AUTH_CLIENT_SECRET=            # From Google Cloud Console
AUTH_REDIRECT_URI=             # e.g. http://localhost:8501/oauth2callback
AUTH_COOKIE_SECRET=            # Random secret for session cookies

# Chat Database (Supabase)
SUPABASE_URL=                  # https://your-project.supabase.co
SUPABASE_KEY=                  # Service role key

# Access Control
ALLOWED_VIEWER_EMAILS=         # Comma-separated whitelist
STAFF_ORG_ID=2                 # organization_id that grants staff-mode access
```

## Usage

1. **Login** with your whitelisted Google account
2. **Select a conversation** from the sidebar (Groups or Direct Messages)
3. **Adjust date range** to filter messages
4. **View messages** in chronological order
5. **Expand tool calls** to see function details
6. **Search** using the sidebar search box

## Database Schema

The chat viewer queries the following tables (read-only):

### chat_messages
- `id`: Message UUID
- `session_id`: Conversation session
- `role`: user, model, tool
- `content`: Message text
- `function_call`: Tool invocation data
- `tool_result`: Tool execution result
- `from_chat_id`: Telegram chat ID
- `group_id`: Telegram group ID (if applicable)
- `created_at`: Timestamp

### chat_sessions
- `id`: Session UUID
- `session_id`: Session identifier
- `user_id`: User UUID
- `metadata`: Session metadata

### accounts (auth DB)
- `email`: User email
- `telegram_id`: Telegram user ID
- `name`: User name
- `organization_id`: Organization membership

## Security

- ✅ Google OAuth with domain restriction
- ✅ Whitelist-based access control
- ✅ Read-only database permissions for the chat viewer (grid design has scoped write access, see Access Control)
- ✅ HTTPS only in production
- ✅ Session-based authentication

## Troubleshooting

### "Database not configured"
- Check `CHAT_DB_URL` and `CHAT_DB_SERVICE_KEY` are set correctly
- Verify database credentials are valid

### "Access Denied"
- Ensure your email is in `ALLOWED_VIEWER_EMAILS`
- Check you're logging in with the correct Google account
- For External OAuth apps, ensure you're added as a test user

### "No conversations found"
- Check the date range slider (increase days to look back)
- Verify `from_chat_id` field is populated in chat_messages table
- Run migration: `20251204_add_chat_id_to_messages.sql`

### OAuth redirect mismatch
- Ensure redirect URIs match exactly in Google Cloud Console
- Check that `AUTH_REDIRECT_URI` in `.env` matches the registered URI

## Development

### Adding New Features

1. **New page:** Add to `nicegui_app/pages/`, register the route in `nicegui_app/main.py`
2. **New service:** Add to `services/`
3. **Test locally** before deploying

### Code Style

- Use type hints
- Document functions with docstrings
- Follow PEP 8 style guide
- Keep pages modular

## Support

- **Issues:** Report via GitHub Issues
- **Questions:** Open a GitHub issue or contact your maintainer
- **Documentation:** See root [README.md](../README.md)

## License

Apache 2.0 — see [LICENSE](../LICENSE)
