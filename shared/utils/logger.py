"""Logging utilities for MCP servers."""

import sys
from pathlib import Path

from loguru import logger
from shared_code.config.settings import server_settings


def setup_logger(name: str = None) -> logger:
    """Set up logger with consistent configuration."""

    # Remove default handler
    logger.remove()

    # Add console handler (stderr for MCP servers)
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=server_settings.log_level,
        colorize=True,
    )

    # Use /tmp for logs to avoid read-only filesystem issues
    log_dir = Path("/tmp/mcp_servers/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    # Add file handler
    logger.add(
        log_dir / f"{name or 'mcp_server'}.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level=server_settings.log_level,
        rotation="10 MB",
        retention="7 days",
        compression="zip",
    )

    return logger
