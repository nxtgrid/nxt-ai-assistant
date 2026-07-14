# RAG Pipeline - GraphRAG Data Ingestion

A comprehensive data ingestion pipeline that implements GraphRAG (Graph-based Retrieval Augmented Generation) for processing documents from multiple sources into a unified knowledge graph.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Supported Sources](#supported-sources)
- [Quick Start](#quick-start)
- [GraphRAG Pipeline](#graphrag-pipeline)
- [Credentials Configuration](#credentials-configuration)
- [Batch Ingestion](#batch-ingestion)
- [Access Control](#access-control)
- [Database Schema](#database-schema)
- [Deployment](#deployment)

---

## Overview

The RAG pipeline ingests data from multiple sources (GitHub, Google Drive, Telegram) and builds a unified knowledge graph with:

✅ **Semantic chunking** - Preserves document structure  
✅ **Vector embeddings** - Vertex AI gemini-embedding-001 (768 dimensions)
✅ **Entity extraction** - Identifies concepts, technologies, people, organizations  
✅ **Relationship mapping** - Discovers connections between entities  
✅ **Community detection** - Finds clusters of related information  
✅ **Access control** - Organization and role-based permissions  
✅ **Incremental sync** - Only process new/modified content

### Key Features

- **Multi-source ingestion**: GitHub, Google Drive, Telegram (extensible to Slack, Notion)
- **GraphRAG implementation**: Complete entity-relationship-community pipeline
- **Cross-source graph**: Entities merge across sources (e.g., "React" mentioned everywhere)
- **Incremental updates**: Track what's changed since last run
- **Batch processing**: Coordinate multiple sources in one operation
- **Role-based access**: Documents inherit organization and role permissions

---

## Architecture

### Directory Structure

```
rag_pipeline/
├── ingestion/                   # Source-specific indexers
│   ├── codebase_indexer_v2.py         # GitHub repositories
│   ├── gdrive_indexer_v2.py           # Google Drive folders
│   ├── telegram_indexer_v2.py         # Telegram chats
│   ├── gdrive_doc_fetcher.py          # Google Docs utility
│   ├── google_auth.py                 # Google authentication
│   ├── graphrag_pipeline.py           # GraphRAG coordinator
│   ├── entity_extractor.py            # Entity extraction
│   ├── community_detector.py          # Community detection
│   ├── batch_ingest.py                # Batch orchestration
│   └── access_control.py              # Access control utilities
├── database/                    # Database schemas and migrations
│   ├── GraphRAG-Compatible_Supabase_Schema_Google.sql
│   ├── incremental_sync_schema.sql
│   ├── unified_source_access_control.sql
│   └── ...
├── .env.example                 # Environment template
├── requirements.txt             # Python dependencies
└── README.md                    # This file
```

### Pipeline Phases

#### Phase 1: Per-Source Ingestion
```
1. Document Extraction → 2. Chunking → 3. Embeddings → 4. Entity Extraction → 5. Relationships
```

#### Phase 2: Cross-Source Graph Building  
```
6. Entity Deduplication → 7. Relationship Merging → 8. Community Detection → 9. Summarization
```

---

## Supported Sources

### 1. 📁 GitHub Repositories

**Script:** `ingestion/codebase_indexer_v2.py`

Indexes code from GitHub repositories for semantic code search.

**Features:**
- Incremental sync (only changed files)
- Language detection
- PR and issue integration
- Code structure preservation

**Usage:**
```bash
python ingestion/codebase_indexer_v2.py \
  --repo owner/repo \
  --branch main \
  --incremental
```

### 2. 📄 Google Drive

**Script:** `ingestion/gdrive_indexer_v2.py`

Indexes documents from Google Drive folders (Docs, PDFs, Sheets, Slides).

**Features:**
- Recursive folder scanning
- File type detection (Docs, PDFs, DOCX, etc.)
- Incremental sync based on modification time
- OCR for images (optional)

**Usage:**
```bash
python ingestion/gdrive_indexer_v2.py \
  --folder-id 1abc123xyz \
  --source-name "Engineering Docs" \
  --incremental
```

### 3. 💬 Telegram

**Script:** `ingestion/telegram_indexer_v2.py`

Indexes messages from Telegram chats and topics.

**Features:**
- Topic-based organization
- Monthly document aggregation
- Media file extraction
- Link preservation

**Usage:**
```bash
python ingestion/telegram_indexer_v2.py \
  --chat-id @your_channel \
  --incremental
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- Supabase account and database
- Google AI Studio API key
- Source-specific credentials (GitHub token, Google service account, etc.)

### Installation

1. **Install dependencies:**
   ```bash
   cd rag_pipeline
   python3 -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Set up database:**
   ```bash
   # Run in Supabase SQL editor
   # 1. GraphRAG schema
   database/GraphRAG-Compatible_Supabase_Schema_Google.sql
   
   # 2. Incremental sync tracking
   database/incremental_sync_schema.sql
   
   # 3. Access control
   database/unified_source_access_control.sql
   ```

3. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials (see Credentials section)
   ```

4. **Test setup:**
   ```bash
   # Test Google auth
   python ingestion/google_auth.py
   
   # Test a small ingestion
   python ingestion/gdrive_doc_fetcher.py <doc_id>
   ```

### First Ingestion

```bash
# Index a GitHub repository
python ingestion/codebase_indexer_v2.py \
  --repo facebook/react \
  --branch main

# Index a Google Drive folder
python ingestion/gdrive_indexer_v2.py \
  --folder-id 1abc123xyz456 \
  --source-name "Company Docs"

# Check results in Supabase
# SELECT count(*) FROM documents;
# SELECT count(*) FROM chunks;
# SELECT count(*) FROM entities;
```

---

## GraphRAG Pipeline

### What is GraphRAG?

GraphRAG extends traditional RAG by:
1. **Extracting entities** (concepts, people, technologies) from text
2. **Identifying relationships** between entities
3. **Building a knowledge graph** that connects information
4. **Detecting communities** of related entities
5. **Enabling multi-hop reasoning** across connected concepts

### Pipeline Stages

#### 1. Document Chunking
```python
# semantic_chunker.py
# Splits documents into ~512 token chunks
# Preserves sentence boundaries and structure
```

#### 2. Vector Embeddings
```python
# vector_embedder.py  
# Vertex AI gemini-embedding-001 (768 dimensions)
# Parallel processing for speed
```

#### 3. Entity Extraction
```python
# entity_extractor.py
# Uses Gemini 1.5 Flash to identify:
# - Classes, functions, modules (code)
# - Technologies, frameworks, tools
# - People, organizations
# - Concepts, topics
```

#### 4. Relationship Extraction
```python
# graph_extractor.py
# Identifies connections:
# - uses, implements, calls (code)
# - depends_on, contains
# - related_to, mentioned_in
```

#### 5. Global Graph Building
```python
# global_graph_builder.py
# Merges entities across sources
# Deduplicates similar entities
# Creates unified knowledge graph
```

#### 6. Community Detection
```python
# community_detector.py
# Leiden algorithm for clustering
# Hierarchical communities (levels 0, 1, 2)
# Generates community summaries
```

### Running Full GraphRAG Pipeline

```bash
# Option 1: End-to-end for one source
python ingestion/graphrag_pipeline.py \
  --source github \
  --repo owner/repo

# Option 2: Batch process all sources
python ingestion/batch_ingest.py --all-sources

# Option 3: Rebuild global graph from existing data
python ingestion/global_graph_builder.py
```

---

## Credentials Configuration

### Required Environment Variables

#### 1. Chat Database (All Indexers)
```bash
CHAT_DB_URL=https://your-project.supabase.co
CHAT_DB_SERVICE_KEY=your-service-role-key
```

**How to get:**
- Go to Supabase → Project Settings → API
- Copy "Project URL" and "service_role" key

#### 2. Google AI Studio (Embeddings & LLM)
```bash
GOOGLE_API_KEY=your-api-key-here
```

**How to get:**
- Go to https://aistudio.google.com/app/apikey
- Click "Create API key"
- Copy the generated key

#### 3. Google Service Account (Drive Access)
```bash
GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
```

**How to get:**
1. Go to Google Cloud Console
2. Create service account
3. Download JSON key
4. Copy entire JSON as single-line string to .env

**Share Drive folders** with service account email (found in JSON as `client_email`)

#### 4. GitHub (Optional)
```bash
GITHUB_TOKEN=ghp_your_token_here
```

**How to get:**
- GitHub Settings → Developer settings → Personal access tokens
- Generate token with `repo` scope

#### 5. Telegram (Optional)
```bash
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE=+1234567890
```

**How to get:**
- Go to https://my.telegram.org/apps
- Create app to get API ID and hash

### .env Example

```bash
# Chat Database
CHAT_DB_URL=https://abc123.supabase.co
CHAT_DB_SERVICE_KEY=eyJhbG...

# Google AI Studio
GOOGLE_API_KEY=AIzaSy...

# Google Service Account (for Drive)
GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account","project_id":"..."}'

# GitHub (optional)
GITHUB_TOKEN=ghp_abc123...

# Telegram (optional)
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abc123def456...
TELEGRAM_PHONE=+1234567890
```

---

## Batch Ingestion

### Overview

Batch ingestion coordinates multiple sources in one operation with incremental sync support.

### Configure Sources

```sql
-- Add sources to ingest
INSERT INTO ingestion_sources (source_type, source_name, config) VALUES
('github', 'backend-api', '{"repo": "company/backend", "branch": "main"}'::jsonb),
('github', 'frontend-app', '{"repo": "company/frontend", "branch": "main"}'::jsonb),
('gdrive', 'eng-docs', '{"folder_id": "1abc123", "recursive": true}'::jsonb),
('telegram', 'team-chat', '{"chat_id": "1234567890"}'::jsonb);
```

### Run Batch Ingestion

```bash
# Ingest all configured sources
python ingestion/batch_ingest.py --all-sources

# Ingest specific sources
python ingestion/batch_ingest.py \
  --sources github:backend-api,gdrive:eng-docs

# Incremental only (skip unchanged)
python ingestion/batch_ingest.py --all-sources --incremental
```

### Monitor Progress

```sql
-- View batch runs
SELECT * FROM batch_ingestion_runs
ORDER BY created_at DESC
LIMIT 10;

-- View individual sync runs
SELECT
  s.source_name,
  s.source_type,
  r.status,
  r.documents_processed,
  r.chunks_created,
  r.entities_extracted
FROM sync_runs r
JOIN ingestion_sources s ON r.source_id = s.id
ORDER BY r.created_at DESC;
```

### Schedule Automated Syncs

```bash
# Cron example: Daily at 2 AM
0 2 * * * cd /path/to/rag_pipeline && .venv/bin/python ingestion/batch_ingest.py --all-sources --incremental
```

---

## Access Control

### How It Works

Documents inherit organization and role permissions from source configuration:

1. **Configure source access** in `source_access_control` table
2. **Documents inherit** `org_ids` and `role_ids` during ingestion
3. **Chunks inherit** from parent document
4. **Queries filter** based on user's org/role

### Setting Up Access Control

```sql
-- Grant access to a GitHub repo
INSERT INTO source_access_control (
  source_type,
  source_id,
  org_ids,
  role_ids
) VALUES (
  'github',
  'company/backend-api',
  ARRAY[1, 2],              -- Organization IDs
  ARRAY['developer', 'admin']  -- Role names
);

-- Grant access to a Telegram topic
INSERT INTO source_access_control (
  source_type,
  source_id,
  scope,
  org_ids,
  role_ids
) VALUES (
  'telegram',
  '1812110022',      -- Chat ID
  '5',               -- Topic ID
  ARRAY[1],
  ARRAY['team-member', 'manager']
);

-- Grant access to Google Drive folder
INSERT INTO source_access_control (
  source_type,
  source_id,
  org_ids,
  role_ids
) VALUES (
  'gdrive',
  '1abc123xyz456',   -- Folder ID
  ARRAY[1, 2, 3],
  ARRAY['employee']
);
```

### User Access Flow

```python
# When user queries, filter by their permissions
user_org_id = 1
user_roles = ['developer']

# Query only returns chunks user can access
results = supabase.rpc(
    'match_chunks',
    {
        'query_embedding': embedding,
        'match_count': 10,
        'filter': {
            'org_ids': [user_org_id],
            'role_ids': user_roles
        }
    }
)
```

See `ingestion/access_control.py` for utilities and `ingestion/ACCESS_CONTROL_FLOW.md` for detailed flow.

---

## Database Schema

### Core Tables

```sql
documents           -- Source documents (files, chats, repos)
chunks              -- Text chunks with embeddings
entities            -- Extracted entities
relationships       -- Entity-to-entity relationships
communities         -- Detected entity communities
entity_mentions     -- Links entities to chunks
```

### Tracking Tables

```sql
ingestion_sources        -- Configured data sources
sync_runs               -- Individual sync history
batch_ingestion_runs    -- Coordinated batch runs
source_access_control   -- Access permissions
```

### Key Features

- **pgvector** for embeddings
- **RLS policies** for access control
- **Incremental sync** tracking
- **Community hierarchies** (levels 0, 1, 2)

Full schema: `database/GraphRAG-Compatible_Supabase_Schema_Google.sql`

---

## Deployment

### Local Development

```bash
# Run indexer locally
python ingestion/gdrive_indexer_v2.py \
  --folder-id 1abc123 \
  --source-name "Docs"
```

### Scheduled (Cron)

```bash
# Daily sync at 2 AM
0 2 * * * cd /path/to/rag_pipeline && \
  .venv/bin/python ingestion/batch_ingest.py \
  --all-sources --incremental >> logs/cron.log 2>&1
```

### Cloud Functions

The indexers can run as serverless functions:

```python
# handler.py
def main(event, context):
    return batch_ingest(
        sources=event.get('sources'),
        incremental=True
    )
```

DigitalOcean Functions deployment is deprecated — see `../README.md` for current deployment on App Platform.

---

## Troubleshooting

### Module Not Found Errors

**Issue:** `ModuleNotFoundError: No module named 'google_auth'`

**Solution:**
```bash
export PYTHONPATH=$PWD
python ingestion/your_script.py
```

### Credential Errors

**Issue:** `GOOGLE_SERVICE_ACCOUNT_JSON not set`

**Solution:**
- Check `.env` file exists and is loaded
- Verify JSON is valid and single-line
- Test with `python ingestion/google_auth.py`

### Rate Limiting

**Issue:** `429 Too Many Requests` from Google AI

**Solution:**
- Add delays: `time.sleep(1)` between batches
- Reduce batch size in embedder
- Use quotas in Google Cloud Console

### Access Control Not Working

**Issue:** User can't see documents they should access

**Solution:**
1. Check `source_access_control` table has correct entries
2. Verify user's `org_ids` and `role_ids` in query
3. Check RLS policies are enabled in Supabase
4. Review `ingestion/ACCESS_CONTROL_FLOW.md`

---

## Additional Resources

- **GraphRAG Concepts:** See archived `GRAPHRAG_PIPELINE.md` in anansi_deprecated
- **Telegram Processing:** See archived `TELEGRAM_JSON_PROCESSOR.md`
- **Migration Guides:** See anansi_deprecated/rag_deprecated/docs/

For questions, check individual script docstrings and inline comments.
