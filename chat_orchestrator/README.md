# Chat Orchestrator

Gemini-powered conversation orchestrator with role-aware system instructions, bot artifacts, and Google Docs knowledge base integration.

## Quick Start

```bash
# Install
cd chat_orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]

# Configure
cp .env.example .env
# Add: GOOGLE_API_KEY, CHAT_DB_URL, CHAT_DB_SERVICE_KEY

# Run
python local_server.py
```

Visit `http://localhost:8000/docs` for API documentation.

---

## Core Features

### 1. Gemini Function Calling
- Orchestrates multi-turn conversations with Gemini
- Executes MCP service tools via HTTP
- Parallel tool execution support
- Automatic retry and error handling

### 2. Role-Aware Instructions
Dynamic system instructions based on user context:
- **Customer Mode** (org_id ≠ 2): Support bot with Q&A knowledge base
- **Staff Mode** (org_id = 2): Full access with role-specific instructions

### 3. Bot Artifacts (Database)
Store knowledge in Supabase:
- **Q&A Pairs** - Question/answer knowledge base
- **Response Templates** - Consistent response patterns
- **Decision Rules** - Pattern matching logic (technical data)
- **Entity Training** - NER training data (technical data)

### 4. Google Docs Knowledge Base
**Recommended approach** for Q&A and templates:
- Direct editing by support staff
- No CSV export/import workflow
- Better for RAG semantic search

**Setup:**
1. Create Google Doc for customer/staff knowledge
2. Share with service account email
3. Set environment variables:
   ```bash
   CUSTOMER_SUPPORT_DOC_ID=doc-id
   STAFF_SUPPORT_DOC_ID=doc-id
   ```

---

## Architecture

```
chat_orchestrator/
├── orchestrator/          # Main package
│   ├── api/              # FastAPI endpoints
│   ├── clients/          # Gemini API client
│   ├── services/         # Business logic
│   │   ├── conversation.py           # Orchestrator
│   │   ├── artifacts_provider.py     # DB/GDocs artifacts
│   │   ├── instructions_provider.py  # Role-based instructions
│   │   ├── tool_executor.py          # HTTP tool calls
│   │   └── tool_registry.py          # Service discovery
│   ├── models/           # Pydantic schemas
│   └── config/           # Settings & service configs
├── handler.py            # Serverless entry point
├── local_server.py       # Local testing server
└── tests/               # Unit tests
```

### Request Flow

```
External Client (Telegram/Web)
    ↓
POST /v1/chat
    ↓
Instructions Provider
  → Google Docs (priority)
  → Supabase (fallback)
  → Default
    ↓
Conversation Orchestrator
  ↔ Gemini API
  ↔ Tool Executor → Tools Service (MCP)
    ↓
Response
```

---

## Configuration

### Environment Variables

```bash
# Required - Google AI Studio
GOOGLE_API_KEY=your-gemini-api-key  # Get from: https://aistudio.google.com/app/apikey

# Required - Chat Database (conversation history, bot artifacts, RAG documents)
CHAT_DB_URL=https://your-project.supabase.co
CHAT_DB_SERVICE_KEY=your-service-role-key  # Service role key for full access

# Required - Auth Supabase (user permissions, organizations, grids, meters)
# Uses direct PostgreSQL connection with readonly user on port 6543 (PgBouncer)
AUTH_DB_DIRECT_CONNECTION=true
AUTH_DB_HOST=db.your-auth-project.supabase.co
AUTH_DB_PORT=6543
AUTH_DB_NAME=postgres
AUTH_DB_USER=make_readonly
AUTH_DB_PASSWORD=your-readonly-password
AUTH_DB_SSL_MODE=require

# Google Docs (recommended for customer instructions)
CUSTOMER_SUPPORT_DOC_ID=doc-id
STAFF_SUPPORT_DOC_ID=doc-id
GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'

# Gemini Settings
GEMINI_MODEL=gemini-flash-latest
GEMINI_TEMPERATURE=0.2
MAX_TOOL_ROUNDS=3

# Features
ALLOW_PARALLEL_CALLS=true

# Response Verification (optional, customer mode only)
VERIFICATION_ENABLED=false
VERIFICATION_DOC_ID=your-verification-doc-id
VERIFICATION_MODEL=gemini-2.5-flash-lite
```

### Google Cloud Setup

**Required APIs** (enable in Google Cloud Console):
1. **Google Docs API** - For fetching system instructions from Google Docs
   - Visit: https://console.developers.google.com/apis/api/docs.googleapis.com
   - Click "Enable API"
   - Wait a few minutes for propagation
2. **Google Drive API** - For document access
   - Visit: https://console.developers.google.com/apis/api/drive.googleapis.com
   - Click "Enable API"

**Service Account Setup:**
1. Create service account in Google Cloud Console
2. Download JSON key file
3. Copy entire JSON content to `GOOGLE_SERVICE_ACCOUNT_JSON` env var
4. **IMPORTANT**: Share your Google Docs with the service account email
   - Find the email in your JSON key file (look for `client_email` field)
   - Open your Google Doc (customer/staff support docs)
   - Click "Share" button
   - Paste the service account email (e.g., `your-service@project.iam.gserviceaccount.com`)
   - Grant "Viewer" or "Editor" access
   - Click "Send"
   - **Without this step, you'll get "403 The caller does not have permission" errors**

### Database Architecture

The orchestrator uses **two separate Supabase instances**:

#### 1. Auth Database (Readonly Access)
- **Purpose**: User permissions, organizations, grids, meters
- **Access Method**: Direct PostgreSQL connection via asyncpg
- **Port**: 6543 (PgBouncer connection pooler)
- **User**: `make_readonly` (limited to SELECT only)
- **Why Direct**: Bypasses Supabase RLS for explicit permission checks
- **Tables**: `accounts`, `organizations`, `grids`, `meters`

#### 2. Main Database (Full Access)
- **Purpose**: Conversation history, bot artifacts, RAG documents
- **Access Method**: Supabase client library (REST API)
- **Authentication**: Service role key
- **Why Client**: Leverages Supabase features (RLS, realtime, storage)
- **Tables**: `chat_sessions`, `chat_messages`, `bot_artifacts`

**Important:** Statement caching is disabled (`statement_cache_size=0`) for the auth database to ensure compatibility with PgBouncer's transaction pooling mode.

### Telegram Bot Configuration

To enable emoji reaction feedback tracking, configure your Telegram webhook to receive reaction updates:

**1. Set Webhook with Reaction Updates:**
```bash
curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://your-orchestrator-url.com",
    "allowed_updates": ["message", "message_reaction"]
  }'
```

**2. Supported Emoji Reactions:**

The bot automatically maps Telegram emoji reactions to feedback types:

**Positive (thumbs_up):** 👍 ❤️ 🔥 🎉 🤩 😍 ❤️‍🔥 ⭐ 💯 👏 🙏 🤗 🫡 👌 🏆 💋 😘 🥰 😇 🤝 💘 🦄 😎 🤣 😁 🍾 ⚡

**Negative (thumbs_down):** 👎 😢 😭 😡 🤬 💩 🤮 💔 😱 😨 🖕 🤡

Unmapped emojis are logged but not saved (allows adding new mappings based on usage).

**3. Feedback Storage:**

Reactions are automatically saved to the `chat_messages.metadata.feedback` field:
```json
{
  "feedback": {
    "type": "thumbs_up",
    "emoji": "👍",
    "telegram_user_id": "123456",
    "telegram_message_id": 42,
    "timestamp": "2025-12-26T10:00:00+00:00"
  }
}
```

**Note:** The bot must be a member of the group/channel to receive reaction updates.

### Service Configuration

Edit `orchestrator/config/services.yaml` to add MCP services:

```yaml
- name: get_service_status
  description: Gets the operational status of a service
  url: https://api.example.com/status
  method: GET
  payload_mode: query
  arguments_schema:
    type: OBJECT
    properties:
      serviceName:
        type: STRING
        description: Name of the service
    required: [serviceName]
```

---

## API Reference

### POST /v1/chat

Main conversational endpoint.

**Request:**
```json
{
  "user_input": "How do I reset my password?",
  "user_context": {
    "user_email": "user@example.com",
    "roles": ["user"],
    "is_admin": false
  },
  "conversation": [],
  "metadata": {"channel": "web"}
}
```

**Response:**
```json
{
  "final_text": "To reset your password...",
  "tool_calls": [...],
  "tool_results": [...],
  "history": [...]
}
```

### GET /health

Health check endpoint.

---

## Customer Support Mode

Automatically activates when user's `organization_id != 2`.

**Features:**
- Loads customer support knowledge base (Google Docs or DB)
- Uses Q&A pairs and response templates
- Simplified, customer-facing responses
- Decision rules for categorization
- Escalation patterns

**Knowledge Priority:**
1. Google Docs (if `CUSTOMER_SUPPORT_DOC_ID` set)
2. Supabase bot_artifacts table
3. Default fallback instructions

---

## Response Verification (LLM-as-Judge)

Optional quality verification layer for customer-facing responses. Uses a separate LLM call to verify responses meet quality standards before sending.

### How It Works

1. **Customer mode only** - Staff mode bypasses verification
2. **Post-response check** - After Gemini generates response, verification LLM evaluates it
3. **Regenerate on failure** - If verification fails, response is regenerated with feedback
4. **Escalate on repeated failure** - If second attempt fails, escalates to support team

### Configuration

```bash
# Enable verification (disabled by default)
VERIFICATION_ENABLED=true

# Google Doc with verification criteria
VERIFICATION_DOC_ID=your-verification-doc-id

# Model for verification (lightweight, fast)
VERIFICATION_MODEL=gemini-2.5-flash-lite
```

### Verification Criteria

The verification Google Doc should define:
- **Tone & Professionalism** - Helpful, not condescending
- **Accuracy** - No fabricated data or false claims
- **Completeness** - Addresses the actual question
- **Safety** - No internal system exposure
- **Clarity** - Understandable to non-technical users

### Verification Flow

```
Customer Message
    ↓
Gemini Response
    ↓
Verification LLM → PASS → Send Response
    ↓ FAIL
Regenerate with Feedback
    ↓
Verification LLM → PASS → Send Response
    ↓ FAIL
Escalate to Support + Placeholder Response
```

### Admin UI Toggle

Enable/disable via anansi-app: **Settings → Response Quality → Enable Response Verification**

---

## Database Setup

### 1. Deploy Schema

Run `db/schema/chat_db.sql` from the repository root in the Supabase SQL Editor.
This creates all required tables including `bot_artifacts`, sessions, messages, RAG, and agent workflow tables.

### 2. Verify

```sql
SELECT bot_mode, artifact_type, COUNT(*) 
FROM bot_artifacts 
WHERE is_active = true 
GROUP BY bot_mode, artifact_type;
```

---

## Deployment

### Local Development

```bash
pip install -e .[dev]
python local_server.py
```

### Production (Serverless)

```bash
# DigitalOcean Functions / AWS Lambda
# Entry point: handler.main
gunicorn handler:app --workers 4
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -e .
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0"]
```

---

## Testing

```bash
# All tests
pytest

# Specific test
pytest tests/test_conversation.py

# With coverage
pytest --cov=orchestrator tests/
```

### Test Google Docs Integration

```bash
export CUSTOMER_SUPPORT_DOC_ID="your-doc-id"
python test_google_docs_integration.py
```

---

## Troubleshooting

**"GOOGLE_API_KEY not configured"**
→ Get API key from: https://aistudio.google.com/app/apikey
→ Add to .env file: `GOOGLE_API_KEY=your-key`

**"CHAT_DB_URL not configured"**
→ Set CHAT_DB_URL and CHAT_DB_SERVICE_KEY for chat database
→ Set AUTH_DB_* variables for auth database

**"Google Docs API has not been used in project"**
→ Enable Google Docs API in Cloud Console
→ Visit: https://console.developers.google.com/apis/api/docs.googleapis.com
→ Click "Enable API" and wait a few minutes

**"HttpError 403: The caller does not have permission"**
→ **Most common cause**: Google Doc not shared with service account
→ Find service account email in `GOOGLE_SERVICE_ACCOUNT_JSON` (look for `client_email`)
→ Open the Google Doc and click "Share"
→ Add the service account email with Viewer or Editor access
→ This error occurs even if APIs are enabled - sharing is required!

**"Failed to fetch Google Doc - customer mode cannot proceed"**
→ Verify Google Docs API is enabled (see above)
→ Check doc is shared with service account email (see 403 error above)
→ Verify GOOGLE_SERVICE_ACCOUNT_JSON is valid JSON
→ Ensure CUSTOMER_SUPPORT_DOC_ID is correct

**"prepared statement already exists"**
→ This is fixed automatically with `statement_cache_size=0`
→ Occurs when using PgBouncer with asyncpg (Supabase port 6543)

**Customer mode not working**
→ Check user's organization_id in accounts table (must be ≠ 2 for customer mode)
→ Verify chat is registered in organizations or grids table

**Tool calls failing**
→ Check Tools Service is running (localhost:8001) or disable via TOOLS_SERVICE_URL
→ Verify service URL is accessible
→ Check service config in services.yaml
→ Review tools service logs

---

## Key Differences from Standard Approach

### Why Google Docs?
- ✅ Support staff can edit directly
- ✅ No CSV export/import workflow
- ✅ Better for RAG semantic search
- ✅ Version history built-in
- ❌ Decision rules and entity training stay in DB (technical data)

### Database vs Google Docs
- **Google Docs**: Q&A, templates, general instructions
- **Database**: Decision rules, entity training, metadata

---

## Related Documentation

- **Main Anansi README**: `../README.md`
- **MCP Servers**: `../mcp_servers/README.md`
- **RAG Pipeline**: `../rag_pipeline/README.md`
- **Deployment Guide**: `../README.md#deployment`
