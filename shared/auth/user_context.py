#!/usr/bin/env python3
"""
User Context Management for MCP Servers

Provides role-aware and user-aware functionality across all MCP servers.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class UserRole(Enum):
    """User role definitions"""

    ADMIN = "admin"
    MANAGER = "manager"
    ANALYST = "analyst"
    VIEWER = "viewer"
    GUEST = "guest"


class ActionType(Enum):
    """Action type definitions"""

    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    ADMIN = "admin"


@dataclass
class UserContext:
    """User context information passed to MCP servers"""

    user_id: str
    username: str
    role: UserRole
    permissions: Set[str]
    grid_access: List[str] = None  # For TimescaleDB grid access
    actions_enabled: bool = True  # Global action flag

    def __post_init__(self):
        if self.grid_access is None:
            self.grid_access = []

    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission"""
        return permission in self.permissions or self.role == UserRole.ADMIN

    def can_perform_action(self, action: ActionType) -> bool:
        """Check if user can perform an action type"""
        if not self.actions_enabled and action in [
            ActionType.WRITE,
            ActionType.DELETE,
            ActionType.ADMIN,
        ]:
            return False

        if self.role == UserRole.ADMIN:
            return True

        # Role-based action permissions
        role_permissions = {
            UserRole.MANAGER: {ActionType.READ, ActionType.WRITE},
            UserRole.ANALYST: {ActionType.READ, ActionType.WRITE},
            UserRole.VIEWER: {ActionType.READ},
            UserRole.GUEST: {ActionType.READ},
        }

        return action in role_permissions.get(self.role, set())

    def has_grid_access(self, grid_id: str) -> bool:
        """Check if user has access to a specific grid"""
        return self.role == UserRole.ADMIN or grid_id in self.grid_access

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role.value,
            "permissions": list(self.permissions),
            "grid_access": self.grid_access,
            "actions_enabled": self.actions_enabled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserContext":
        """Create from dictionary"""
        return cls(
            user_id=data["user_id"],
            username=data["username"],
            role=UserRole(data["role"]),
            permissions=set(data.get("permissions", [])),
            grid_access=data.get("grid_access", []),
            actions_enabled=data.get("actions_enabled", True),
        )


class UserContextManager:
    """Manages user context and permissions"""

    def __init__(self):
        self.user_cache: Dict[str, UserContext] = {}

    async def get_user_context(self, user_id: str, **kwargs) -> Optional[UserContext]:
        """Get user context by user ID"""
        if user_id in self.user_cache:
            return self.user_cache[user_id]

        # In a real implementation, this would query your user database
        # For now, return a default context
        return await self._load_user_context(user_id, **kwargs)

    async def _load_user_context(self, user_id: str, **kwargs) -> UserContext:
        """Load user context from database/external source"""
        # Default implementation - in practice, this would query your user system

        # Example user contexts for testing
        default_users = {
            "admin": UserContext(
                user_id="admin",
                username="admin@company.com",
                role=UserRole.ADMIN,
                permissions={"*"},
                grid_access=["*"],
                actions_enabled=kwargs.get("actions_enabled", True),
            ),
            "manager": UserContext(
                user_id="manager",
                username="manager@company.com",
                role=UserRole.MANAGER,
                permissions={
                    "supabase.read",
                    "supabase.write",
                    "jira.read",
                    "jira.write",
                    "timescale.read",
                    "timescale.write",
                },
                grid_access=["grid1", "grid2"],
                actions_enabled=kwargs.get("actions_enabled", True),
            ),
            "analyst": UserContext(
                user_id="analyst",
                username="analyst@company.com",
                role=UserRole.ANALYST,
                permissions={"supabase.read", "jira.read", "timescale.read"},
                grid_access=["grid1"],
                actions_enabled=kwargs.get("actions_enabled", True),
            ),
            "viewer": UserContext(
                user_id="viewer",
                username="viewer@company.com",
                role=UserRole.VIEWER,
                permissions={"supabase.read", "jira.read", "timescale.read"},
                grid_access=["grid1"],
                actions_enabled=kwargs.get("actions_enabled", True),
            ),
        }

        if user_id in default_users:
            context = default_users[user_id]
            context.actions_enabled = kwargs.get("actions_enabled", True)
            self.user_cache[user_id] = context
            return context

        # Default guest context
        context = UserContext(
            user_id=user_id,
            username=f"user_{user_id}",
            role=UserRole.GUEST,
            permissions={"supabase.read"},
            grid_access=[],
            actions_enabled=kwargs.get("actions_enabled", True),
        )
        self.user_cache[user_id] = context
        return context

    def clear_cache(self):
        """Clear user context cache"""
        self.user_cache.clear()


# Global user context manager
user_context_manager = UserContextManager()
