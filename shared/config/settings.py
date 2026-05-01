"""Unified configuration settings for all Anansi projects."""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class SharedDatabaseSettings(BaseSettings):
    """Shared database configuration."""

    # Auth Database - Direct PostgreSQL Connection (Read-Only)
    auth_db_host: Optional[str] = None
    auth_db_port: int = 6543  # Default to pooler port
    auth_db_name: str = "postgres"
    auth_db_user: Optional[str] = None
    auth_db_password: Optional[str] = None

    # Chat Database - Supabase (Service Role Key)
    chat_db_url: Optional[str] = None
    chat_db_service_key: Optional[str] = None

    # TimescaleDB
    timescale_host: Optional[str] = None
    timescale_port: int = 5432
    timescale_database: Optional[str] = None
    timescale_user: Optional[str] = None
    timescale_password: Optional[str] = None

    @property
    def timescale_url(self) -> Optional[str]:
        """Build TimescaleDB connection URL."""
        if all(
            [
                self.timescale_host,
                self.timescale_user,
                self.timescale_password,
                self.timescale_database,
            ]
        ):
            return f"postgresql://{self.timescale_user}:{self.timescale_password}@{self.timescale_host}:{self.timescale_port}/{self.timescale_database}"
        return None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="allow",
    )


class SharedServerSettings(BaseSettings):
    """Shared server/application settings."""

    # Logging
    log_level: str = "INFO"

    # Environment
    environment: str = "development"
    debug: bool = False

    # API Keys
    google_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None

    # Bridge/Service URLs
    bridge_url: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="allow",
    )


# Global instances
db_settings = SharedDatabaseSettings()
server_settings = SharedServerSettings()


__all__ = ["SharedDatabaseSettings", "SharedServerSettings", "db_settings", "server_settings"]
