"""
Shared authentication module for all MCP servers.

This module provides common authentication and authorization functionality
that all MCP servers should use as their first step before executing any tool.

This is now a lightweight wrapper around the shared AuthService for MCP compatibility.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from shared.auth.auth_service import UserPermissions as AuthServiceUserPermissions
from shared.auth.auth_service import get_auth_service


# Re-export UserPermissions from auth_service for backwards compatibility
class UserPermissions(AuthServiceUserPermissions):
    """User permissions for filtering queries (extends AuthService UserPermissions)."""

    def has_grid_access(self, grid_id: str) -> bool:
        """Check if user has access to a specific grid."""
        return self.is_admin or grid_id in self.grid_ids

    def has_meter_access(self, meter_id: str) -> bool:
        """Check if user has access to a specific meter."""
        return self.is_admin or meter_id in self.meter_ids

    def has_organization_access(self, org_id: str) -> bool:
        """Check if user has access to a specific organization."""
        return self.is_admin or org_id in self.organization_ids


class MCPAuthContext:
    """
    Authentication context for MCP servers.

    This is now a lightweight wrapper around AuthService for MCP compatibility.
    Delegates all auth operations to the shared AuthService.
    """

    def __init__(self):
        """
        Initialize auth context by wrapping AuthService.

        Uses environment variables for database configuration:
        - AUTH_DB_HOST
        - AUTH_DB_PORT
        - AUTH_DB_NAME
        - AUTH_DB_USER
        - AUTH_DB_PASSWORD
        """
        # Delegate to shared AuthService singleton
        self._auth_service = get_auth_service()

    async def get_user_permissions(self, user_email: str) -> UserPermissions:
        """
        Get user permissions for the given email.

        This method delegates to AuthService and wraps the result in the
        MCP-compatible UserPermissions class with additional helper methods.

        Args:
            user_email: User's email address

        Returns:
            UserPermissions object with access control information
        """
        # Delegate to AuthService
        permissions = await self._auth_service.get_user_permissions(user_email)

        # Convert to MCP UserPermissions (which extends AuthService UserPermissions)
        return UserPermissions(**permissions.model_dump())

    def validate_grid_access(self, permissions: UserPermissions, grid_id: str) -> bool:
        """
        Validate that user has access to a specific grid.

        Args:
            permissions: User permissions object
            grid_id: Grid ID to check access for

        Returns:
            True if user has access, False otherwise
        """
        return permissions.has_grid_access(grid_id)

    def validate_meter_access(self, permissions: UserPermissions, meter_id: str) -> bool:
        """
        Validate that user has access to a specific meter.

        Args:
            permissions: User permissions object
            meter_id: Meter ID to check access for

        Returns:
            True if user has access, False otherwise
        """
        return permissions.has_meter_access(meter_id)

    def filter_grids(
        self,
        permissions: UserPermissions,
        all_grids: List[Dict],
        grid_id_field: str = "id",
    ) -> List[Dict]:
        """
        Filter list of grids based on user permissions.

        Args:
            permissions: User permissions object
            all_grids: List of grid dictionaries
            grid_id_field: Field name containing grid ID

        Returns:
            Filtered list of grids user has access to
        """
        if permissions.is_admin:
            return all_grids

        return [g for g in all_grids if g.get(grid_id_field) in permissions.grid_ids]

    def filter_meters(
        self,
        permissions: UserPermissions,
        all_meters: List[Dict],
        meter_id_field: str = "id",
    ) -> List[Dict]:
        """
        Filter list of meters based on user permissions.

        Args:
            permissions: User permissions object
            all_meters: List of meter dictionaries
            meter_id_field: Field name containing meter ID

        Returns:
            Filtered list of meters user has access to
        """
        if permissions.is_admin:
            return all_meters

        return [m for m in all_meters if m.get(meter_id_field) in permissions.meter_ids]


# Global instance for convenience
_auth_context: Optional[MCPAuthContext] = None


def get_auth_context(
    auth_supabase_url: Optional[str] = None,
    auth_supabase_key: Optional[str] = None,
    auth_supabase_anon_key: Optional[str] = None,
    auth_supabase_jwt: Optional[str] = None,
) -> MCPAuthContext:
    """
    Get or create the global auth context.

    Args:
        auth_supabase_url: Read-only auth Supabase URL
        auth_supabase_key: Read-only auth Supabase service key
        auth_supabase_anon_key: Anon/public key
        auth_supabase_jwt: Service JWT token

    Returns:
        MCPAuthContext instance
    """
    global _auth_context

    if _auth_context is None:
        _auth_context = MCPAuthContext()

    return _auth_context


async def get_user_permissions(user_email: str) -> UserPermissions:
    """
    Convenience function to get user permissions using the global context.

    Args:
        user_email: User's email address

    Returns:
        UserPermissions object
    """
    context = get_auth_context()
    return await context.get_user_permissions(user_email)


__all__ = [
    "UserPermissions",
    "MCPAuthContext",
    "get_auth_context",
    "get_user_permissions",
]
