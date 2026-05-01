"""
Shared MCP response formatting utilities with function composition.

Provides reusable functions for formatting MCP tool responses.
"""

import json
from typing import Any, Callable, Dict, List

from mcp.types import TextContent


def to_json_text(data: Any, indent: int = 2, default: Callable = str) -> str:
    """
    Convert data to JSON string.

    Pure function: data -> json_string

    Args:
        data: Data to convert
        indent: JSON indentation
        default: Default serializer for non-JSON types

    Returns:
        JSON string
    """
    return json.dumps(data, indent=indent, default=default)


def wrap_text_content(text: str) -> List[TextContent]:
    """
    Wrap text in MCP TextContent.

    Pure function: text -> [TextContent]

    Args:
        text: Text to wrap

    Returns:
        List with single TextContent item
    """
    return [TextContent(type="text", text=text)]


def compose_json_response(data: Any, indent: int = 2, default: Callable = str) -> List[TextContent]:
    """
    Compose JSON response for MCP tool.

    Function composition: data -> json_string -> TextContent

    Args:
        data: Data to return
        indent: JSON indentation
        default: Default serializer

    Returns:
        MCP TextContent response
    """
    json_str = to_json_text(data, indent, default)
    return wrap_text_content(json_str)


def compose_error_response(error: Exception) -> List[TextContent]:
    """
    Compose error response for MCP tool.

    Pure function: Exception -> TextContent

    Args:
        error: Exception to format

    Returns:
        MCP TextContent error response
    """
    error_text = f"Error: {str(error)}"
    return wrap_text_content(error_text)


def add_metadata(response_data: Dict[str, Any], **metadata: Any) -> Dict[str, Any]:
    """
    Add metadata to response data.

    Pure function: data + metadata -> enriched_data

    Args:
        response_data: Base response data
        **metadata: Metadata key-value pairs

    Returns:
        Response data with metadata
    """
    return {**response_data, **metadata}


def compose_response_with_metadata(
    data: Any, metadata: Dict[str, Any], indent: int = 2
) -> List[TextContent]:
    """
    Compose response with metadata.

    Function composition chain.

    Args:
        data: Response data
        metadata: Metadata to add
        indent: JSON indentation

    Returns:
        MCP TextContent with metadata
    """
    enriched = add_metadata(data, **metadata) if isinstance(data, dict) else data
    return compose_json_response(enriched, indent)


def truncate_text(text: str, max_length: int, suffix: str = "...") -> str:
    """
    Truncate text to maximum length.

    Pure function: text -> truncated_text

    Args:
        text: Text to truncate
        max_length: Maximum length
        suffix: Suffix for truncated text

    Returns:
        Truncated text
    """
    if len(text) <= max_length:
        return text
    return text[:max_length] + suffix


def compose_paginated_response(
    items: List[Any], total: int, page: int = 1, page_size: int = 50
) -> Dict[str, Any]:
    """
    Compose paginated response.

    Pure function composition for pagination metadata.

    Args:
        items: Current page items
        total: Total items available
        page: Current page number
        page_size: Items per page

    Returns:
        Paginated response dict
    """
    return {
        "items": items,
        "pagination": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
            "has_more": page * page_size < total,
        },
    }
