"""Configuration settings for MCP servers."""

from typing import Optional

from pydantic_settings import BaseSettings


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

    class Config:
        env_file = ".env"
        env_prefix = ""
        extra = "ignore"  # Ignore extra fields from .env file


class APISettings(BaseSettings):
    """External API configuration settings."""

    # Common API settings
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0

    # Add your external API keys here
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None

    class Config:
        env_file = ".env"
        env_prefix = ""
        extra = "ignore"  # Ignore extra fields from .env file


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

    class Config:
        env_file = ".env"
        env_prefix = ""
        extra = "ignore"  # Ignore extra fields from .env file


# Global settings instances
db_settings = DatabaseSettings()
api_settings = APISettings()
server_settings = ServerSettings()
