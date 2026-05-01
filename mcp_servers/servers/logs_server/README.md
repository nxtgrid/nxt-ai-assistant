# Logs MCP Server

An intelligent log analysis server that fetches logs from Grafana Loki, stores them in a vector database, and provides semantic search and chunked analysis for LLMs.

## Overview

This server combines the power of:
- **Grafana Loki** - Log aggregation and querying
- **ChromaDB** - Vector database for semantic search
- **Sentence Transformers** - Text embeddings for intelligent log matching
- **Intelligent Chunking** - Smart log grouping for LLM consumption

## Key Features

### 1. Loki Integration
- Fetch logs from the past 12 hours (or configurable timeframe)
- Support for LogQL queries and filters
- Automatic label discovery
- Time range queries

### 2. Vector Search
- Semantic log search (find conceptually similar logs, not just keywords)
- Automatic embedding generation
- Persistent storage with ChromaDB
- Filter by log level (error, warning, info, debug)

### 3. Intelligent Chunking
- Automatically group related logs together
- Format chunks for optimal LLM consumption
- Include contextual summaries
- Error-aware chunking (errors create new chunks)

### 4. Pattern Analysis
- Identify recurring log patterns
- Group similar errors
- Statistical analysis of log distribution
- Anomaly detection

## Configuration

Add these environment variables to your `.env` file:

```bash
# Logs Server Configuration
LOKI_URL=http://localhost:3100
LOKI_USERNAME=                    # Optional
LOKI_PASSWORD=                    # Optional
VECTOR_DB_PATH=./data/logs_vectordb
DEFAULT_LOG_HOURS=12
```

### Configuration Details

- **LOKI_URL**: Grafana Loki endpoint URL
- **LOKI_USERNAME**: (Optional) Basic auth username
- **LOKI_PASSWORD**: (Optional) Basic auth password
- **VECTOR_DB_PATH**: Directory for ChromaDB storage
- **DEFAULT_LOG_HOURS**: Default hours to look back for logs

## Available Tools

### fetch_logs_from_loki

Fetch logs from Loki and optionally store in vector database.

**Parameters:**
- `query` (optional): LogQL query (default: all logs)
- `hours` (optional): Hours to look back (default: 12)
- `limit` (optional): Maximum log entries (default: 5000)
- `store_in_vector_db` (optional): Store in vector DB (default: true)

**Example:**
```json
{
  "query": "{job=\"mcp_servers\", level=\"error\"}",
  "hours": 6,
  "limit": 1000
}
```

**LogQL Query Examples:**
```
{job="api"}                          # All logs from api job
{app="payments", level="error"}      # Error logs from payments app
{job=~".+"}                         # All logs from all jobs
{namespace="production"} |= "timeout" # Logs containing "timeout"
```

### search_logs_semantic

Search logs using natural language/semantic search.

**Parameters:**
- `query` (required): Natural language search query
- `max_results` (optional): Maximum results (default: 20)
- `level_filter` (optional): Filter by level (error, warning, info, debug)
- `chunk_for_llm` (optional): Format for LLM (default: true)

**Example:**
```json
{
  "query": "database connection problems",
  "level_filter": "error",
  "max_results": 30
}
```

**Query Examples:**
- "database connection problems"
- "authentication failures"
- "slow API responses"
- "payment processing errors"
- "memory leaks"

### get_error_logs

Get all error logs, chunked and formatted for analysis.

**Parameters:**
- `max_results` (optional): Maximum errors (default: 50)
- `chunk_size` (optional): Logs per chunk (default: 10)

**Example:**
```json
{
  "max_results": 100,
  "chunk_size": 15
}
```

### analyze_log_patterns

Analyze logs to identify recurring patterns and anomalies.

**Parameters:**
- `hours` (optional): Hours to analyze (default: 12)
- `min_occurrences` (optional): Minimum pattern occurrences (default: 3)

**Example:**
```json
{
  "hours": 24,
  "min_occurrences": 5
}
```

### get_log_statistics

Get statistics about stored logs.

**Parameters:** None

**Returns:**
- Total log count
- Distribution by level
- Collection information

### get_logs_by_timeframe

Get logs for a specific timeframe.

**Parameters:**
- `start_time` (required): Start time (ISO format)
- `end_time` (required): End time (ISO format)
- `query` (optional): LogQL query
- `chunk_size` (optional): Logs per chunk (default: 10)

**Example:**
```json
{
  "start_time": "2024-01-15T10:00:00",
  "end_time": "2024-01-15T12:00:00",
  "query": "{job=\"api\"}",
  "chunk_size": 20
}
```

### get_loki_labels

Get available Loki labels and their values.

**Parameters:**
- `label` (required): Label name (e.g., "job", "app", "level")

**Example:**
```json
{
  "label": "job"
}
```

## Use Cases

### 1. Debugging Production Issues

**Scenario**: "API is returning 500 errors"

**Approach**:
```
1. fetch_logs_from_loki({query: "{job=\"api\", level=\"error\"}", hours: 2})
2. search_logs_semantic({query: "500 internal server error"})
3. analyze_log_patterns({hours: 2})
```

### 2. Understanding System Behavior

**Scenario**: "Why is the payment system slow?"

**Approach**:
```
1. search_logs_semantic({query: "payment processing slow timeout"})
2. get_logs_by_timeframe({start: "10:00:00", end: "11:00:00"})
3. analyze_log_patterns({min_occurrences: 5})
```

### 3. Post-Incident Analysis

**Scenario**: "What happened during the outage?"

**Approach**:
```
1. get_logs_by_timeframe({start: outage_start, end: outage_end})
2. get_error_logs({max_results: 200})
3. analyze_log_patterns({hours: 3})
```

### 4. Proactive Monitoring

**Scenario**: "Are there any concerning patterns?"

**Approach**:
```
1. fetch_logs_from_loki({hours: 12})
2. analyze_log_patterns({min_occurrences: 10})
3. search_logs_semantic({query: "connection refused timeout"})
```

## How It Works

### 1. Log Retrieval
```
Loki → Query Logs → Parse Results → Extract Log Levels
```

### 2. Vector Storage
```
Logs → Generate Embeddings → Store in ChromaDB → Index for Search
```

### 3. Semantic Search
```
Query → Generate Embedding → Vector Similarity → Return Matches
```

### 4. Chunking
```
Logs → Group by Error/Size → Format with Context → Return Chunks
```

## Technical Details

### Log Entry Structure
```python
{
  "timestamp": "2024-01-15T10:30:00",
  "message": "Database connection timeout",
  "labels": {"job": "api", "level": "error"},
  "level": "error"
}
```

### Vector Database Schema
- **ID**: MD5 hash of timestamp + message + labels
- **Document**: Formatted log for embedding
- **Metadata**: Timestamp, level, labels
- **Embedding**: Auto-generated by ChromaDB

### Chunking Algorithm
1. Start new chunk
2. Add logs until max size OR error encountered
3. Errors trigger new chunk creation
4. Format with summary header

## Performance

- **Loki queries**: 30 second timeout
- **Vector search**: Sub-second response
- **Chunk processing**: ~100 logs/second
- **Storage**: ~1KB per log entry (with embedding)

## Dependencies

Required packages (in `requirements.txt`):
- `chromadb>=0.4.0` - Vector database
- `sentence-transformers>=2.2.0` - Text embeddings
- `aiohttp>=3.9.0` - Async HTTP for Loki
- `mcp` - Model Context Protocol SDK

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure environment:
```bash
cp env.example .env
# Edit .env with your Loki configuration
```

3. Test connection:
```bash
python mcp_launcher.py --info logs_server
```

## Limitations

- **Loki query timeout**: 30 seconds
- **Vector DB initialization**: ~1-2 seconds on first run
- **Embedding generation**: Requires sentence-transformers model (~400MB)
- **Storage**: ChromaDB requires disk space for vectors
- **Memory**: Embedding model requires ~500MB RAM

## Troubleshooting

### "Loki query failed: 401"
Check LOKI_USERNAME and LOKI_PASSWORD configuration.

### "ChromaDB not available"
Install: `pip install chromadb sentence-transformers`

### "Vector store not initialized"
Check VECTOR_DB_PATH is writable and has sufficient disk space.

### "No logs found"
- Verify Loki URL is correct
- Check LogQL query syntax
- Ensure time range includes logs

### Slow semantic search
- Reduce `max_results`
- Use more specific queries
- Add level filters

## Advanced Usage

### Custom LogQL Queries

**Filter by multiple labels:**
```
{job="api", environment="production", level!="debug"}
```

**Line filtering:**
```
{job="api"} |= "error" != "timeout"
```

**Regex matching:**
```
{job="api"} |~ "error|exception|fail"
```

### Batch Operations

Fetch and analyze in one workflow:
```
1. fetch_logs_from_loki({hours: 24, store_in_vector_db: true})
2. analyze_log_patterns({hours: 24})
3. search_logs_semantic({query: "critical errors"})
```

### Time-based Analysis

Compare different time periods:
```
1. get_logs_by_timeframe({start: "yesterday 10:00", end: "yesterday 11:00"})
2. get_logs_by_timeframe({start: "today 10:00", end: "today 11:00"})
```

## Security Notes

- Loki credentials stored in environment variables
- Vector DB stored locally (not shared)
- No log data sent to external services (embeddings generated locally)
- Read-only access to Loki (no write operations)

## Future Enhancements

Potential features:
- Real-time log streaming
- Alert generation based on patterns
- ML-based anomaly detection
- Log correlation across services
- Custom embedding models
- Multi-Loki instance support
- Log archival and compression

## Integration Examples

### Claude Desktop
```json
{
  "mcpServers": {
    "logs-server": {
      "command": "/path/to/venv/bin/python3",
      "args": ["/path/to/servers/logs_server/logs_mcp_server.py"],
      "env": {
        "PYTHONPATH": "/path/to/mcp-servers",
        "LOKI_URL": "http://localhost:3100",
        "VECTOR_DB_PATH": "./data/logs_vectordb"
      }
    }
  }
}
```

### MCP Launcher
```bash
# Discover server
python mcp_launcher.py --info logs_server

# Use via API
python mcp_launcher.py --api --port 8000
curl http://localhost:8000/servers/logs_server/tools
```
