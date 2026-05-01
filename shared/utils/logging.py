"""Unified logging utilities shared across all Anansi projects (borg, mcp_servers, rag_pipeline)."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import Optional

from loguru import logger


@lru_cache(maxsize=1)
def setup_logging(
    name: Optional[str] = None,
    log_level: str = "INFO",
    log_dir_base: str = "/tmp",
    project_name: str = "anansi",
):
    """
    Configure loguru with console handler only (stderr can be piped to file if needed).

    Args:
        name: Logger name (module/service name)
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_dir_base: Base directory for logs (deprecated, kept for compatibility)
        project_name: Project name (deprecated, kept for compatibility)

    Returns:
        Configured logger instance
    """
    # Get log level from environment or use parameter
    log_level = os.getenv("LOG_LEVEL", log_level).upper()

    # Remove default handler
    logger.remove()

    # Add console handler (stderr)
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=log_level,
        colorize=True,
    )

    return logger


def get_logger(
    module_name: str,
    project_name: str = "anansi",
    log_level: Optional[str] = None,
):
    """
    Return a named logger configured via :func:`setup_logging`.

    Args:
        module_name: Module name for the logger
        project_name: Project name (borg, mcp_servers, rag_pipeline)
        log_level: Optional log level override

    Returns:
        Configured named logger
    """
    setup_logging(
        name=module_name,
        log_level=log_level or os.getenv("LOG_LEVEL", "INFO"),
        project_name=project_name,
    )
    return logger.bind(module=module_name)


__all__ = ["get_logger", "setup_logging", "logger"]
