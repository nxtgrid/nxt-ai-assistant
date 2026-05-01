"""Logging utilities for MCP servers."""

import sys

from loguru import logger
from shared_code.config.settings import server_settings


def setup_logger(name: str = None):
    """Set up logger with console handler only (stderr can be piped to file if needed)."""

    # Remove default handler
    logger.remove()

    # Add console handler (stderr for MCP servers)
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=server_settings.log_level,
        colorize=True,
    )

    return logger
