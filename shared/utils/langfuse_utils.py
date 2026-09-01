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


def _has_active_span() -> bool:
    """True when an OpenTelemetry span is active in the current context.

    The ``update_current_*`` / ``score_current_*`` SDK calls below are no-ops
    without an active span -- but calling them anyway makes Langfuse log
    ``Context error: No active span in current context`` on every call and
    forces the global Langfuse client (background exporter threads, ingestion
    queues) into existence in processes that never open a span: every model
    call from a path not wrapped in ``@langfuse_observe`` (alert judgment,
    summarisers, verification sub-calls, the MCP servers, the anansi_app poller
    daemons). Gate on this so the SDK stays completely dormant there.

    Mirrors the SDK's own private check without tripping its warning. Any
    failure (OTel not installed, no context) is treated as "no span".
    """
    try:
        from opentelemetry import trace as _otel_trace

        return _otel_trace.get_current_span().get_span_context().is_valid
    except Exception:
        return False


def update_generation(**kwargs):
    """Safely update the current Langfuse generation metadata."""
    if not LANGFUSE_ENABLED or not _has_active_span():
        return
    try:
        from langfuse import get_client

        get_client().update_current_generation(**kwargs)
    except Exception as e:
        LOGGER.debug(f"Langfuse update_generation failed (non-fatal): {e}")


def update_span(**kwargs):
    """Safely update the current Langfuse span."""
    if not LANGFUSE_ENABLED or not _has_active_span():
        return
    try:
        from langfuse import get_client

        get_client().update_current_span(**kwargs)
    except Exception as e:
        LOGGER.debug(f"Langfuse update_span failed (non-fatal): {e}")


def update_trace(**kwargs):
    """Safely update the current Langfuse trace."""
    if not LANGFUSE_ENABLED or not _has_active_span():
        return
    try:
        from langfuse import get_client

        get_client().update_current_trace(**kwargs)
    except Exception as e:
        LOGGER.debug(f"Langfuse update_trace failed (non-fatal): {e}")


def prompt_metadata(rendered) -> dict:
    """Trace metadata identifying which prompt version produced a generation.

    ``rendered`` is a ``shared.prompts.types.RenderedPrompt``. Typed as a
    plain object here (not imported) to avoid a shared.utils -> shared.prompts
    import edge purely for a type hint.
    """
    return {
        "prompt_id": rendered.prompt_id,
        "prompt_source": rendered.source.value,
        "prompt_version": rendered.version,
        "prompt_checksum": rendered.checksum[:8],
    }


def score_trace(**kwargs):
    """Safely score the current Langfuse trace."""
    if not LANGFUSE_ENABLED or not _has_active_span():
        return
    try:
        from langfuse import get_client

        get_client().score_current_trace(**kwargs)
    except Exception as e:
        LOGGER.debug(f"Langfuse score_trace failed (non-fatal): {e}")
