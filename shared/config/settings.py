"""Runtime settings for MCP servers and the services that share their config.

Moved here from ``mcp_servers/shared_code/config/settings.py``. A parallel
``shared/config/settings.py`` used to exist defining ``SharedDatabaseSettings``
and ``SharedServerSettings``, but nothing ever imported it -- only its own
``__init__``. This file is the implementation services actually run on.

Secrets and connection strings come from the environment (see each service's
``.env.example``). Operator-tunable feature flags are declared separately in
:mod:`shared.config.flag_registry`, which drives the settings UI; this module is
only the typed read side.
"""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# Every settings class reads the same .env with no prefix and ignores unknown
# keys, since one .env serves several services. Expressed as SettingsConfigDict
# rather than the pydantic-v1 `class Config` the original used -- equivalent,
# minus the PydanticDeprecatedSince20 warning on every import.
_CONFIG = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")


class DatabaseSettings(BaseSettings):
    """Database configuration settings."""

    # Chat Database (Supabase) - REST API access
    chat_db_url: Optional[str] = None
    chat_db_service_key: Optional[str] = None
    # For direct PostgreSQL access (optional)
    chat_db_user: Optional[str] = None
    chat_db_password: Optional[str] = None

    # TimescaleDB settings - Username/Password authentication
    timescale_host: str = "localhost"
    timescale_port: int = 5432
    timescale_database: str = "tsdb"
    timescale_user: Optional[str] = None
    timescale_password: Optional[str] = None

    model_config = _CONFIG


class APISettings(BaseSettings):
    """External API configuration settings."""

    # Common API settings
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0

    # Add your external API keys here
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None

    model_config = _CONFIG


class ServerSettings(BaseSettings):
    """MCP server configuration settings."""

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    log_level: str = "INFO"

    # MCP settings
    server_name: str = "mcp-server"
    server_version: str = "1.0.0"

    # Operator identity — shown in chart watermarks and equipment error messages
    organization_name: str = "the operator"

    model_config = _CONFIG


# Global settings instances
db_settings = DatabaseSettings()
api_settings = APISettings()
server_settings = ServerSettings()

__all__ = [
    "APISettings",
    "DatabaseSettings",
    "ServerSettings",
    "api_settings",
    "db_settings",
    "server_settings",
]
