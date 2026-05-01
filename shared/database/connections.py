"""Database connection utilities."""

from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from supabase import Client, create_client

from shared.config.settings import db_settings
from shared.utils.logging import get_logger

logger = get_logger("database", project_name="shared")


class DatabaseManager:
    """Manages database connections for different databases."""

    def __init__(self):
        self.supabase_client: Optional[Client] = None
        self.timescale_engine = None
        self.timescale_async_engine = None
        self.timescale_session = None

    async def initialize_chat_db(self) -> Client:
        """Initialize Supabase client using service role key for chat database."""
        try:
            import os

            # Get service role key from settings or environment
            # Support both new (CHAT_DB_*) and legacy (SUPABASE_*) env var names
            service_key = (
                db_settings.chat_db_service_key
                or os.getenv("CHAT_DB_SERVICE_KEY", "")
                or os.getenv("SUPABASE_KEY", "")  # Legacy fallback
            )
            chat_db_url = (
                db_settings.chat_db_url
                or os.getenv("CHAT_DB_URL", "")
                or os.getenv("SUPABASE_URL", "")  # Legacy fallback
            )

            if not service_key:
                logger.warning("CHAT_DB_SERVICE_KEY not configured")
                return None

            self.supabase_client = create_client(chat_db_url, service_key)
            logger.info("Chat database client initialized with service role key")

            return self.supabase_client
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client: {e}")
            return None

    def initialize_timescale(self):
        """Initialize TimescaleDB connections."""
        try:
            # Synchronous engine
            self.timescale_engine = create_engine(
                db_settings.timescale_url, echo=False, pool_pre_ping=True, pool_recycle=300
            )

            # Asynchronous engine
            self.timescale_async_engine = create_async_engine(
                db_settings.timescale_url.replace("postgresql://", "postgresql+asyncpg://"),
                echo=False,
                pool_pre_ping=True,
                pool_recycle=300,
            )

            # Session factory
            self.timescale_session = sessionmaker(
                bind=self.timescale_async_engine, class_=AsyncSession, expire_on_commit=False
            )

            logger.info("TimescaleDB connections initialized")
        except Exception as e:
            logger.error(f"Failed to initialize TimescaleDB: {e}")
            raise

    async def get_timescale_session(self) -> AsyncSession:
        """Get TimescaleDB async session."""
        if not self.timescale_session:
            self.initialize_timescale()
        return self.timescale_session()

    async def close_connections(self):
        """Close all database connections."""
        if self.timescale_async_engine:
            await self.timescale_async_engine.dispose()
        if self.timescale_engine:
            self.timescale_engine.dispose()
        logger.info("Database connections closed")


# Global database manager instance
db_manager = DatabaseManager()
