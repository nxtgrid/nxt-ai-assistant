# RAG Pipeline - Ingestion Scripts

Source-specific ingestion scripts for the GraphRAG pipeline. Each indexer extracts data from its source, processes it through the unified pipeline, and stores it in Supabase.

## Quick Reference

### Production Indexers (V2)

| Script | Source | Purpose | Usage |
|--------|--------|---------|-------|
| `codebase_indexer_v2.py` | GitHub | Index code repositories | `python codebase_indexer_v2.py --repo owner/repo` |
| `gdrive_indexer_v2.py` | Google Drive | Index Drive folders | `python gdrive_indexer_v2.py --folder-id ID` |
| `telegram_indexer_v2.py` | Telegram | Index chats/topics | `python telegram_indexer_v2.py --json export.json` |
| `grafana_indexer_v2.py` | Grafana | Index dashboard panels | `python grafana_indexer_v2.py --folder-name "Team"` |

### GraphRAG Pipeline

| Script | Purpose |
|--------|---------|
| `graphrag_pipeline.py` | Coordinate full GraphRAG flow |
| `entity_extractor.py` | Extract entities from chunks |
| `graph_extractor.py` | Extract relationships |
| `community_detector.py` | Detect entity communities |
| `global_graph_builder.py` | Build cross-source graph |

### Utilities

| Script | Purpose |
|--------|---------|
| `batch_ingest.py` | Coordinate multi-source ingestion |
| `access_control.py` | Access control utilities |
| `semantic_chunker.py` | Smart text chunking |
| `vector_embedder.py` | Generate embeddings |
| `sync_tracker.py` | Track incremental syncs |
| `google_auth.py` | Google authentication |
| `gdrive_doc_fetcher.py` | Fetch individual Google Docs |

---

## GitHub Indexer

**Script:** `codebase_indexer_v2.py`

### Features
- Full and incremental sync
- Language detection
- Code structure preservation
- PR and issue integration (optional)

### Usage

```bash
# Full sync
python codebase_indexer_v2.py --repo facebook/react --branch main

# Incremental sync (only changed files)
python codebase_indexer_v2.py --repo facebook/react --incremental

# Include pull requests
python codebase_indexer_v2.py --repo myorg/backend --include-prs
```

### Environment Variables
```bash
GITHUB_TOKEN=ghp_...        # For private repos, higher rate limits
CHAT_DB_URL=...
CHAT_DB_SERVICE_KEY=...
GOOGLE_API_KEY=...
```

---

## Google Drive Indexer

**Script:** `gdrive_indexer_v2.py`

### Features
- Recursive folder scanning
- Supports: Docs, Sheets, Slides, PDFs, DOCX, PPTX, TXT
- Incremental sync based on modification time
- Document metadata preservation

### Usage

```bash
# Index a folder
python gdrive_indexer_v2.py \
  --folder-id 1abc123xyz456 \
  --source-name "Engineering Docs"

# Incremental sync
python gdrive_indexer_v2.py \
  --folder-id 1abc123xyz456 \
  --incremental

# Non-recursive (top-level only)
python gdrive_indexer_v2.py \
  --folder-id 1abc123xyz456 \
  --no-recursive
```

### Environment Variables
```bash
GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
CHAT_DB_URL=...
CHAT_DB_SERVICE_KEY=...
GOOGLE_API_KEY=...
```

**Setup:**
1. Create Google Cloud service account
2. Enable Google Drive API
3. Download JSON key
4. Share Drive folders with service account email

---

## Telegram Indexer

**Script:** `telegram_indexer_v2.py`

### Features
- Bulk ingestion from JSON exports
- Google Drive folder monitoring
- API-based incremental updates
- Topic-based organization
- Monthly document aggregation
- Automatic duplicate prevention

### Usage

#### Bulk JSON Ingestion
```bash
# From exported JSON
python telegram_indexer_v2.py \
  --json export.json \
  --bulk \
  --initial-sync-date "2025-10-25T00:00:00"

# With chat filtering
python telegram_indexer_v2.py \
  --json export.json \
  --bulk \
  --allowed-chat-ids "1812110022,2641051445"
```

#### Google Drive Monitoring
```bash
# Monitor folder for new exports
python telegram_indexer_v2.py --gdrive FOLDER_ID

# Using env var
export GOOGLE_DRIVE_TELEGRAM_CHAT_EXPORTS=FOLDER_ID
python telegram_indexer_v2.py --gdrive
```

#### Incremental API Mode
```bash
python telegram_indexer_v2.py \
  --api \
  --group-ids "1812110022" \
  --incremental
```

### Environment Variables
```bash
# For JSON/GDrive modes
GOOGLE_SERVICE_ACCOUNT_JSON='...'
GOOGLE_DRIVE_TELEGRAM_CHAT_EXPORTS=folder_id
CHAT_DB_URL=...
CHAT_DB_SERVICE_KEY=...
GOOGLE_API_KEY=...

# For API mode
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abc123...
TELEGRAM_PHONE=+1234567890
```

---

## Grafana Dashboard Indexer

**Script:** `grafana_indexer_v2.py`

### Overview

The Grafana indexer extracts dashboard panel metadata and generates AI-powered tool descriptions for each panel. Unlike other indexers, it stores metadata in environment variables (`GRAFANA_PANELS_METADATA`) rather than the database, as these are used directly by the Grafana MCP server to dynamically create visualization tools.

### Features

- **Incremental Updates**: Only regenerates descriptions when dashboards change or system prompts are updated
- **LLM-Generated Descriptions**: Uses Gemini to create intelligent tool descriptions from panel queries
- **Dashboard Filtering**: Select specific dashboards to index via `GRAFANA_ENABLED_DASHBOARDS`
- **Version Tracking**: Monitors Grafana dashboard versions to detect changes
- **Cost Optimization**: Reduces Gemini API calls by ~95% through smart caching

### How It Works

1. **Fetch Dashboards** from specified Grafana folder
2. **Extract Panel Metadata** (queries, variables, titles)
3. **Generate Tool Descriptions** using Gemini LLM (only when needed)
4. **Store in Environment** as `GRAFANA_PANELS_METADATA`
5. **MCP Server Reads** metadata to create visualization tools

### Incremental Update Logic

The indexer implements intelligent change detection to minimize API costs:

**Metadata Tracking:**
```json
{
  "dashboard_uid:panel_id": {
    "title": "Panel Title",
    "tool_description": "AI-generated description",
    "dashboard_version": 447,
    "dashboard_updated": "2025-10-22T09:58:40Z",
    "system_prompt_hash": "abc123",
    "last_indexed_at": "2025-12-09T02:00:00Z",
    ...
  }
}
```

**Regeneration Triggers:**
- ✅ Dashboard version changed (Grafana increments on any save)
- ✅ System prompt modified (`GRAFANA_PANEL_DESCRIPTION_PROMPT`)
- ✅ Panel is new (not in previous metadata)
- ✅ Force reindex flag set (`GRAFANA_FORCE_FULL_REINDEX=true`)

**Skip Conditions:**
- ✅ Dashboard version unchanged
- ✅ System prompt unchanged
- ✅ Panel exists in metadata with matching version

**Performance Impact:**
- First run: ~50 panels = 50 Gemini API calls (~2-3 minutes)
- Subsequent runs: ~0-5 calls (~10-30 seconds)
- Weekly savings: ~95% reduction (from 350 to 15-20 API calls)

### Usage

#### Standalone Execution

```bash
# Index all dashboards in folder
python grafana_indexer_v2.py --folder-name "Your Team"

# Force full reindex (ignore version tracking)
GRAFANA_FORCE_FULL_REINDEX=true python grafana_indexer_v2.py

# Save to file (for testing)
python grafana_indexer_v2.py --output-file metadata.json
```

#### Automated Nightly Sync

The indexer runs automatically via `grafana_indexer_incremental.py`:

```bash
# Called by scheduler at 2am UTC
python grafana_indexer_incremental.py

# The wrapper:
# 1. Loads existing GRAFANA_PANELS_METADATA
# 2. Passes to indexer for incremental updates
# 3. Updates DigitalOcean env vars
# 4. Triggers app restart (optional)
```

### Environment Variables

```bash
# Grafana Connection
GRAFANA_URL=https://grafana.example.com
GRAFANA_USERNAME=user@example.com
GRAFANA_PASSWORD=***
GRAFANA_FOLDER_NAME="Your Team"

# LLM Configuration
GOOGLE_API_KEY=***
GRAFANA_PANEL_DESCRIPTION_PROMPT="You are a system that generates..."

# Dashboard Selection
GRAFANA_ENABLED_DASHBOARDS="uid1,uid2,uid3"  # Comma-separated UIDs
GRAFANA_AVAILABLE_DASHBOARDS='{"uid1":"Dashboard Name",...}'

# Output (managed automatically)
GRAFANA_PANELS_METADATA='{"uid:panel_id": {...}, ...}'
GRAFANA_ENABLED_PANELS="uid1:34,uid2:12,..."

# Control Flags
GRAFANA_ACTIONS_ENABLED=true
GRAFANA_FORCE_FULL_REINDEX=false  # Override incremental mode
GRAFANA_SYNC_HOUR=2  # Hour (UTC) for nightly sync

# DigitalOcean (for env var updates)
DIGITALOCEAN_APP_ID=***
DIGITALOCEAN_API_TOKEN=***
```

### System Prompt Customization

The system prompt controls how Gemini generates tool descriptions:

```bash
export GRAFANA_PANEL_DESCRIPTION_PROMPT="You are a system that generates tool descriptions for Grafana dashboard panels. Given a panel with title, description, query, and dashboard variables, create a concise tool description that explains what data this panel shows and what variables it requires. Format: A tool description suitable for an LLM to understand when to use this panel."
```

**What Gemini Receives Per Panel:**
- Panel title (e.g., "Top ups - $gridName")
- Panel description (often empty)
- Panel query (SQL or PromQL)
- Dashboard variables (e.g., gridName, meterType)

**What Gemini Returns:**
- Concise tool description explaining what data the panel shows
- Which variables are required
- What the visualization represents

### Dashboard Selection Workflow

1. **Fetch Available Dashboards** (via `fetch_grafana_dashboards.py`):
   ```bash
   python fetch_grafana_dashboards.py
   # Updates GRAFANA_AVAILABLE_DASHBOARDS
   ```

2. **Select Dashboards in Admin UI**:
   - Go to anansi-app settings page
   - See dropdown with all dashboards from folder
   - Select which ones to index
   - Save (updates `GRAFANA_ENABLED_DASHBOARDS`)

3. **Nightly Sync**:
   - Runs at 2am UTC
   - Only processes panels from enabled dashboards
   - Uses incremental logic to skip unchanged panels

### Integration with MCP Server

The Grafana MCP server (`mcp_servers/servers/grafana_server/grafana_mcp_server.py`) reads the metadata at startup:

```python
# Reads from environment
GRAFANA_PANELS_METADATA = os.getenv("GRAFANA_PANELS_METADATA", "{}")
GRAFANA_ENABLED_PANELS = os.getenv("GRAFANA_ENABLED_PANELS", "")

# Creates one MCP tool per enabled panel
for panel_key, panel_info in PANELS_METADATA.items():
    if panel_key in ENABLED_PANEL_IDS:
        tool_name = f"render_{panel_key.replace(':', '_')}"
        tool_description = panel_info["tool_description"]  # From Gemini
        # Register tool with LLM
```

When the main LLM sees these tools, it can call them to render Grafana visualizations as images and analyze the data.

### Monitoring

Check indexer stats in logs:

```
================================================================================
GRAFANA INDEXING COMPLETE
================================================================================
Total dashboards available: 8
Total dashboards processed: 3
Total panels indexed: 47

📊 Incremental Update Stats:
  New panels: 0
  Regenerated (dashboard version changed): 3
  Regenerated (prompt changed): 0
  Regenerated (force reindex): 0
  Skipped (unchanged): 44
  Total Gemini API calls: 3
  API calls saved: 44
================================================================================
```

### Troubleshooting

**No dashboards found:**
```bash
# Check Grafana folder name
curl -u "user:pass" "https://grafana.example.com/api/search?type=dash-folder"

# Verify credentials
curl -u "user:pass" "https://grafana.example.com/api/folders"
```

**Panels not regenerating:**
```bash
# Force full reindex
GRAFANA_FORCE_FULL_REINDEX=true python grafana_indexer_incremental.py

# Check dashboard version changed
# Grafana only increments version when dashboard is saved
```

**Gemini API errors:**
```bash
# Test API key
curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GOOGLE_API_KEY"

# Check quota/billing in Google AI Studio
```

**MCP server not seeing panels:**
```bash
# Check environment variable is set
doctl apps spec get APP_ID --format json | jq '.envs[] | select(.key == "GRAFANA_PANELS_METADATA")'

# Verify MCP server logs show panel count
```

### Related Scripts

- `fetch_grafana_dashboards.py` - Fetch dashboard list without processing panels
- `grafana_indexer_incremental.py` - Production wrapper with env var management
- `promote_grafana_vars_to_global.py` - One-time migration script

---

## Access Control

All indexers use unified access control via the `source_access_control` table.

### How It Works

1. **Configure access** in database:
```sql
INSERT INTO source_access_control (
  source_type, source_id, scope, org_ids, role_ids
) VALUES (
  'github', 'facebook/react', NULL, ARRAY[1, 2], ARRAY['developer', 'admin']
);
```

2. **Indexer looks up access** during ingestion
3. **Documents inherit** org_ids and role_ids
4. **Chunks inherit** from parent document
5. **Queries filter** based on user permissions

### Utility Functions

```python
from access_control import get_source_access

# Get access for a source
org_ids, role_ids = get_source_access(
    supabase_client,
    source_type='telegram',
    source_id='1812110022',
    scope='5',  # Topic ID
    fallback_to_parent=True
)
```

### Configure Access

```sql
-- GitHub repository
INSERT INTO source_access_control (source_type, source_id, org_ids, role_ids)
VALUES ('github', 'myorg/backend', ARRAY[1], ARRAY['developer']);

-- Google Drive folder
INSERT INTO source_access_control (source_type, source_id, org_ids, role_ids)
VALUES ('gdrive', '1abc123xyz', ARRAY[1, 2], ARRAY['employee']);

-- Telegram topic
INSERT INTO source_access_control (source_type, source_id, scope, org_ids, role_ids)
VALUES ('telegram', '1812110022', '5', ARRAY[1], ARRAY['team-member']);
```

---

## GraphRAG Pipeline

### Full Pipeline Execution

```bash
# Run complete GraphRAG for a source
python graphrag_pipeline.py \
  --source github \
  --repo facebook/react

# Rebuild global graph from existing data
python global_graph_builder.py
```

### Pipeline Stages

1. **Chunking** (`semantic_chunker.py`) - Split into ~512 token chunks
2. **Embeddings** (`vector_embedder.py`) - Google AI Studio text-embedding-005
3. **Entity Extraction** (`entity_extractor.py`) - Extract concepts, people, technologies
4. **Relationship Extraction** (`graph_extractor.py`) - Find connections
5. **Community Detection** (`community_detector.py`) - Cluster related entities
6. **Global Graph** (`global_graph_builder.py`) - Merge across sources

---

## Batch Ingestion

**Script:** `batch_ingest.py`

Coordinate multiple sources in one operation.

### Configure Sources

```sql
INSERT INTO ingestion_sources (source_type, source_name, config) VALUES
('github', 'backend', '{"repo": "myorg/backend", "branch": "main"}'::jsonb),
('gdrive', 'docs', '{"folder_id": "1abc123", "recursive": true}'::jsonb);
```

### Run Batch

```bash
# All sources
python batch_ingest.py --all-sources

# Specific sources
python batch_ingest.py --sources github:backend,gdrive:docs

# Incremental only
python batch_ingest.py --all-sources --incremental
```

---

## Utilities

### Google Authentication

**Script:** `google_auth.py`

```python
from google_auth import get_drive_credentials, get_service_account_email

# Get Drive credentials
credentials = get_drive_credentials()

# Get service account email (for sharing folders)
email = get_service_account_email()
print(f"Share Drive folders with: {email}")
```

### Google Docs Fetcher

**Script:** `gdrive_doc_fetcher.py`

```bash
# Fetch a single document
python gdrive_doc_fetcher.py DOC_ID

# From Python
from gdrive_doc_fetcher import fetch_google_doc
content = fetch_google_doc('1abc123xyz')
```

### Sync Tracking

**Script:** `sync_tracker.py`

Track incremental sync state for each source.

```python
from sync_tracker import SyncTracker

tracker = SyncTracker(supabase_client)
last_run = tracker.get_last_sync('github', 'facebook/react')
tracker.record_sync('github', 'facebook/react', {'files': 42})
```

---

## Deprecated Files

These files are deprecated and will be removed:

### ⚠️ Deprecated
- `entity_extraction_only.py` - Use `graph_extractor.py` instead
- `codebase_indexer_incremental.py` - Use `codebase_indexer_v2.py --incremental`

### 📁 Analysis/Initial Ingestion (Archived)
- `telegram_initial_ingestion/` - One-time ingestion scripts
  - Customer support Q&A extraction
  - Data cleaning utilities
  - Not needed for ongoing operations

---

## Troubleshooting

### Import Errors

```bash
# Set PYTHONPATH
export PYTHONPATH=$PWD/..
python ingestion/codebase_indexer_v2.py --repo owner/repo
```

### Google Auth Errors

```bash
# Test authentication
python google_auth.py

# Verify service account JSON is valid
echo $GOOGLE_SERVICE_ACCOUNT_JSON | python -m json.tool
```

### Access Control Not Working

```sql
-- Check access control entries
SELECT * FROM source_access_control 
WHERE source_type = 'github' 
AND source_id = 'myorg/repo';

-- Check document org_ids
SELECT id, title, org_ids, role_ids 
FROM documents 
WHERE source_type = 'github' 
LIMIT 5;
```

### Rate Limiting

```bash
# Add delays between batches (edit the script)
# Or reduce batch size in vector_embedder.py
```

---

## For More Information

- **Main README:** `../README.md` - Complete pipeline documentation
- **Access Control Flow:** See `access_control.py` docstrings
- **Database Schema:** `../database/GraphRAG-Compatible_Supabase_Schema_Google.sql`
