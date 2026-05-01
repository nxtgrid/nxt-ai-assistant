"""Telegram Markdown utilities.

This module provides centralized utilities for handling Telegram markdown formatting.
All code that sends messages to Telegram should use these functions for consistency.

Telegram Markdown v1 format:
- *bold* (not **bold** like GitHub)
- _italic_
- `code`
- [link](url)

NOT supported by Telegram:
- Tables (| col | col |)
- Headers (### Header)
- Horizontal rules (***)

Special characters that cause parsing errors: _ * ` [
"""

import re
from typing import List, Optional


def _convert_table_to_text(table_lines: List[str]) -> str:
    """Convert a markdown table to readable text format.

    Args:
        table_lines: List of lines that make up the table (including header row)

    Returns:
        Text representation of the table
    """
    if not table_lines:
        return ""

    rows: List[List[str]] = []
    for line in table_lines:
        # Skip separator rows (| :--- | :--- |)
        if re.match(r"^\|[\s:\-|]+\|$", line.strip()):
            continue

        # Parse cells from the row
        cells = [cell.strip() for cell in line.strip().split("|")]
        # Remove empty first/last cells from leading/trailing |
        cells = [c for c in cells if c]

        if cells:
            rows.append(cells)

    if not rows:
        return ""

    # First row is header
    header = rows[0] if rows else []
    data_rows = rows[1:] if len(rows) > 1 else []

    # Format as "Header: Value" pairs for each row
    result_lines = []
    for row in data_rows:
        row_parts = []
        for i, cell in enumerate(row):
            if i < len(header):
                # Skip if header and value are the same (redundant)
                if header[i].lower() != cell.lower():
                    row_parts.append(f"{header[i]}: {cell}")
                else:
                    row_parts.append(cell)
            else:
                row_parts.append(cell)
        result_lines.append(" | ".join(row_parts))

    return "\n".join(result_lines)


def _convert_tables_in_text(text: str) -> str:
    """Find and convert all markdown tables in the text.

    Args:
        text: Text potentially containing markdown tables

    Returns:
        Text with tables converted to readable format
    """
    lines = text.split("\n")
    result_lines = []
    table_lines = []
    in_table = False

    for line in lines:
        # Check if this line is part of a table (starts and ends with |)
        is_table_line = bool(re.match(r"^\s*\|.*\|\s*$", line))

        if is_table_line:
            in_table = True
            table_lines.append(line)
        else:
            if in_table:
                # End of table, convert it
                converted = _convert_table_to_text(table_lines)
                if converted:
                    result_lines.append(converted)
                table_lines = []
                in_table = False
            result_lines.append(line)

    # Handle table at end of text
    if table_lines:
        converted = _convert_table_to_text(table_lines)
        if converted:
            result_lines.append(converted)

    return "\n".join(result_lines)


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram Markdown v1.

    Use this when you have plain text that needs to be safely embedded
    in a Telegram markdown message (e.g., user input, database values).

    Telegram Markdown v1 uses: *bold*, _italic_, `code`, [link](url)
    Characters that can cause parsing errors: _ * ` [

    Args:
        text: Plain text to escape

    Returns:
        Escaped text safe for Telegram Markdown
    """
    if not text:
        return text

    # Escape characters that have special meaning in Telegram Markdown v1
    # We need to escape: _ * ` [
    escape_chars = ["_", "*", "`", "["]

    result = text
    for char in escape_chars:
        result = result.replace(char, f"\\{char}")

    return result


def convert_github_to_telegram_markdown(text: str) -> str:
    """Convert GitHub-style markdown to Telegram markdown format.

    Use this when you have markdown text (e.g., from LLM output) that needs
    to be converted to Telegram's markdown format.

    Conversions performed:
    - **bold** -> *bold*
    - Tables -> "Header: Value" text format
    - ### Headers -> *Header* (bold)
    - *** or --- horizontal rules -> ─────────
    - Bullet * -> -

    Also sanitizes text to avoid Telegram markdown parsing errors:
    - Escapes underscores in the middle of words (e.g., grid_name -> grid\\_name)

    Args:
        text: GitHub-flavored markdown text

    Returns:
        Telegram markdown formatted text
    """
    if not text:
        return text

    # Convert tables FIRST (before other transformations mess with | characters)
    text = _convert_tables_in_text(text)

    # Convert headers: ### Header -> *Header*
    # Match 1-6 # characters at start of line followed by space and text
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # Convert horizontal rules: *** or --- or ___ to a line
    text = re.sub(r"^[\*\-_]{3,}\s*$", "─────────", text, flags=re.MULTILINE)

    # Convert **bold** to *bold* (do this after headers to avoid conflicts)
    text = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", text)

    # Convert bullet points: lines starting with * to -
    # Match lines that start with * followed by space (bullet point)
    text = re.sub(r"^(\s*)\* ", r"\1- ", text, flags=re.MULTILINE)

    # Escape underscores that are in the middle of words (not italic formatting)
    # BUT preserve slash commands and URLs - they should not be escaped
    # because when markdown parsing fails, the backslash becomes visible

    # Protected items storage
    protected_items: List[str] = []

    def protect_item(m: re.Match) -> str:
        item = m.group(0)
        protected_items.append(item)
        return f"⟦PROT{len(protected_items) - 1}⟧"

    # Protect markdown links [text](url) - protect the entire link to preserve URL
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", protect_item, text)

    # Protect bare URLs (https:// or http://)
    text = re.sub(r"https?://[^\s\)]+", protect_item, text)

    # Protect slash commands like /equipment_history
    text = re.sub(r"/[a-zA-Z][a-zA-Z0-9_]*", protect_item, text)

    # Now escape underscores in remaining text
    # Match underscore surrounded by word characters (e.g., grid_name, user_id)
    text = re.sub(r"(\w)_(\w)", r"\1\\_\2", text)

    # Handle multiple consecutive underscores in identifiers (e.g., __init__)
    text = re.sub(r"(\w)_(\\_)", r"\1\\_\2", text)
    text = re.sub(r"(\\_)_(\w)", r"\1\\_\2", text)

    # Restore protected items
    for i, item in enumerate(protected_items):
        text = text.replace(f"⟦PROT{i}⟧", item)

    # Fail-safe cleanup: remove any remaining markers that weren't restored
    text = re.sub(r"⟦PROT\d+⟧", "", text)

    return text


def strip_markdown(text: str) -> str:
    """Remove markdown formatting to get plain text.

    Use this when markdown parsing fails and you need to fall back to plain text.

    Args:
        text: Markdown-formatted text

    Returns:
        Plain text with markdown formatting removed
    """
    if not text:
        return text

    # Remove bold markers
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)

    # Remove italic markers
    text = re.sub(r"_([^_]+)_", r"\1", text)

    # Remove code markers
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # Remove link formatting, keep link text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Remove escaped characters
    text = text.replace("\\_", "_")
    text = text.replace("\\*", "*")
    text = text.replace("\\`", "`")
    text = text.replace("\\[", "[")

    return text


def sanitize_for_telegram(
    text: str, is_markdown: bool = True, max_length: Optional[int] = 4096
) -> str:
    """Sanitize text for safe Telegram message sending.

    This is the main entry point for preparing text for Telegram.
    It handles conversion, escaping, and length limits.

    Args:
        text: Text to sanitize (can be markdown or plain text)
        is_markdown: If True, convert GitHub markdown to Telegram format.
                     If False, escape special characters for plain text.
        max_length: Maximum message length (Telegram limit is 4096).
                    Set to None to disable truncation.

    Returns:
        Sanitized text ready for Telegram
    """
    if not text:
        return text

    if is_markdown:
        result = convert_github_to_telegram_markdown(text)
    else:
        result = escape_markdown(text)

    # Truncate if needed
    if max_length and len(result) > max_length:
        # Leave room for truncation indicator
        result = result[: max_length - 20] + "\n\n... (truncated)"

    return result
