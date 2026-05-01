# Codebase MCP Server

A Model Context Protocol (MCP) server for **retrieving and searching** code from your indexed codebase. This server provides read-only access to code analysis and semantic search capabilities.

> **Note**: This server only handles **retrieval**. For **ingestion** (indexing new code), see the separate [rag_pipeline](../../../rag_pipeline) repository.

## Overview

This server provides tools to query and analyze code that has been indexed into Supabase, enabling LLMs to:

- **Search code semantically** using natural language queries
- **Search code patterns** with regex and text matching
- **Understand why the platform behaves a certain way** by finding relevant code
- **Identify recent changes** that might have caused behavioral changes
- **Trace code flow** through the system by finding definitions and usages
- **Analyze file structure** and dependencies
- **Track git history** to see when and why files were changed

## Features

### 1. Semantic Code Search (Vector Search)

- **Search Code Semantic**: Natural language search over indexed code using pgvector
  - Powered by OpenAI embeddings and Supabase pgvector
  - Returns code chunks ranked by semantic similarity
  - Filter by programming language

### 2. Text-based Code Search & Analysis

- **Search Codebase**: Search for code patterns, functions, classes, or text with regex support
- **Find Symbol Definition**: Locate where functions, classes, or variables are defined
- **Find Symbol Usage**: Find all places where a symbol is used
- **Analyze File**: Get file structure, imports, classes, functions, and content preview
- **Git History**: Track commit history for specific files

### 3. Production Change Tracking

- **Get Recent PRs**: Retrieve PRs merged to production main branch
- **PR File Changes**: Get detailed file changes and diffs for specific PRs

### 4. Vector Database Statistics

- **Get Vector DB Stats**: See how many code chunks are indexed and language distribution

## Configuration

Add these environment variables to your `.env` file:

```bash
# Chat Database (for vector search)
CHAT_DB_URL=https://your-project.supabase.co
CHAT_DB_SERVICE_KEY=your-service-role-key

# OpenAI (for semantic search queries)
OPENAI_API_KEY=sk-...

# Local Codebase Analysis
CODEBASE_PATH=/path/to/your/codebase

# GitHub (for PR tracking)
GITHUB_TOKEN=your-github-personal-access-token
GITHUB_REPO=owner/repo
```

### Configuration Details

- **CHAT_DB_URL**: Your chat database URL (required for vector search)
- **CHAT_DB_SERVICE_KEY**: Chat database service role key (required for vector search)
- **OPENAI_API_KEY**: OpenAI API key for generating query embeddings (required for semantic search)
- **CODEBASE_PATH**: Absolute path to the codebase you want to analyze locally
- **GITHUB_TOKEN**: (Optional) GitHub personal access token for PR analysis
  - Create at: https://github.com/settings/tokens
  - Required scopes: `repo` (for private repos) or `public_repo` (for public repos)
- **GITHUB_REPO**: (Optional) Repository in format `owner/repo` (e.g., `facebook/react`)

## Available Tools

### search_code_semantic

**Search indexed code using natural language/semantic similarity.**

Uses OpenAI embeddings and Supabase pgvector to find code chunks that are semantically related to your query.

**Parameters:**
- `query` (required): Natural language query (e.g., "authentication logic", "payment processing")
- `max_results` (optional): Number of results (default: 10)
- `language_filter` (optional): Filter by language (e.g., "python", "typescript")

**Example:**
```json
{
  "query": "how user authentication works",
  "max_results": 10,
  "language_filter": "python"
}
```

**Returns:** Code chunks ranked by semantic similarity with file paths, line numbers, and similarity scores.

---

### get_vector_db_stats

**Get statistics about the indexed codebase.**

Returns total code chunks indexed, language distribution, and storage information.

**No parameters required.**

**Example:**
```json
{}
```

---

### search_codebase

**Search for specific code patterns, functions, classes, or text in local codebase.**

Uses grep/regex to search through files in your local codebase directory.

**Parameters:**
- `query` (required): Search query (supports regex)
- `file_pattern` (optional): File pattern like `*.py`, `*.ts`
- `context_lines` (optional): Number of context lines (default: 3)
- `max_results` (optional): Maximum results (default: 50)

**Example:**
```json
{
  "query": "def process_payment",
  "file_pattern": "*.py",
  "context_lines": 5
}
```

### find_symbol_definition

Find where a function, class, or variable is defined.

**Parameters:**
- `symbol_name` (required): Name of the symbol
- `symbol_type` (optional): Type (`class`, `function`, `variable`)

**Example:**
```json
{
  "symbol_name": "UserAuthentication",
  "symbol_type": "class"
}
```

### find_symbol_usage

Find all places where a symbol is used.

**Parameters:**
- `symbol_name` (required): Name of the symbol
- `max_results` (optional): Maximum usage examples (default: 30)

**Example:**
```json
{
  "symbol_name": "process_payment",
  "max_results": 20
}
```

### analyze_file

Analyze a file's structure and content.

**Parameters:**
- `file_path` (required): Relative path from codebase root

**Example:**
```json
{
  "file_path": "src/utils/payment.py"
}
```

### get_file_git_history

Get git commit history for a file.

**Parameters:**
- `file_path` (required): Relative path from codebase root
- `max_commits` (optional): Maximum commits (default: 10)

**Example:**
```json
{
  "file_path": "src/utils/payment.py",
  "max_commits": 5
}
```

### get_recent_production_prs

Get recent PRs merged to production.

**Parameters:**
- `branch` (optional): Target branch (default: `main`)
- `days` (optional): Days to look back (default: 30)
- `max_prs` (optional): Maximum PRs (default: 20)

**Example:**
```json
{
  "branch": "main",
  "days": 14,
  "max_prs": 10
}
```

### get_pr_file_changes

Get detailed file changes for a specific PR.

**Parameters:**
- `pr_number` (required): PR number from GitHub

**Example:**
```json
{
  "pr_number": 123
}
```

## Use Cases

### 1. Debugging Behavioral Changes

**Scenario**: "The payment processing started failing yesterday"

**Approach**:
1. Use `get_recent_production_prs` to see recent changes
2. Use `get_pr_file_changes` to inspect relevant PRs
3. Use `search_codebase` to find payment processing code
4. Use `get_file_git_history` to see recent changes to payment files

### 2. Understanding Feature Implementation

**Scenario**: "How does user authentication work?"

**Approach**:
1. Use `find_symbol_definition` to find the authentication class/function
2. Use `analyze_file` to understand the file structure
3. Use `find_symbol_usage` to see how it's called throughout the app

### 3. Impact Analysis

**Scenario**: "What would break if I change this function?"

**Approach**:
1. Use `find_symbol_usage` to find all usages
2. Use `analyze_file` on each file to understand context
3. Use `search_codebase` to find related patterns

## Supported Languages

- Python (`.py`)
- TypeScript (`.ts`, `.tsx`)
- JavaScript (`.js`, `.jsx`)

The server can search any text file, but detailed structure analysis (imports, classes, functions) is optimized for the above languages.

## Integration

### Claude Desktop

Add to your Claude Desktop config:

```json
{
  "mcpServers": {
    "codebase-server": {
      "command": "/path/to/venv/bin/python3",
      "args": ["/path/to/servers/codebase_server/codebase_mcp_server.py"],
      "env": {
        "PYTHONPATH": "/path/to/mcp-servers",
        "CODEBASE_PATH": "/path/to/your/codebase",
        "GITHUB_TOKEN": "your-token",
        "GITHUB_REPO": "owner/repo"
      }
    }
  }
}
```

### MCP Launcher

The server is automatically discovered by `mcp_launcher.py`:

```bash
# List all tools
python mcp_launcher.py --info codebase_server

# Use via API
python mcp_launcher.py --api --port 8000
```

## Limitations

- **Search timeout**: Searches timeout after 30 seconds
- **File size**: Very large files may be truncated in previews
- **GitHub rate limits**: API calls are subject to GitHub rate limits
- **Local only**: Currently analyzes local codebases only (not remote repos)

## Security Notes

- GitHub tokens should have minimal required scopes
- Codebase path should point to trusted code only
- PR analysis requires appropriate repository access permissions
- All file operations are read-only (no write/modify capabilities)

## Dependencies

Required Python packages (already in `requirements.txt`):
- `mcp` - Model Context Protocol SDK
- `python-dotenv` - Environment variable management
- `aiohttp` - Async HTTP for GitHub API calls

## Troubleshooting

### "Codebase path does not exist"

Ensure `CODEBASE_PATH` points to a valid directory with an absolute path.

### "GitHub API error: 401"

Check that your `GITHUB_TOKEN` is valid and has appropriate scopes.

### "GitHub integration not configured"

Set both `GITHUB_TOKEN` and `GITHUB_REPO` environment variables for PR features.

### Search returns no results

- Verify the codebase path is correct
- Try simpler search patterns
- Check file permissions

## Semantic Search Setup

The codebase server provides semantic code search using Supabase's pgvector extension. To enable this:

### Prerequisites

1. **Supabase Project** with pgvector extension and `rag_documents` table
2. **Code must be indexed** - See the [rag_pipeline](../../../rag_pipeline) repository for ingestion
3. **Environment variables** configured (see Configuration section above)

### Quick Setup

1. Ensure code is indexed (see [rag_pipeline/ingestion/codebase_indexer.py](../../../rag_pipeline/ingestion/codebase_indexer.py))
2. Run SQL functions from [rag_pipeline/database/supabase_functions.sql](../../../rag_pipeline/database/supabase_functions.sql)
3. Configure environment variables in your `.env`
4. Restart the MCP server

### Verify Setup

```sql
-- In Supabase SQL Editor
SELECT count(*), source_metadata->>'language' as language
FROM rag_documents
WHERE source_type = 'codebase'
GROUP BY language;
```

You should see indexed code chunks grouped by programming language.

### Indexing New Code

To index a new repository or update existing code, use the **rag_pipeline** repository:

```bash
cd ../../../rag_pipeline
python ingestion/codebase_indexer.py owner/repo main
```

See the [rag_pipeline README](../../../rag_pipeline/README.md) for detailed ingestion documentation.

### Troubleshooting

**"Vector search not initialized"**
- Check CHAT_DB_URL and CHAT_DB_SERVICE_KEY are set in `.env`
- Verify chat database connection is working

**"No codebase documents found"**
- Code hasn't been indexed yet
- Run the indexer from rag_pipeline: `python ingestion/codebase_indexer.py owner/repo`
- Verify: `SELECT count(*) FROM rag_documents WHERE source_type = 'codebase'`

**"Using basic query without semantic search"**
- SQL functions not created
- Run SQL from `rag_pipeline/database/supabase_functions.sql` in Supabase SQL editor

---

## Future Enhancements

Potential future features:
- Multi-language AST parsing for better structure analysis
- Dependency graph visualization
- Code complexity metrics
- Test coverage analysis
- Integration with CI/CD systems
