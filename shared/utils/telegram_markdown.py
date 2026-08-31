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
from typing import Callable, List, Optional, Set, Tuple


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
    - Escapes any *, _, ` or [ left unpaired, which Telegram would otherwise
      reject as an unterminated entity (see balance_markdown_entities)

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

    # Escape underscores that are in the middle of words (not italic formatting).
    # Links, URLs and slash commands are lifted out first so the generic
    # word-boundary rules below cannot mangle them.
    #
    # A complete [text](url) link is restored verbatim: Telegram reads the URL
    # between the parens literally, so a backslash there would corrupt the href.
    # Bare URLs and slash commands are restored *escaped*, because to Telegram
    # the underscore in /equipment_history is a live italic marker -- an odd
    # count fails the whole message, an even one italicises everything between
    # two commands. The backslash is consumed when Telegram parses the message,
    # so the command still reads (and stays tappable) exactly as written.
    protected_items: List[str] = []
    escape_on_restore: List[bool] = []

    def protect(escape: bool) -> Callable[[re.Match], str]:
        def protect_item(m: re.Match) -> str:
            protected_items.append(m.group(0))
            escape_on_restore.append(escape)
            return f"⟦PROT{len(protected_items) - 1}⟧"

        return protect_item

    # Protect markdown links [text](url) - protect the entire link to preserve URL
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", protect(escape=False), text)

    # Protect bare URLs (https:// or http://)
    text = re.sub(r"https?://[^\s\)]+", protect(escape=True), text)

    # Protect slash commands like /equipment_history
    text = re.sub(r"/[a-zA-Z][a-zA-Z0-9_]*", protect(escape=True), text)

    # Now escape underscores in remaining text
    # Match underscore surrounded by word characters (e.g., grid_name, user_id)
    text = re.sub(r"(\w)_(\w)", r"\1\\_\2", text)

    # Handle multiple consecutive underscores in identifiers (e.g., __init__)
    text = re.sub(r"(\w)_(\\_)", r"\1\\_\2", text)
    text = re.sub(r"(\\_)_(\w)", r"\1\\_\2", text)

    # Restore protected items
    for i, item in enumerate(protected_items):
        text = text.replace(f"⟦PROT{i}⟧", escape_markdown(item) if escape_on_restore[i] else item)

    # Fail-safe cleanup: remove any remaining markers that weren't restored
    text = re.sub(r"⟦PROT\d+⟧", "", text)

    # Last, because everything above can leave a delimiter stranded: neutralise
    # anything Telegram would read as an entity that never closes. One stray
    # "[" is enough to fail the whole message.
    return balance_markdown_entities(text)


# A well-formed inline link -- deliberately the same shape the converter above
# protects, so a link that survived conversion is not escaped here instead. Its
# interior goes to Telegram's own link parser, so the delimiters inside it are
# none of our business.
_COMPLETE_LINK_RE = re.compile(r"\[[^\[\]]*\]\([^()]*\)")

# Delimiters that open a Markdown v1 entity Telegram expects to see closed.
_PAIRED_DELIMITERS = ("*", "_")


def _tokenize_for_balancing(text: str) -> List[Tuple[str, str]]:
    """Split text into (kind, chunk) tokens for entity balancing.

    "atomic" tokens are already valid (escapes, closed code spans, complete
    links, ordinary characters) and pass through untouched. Every other kind
    names a delimiter whose pairing still has to be decided.
    """
    tokens: List[Tuple[str, str]] = []
    i = 0
    n = len(text)

    while i < n:
        char = text[i]

        # An existing backslash escape already neutralises whatever follows.
        if char == "\\" and i + 1 < n:
            tokens.append(("atomic", text[i : i + 2]))
            i += 2
            continue

        if char == "`":
            fence = "```" if text.startswith("```", i) else "`"
            close = text.find(fence, i + len(fence))
            if close != -1:
                # Closed code span: its contents are literal to Telegram.
                tokens.append(("atomic", text[i : close + len(fence)]))
                i = close + len(fence)
                continue
            tokens.append(("`", "`"))
            i += 1
            continue

        if char == "[":
            match = _COMPLETE_LINK_RE.match(text, i)
            if match:
                tokens.append(("atomic", match.group(0)))
                i = match.end()
                continue
            tokens.append(("[", "["))
            i += 1
            continue

        if char in _PAIRED_DELIMITERS:
            tokens.append((char, char))
            i += 1
            continue

        tokens.append(("atomic", char))
        i += 1

    return tokens


def balance_markdown_entities(text: str) -> str:
    """Escape every delimiter Telegram would read as an unterminated entity.

    Telegram's Markdown v1 parser is all-or-nothing: a single delimiter that
    opens an entity without closing it fails the *whole* message with
    "can't parse entities", and the sender then has to fall back to plain
    text. Text assembled from external systems hits this constantly -- a Jira
    summary truncated mid-sentence keeps its opening "[" but loses the closing
    "]", and that lone bracket is enough to strip the formatting off
    everything around it.

    Rather than drop formatting wholesale, escape only the delimiters that
    cannot pair, so intended bold/italic/code still renders and the stray
    character renders as itself. Delimiters are paired greedily left to right
    (the order Telegram itself pairs them in); the leftover odd one is
    escaped. Empty pairs are escaped too -- Telegram rejects a zero-length
    entity.

    Escapes already present are preserved, so this is safe to apply more than
    once -- message chunking re-balances each chunk after the split.

    Args:
        text: Telegram Markdown v1 text

    Returns:
        The same text with unpairable delimiters backslash-escaped.
    """
    if not text:
        return text

    tokens = _tokenize_for_balancing(text)

    # "[" only ever opens an entity; a bare "]" is harmless on its own. Any
    # bracket that did not form a complete link above cannot pair, so it goes.
    unpairable = {index for index, (kind, _) in enumerate(tokens) if kind in ("[", "`")}

    for delimiter in _PAIRED_DELIMITERS:
        positions = [index for index, (kind, _) in enumerate(tokens) if kind == delimiter]
        opener: Optional[int] = None
        paired: Set[int] = set()
        for position in positions:
            if opener is None:
                opener = position
            elif position > opener + 1:
                # Non-empty content between the two: a real entity.
                paired.add(opener)
                paired.add(position)
                opener = None
            else:
                # Adjacent delimiters would make an empty entity. Abandon the
                # earlier one (it stays unpaired) and try this one as opener.
                opener = position
        unpairable.update(position for position in positions if position not in paired)

    return "".join(
        f"\\{chunk}" if index in unpairable else chunk
        for index, (_, chunk) in enumerate(tokens)
    )


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

    # Lift backslash escapes out before stripping anything. "\\_" is a literal
    # underscore, not an italic delimiter -- unescaping it last (as this used
    # to) lets the italic rule below pair it with a later escape and swallow
    # the characters in between.
    escaped: List[str] = []

    def protect_escape(m: re.Match) -> str:
        escaped.append(m.group(1))
        return f"⟦ESC{len(escaped) - 1}⟧"

    text = re.sub(r"\\([_*`\[\]])", protect_escape, text)

    # Remove bold markers
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)

    # Remove italic markers
    text = re.sub(r"_([^_]+)_", r"\1", text)

    # Remove code markers
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # Remove link formatting, keep link text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Restore what the escapes were protecting, now as plain characters
    for i, char in enumerate(escaped):
        text = text.replace(f"⟦ESC{i}⟧", char)

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
        # The cut can land inside an entity, so re-balance what survived.
        if is_markdown:
            result = balance_markdown_entities(result)

    return result
