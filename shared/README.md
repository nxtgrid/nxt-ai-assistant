# Shared Code Module

This directory contains shared code used across all Anansi projects: **borg**, **mcp_servers**, and **rag_pipeline**.

## Purpose

To eliminate code duplication and ensure consistency across all projects, common functionality has been extracted into this shared module.

## Structure

```
shared/
├── __init__.py
├── README.md
├── auth/                  # Authentication & authorization
│   ├── __init__.py
│   ├── auth_service.py    # Main auth service (from borg)
│   └── auth_context.py    # MCP auth context (from mcp_servers)
├── database/              # Database connections & clients
│   ├── __init__.py
│   ├── connections.py     # Database connection manager
│   └── supabase_client.py # Enhanced Supabase client
├── config/                # Configuration & settings
│   ├── __init__.py
│   └── settings.py        # Unified settings for all projects
├── utils/                 # Utility functions
│   ├── __init__.py
│   ├── logging.py         # Unified logging setup
│   ├── date_utils.py      # Date/time utilities
│   ├── http_client.py     # HTTP client utilities
│   └── response_formatters.py  # Response formatting
└── models/                # Shared data models
    └── __init__.py
```

## Usage

### From Borg

```python
# Old import
from borg.utils.logging import get_logger
from borg.services.auth_service import AuthService

# New import
from shared.utils import get_logger
from shared.auth import AuthService
```

### From MCP Servers

```python
# Old import
from shared_code.utils.logger import setup_logger
from shared_code.auth import get_auth_context

# New import
from shared.utils import get_logger
from shared.auth import get_auth_context
```

### From RAG Pipeline

```python
# New import (no old code to migrate)
from shared.utils import get_logger
from shared.database import SupabaseClient
```

## Modules

### Auth (`shared.auth`)

Authentication and authorization services:

- **AuthService**: Main authentication service with support for:
  - Direct PostgreSQL connection (readonly role)
  - Supabase client with JWT
  - Legacy service role key
- **UserPermissions**: Pydantic model for user permissions
- **MCPAuthContext**: MCP-specific auth context

Example:
```python
from shared.auth import AuthService, UserPermissions

auth = AuthService()
permissions = await auth.get_user_permissions("user@example.com")
```

### Database (`shared.database`)

Database connection managers and clients:

- **DatabaseManager**: Manages connections to multiple databases (Supabase, TimescaleDB)
- **EnhancedSupabaseClient**: Full-featured Supabase client with conversation history, RAG, etc.
- **SupabaseClient**: Alias for EnhancedSupabaseClient

Example:
```python
from shared.database import DatabaseManager, SupabaseClient

db_manager = DatabaseManager()
await db_manager.initialize_supabase()

# Or use the enhanced client directly
client = SupabaseClient(url=..., key=...)
```

### Config (`shared.config`)

Unified configuration and settings:

- **SharedDatabaseSettings**: Database configuration (Supabase, TimescaleDB)
- **SharedServerSettings**: Server/application configuration
- **db_settings**: Global database settings instance
- **server_settings**: Global server settings instance

Example:
```python
from shared.config import db_settings, server_settings

# Access configuration
print(db_settings.supabase_url)
print(server_settings.log_level)
```

### Utils (`shared.utils`)

Utility functions:

- **logging**: Unified logging setup with loguru
  - `get_logger(module_name, project_name)`: Get a configured logger
  - `setup_logging(...)`: Setup logging with file and console handlers
- **date_utils**: Date and time utilities
- **http_client**: HTTP client wrapper
- **response_formatters**: Response formatting utilities

Example:
```python
from shared.utils import get_logger

logger = get_logger(__name__, project_name="borg")
logger.info("Starting service...")
```

## Migration Guide

### Step 1: Update Dependencies

Ensure the `shared` module is in your Python path. From the project root:

```bash
export PYTHONPATH="/path/to/anansi:$PYTHONPATH"
```

Or add to your IDE's Python path configuration.

### Step 2: Update Imports

**Borg:**
```bash
# Find and replace
borg.utils.logging → shared.utils.logging
borg.services.auth_service → shared.auth.auth_service
```

**MCP Servers:**
```bash
# Find and replace
shared_code.utils → shared.utils
shared_code.auth → shared.auth
shared_code.config → shared.config
shared_code.database → shared.database
```

**RAG Pipeline:**
```bash
# Add new imports
from shared.utils import get_logger
from shared.database import SupabaseClient
```

### Step 3: Remove Duplicate Code

After verifying imports work:

1. Delete `borg/borg/utils/logging.py` (replaced by `shared/utils/logging.py`)
2. Delete `mcp_servers/shared_code/` (replaced by `shared/`)
3. Keep project-specific code in original locations

## Benefits

✅ **Single Source of Truth**: One implementation of auth, logging, database clients
✅ **Easier Maintenance**: Update code in one place, all projects benefit
✅ **Consistency**: Same behavior across all projects
✅ **Reduced Duplication**: ~500 lines of duplicate code eliminated
✅ **Better Testing**: Test shared code once, reuse everywhere

## Environment Variables

All shared modules use the same environment variables. See `.env.example` at the root level.

Key variables:
- `AUTH_DB_DIRECT_CONNECTION`: Use direct PostgreSQL for auth
- `AUTH_DB_HOST`, `AUTH_DB_USER`, `AUTH_DB_PASSWORD`: Direct connection credentials
- `AUTH_SUPABASE_URL`, `AUTH_SUPABASE_ANON_KEY`: Auth Supabase connection
- `CHAT_DB_URL`, `CHAT_DB_SERVICE_KEY`: Chat database
- `LOG_LEVEL`: Logging level (DEBUG, INFO, WARNING, ERROR)

## Development

When adding new shared functionality:

1. **Identify duplication**: Is this code used in 2+ projects?
2. **Extract to shared**: Create in appropriate `shared/` subdirectory
3. **Make generic**: Remove project-specific dependencies
4. **Add to __init__.py**: Export from module's `__init__.py`
5. **Update imports**: Refactor all projects to use shared code
6. **Test**: Ensure all projects still work
7. **Document**: Update this README

## Testing

Test shared modules independently:

```bash
cd /path/to/anansi

# Test imports
python3 -c "from shared.utils import get_logger; print('✅ Utils OK')"
python3 -c "from shared.auth import AuthService; print('✅ Auth OK')"
python3 -c "from shared.config import db_settings; print('✅ Config OK')"
```

## Troubleshooting

### Import Error: "No module named 'shared'"

Solution: Add project root to PYTHONPATH:
```bash
export PYTHONPATH="/path/to/anansi:$PYTHONPATH"
```

### Import Error: "No module named 'borg.models'"

Some modules (like `supabase_client.py`) have optional imports for project-specific models. This is expected and handled with try/except blocks.

### Circular Import Error

Shared modules should not import from `borg`, `mcp_servers`, or `rag_pipeline`. Only the reverse is allowed.

## Version

Current version: 0.1.0

See `__init__.py` for version information.
