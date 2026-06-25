# Anansi — Open Source AI Operations Assistant for Mini-Grids

In Akan folklore, Anansi the spider tricked the sky-god into giving him every story in the world, then wove them into a single web so people could share them. This Anansi does the same thing for mini-grid operators, sans trickery — weaving the scattered threads of daily work (meters, maps, tickets, field conversations, tribal knowledge in Telegram groups) into one chat thread your team and customers can actually talk to.

Built and run in production by [NXT Grid](https://nxtgrid.co), open-sourced for the wider energy-access community.

## What it does for mini-grid operators

### Customer support automation for prepaid meters

Customers message the bot to check balance, buy tokens, report no-power, or get a token resent. Staff use the same bot to commission new meters, unassign them, change power limits, and resolve disputes. Anansi talks to your meter backend (Metering Platform by default, swappable via MCP) and applies the right approval rules depending on whether the requester is a customer or a staff member.

<table>
  <tr>
    <td align="center"><a href="docs/illustrations/meterIssue.jpeg"><img src="docs/illustrations/meterIssue.jpeg" width="320" alt="Customer reports a meter issue"></a><br/><sub>Customer reports a meter issue</sub></td>
    <td align="center"><a href="docs/illustrations/resolved.jpeg"><img src="docs/illustrations/resolved.jpeg" width="320" alt="Issue resolved end-to-end"></a><br/><sub>Issue resolved end-to-end</sub></td>
  </tr>
</table>

### Geospatial design & site planning

The `/lpp` expert generates a Light Preliminary Package for a candidate site. Give it a GPS point and a site name; it pulls the community boundary from the GRID3 Nigeria settlement-extents dataset, runs the layout engine to place poles and lines, renders a map, and drafts a Google Doc package. The output is structured so the design can feed directly into a downstream Bill-of-Materials tool without re-keying.

<table>
  <tr>
    <td align="center"><a href="docs/illustrations/siteSelection.jpeg"><img src="docs/illustrations/siteSelection.jpeg" width="280" alt="Site selection with community boundary"></a><br/><sub>Site selection &amp; community boundary</sub></td>
    <td align="center"><a href="docs/illustrations/distrib.jpeg"><img src="docs/illustrations/distrib.jpeg" width="280" alt="Distribution layout"></a><br/><sub>Generated distribution layout</sub></td>
    <td align="center"><a href="docs/illustrations/powerHeatMap.png"><img src="docs/illustrations/powerHeatMap.png" width="280" alt="Power demand heat map"></a><br/><sub>Power demand heat map</sub></td>
  </tr>
</table>

#### Power plant site layout

For the generation side, Anansi renders the power-plant footprint itself: PV array blocks with plinths, earth pits, lightning-arrester coverage circles, DC and AC cable runs with lengths, the Victron cabin, feeder pillar, VSAT, and the fenced site boundary with a gate. Module count, achieved vs target kWp, and cable lengths (including contingency) are summarised in the title block, so the same image doubles as a quick BoM sanity-check.

<table>
  <tr>
    <td align="center"><a href="docs/illustrations/powerPlantLayout.png"><img src="docs/illustrations/powerPlantLayout.png" width="560" alt="Power plant site layout"></a><br/><sub>PV arrays, earth pits, lightning coverage, cable trenches &amp; cabin</sub></td>
  </tr>
</table>

### Grid analytics & KPIs on demand

`/analyze`, `/kpi`, and `/report` let staff ask "how did Site X perform last week?" in plain English. Anansi pulls from TimescaleDB, the Victron VRM API (solar inverter telemetry), and your operational DB, then returns charts plus a written summary. Reports can be scheduled — e.g. every Monday at 9am to a specific Telegram group.

<table>
  <tr>
    <td align="center"><a href="docs/illustrations/grid.jpeg"><img src="docs/illustrations/grid.jpeg" width="280" alt="Single-grid status report"></a><br/><sub>Single-grid status report</sub></td>
    <td align="center"><a href="docs/illustrations/grids.jpeg"><img src="docs/illustrations/grids.jpeg" width="280" alt="Multi-grid KPI overview"></a><br/><sub>Multi-grid KPI overview</sub></td>
    <td align="center"><a href="docs/illustrations/gridIssue.jpeg"><img src="docs/illustrations/gridIssue.jpeg" width="280" alt="Grid issue diagnosis"></a><br/><sub>Grid issue diagnosis</sub></td>
  </tr>
</table>

### Ticketing, escalation, and institutional memory

Conversations that need a human are routed to the right internal Telegram group automatically, or opened as a JIRA issue with the full transcript attached. The same pipeline ingests your historical Telegram support chats, Google Drive docs, and GitHub repos into a GraphRAG index — so the bot answers from your actual past decisions, not generic LLM knowledge.

<table>
  <tr>
    <td align="center"><a href="docs/illustrations/tracking.jpeg"><img src="docs/illustrations/tracking.jpeg" width="320" alt="Escalation tracking"></a><br/><sub>Escalation tracking</sub></td>
    <td align="center"><a href="docs/illustrations/meta.jpg"><img src="docs/illustrations/meta.jpg" width="320" alt="Bot performance / meta analytics"></a><br/><sub>Bot performance &amp; escalation analytics</sub></td>
  </tr>
</table>

## Why "general-purpose underneath" matters

Anansi is a general Gemini chat orchestrator at its core — Google Docs system instructions (your ops team edits the prompt in a browser, no redeploy), MCP tools, RAG, expert workflows. The mini-grid focus comes from the *tools and embellishments* layered on top, and from the "messenger-first" assumption that field staff and customers live in chat apps, not dashboards. Telegram is the primary surface today; WhatsApp is on the roadmap but not yet supported.

**Project structure:**
- `chat_orchestrator/` - Main Gemini orchestration service with Google Docs instructions
- `mcp_servers/` - MCP tool servers (Supabase, Timescale, JIRA, logs, codebase)
- `rag_pipeline/` - Knowledge ingestion from GitHub, Google Drive, Telegram
- `shared/` - Common utilities (auth, database, logging, Google Docs fetching)
- `anansi_app/` - Streamlit admin UI for chat history, broadcasts, and settings

## Quick Start

### Prerequisites

- Python 3.11+
- Google AI Studio or Gemini API key ([Get one](https://aistudio.google.com/apikey))
- Google Cloud service account with Docs API enabled
- Supabase account

### 1. Setup Google Service Account

```bash
# Create service account and enable APIs
# See: https://console.cloud.google.com/iam-admin/serviceaccounts

# Enable these APIs:
# - Google Docs API
# - Google Drive API

# Download credentials JSON
```

### 2. Create System Instructions in Google Docs

Create two Google Docs (one for customer mode, one for staff mode):

**Structure:**
```
[Optional title page]

[PAGE BREAK]

Heading 1: System Instructions
You are a helpful assistant for [Company Name].
Be professional, empathetic, and accurate.

Heading 1: QnA Knowledge Base
Q: What are your business hours?
A: Monday-Friday, 9 AM - 5 PM EST.

Heading 1: Example Conversations
User: I can't log in
Assistant: I understand that's frustrating. Let me help...
```

**Share docs with service account:**
- Get service account email from JSON: `"client_email": "..."`
- Share both docs with Viewer access

### 3. Install and Configure

```bash
# Clone and setup shared utilities
git clone <repository-url>
cd anansi
./setup_shared.sh

# Chat Orchestrator
cd chat_orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]

# Install pre-commit hooks (code quality checks on every commit)
pre-commit install

# Configure environment
cp .env.example .env
```

**Edit `.env`:**
```bash
# Required
GOOGLE_API_KEY=your-gemini-api-key
GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
CUSTOMER_SUPPORT_DOC_ID=1abc123xyz456  # From GDoc URL
STAFF_SUPPORT_DOC_ID=1def789uvw012     # From GDoc URL

# Chat Database (for conversations and RAG)
CHAT_DB_URL=https://your-project.supabase.co
CHAT_DB_SERVICE_KEY=your-service-role-key

# Auth Database (read-only for user lookup)
AUTH_DB_DIRECT_CONNECTION=true
AUTH_DB_HOST=db.your-auth-project.supabase.co
AUTH_DB_PORT=6543
AUTH_DB_NAME=postgres
AUTH_DB_USER=readonly_user
AUTH_DB_PASSWORD=your_password
AUTH_DB_SSL_MODE=require

# Gemini Config (optional)
GEMINI_MODEL=gemini-1.5-flash
GEMINI_TEMPERATURE=0.7
```

### 4. Run

```bash
# Development (all services)
./dev.sh

# Or orchestrator only
cd chat_orchestrator && source .venv/bin/activate
uvicorn orchestrator.api.app:app --host 0.0.0.0 --port 8000 --reload

# Test endpoint
curl http://localhost:8000/health
```

## Customizing for Your Deployment

After the basic setup works, these are the things every operator should configure before going live:

### Staff organization ID

`STAFF_ORG_ID` controls which `organization_id` in your `accounts` table gets staff-mode access (full tools, staff instructions). The default is `2` — change it to match your own database:

```bash
STAFF_ORG_ID=5   # whatever your staff org's ID is in the accounts table
```

Staff users see the `STAFF_SUPPORT_DOC_ID` instructions and have access to all MCP tools. Everyone else gets `CUSTOMER_SUPPORT_DOC_ID` and a limited tool set.

### Bot username

Set your Telegram bot's @handle so group-chat mention detection works correctly:

```bash
TELEGRAM_BOT_USERNAME=YourBotName   # without the @ prefix
```

Without this, the bot won't respond when mentioned by name in group chats.

### System instructions

The files under `chat_orchestrator/instructions/` are fallback examples used when the Google Doc env vars aren't set. For a real deployment, create your own Google Docs and point to them:

```bash
CUSTOMER_SUPPORT_DOC_ID=<your-customer-doc-id>
STAFF_SUPPORT_DOC_ID=<your-staff-doc-id>
EXPERT_INSTRUCTIONS_DOC_ID=<your-expert-doc-id>
```

The fallback files are intentionally generic and will not reflect your organization's actual support process.

### Operator-specific database columns

The `customer_server` and `shared/auth` code reference a column named `is_generation_managed_by_nxt_grid` in the `grids` table. This is an operator-specific column from the reference deployment. If your schema uses a different name (or doesn't have this concept), update the column name in:

- `shared/auth/auth_service.py`
- `mcp_servers/servers/customer_server/customer_mcp_server.py`

Search for `is_generation_managed_by_nxt_grid` to find all references.

### MCP servers

Most MCP servers are disabled by default. Enable only what you have credentials for via the `{SERVER_NAME}_ENABLED` env vars. See `mcp_servers/.env.example` for the full list with documentation.

---

## How It Works

### System Instructions Flow

```
Google Doc (customer or staff)
    ↓
1. Fetch via Docs API
    ↓
2. Convert to Markdown
   - Heading 1-6 → # to ######
   - Bold → **text**
   - Italic → *text*
    ↓
3. Auto-strip unwanted content
   - Title page (before first page break)
   - Headers and footers
   - Inline images
   - Content before "System Instructions"
    ↓
4. Parse into sections by Heading 1
    ↓
5. Split sections:
   - "System Instructions" → systemInstruction field
   - Other sections (QnA, Examples) → First user message
    ↓
6. Send to Gemini API

{
  "systemInstruction": {
    "parts": [{"text": "System Instructions section"}]
  },
  "contents": [
    {
      "role": "user",
      "parts": [{"text": "QnA + Examples sections"}]
    },
    {
      "role": "user", 
      "parts": [{"text": "Actual user question"}]
    }
  ]
}
```

### Customer vs Staff Mode

**Determined by user's `organization_id`:**
- `organization_id = STAFF_ORG_ID` (env var, default `2`) → **Staff mode** (internal users)
  - Fetches from `STAFF_SUPPORT_DOC_ID`
  - Access to all MCP tools
  - Full system capabilities

- All other `organization_id` values → **Customer mode** (external users)
  - Fetches from `CUSTOMER_SUPPORT_DOC_ID`
  - Limited to customer support tools
  - Safe, scoped responses

**Both modes use identical processing pipeline.**

## Architecture

### Request Flow

```mermaid
sequenceDiagram
    participant U as User (Telegram / API)
    participant O as chat_orchestrator<br/>(FastAPI + LangGraph)
    participant G as Gemini API
    participant M as mcp_servers<br/>(tool handlers)
    participant DB as Databases<br/>(Supabase / TimescaleDB)
    participant GD as Google Docs<br/>(system instructions)

    U->>O: POST /chat {message}
    O->>GD: Fetch system instructions doc
    GD-->>O: Markdown sections
    O->>DB: Load conversation history
    O->>G: Chat request (systemInstruction + history + message)
    G-->>O: Response or tool_call
    alt tool call requested
        O->>M: Execute tool (e.g. get_grid_status)
        M->>DB: Query data
        DB-->>M: Results
        M-->>O: Tool result
        O->>G: Continue with tool result
        G-->>O: Final response
    end
    O->>DB: Persist message
    O-->>U: Response
```

### Service Map

```
Telegram / Web Client
        │
        ▼
┌───────────────────────┐
│   chat_orchestrator   │  FastAPI — main Gemini orchestration
│   (port 8000)         │  LangGraph stategraph, expert workflows,
│                       │  MCP tool execution
└───────┬───────────────┘
        │ Python imports (monorepo)
        ▼
┌───────────────────────┐
│     mcp_servers       │  MCP tool servers — JIRA, Grafana,
│                       │  customer data, equipment control,
│                       │  meters, schedule, payments, knowledge
└───────────────────────┘

┌───────────────────────┐
│    rag_pipeline       │  Document ingestion — GitHub, Google
│                       │  Drive, Telegram; GraphRAG embeddings
└───────────────────────┘

┌───────────────────────┐
│     anansi_app        │  Streamlit admin UI — settings, logs,
│   (port 8501)         │  MCP server toggles, scheduler
└───────────────────────┘

┌───────────────────────┐
│      mini_app         │  Vite/Vanilla JS customer chat widget
│   (served via bot)    │  (embedded in operator portals)
└───────────────────────┘
```

### Core Components

#### 1. Chat Orchestrator
**Purpose:** Orchestrate Gemini conversations with dynamic instructions

**Key Features:**
- Google Docs integration (single source of truth)
- Section parsing and markdown conversion
- Proper Gemini API usage (`systemInstruction` field)
- Context injection as first user message
- Multi-turn conversation loops
- Parallel tool execution

**Location:** `chat_orchestrator/`

#### 2. RAG Pipeline
**Purpose:** Ingest and index knowledge from multiple sources

**Two Ingestion Methods:**

1. **Batch Indexers** (CLI) - Bulk ingestion for initial setup
   - GitHub repositories (code + docs)
   - Google Drive folders (docs, PDFs, spreadsheets)
   - Telegram chats (messages, topics)

2. **Interactive Ingestion** (`/ingest` command) - Individual documents via Telegram
   - Google Docs → Markdown (preserves formatting)
   - PDFs → `pymupdf4llm` (markdown output, tables)
   - LLM-based document classification
   - Procedure matching for support examples
   - User approval before storage

**GraphRAG Features:**
- Semantic chunking (~512 tokens)
- Vector embeddings (768-dim, Google AI Studio)
- Entity extraction
- Relationship mapping
- Procedure tagging for filtered retrieval

**Location:** `rag_pipeline/` (batch), `chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/` (interactive)

#### 3. MCP Servers
**Purpose:** Tool integration via Model Context Protocol

**Available Tools:**
- **Supabase** - Database CRUD
- **Timescale** - Time-series data
- **JIRA** - Issue management
- **Logs** - System log access
- **Codebase** - Code search via RAG
- **Customer** - Grid status and customer info
- **Schedule** - Command scheduling (staff only)
- **Meta** - Bot performance analytics (staff only)

**Location:** `mcp_servers/`

#### 4. Expert Subagents
**Purpose:** Handle complex, multi-step workflows with structured state management

**How It Works:**
- Triggered by slash commands (`/lpp`, `/analyze`, `/kpi`)
- Maintain workflow state in work packets (database-persisted)
- Can pause for user input and resume later
- Failed workflows can be retried or abandoned

**Available Experts:**
| Command | Expert | Description |
|---------|--------|-------------|
| `/lpp` | Package Generator | Generate Light Preliminary Packages |
| `/analyze` | Grid Analyst | Analyze grid performance and faults |
| `/kpi`, `/report` | Grid Analyst | Generate KPI reports |

**Expert Definition (Google Doc):**
```markdown
# Expert: package_generator

## Model
gemini-3-flash

## System Instructions
You are a specialist in creating Light Preliminary Packages...

## Tools
- google_docs

## Packet Types
- light_preliminary_package

## Packet: light_preliminary_package

### Workflow
[llm] parse_request - Extract site name
[function:generate_lpp_map] - Generate map
[function:copy_lpp_template] - Create document
[llm] summarize_result - Report to user
```

**Per-Expert Model Override:** Add `## Model` section to use a different Gemini model for that expert (e.g., `gemini-3-flash`).

**Location:** `chat_orchestrator/orchestrator/experts/`

### Data Flow

```
User Request
    ↓
Instructions Provider
    ├─ Fetch Google Doc (customer or staff mode)
    ├─ Parse sections
    └─ Split: System Instructions vs Context
    ↓
Conversation Orchestrator
    ├─ systemInstruction field
    ├─ Context as first user message
    └─ User request as last message
    ↓
Gemini API
    ├─ Calls tools if needed (MCP servers)
    └─ Retrieves RAG context if needed
    ↓
Response
```

### Key Files

| File | Purpose |
|------|---------|
| `orchestrator/services/conversation.py` | Main Gemini orchestration |
| `orchestrator/graphs/conversation_graph.py` | LangGraph stategraph |
| `orchestrator/services/instructions_provider.py` | Google Doc fetching |
| `orchestrator/services/tool_executor.py` | MCP tool execution |
| `orchestrator/services/command_registry.py` | Slash command definitions |
| `mcp_servers/tool_definitions.json` | All tool definitions (source of truth) |
| `mcp_servers/server_registry.py` | MCP server registry |
| `shared/auth/auth_service.py` | Authentication |

## Configuration

### Environment Variables

#### Required (All Components)
```bash
GOOGLE_API_KEY=your-gemini-api-key
CHAT_DB_URL=https://your-project.supabase.co
CHAT_DB_SERVICE_KEY=your-service-role-key
```

#### System Instructions (Chat Orchestrator)
```bash
GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
CUSTOMER_SUPPORT_DOC_ID=1abc123xyz456
STAFF_SUPPORT_DOC_ID=1def789uvw012
EXPERT_INSTRUCTIONS_DOC_ID=1ghi456jkl789  # Expert definitions
```

#### Authentication (Chat Orchestrator)
```bash
# Option A: Direct PostgreSQL (Recommended)
AUTH_DB_DIRECT_CONNECTION=true
AUTH_DB_HOST=db.your-auth-project.supabase.co
AUTH_DB_PORT=6543
AUTH_DB_NAME=postgres
AUTH_DB_USER=readonly_user
AUTH_DB_PASSWORD=your_password
AUTH_DB_SSL_MODE=require

# Option B: Supabase Client
# AUTH_SUPABASE_URL=https://your-auth-project.supabase.co
# AUTH_SUPABASE_KEY=your_auth_service_key
```

#### RAG Pipeline (Optional)
```bash
# GitHub
GITHUB_TOKEN=ghp_your-token
GITHUB_REPO=owner/repo

# Telegram
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abc123...
```

#### MCP Tools (Optional)
```bash
# Timescale
TIMESCALE_CONNECTION_STRING=postgresql://user:pass@host:5432/db  # pragma: allowlist secret

# JIRA
JIRA_DOMAIN=your-domain.atlassian.net
JIRA_EMAIL=your-email@example.com
JIRA_API_TOKEN=your-api-token

# Action Flags
SUPABASE_ACTIONS_ENABLED=true
TIMESCALE_ACTIONS_ENABLED=true
JIRA_ACTIONS_ENABLED=true
```

### Getting Google Doc IDs

From Google Doc URL:
```
https://docs.google.com/document/d/1abc123xyz456/edit
                                  ^^^^^^^^^^^^^^
                                  This is the doc ID
```

## Database Setup

### Auth Database

The Auth DB is your existing business database (users, organizations, sites). The bot connects read-only via `asyncpg`.

**Minimum required tables** (in `public` schema):

| Table | Columns used |
|-------|-------------|
| `accounts` | `id`, `email`, `telegram_id`, `organization_id`, `deleted_at` |
| `organizations` | `id`, `name`, `developer_group_telegram_chat_id` |
| `grids` | `id`, `name`, `organization_id`, `internal_telegram_group_chat_id`, `internal_telegram_group_thread_id`, `deleted_at` |
| `dcus` | `id`, `grid_id`, `deleted_at` |

**Create a read-only user:**

```sql
CREATE USER anansi_readonly WITH PASSWORD 'your-strong-password';
GRANT CONNECT ON DATABASE postgres TO anansi_readonly;
GRANT USAGE ON SCHEMA public TO anansi_readonly;
GRANT SELECT ON public.accounts TO anansi_readonly;
GRANT SELECT ON public.organizations TO anansi_readonly;
GRANT SELECT ON public.grids TO anansi_readonly;
GRANT SELECT ON public.dcus TO anansi_readonly;
-- Add more grants as you enable optional MCP tool servers
```

If using Supabase for the Auth DB, use port `6543` (PgBouncer) and set `statement_cache_size=0`. **Do not** use `make_readonly` or the PostgREST client for this database.

#### Auth DB — key columns used by Anansi

| Table | Columns used | Purpose |
|-------|-------------|---------|
| `accounts` | `id`, `email`, `telegram_id`, `organization_id`, `deleted_at` | Map Telegram user → org |
| `organizations` | `id`, `name`, `developer_group_telegram_chat_id` | Org lookup and staff chat ID |
| `grids` | `id`, `name`, `organization_id`, `internal_telegram_group_chat_id`, `internal_telegram_group_thread_id`, `deleted_at` | Map grid → Telegram group |
| `dcus` | `id`, `grid_id`, `deleted_at` | Device lookup for grid tools |

Anansi only reads these tables (never writes). Add `GRANT SELECT ON ...` for any additional tables your MCP servers query.

### Chat Database (Supabase)

1. Create a Supabase project at [supabase.com](https://supabase.com).
2. Enable `pgvector`: `CREATE EXTENSION IF NOT EXISTS "vector";`
3. Run the bootstrap schema in Supabase SQL Editor:
   ```bash
   # Copy and execute db/schema/chat_db.sql in the Supabase SQL Editor
   # (Project → SQL Editor → New query → paste → Run)
   ```
4. From **Project Settings → API**, copy the project URL and `service_role` key (not the `anon` key).

#### Chat DB — key tables

| Table | Purpose |
|-------|---------|
| `chat_sessions` | One row per conversation thread (keyed by Telegram chat/thread ID or API session) |
| `chat_messages` | Full message history with role, content, and tool call records |
| `agent_work_packets` | State for multi-step expert workflows (paused, running, completed, failed) |
| `agent_work_packet_logs` | Execution log per workflow run — step timings, success/failure |
| `persistent_agent_instances` | User-created monitoring agents with schedule and check/response prompts |
| `pending_decisions` | Multi-turn decision state (e.g. "duplicate detected — resume or start fresh?") |
| `documents` | RAG document store — metadata, access control, embeddings |
| `document_chunks` | Chunked text with vector embeddings (pgvector) |

The full schema is in `db/schema/chat_db.sql` and can be applied in one step via the Supabase SQL Editor.

## RAG Pipeline Setup

### 1. Deploy Database Schema

The RAG schema (documents, chunks, vector index) is included in the main Chat DB schema.
Run `db/schema/chat_db.sql` in Supabase SQL Editor if you haven't already — no separate RAG migration needed.

### 2. Install and Configure

```bash
cd rag_pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Add GOOGLE_API_KEY, SUPABASE credentials, etc.
```

### 3. Run Ingestion

**GitHub:**
```bash
python ingestion/github_indexer_v2.py \
  --repo owner/repo \
  --source-name "Company Codebase"
```

**Google Drive:**
```bash
python ingestion/gdrive_indexer_v2.py \
  --folder-id YOUR_FOLDER_ID \
  --source-name "Company Docs"
```

**Telegram:**
```bash
python ingestion/telegram_indexer_v2.py \
  --folder-id TELEGRAM_EXPORTS_FOLDER_ID \
  --source-name "Support Chats"
```

### 4. Query RAG

RAG is automatically used by the chat orchestrator when `rag.enabled=true` in settings.

## Deployment

### DigitalOcean App Platform

**Main App (`anansi`)**:
| Component | Type | Description |
|-----------|------|-------------|
| anansi-bot | Service | Gemini orchestration + MCP tools (consolidated) |
| broadcast-scheduler | Job | Scheduled broadcast processor (8am-7pm UTC) |

**Admin App (`anansi-app`)**:
| Component | Type | Description |
|-----------|------|-------------|
| anansi-app | Service | Streamlit admin UI |

```bash
# Deploy via doctl (SAFE pattern — never update directly from .do/app.yaml which has placeholders)
doctl apps spec get <app-id> > /tmp/live-spec.yaml
# edit /tmp/live-spec.yaml, then:
doctl apps update <app-id> --spec /tmp/live-spec.yaml
doctl apps create-deployment <app-id>

# Or push to main branch (auto-deploys)
git push origin main
```

### Docker

```bash
cd chat_orchestrator
docker build -t anansi-orchestrator .
docker run -p 8000:8000 --env-file .env anansi-orchestrator
```

## Optional Data & External Services

Some features require external data files or third-party services. All are optional — the core chat orchestrator works without them.

### GRID3 GeoPackages (Community Boundary Detection)

The layout engine and community detection expert use GRID3 settlement-extents datasets to detect community boundaries around GPS points. GRID3 publishes these per-country for all of sub-Saharan Africa, so coverage is driven by a **manifest** rather than a single hardcoded file: an anchor's country is reverse-geocoded and matched against the datasets you have on hand.

**Set up the data location:**
1. Go to [https://grid3.org/resources/datasets](https://grid3.org/resources/datasets) and download the **"Settlement Extents"** GeoPackage (`.gpkg`) for each country you operate in (e.g. `NGA`, ≈3.4 GB).
2. Put them in one directory (local path or `s3://` prefix) and point `SETTLEMENT_DATA_DIR` at it.
3. Add a `manifest.json` in that directory describing each dataset:

```json
{
  "datasets": [
    {
      "iso2": "NG",
      "iso3": "NGA",
      "country_name": "Nigeria",
      "file": "GRID3_NGA_settlement_extents_v04_3.gpkg",
      "layer": "main_GRID3_NGA_settlement_extents_v4_0",
      "building_count_col": "building_count"
    }
  ]
}
```

```bash
SETTLEMENT_DATA_DIR=/path/to/settlement-data   # holds the .gpkg files + manifest.json
```

- `iso2` is the ISO 3166-1 alpha-2 code returned by reverse-geocoding (this is the match key).
- `file` may be a bare filename (resolved against `SETTLEMENT_DATA_DIR`), an absolute path, or an `s3://` URI — so a small local manifest can point at large remote GeoPackages.
- `layer` / `building_count_col` vary by country/version; copy them from each GeoPackage.
- For container/S3 deploys you can instead set `SETTLEMENT_MANIFEST_JSON` to the inline manifest JSON.

**Adding a country:** drop its `.gpkg` in the location and add a manifest entry — no code change.

**Legacy mode:** if `SETTLEMENT_DATA_DIR` is unset but `GRID3_GPKG_PATH` points at a single Nigeria GeoPackage, that still works (Nigeria-only). If neither is configured, community detection steps fail with a clear, human-readable error and the rest of the system is unaffected. Anchors in a country with no dataset get a message naming the country and listing supported ones.

### Metering Platform API (Meter Management)

The customer server includes tools for meter commissioning, unassignment, power limit control, and token resend. These call the Metering Platform API — an external meter management backend (see `METERING_*` env vars).

Without these env vars, meter write tools return a "not configured" error and all read-only tools continue to work normally:
```bash
METERING_API_URL=https://your-metering-instance
METERING_BEARER_TOKEN=your-bearer-token
METERING_API_KEY=your-api-key
```

### VRM Platform (Solar Inverter Monitoring)

Grid status and inverter data tools use the [Victron VRM API](https://www.victronenergy.com/live/ccgx:ccgx_ve_direct_vlans#vrm_api):
```bash
VRM_API_KEY=your-vrm-api-key
```

---

### Telegram Bot Setup

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to choose a name and username.
3. Copy the API token BotFather gives you — this is your `TELEGRAM_BOT_TOKEN`.
4. Set `TELEGRAM_BOT_USERNAME` to your bot's @handle (without the `@`).

### Telegram Webhook

After deploying (or when testing locally with a tunnel like [ngrok](https://ngrok.com)):

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=https://yourapp.example.com/chat" \
  -d "secret_token=<API_KEY>"
```

`API_KEY` must match the `API_KEY` env var. To verify:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

Re-run `setWebhook` after redeployments if the URL changes.

### Environment Variables in Production

Set all required env vars in your platform:
- DigitalOcean: App Settings → Environment Variables
- Docker: `--env-file` or `-e` flags
- Kubernetes: ConfigMaps + Secrets

**Critical:** Never commit `.env` files with real credentials!

## Development

### Pre-commit Hooks

Install pre-commit hooks for code quality checks:

```bash
# Install pre-commit
pip install pre-commit

# Install hooks
pre-commit install

# Run manually on all files
pre-commit run --all-files
```

**Enabled checks:**
- ✅ **ruff** - Linting with auto-fix (replaces flake8 + isort)
- ✅ **ruff-format** - Code formatting (100 line length, replaces black)
- ✅ **mypy** - Type checking (`--ignore-missing-imports`)
- ✅ **detect-secrets** - Credential detection (baseline: `.secrets.baseline`)
- ✅ **pre-commit-hooks** - Trailing whitespace, EOF, merge conflicts, YAML/JSON syntax

**Configuration:**
- `.pre-commit-config.yaml` - Hook definitions and pinned versions
- `chat_orchestrator/pyproject.toml` - ruff and mypy settings
- `.secrets.baseline` - Known-safe strings excluded from secret scanning

### Shared Code

Common utilities in `shared/`:

```python
from shared.utils.google_auth import get_drive_credentials
from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown_sections
from shared.utils.logging import get_logger
```

Run `./setup_shared.sh` to make shared code importable.

### Testing with Claude Desktop

Test the orchestrator locally via Claude Desktop:

**1. Configure Claude:**

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "anansi": {
      "command": "/full/path/to/chat_orchestrator/.venv/bin/python",
      "args": ["/full/path/to/chat_orchestrator/orchestrator_mcp_server.py"],
      "env": {
        "CHAT_DB_URL": "...",
        "CHAT_DB_SERVICE_KEY": "...",
        "GOOGLE_API_KEY": "..."
      }
    }
  }
}
```

**2. Restart Claude Desktop**

The orchestrator appears as available tools in Claude.

### Project Structure

```
anansi/
├── chat_orchestrator/       # Main orchestrator
│   ├── orchestrator/
│   │   ├── api/            # FastAPI endpoints
│   │   ├── services/       # Core logic
│   │   │   ├── conversation.py        # Gemini orchestration
│   │   │   ├── instructions_provider.py  # Google Docs fetching
│   │   │   └── artifacts_provider.py     # Section parsing
│   │   └── clients/        # External API clients
│   └── local_server.py     # Development server
│
├── anansi_app/           # Streamlit admin UI
│   ├── pages/              # UI pages (chat viewer, settings)
│   ├── components/         # Reusable UI components
│   ├── services/           # Business logic (broadcast, scheduling)
│   └── scripts/            # Background jobs (broadcast_scheduler.py)
│
├── rag_pipeline/           # Knowledge ingestion
│   ├── ingestion/          # Source-specific indexers
│   └── database/           # SQL schemas
│
├── mcp_servers/            # Tool servers
│   ├── servers/            # Individual MCP servers
│   │   ├── jira_server/    # JIRA integration
│   │   ├── meters_server/  # Meter operations
│   │   ├── customer_server/# Customer/grid info
│   │   ├── schedule_server/# Command scheduling
│   │   └── meta_server/    # Bot analytics
│   └── mcp_launcher.py     # Server manager
│
└── shared/                 # Common utilities
    └── utils/
        ├── google_auth.py          # Google authentication
        ├── gdrive_doc_fetcher.py   # Doc fetching + parsing
        └── logging.py              # Logging setup
```

## Troubleshooting

### "CUSTOMER_SUPPORT_DOC_ID not set"
- Add to `.env` file
- Get from Google Doc URL

### "Failed to fetch Google Doc"
- Verify doc is shared with service account email
- Check service account has Docs API enabled
- Confirm doc ID is correct

### "No 'System Instructions' section found"
- Add "System Instructions" as Heading 1 (not bold text)
- Section name is case-insensitive
- Must be the first section (after title page)

### "No module named 'shared'"
```bash
./setup_shared.sh
export PYTHONPATH=$PWD
```

### Google authentication errors
```bash
# Test credentials
python3 -c "from shared.utils.google_auth import verify_credentials; verify_credentials()"
```

### Content not updating
- Docs are cached briefly
- Restart service to clear cache
- Check logs for "Using cached Google Doc"

## Key Features

### Google Docs Integration
- ✅ Single source of truth for instructions
- ✅ Collaborative editing
- ✅ Version history via Google Docs
- ✅ No code deployments needed for updates
- ✅ Markdown conversion preserves formatting
- ✅ Auto-strips title pages, headers, footers, images
- ✅ Section parsing by heading styles

### Proper Gemini API Usage
- ✅ System instructions in `systemInstruction` field
- ✅ Context messages as first user message
- ✅ Token-efficient structure
- ✅ Persistent instructions across turns

### Dual Mode Support
- ✅ Customer mode for external users (public-facing, no sensitive data)
- ✅ Staff mode for internal users (restricted access, full knowledge)
- ✅ Identical processing pipeline
- ✅ Organization-based routing

### GraphRAG Knowledge
- ✅ Multi-source ingestion
- ✅ Entity and relationship extraction
- ✅ Semantic search
- ✅ Community detection
- ✅ Incremental sync

### User Command Scheduling
- ✅ Schedule commands like `/tickets` or `/grid` for future execution
- ✅ Recurring schedules (daily at 9am, every monday at 10am, etc.)
- ✅ Natural language time parsing with timezone configurable via `DEFAULT_TIMEZONE` env var
- ✅ Results posted to originating chat
- ✅ Staff only access (not available to customers)

### Bot Analytics (Meta Server)
- ✅ Performance reports with response vs escalation breakdown
- ✅ Escalation reason analysis with pie charts
- ✅ Negative feedback tracking
- ✅ Organization filtering for multi-tenant analytics
- ✅ Staff only access via `/meta` command

### Telegram Inline Buttons
- ✅ Inline keyboard buttons supplement text-based options
- ✅ Expert workflow buttons for duplicate detection and resume prompts
- ✅ Procedure buttons for customer support conversation flows
- ✅ User mentions with Telegram deep links in group chats
- ✅ Authorization: original user, staff, or anyone for cancel
- ✅ Feature flags: `INLINE_BUTTONS_ENABLED`, `PROCEDURE_BUTTONS_ENABLED`
- ✅ Text input always works as fallback

## API Example

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_input": "What are your business hours?",
    "user_context": {
      "user_email": "user@example.com",
      "source": "web"
    }
  }'
```

**Response:**
```json
{
  "final_text": "Our business hours are Monday-Friday, 9 AM - 5 PM EST.",
  "tool_calls": [],
  "tool_results": [],
  "history": [...]
}
```

## Monitoring

### Health Checks
```bash
curl http://localhost:8000/health
```

### Logs
```bash
# Check instruction loading
grep "Loaded.*instructions" chat_orchestrator/logs/app.log

# Check RAG queries
grep "RAG retrieval" chat_orchestrator/logs/app.log

# Check errors
grep "ERROR" chat_orchestrator/logs/app.log
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow. Quick links:

- [Adding an MCP Server](guides/mcp-servers.md) — create a tool server end-to-end
- [Expert Workflows](guides/expert-workflows.md) — build multi-step LLM workflows

General guidelines:

1. Follow existing code structure
2. Use shared utilities from `shared/`
3. Update Google Docs for instruction changes (no code needed)
4. Add tests for new features
5. Keep documentation current

## License

Mozilla Public License 2.0 — see [LICENSE](LICENSE) for full text.

---

**Questions?** Check the troubleshooting section above or review the logs for detailed error messages.
