# Codebase Indexer Deployment Guide

This guide covers deploying the codebase indexer as a DigitalOcean Function that runs twice daily to keep your code vector database up to date.

## Overview

The indexer:
- **Fetches code** from your GitHub repository
- **Chunks code** into manageable pieces (100 lines per chunk)
- **Generates embeddings** using sentence transformers
- **Stores in ChromaDB** for semantic search
- **Runs twice daily** (8 AM and 8 PM UTC) automatically

## Architecture

```
GitHub Repo → Clone/Download → Chunk Code → Generate Embeddings → ChromaDB
     ↓                ↓              ↓              ↓               ↓
  main branch    100 lines    .py, .ts, .js    sentence-        Vector DB
                  per chunk    etc. files     transformers      (persistent)
```

## Prerequisites

1. **DigitalOcean Account** with Functions enabled
2. **GitHub Personal Access Token** with `repo` scope
3. **GitHub Repository** to index

## Option 1: Deploy to DigitalOcean Functions (Recommended)

### Step 1: Install DigitalOcean CLI

```bash
# macOS
brew install doctl

# Linux
cd ~
wget https://github.com/digitalocean/doctl/releases/download/v1.94.0/doctl-1.94.0-linux-amd64.tar.gz
tar xf doctl-1.94.0-linux-amd64.tar.gz
sudo mv doctl /usr/local/bin

# Authenticate
doctl auth init
```

### Step 2: Create Secrets

```bash
# Store GitHub token as secret
doctl serverless secrets create GITHUB_TOKEN "your-github-token-here"

# Verify
doctl serverless secrets list
```

### Step 3: Configure project.yml

Edit `servers/codebase_server/project.yml`:

```yaml
functions:
  - name: codebase-indexer
    triggers:
      - type: SCHEDULED
        cron: "0 8,20 * * *"  # 8 AM and 8 PM UTC

    envs:
      - key: GITHUB_TOKEN
        scope: RUN_TIME
        type: SECRET
      - key: GITHUB_REPO
        value: "your-org/your-repo"  # Change this
        scope: RUN_TIME
      - key: BRANCH
        value: "main"
        scope: RUN_TIME
```

### Step 4: Deploy

```bash
# Navigate to codebase_server directory
cd servers/codebase_server

# Deploy function
doctl serverless deploy .

# Check deployment
doctl serverless functions list
```

### Step 5: Test Function

```bash
# Manual trigger (test run)
doctl serverless functions invoke codebase-indexer \
  --param repo:your-org/your-repo \
  --param branch:main

# View logs
doctl serverless activations logs --function codebase-indexer
```

### Step 6: Monitor

```bash
# View activation history
doctl serverless activations list

# Get specific activation details
doctl serverless activations get <activation-id>

# View function metrics
doctl serverless functions get codebase-indexer
```

## Option 2: Run as Cron Job (Linux/macOS)

If you prefer running on your own server:

### Step 1: Install Dependencies

```bash
pip install -r requirements-indexer.txt
```

### Step 2: Configure Environment

```bash
export GITHUB_TOKEN="your-github-token"
export GITHUB_REPO="your-org/your-repo"
export VECTOR_DB_PATH="/path/to/vector/db"
```

### Step 3: Create Cron Job

```bash
# Edit crontab
crontab -e

# Add these lines (runs at 8 AM and 8 PM daily)
0 8 * * * cd /path/to/mcp-servers/servers/codebase_server && python3 indexer.py $GITHUB_REPO main >> /var/log/codebase-indexer.log 2>&1
0 20 * * * cd /path/to/mcp-servers/servers/codebase_server && python3 indexer.py $GITHUB_REPO main >> /var/log/codebase-indexer.log 2>&1
```

### Step 4: Test Manual Run

```bash
python3 indexer.py your-org/your-repo main
```

## Option 3: GitHub Actions (Alternative)

Use GitHub Actions for serverless execution:

### Step 1: Create Workflow

Create `.github/workflows/index-codebase.yml`:

```yaml
name: Index Codebase

on:
  schedule:
    # Runs at 8 AM and 8 PM UTC
    - cron: '0 8,20 * * *'
  workflow_dispatch:  # Allow manual trigger

jobs:
  index:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout code
        uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: |
          pip install -r servers/codebase_server/requirements-indexer.txt

      - name: Run indexer
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          VECTOR_DB_PATH: ./vector_db
        run: |
          python servers/codebase_server/indexer.py ${{ github.repository }} main

      - name: Upload vector DB artifact
        uses: actions/upload-artifact@v3
        with:
          name: vector-db
          path: ./vector_db
```

## Configuration Options

### Cron Schedule Examples

```bash
# Every 6 hours
cron: "0 */6 * * *"

# Every 12 hours (twice daily)
cron: "0 0,12 * * *"

# Once daily at midnight UTC
cron: "0 0 * * *"

# Every Monday at 9 AM UTC
cron: "0 9 * * 1"

# Business hours only (9 AM - 5 PM, Mon-Fri)
cron: "0 9-17 * * 1-5"
```

### Environment Variables

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `GITHUB_TOKEN` | GitHub personal access token | Yes | - |
| `GITHUB_REPO` | Repository (owner/repo format) | Yes | - |
| `BRANCH` | Branch to index | No | `main` |
| `VECTOR_DB_PATH` | ChromaDB storage path | No | `/tmp/codebase_vectordb` |
| `USE_API` | Use GitHub API vs git clone | No | `false` |

### Function Parameters (DigitalOcean)

When invoking manually:

```bash
doctl serverless functions invoke codebase-indexer --param '{
  "repo": "facebook/react",
  "branch": "main",
  "use_api": true
}'
```

## Vector Database Persistence

### DigitalOcean Functions

⚠️ **Important**: DigitalOcean Functions use ephemeral storage (`/tmp`). For persistence:

1. **Option A: Use DigitalOcean Spaces**
   ```python
   # Upload to Spaces after indexing
   import boto3
   s3 = boto3.client('s3', endpoint_url='https://nyc3.digitaloceanspaces.com')
   s3.upload_file('/tmp/vector_db', 'my-bucket', 'vector_db')
   ```

2. **Option B: Use External Vector DB**
   - Deploy ChromaDB as a separate service
   - Connect indexer to remote ChromaDB instance

3. **Option C: Hybrid Approach**
   - Run indexer locally/cron
   - Use MCP server to query local vector DB

### Recommended: Separate Vector DB Service

For production, deploy ChromaDB separately:

```yaml
# docker-compose.yml
services:
  chromadb:
    image: ghcr.io/chroma-core/chroma:latest
    ports:
      - "8000:8000"
    volumes:
      - ./chroma_data:/chroma/chroma
    environment:
      - IS_PERSISTENT=TRUE
```

Then update indexer to use remote ChromaDB:

```python
import chromadb
client = chromadb.HttpClient(host="your-chromadb-host", port=8000)
```

## Monitoring & Debugging

### Check Function Logs (DigitalOcean)

```bash
# Recent activations
doctl serverless activations list

# Detailed logs
doctl serverless activations logs --function codebase-indexer --follow

# Get activation result
doctl serverless activations result <activation-id>
```

### Common Issues

**Issue**: "ModuleNotFoundError: No module named 'chromadb'"
**Solution**: Add to `requirements-indexer.txt` and redeploy

**Issue**: "Repository not found or permission denied"
**Solution**: Check GITHUB_TOKEN has `repo` scope

**Issue**: "Memory limit exceeded"
**Solution**: Increase instance size or reduce chunk size

**Issue**: "Function timeout"
**Solution**: Increase timeout or use `use_api: true` for faster download

### Performance Tuning

```python
# Adjust chunk size for memory constraints
chunker = CodeChunker(max_chunk_size=50)  # Smaller chunks

# Skip more file types
SKIP_PATTERNS = [
    r"\.git/",
    r"node_modules/",
    r"test/",
    r"tests/",
    r"docs/",
    # Add more patterns
]

# Batch size for vector store
vector_store.add_chunks(chunks, batch_size=50)  # Smaller batches
```

## Cost Estimation (DigitalOcean)

**Functions Pricing**:
- $0.0000185 per GB-second
- Twice daily execution
- ~30 seconds per run
- ~512 MB memory

**Monthly cost**: ~$0.30 - $1.00 depending on repo size

## Integration with MCP Server

After indexing, the MCP server can query the vector database:

```python
# In codebase_mcp_server.py
vector_store = CodeVectorStore("/path/to/vector/db")
results = vector_store.search("authentication logic", n_results=10)
```

## Troubleshooting

### Test Locally First

```bash
# Set environment
export GITHUB_TOKEN="your-token"
export GITHUB_REPO="your-org/your-repo"

# Run indexer
python indexer.py your-org/your-repo main

# Check output
ls -lh data/codebase_vectordb/
```

### Verify Vector DB

```python
import chromadb
client = chromadb.PersistentClient(path="./data/codebase_vectordb")
collection = client.get_collection("codebase")
print(f"Total chunks: {collection.count()}")
```

### Debug Mode

Add debug logging to `indexer.py`:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## Next Steps

1. **Deploy indexer** using chosen method
2. **Verify first run** completes successfully
3. **Configure MCP server** to use indexed vector DB
4. **Set up monitoring** for failed runs
5. **Optimize** chunk size and skip patterns for your repo

## Security Best Practices

✅ **Use secrets management** for GitHub tokens
✅ **Limit token scope** to read-only repo access
✅ **Monitor activation logs** for suspicious activity
✅ **Rotate tokens** periodically
✅ **Use private repos** when possible
✅ **Don't commit tokens** to git

## Support

For issues or questions:
1. Check DigitalOcean Functions docs
2. Review activation logs
3. Test locally first
4. Check GitHub token permissions
