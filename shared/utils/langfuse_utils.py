"""Langfuse LLM observability helpers.

Provides a no-op decorator and safe helpers when LANGFUSE_ENABLED is false.
All Langfuse imports are contained in this module.
"""

import os

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "false").lower() in ("true", "1", "yes")


def langfuse_observe(**kwargs):
    """Return Langfuse @observe decorator when enabled, identity when disabled."""
    if LANGFUSE_ENABLED:
        from langfuse import observe

        return observe(**kwargs)
    return lambda fn: fn


def update_generation(**kwargs):
    """Safely update the current Langfuse generation metadata."""
    if not LANGFUSE_ENABLED:
        return
    try:
        from langfuse import get_client

        get_client().update_current_generation(**kwargs)
    except Exception as e:
        LOGGER.debug(f"Langfuse update_generation failed (non-fatal): {e}")


def update_span(**kwargs):
    """Safely update the current Langfuse span."""
    if not LANGFUSE_ENABLED:
        return
    try:
        from langfuse import get_client

        get_client().update_current_span(**kwargs)
    except Exception as e:
        LOGGER.debug(f"Langfuse update_span failed (non-fatal): {e}")


def update_trace(**kwargs):
    """Safely update the current Langfuse trace."""
    if not LANGFUSE_ENABLED:
        return
    try:
        from langfuse import get_client

        get_client().update_current_trace(**kwargs)
    except Exception as e:
        LOGGER.debug(f"Langfuse update_trace failed (non-fatal): {e}")


def score_trace(**kwargs):
    """Safely score the current Langfuse trace."""
    if not LANGFUSE_ENABLED:
        return
    try:
        from langfuse import get_client

        get_client().score_current_trace(**kwargs)
    except Exception as e:
        LOGGER.debug(f"Langfuse score_trace failed (non-fatal): {e}")
