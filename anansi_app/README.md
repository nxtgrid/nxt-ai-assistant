# Anansi Admin App

A Streamlit web application for administering the Anansi chat bot. Provides chat history browsing, settings management, MCP server toggles, broadcast scheduling, and live bot status — all behind Google OAuth authentication.

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
- Deploy on minimal infrastructure (no background jobs needed)

```
anansi_app/
├── app.py                        # Main Streamlit app + page router
├── components/
│   ├── chat_history_page.py      # Conversation browser
│   ├── settings_page.py          # Env var editor + MCP toggles
│   ├── broadcast_page.py         # Broadcast scheduler UI
│   ├── grid_status_page.py       # Live grid status dashboard
│   └── ...
└── services/
    ├── supabase_reader.py         # Read-only Chat DB queries
    ├── settings_service.py        # Read/write env vars via DO API
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
streamlit run app.py
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
4. Add your Google account email to `ALLOWED_VIEWER_EMAILS` and run `streamlit run app.py`.

## Deployment

### DigitalOcean App Platform (Recommended)

Deploy as part of the `anansi` app spec (see `.do/app.example.yaml`). The admin app runs as a separate service alongside the chat orchestrator.

1. **Add to your app spec** — copy the `anansi-app` service block from `.do/app.example.yaml` into your live spec.
2. **Set env vars** — at minimum `AUTH_CLIENT_ID`, `AUTH_CLIENT_SECRET`, `AUTH_COOKIE_SECRET`, `SUPABASE_URL`, `SUPABASE_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_VIEWER_EMAILS`, and `DIGITALOCEAN_API_TOKEN`.
3. **Update OAuth redirect URIs** in Google Cloud Console to include `https://your-app.example.com/oauth2callback`.

**Cost:** ~$5/month (basic-xxs instance)

## Access Control

The viewer implements two-tier access control:

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
- Gates are enforced on both the rendered buttons and the router (crafted
  `?page=…&edit=1` URLs are blocked).
- The grid code is vendored at `grid_app/` with its entity metadata at `db/entities.json`.
  `GRID_DESIGN_DEV_NO_AUTH=1` bypasses OAuth and grants all permissions for **local dev only**.

## Directory Structure

```
chat_viewer/
├── app.py                      # Main Streamlit application
├── requirements.txt            # Python dependencies
├── .env.example                # Environment template
├── .gitignore                  # Git ignore rules
├── project.yml                 # DigitalOcean deployment config
├── README.md                   # This file
├── .streamlit/
│   └── config.toml             # Streamlit configuration
├── components/
│   ├── __init__.py
│   ├── auth.py                 # Google OAuth + whitelist
│   ├── sidebar.py              # Navigation component
│   └── conversation_view.py    # Message display
└── services/
    ├── __init__.py
    └── supabase_reader.py      # Read-only DB queries
```

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

The viewer queries the following tables (read-only):

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
- ✅ Read-only database permissions
- ✅ HTTPS only in production
- ✅ No write capabilities
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

1. **New component:** Add to `components/`
2. **New service:** Add to `services/`
3. **Update main app:** Import and use in `app.py`
4. **Test locally** before deploying

### Code Style

- Use type hints
- Document functions with docstrings
- Follow PEP 8 style guide
- Keep components modular

## Support

- **Issues:** Report via GitHub Issues
- **Questions:** Open a GitHub issue or contact your maintainer
- **Documentation:** See root [README.md](../README.md)

## License

Apache 2.0 — see [LICENSE](../LICENSE)
