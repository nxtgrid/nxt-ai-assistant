"""Database connection utilities."""

from typing import Optional

import asyncpg
from shared_code.config.settings import db_settings
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from supabase import Client

from shared.utils.logging import get_logger

logger = get_logger("database")


class DatabaseManager:
    """Manages database connections for different databases."""

    def __init__(self):
        self.supabase_client: Optional[Client] = None
        self.postgres_pool = None  # Direct PostgreSQL connection pool
        self.timescale_engine = None
        self.timescale_async_engine = None
        self.timescale_session = None

    async def initialize_postgres_pool(self):
        """Initialize direct PostgreSQL connection pool using asyncpg"""
        if self.postgres_pool is not None:
            return self.postgres_pool

        try:
            import os
            from urllib.parse import urlparse

            # Support both new (CHAT_DB_*) and legacy (SUPABASE_*) env var names
            chat_db_url = (
                getattr(db_settings, "chat_db_url", None)
                or os.getenv("CHAT_DB_URL", "")
                or os.getenv("SUPABASE_URL", "")  # Legacy fallback
            )
            chat_db_user = (
                getattr(db_settings, "chat_db_user", None)
                or os.getenv("CHAT_DB_USER", "")
                or os.getenv("SUPABASE_USER", "")  # Legacy fallback
            )
            chat_db_password = (
                getattr(db_settings, "chat_db_password", None)
                or os.getenv("CHAT_DB_PASSWORD", "")
                or os.getenv("SUPABASE_PASSWORD", "")  # Legacy fallback
            )

            # Remove project suffix from username if present (e.g., "user.project" -> "user")
            # Supabase pgBouncer may require just the role name without project suffix
            if chat_db_user and "." in chat_db_user:
                db_user = chat_db_user.split(".")[0]
                logger.info(f"Using database username '{db_user}' (stripped from '{chat_db_user}')")
            else:
                db_user = chat_db_user

            if not chat_db_url or not chat_db_user or not chat_db_password:
                logger.error(
                    "CHAT_DB_URL, CHAT_DB_USER, and CHAT_DB_PASSWORD must be configured for direct PostgreSQL access"
                )
                return None

            # Parse URL to get host
            # Format: https://project.supabase.co -> db.project.supabase.co
            parsed = urlparse(chat_db_url)
            hostname = parsed.hostname
            if hostname:
                # Convert https://xyz.supabase.co to db.xyz.supabase.co:5432
                db_host = f"db.{hostname.replace('https://', '').replace('http://', '')}"
            else:
                logger.error(f"Could not parse Chat DB URL: {chat_db_url}")
                return None

            logger.info(f"Creating PostgreSQL connection pool to {db_host} as user '{db_user}'")

            # Create asyncpg connection pool
            # Supabase uses port 6543 for connection pooling (pgBouncer)
            self.postgres_pool = await asyncpg.create_pool(
                host=db_host,
                port=6543,
                database="postgres",
                user=db_user,
                password=chat_db_password,
                ssl="require",
                min_size=1,
                max_size=10,
                command_timeout=30,
            )

            logger.info("PostgreSQL connection pool created successfully")
            return self.postgres_pool

        except Exception as e:
            logger.error(f"Failed to create PostgreSQL connection pool: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return None

    async def initialize_chat_db(self) -> Client:
        """
        Initialize Supabase client using service role key for chat database.
        """
        try:
            import os

            from supabase import create_client

            # Support both new (CHAT_DB_*) and legacy (SUPABASE_*) env var names
            chat_db_url = (
                getattr(db_settings, "chat_db_url", None)
                or os.getenv("CHAT_DB_URL", "")
                or os.getenv("SUPABASE_URL", "")  # Legacy fallback
            )
            service_key = os.getenv("CHAT_DB_SERVICE_KEY", "") or os.getenv(
                "SUPABASE_KEY", ""
            )  # Legacy fallback

            if not chat_db_url:
                logger.error("CHAT_DB_URL not configured")
                return None

            if not service_key:
                logger.error("CHAT_DB_SERVICE_KEY not configured")
                return None

            self.supabase_client = create_client(chat_db_url, service_key)
            logger.info("Chat database client initialized with service role key")

            return self.supabase_client

        except Exception as e:
            logger.error(f"Failed to initialize chat database: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return None

    # Backward compatibility alias
    async def initialize_supabase(self) -> Client:
        """Alias for initialize_chat_db for backward compatibility."""
        return await self.initialize_chat_db()

    async def query_postgres(self, query: str, *args):
        """
        Execute a query using direct PostgreSQL connection

        Args:
            query: SQL query with $1, $2, etc. placeholders
            *args: Query parameters

        Returns:
            Query results as list of records
        """
        if not self.postgres_pool:
            await self.initialize_postgres_pool()

        if not self.postgres_pool:
            raise Exception("PostgreSQL connection pool not available")

        async with self.postgres_pool.acquire() as conn:
            return await conn.fetch(query, *args)

    def initialize_timescale(self):
        """Initialize TimescaleDB connections using username/password authentication."""
        try:
            import os

            # Get credentials from settings or environment
            timescale_host = getattr(db_settings, "timescale_host", None) or os.getenv(
                "TIMESCALE_HOST", "localhost"
            )
            timescale_port = getattr(db_settings, "timescale_port", None) or os.getenv(
                "TIMESCALE_PORT", "5432"
            )
            timescale_database = getattr(db_settings, "timescale_database", None) or os.getenv(
                "TIMESCALE_DATABASE", "tsdb"
            )
            timescale_user = getattr(db_settings, "timescale_user", None) or os.getenv(
                "TIMESCALE_USER"
            )
            timescale_password = getattr(db_settings, "timescale_password", None) or os.getenv(
                "TIMESCALE_PASSWORD"
            )

            if not timescale_user or not timescale_password:
                logger.error("TIMESCALE_USER and TIMESCALE_PASSWORD must be configured")
                raise ValueError("TimescaleDB credentials not configured")

            # Build connection URL
            connection_url = f"postgresql://{timescale_user}:{timescale_password}@{timescale_host}:{timescale_port}/{timescale_database}"
            async_connection_url = f"postgresql+asyncpg://{timescale_user}:{timescale_password}@{timescale_host}:{timescale_port}/{timescale_database}"

            logger.info(
                f"Connecting to TimescaleDB at {timescale_host}:{timescale_port}/{timescale_database} as user '{timescale_user}'"
            )

            # Synchronous engine
            self.timescale_engine = create_engine(
                connection_url, echo=False, pool_pre_ping=True, pool_recycle=300
            )

            # Asynchronous engine
            self.timescale_async_engine = create_async_engine(
                async_connection_url, echo=False, pool_pre_ping=True, pool_recycle=300
            )

            # Session factory
            self.timescale_session = sessionmaker(
                bind=self.timescale_async_engine, class_=AsyncSession, expire_on_commit=False
            )

            logger.info("TimescaleDB connections initialized successfully")
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
