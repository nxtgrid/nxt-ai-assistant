"""
Shared OAuth token cache management with function composition.

Provides reusable OAuth token caching, validation, and cleanup.
"""

from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

import jwt
from shared_code.utils.logger import setup_logger

logger = setup_logger("oauth-cache")


class OAuthTokenCache:
    """
    Reusable OAuth token cache with automatic expiry handling.

    Function composition pattern: separates concerns of token storage,
    validation, and cleanup into composable functions.
    """

    def __init__(self):
        # Cache: (base_url, username, company) -> (token, expiry_ms)
        self.token_cache: Dict[Tuple[str, str, str], Tuple[str, float]] = {}

    def cache_token(
        self, base_url: str, username: str, company: str, token: str, expiry_time: float
    ) -> None:
        """
        Cache a token with its expiry time.

        Args:
            base_url: API base URL
            username: Username
            company: Company/tenant
            token: OAuth token
            expiry_time: Expiry timestamp in milliseconds
        """
        cache_key = (base_url, username, company)
        self.token_cache[cache_key] = (token, expiry_time)
        if logger:
            logger.debug(
                f"Cached token for {username}@{company}, "
                f"expires: {datetime.fromtimestamp(expiry_time / 1000)}"
            )

    def get_cached_token(
        self, base_url: str, username: str, company: str, buffer_ms: int = 30000
    ) -> Optional[str]:
        """
        Get cached token if valid.

        Args:
            base_url: API base URL
            username: Username
            company: Company/tenant
            buffer_ms: Time buffer before expiry (default 30s)

        Returns:
            Valid token or None if expired/not found
        """
        cache_key = (base_url, username, company)
        current_time = datetime.now().timestamp() * 1000

        if cache_key in self.token_cache:
            cached_token, cached_expiry = self.token_cache[cache_key]

            # Check if token is still valid (with buffer)
            if cached_expiry and cached_expiry - current_time > buffer_ms:
                if logger:
                    logger.debug(f"Using cached token for {username}@{company}")
                return cached_token
            else:
                if logger:
                    logger.debug(f"Cached token expired for {username}@{company}")
                return None

        return None

    def cleanup_expired_tokens(self) -> int:
        """
        Remove expired tokens from cache.

        Returns:
            Number of tokens cleaned up
        """
        current_time = datetime.now().timestamp() * 1000
        expired_keys = []

        for cache_key, (token, expiry) in self.token_cache.items():
            if expiry and expiry <= current_time:
                expired_keys.append(cache_key)

        for key in expired_keys:
            del self.token_cache[key]
            if logger:
                logger.debug(f"Cleaned up expired token for {key[1]}@{key[2]}")

        return len(expired_keys)

    def get_cache_status(self) -> Dict[str, Any]:
        """
        Get current token cache status for debugging.

        Returns:
            Dict with cache statistics and token info
        """
        current_time = datetime.now().timestamp() * 1000
        active_tokens: list[Dict[str, Any]] = []
        expired_tokens: list[Dict[str, Any]] = []

        for cache_key, (token, expiry) in self.token_cache.items():
            base_url, username, company = cache_key
            token_info: Dict[str, Any] = {
                "base_url": base_url,
                "username": username,
                "company": company,
                "expires_at": datetime.fromtimestamp(expiry / 1000).isoformat() if expiry else None,
                "is_valid": expiry and expiry > current_time,
            }

            if token_info["is_valid"]:
                active_tokens.append(token_info)
            else:
                expired_tokens.append(token_info)

        cache_status = {
            "total_cached_tokens": len(self.token_cache),
            "active_tokens": active_tokens,
            "expired_tokens": expired_tokens,
        }
        return cache_status

    def clear(self) -> None:
        """Clear all cached tokens."""
        self.token_cache.clear()
        if logger:
            logger.debug("Cleared OAuth token cache")


# Function composition helpers


def decode_jwt_expiry(token: str) -> float:
    """
    Extract expiry time from JWT token.

    Pure function: token -> expiry_timestamp_ms

    Args:
        token: JWT token string

    Returns:
        Expiry timestamp in milliseconds
    """
    decoded = jwt.decode(token, options={"verify_signature": False})
    exp_value = decoded.get("exp", 0)
    return float(exp_value * 1000)


def ensure_token_with_login(
    cache: OAuthTokenCache,
    login_func: Callable[[str, str, str, str], str],
    base_url: str,
    username: str,
    password: str,
    company: str,
    buffer_ms: int = 30000,
) -> Callable[[], str]:
    """
    Compose token ensuring function with login capability.

    Higher-order function that returns a token getter.

    Args:
        cache: OAuthTokenCache instance
        login_func: Async login function
        base_url: API base URL
        username: Username
        password: Password
        company: Company/tenant
        buffer_ms: Expiry buffer in ms

    Returns:
        Async function that ensures valid token
    """

    async def ensure_token() -> str:
        # Try to get cached token
        cached_token = cache.get_cached_token(base_url, username, company, buffer_ms)
        if cached_token:
            return str(cached_token)

        # Need to login/refresh - don't await since login_func is not necessarily async
        token = str(login_func(base_url, username, password, company))  # type: ignore[arg-type]

        # Cache the new token
        expiry = decode_jwt_expiry(token)
        cache.cache_token(base_url, username, company, token, expiry)

        # Periodic cleanup (every ~10 calls)
        if len(cache.token_cache) > 0 and hash((base_url, username, company)) % 10 == 0:
            cache.cleanup_expired_tokens()

        return str(token)

    return ensure_token  # type: ignore[return-value]
