"""Database connection and client modules."""

from typing import Any, Optional

EnhancedSupabaseClient: Optional[Any] = None
SupabaseClient: Optional[Any] = None
DatabaseManager: Optional[Any] = None
db_manager: Optional[Any] = None

try:
    from shared.database.supabase_client import EnhancedSupabaseClient, SupabaseClient
except ImportError:
    pass

try:
    from shared.database.connections import DatabaseManager, db_manager
except ImportError:
    pass


__all__ = [
    "EnhancedSupabaseClient",
    "SupabaseClient",
    "DatabaseManager",
    "db_manager",
]
