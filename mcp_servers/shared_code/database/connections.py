"""Database connection utilities."""

from typing import Optional

from supabase import Client

from shared.config.settings import db_settings
from shared.utils.logging import get_logger

logger = get_logger("database")


class DatabaseManager:
    """Manages database connections for different databases."""

    def __init__(self):
        self.supabase_client: Optional[Client] = None

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


# Global database manager instance
db_manager = DatabaseManager()
