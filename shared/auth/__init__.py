"""Authentication and authorization shared modules."""

from typing import TYPE_CHECKING, Any

from shared.auth.auth_service import (
    AuthService,
    GridTelegramSources,
    UserPermissions,
    get_auth_service,
    parse_telegram_config,
)

if TYPE_CHECKING:
    from shared.auth.auth_context import MCPAuthContext, get_auth_context, get_user_permissions
else:
    try:
        from shared.auth.auth_context import MCPAuthContext, get_auth_context, get_user_permissions
    except ImportError:
        # auth_context may have dependencies not available in all projects
        MCPAuthContext: Any = None  # type: ignore
        get_auth_context: Any = None  # type: ignore
        get_user_permissions: Any = None  # type: ignore


__all__ = [
    "AuthService",
    "GridTelegramSources",
    "UserPermissions",
    "get_auth_service",
    "parse_telegram_config",
    "MCPAuthContext",
    "get_auth_context",
    "get_user_permissions",
]
