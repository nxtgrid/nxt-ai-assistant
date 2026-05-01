"""
Shared HTTP client utilities with function composition patterns.

Provides reusable HTTP session management and retry logic.
"""

import asyncio
from functools import wraps
from typing import Callable, Optional, TypeVar

import aiohttp
from shared_code.utils.logger import setup_logger

logger = setup_logger("http-client")

T = TypeVar("T")


class HTTPClientMixin:
    """Mixin providing HTTP session management for API clients."""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session, recreating if closed."""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=120)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def close_session(self):
        """Close HTTP session."""
        if self.session:
            await self.session.close()
            self.session = None


def retry_with_delay(delay_seconds: int = 5, api_name: str = "API") -> Callable:
    """
    Decorator for retrying API calls with delay.

    Function composition pattern: wraps async function with retry logic.

    Args:
        delay_seconds: Seconds to wait before retry
        api_name: Name of API for error messages

    Returns:
        Decorated function with retry capability
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            try:
                # First attempt
                return await func(*args, **kwargs)  # type: ignore[return-value,misc,no-any-return]
            except Exception as e:
                if logger:
                    logger.debug(f"{api_name} call failed, retrying in {delay_seconds}s: {e}")
                # Wait before retry
                await asyncio.sleep(delay_seconds)
                try:
                    # Second attempt
                    return await func(*args, **kwargs)  # type: ignore[return-value,misc,no-any-return]
                except Exception as retry_error:
                    # Log the real error internally; return a sanitized message to callers
                    # so internal hostnames/paths are not exposed to the LLM or users.
                    if logger:
                        logger.error(f"{api_name} retry failed: {retry_error}")
                    return {  # type: ignore[return-value]
                        "error": f"{api_name} is temporarily unavailable.",
                        "availability_note": f"{api_name} was not available at the moment. The API may be experiencing transient downtime. Please try again later.",
                        "api_available": False,
                    }

        return wrapper  # type: ignore[return-value]

    return decorator


def compose_retry_handler(
    api_func: Callable[..., T], api_version: str, delay_seconds: int = 5
) -> Callable[..., T]:
    """
    Compose a retry handler around an API function.

    Pure function composition pattern.

    Args:
        api_func: Async function to wrap
        api_version: API version/name for logging
        delay_seconds: Retry delay in seconds

    Returns:
        Composed function with retry logic
    """

    async def composed(*args, **kwargs) -> T:
        try:
            return await api_func(*args, **kwargs)  # type: ignore[return-value,misc,no-any-return]
        except Exception:
            await asyncio.sleep(delay_seconds)
            try:
                return await api_func(*args, **kwargs)  # type: ignore[return-value,misc,no-any-return]
            except Exception as retry_error:
                # Log the real error internally; return a sanitized message so internal
                # hostnames/paths are not exposed to the LLM or users.
                logger.error(f"{api_version} API retry failed: {retry_error}")
                return {  # type: ignore[return-value]
                    "error": f"{api_version} API is temporarily unavailable.",
                    "availability_note": f"{api_version} API was not available at the moment. The API may be experiencing transient downtime. Please try again later.",
                    "api_available": False,
                }

    return composed  # type: ignore[return-value]
