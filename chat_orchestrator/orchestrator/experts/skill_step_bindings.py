"""Runtime helpers for user-designed skill steps' `{{var}}` output binding
and read-only tool gating.

Phase 2 of docs/superpowers/plans/2026-08-06-user-designed-skills.md.

Shared between `WorkflowExecutor._execute_llm_step` (runtime) and
`skill_validation.py` (static, save-time validation) so the two never drift
on what counts as a write clause, a read, or a read-only tool.

Nothing here is skill-specific in a way that requires the `skills` table
(Phase 3) to exist -- it operates on plain strings and dicts. What makes a
step "skill-authored" at runtime is `ParsedStep.is_skill_step`, set by
whatever constructs the step (Phase 3's onward). A step with
`is_skill_step=False` (every step parsed from a Google Doc today) never
reaches this module's functions at all -- see workflow_executor.py's
`_execute_llm_step` for the gate.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Tool name prefixes considered non-mutating. A step only gets tools outside
# this list when it explicitly declares allow_write=True. This is the whole
# reason "a rewound step already took effect" is a survivable trade-off
# during design -- see the plan doc's Phase 2 "Work" item 1.
READ_ONLY_TOOL_PREFIXES: Tuple[str, ...] = ("get_", "list_", "search_", "check_", "fetch_")

# `-> {{name}}` or the unicode arrow `→ {{name}}`, at the very end of the
# instruction (trailing whitespace tolerated). Everything before it is what
# gets sent to the LLM as the step's read-only instruction text; everything
# else written as `{{other}}` within that remaining text is a READ, not a
# write -- this pattern only matches the trailing write clause.
_OUTPUT_BINDING_RE = re.compile(
    r"(?:→|->)\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*\Z"
)

# The line an LLM step's response must end with when the step declared an
# output var, e.g. "RESULT: 42". Multiline so it matches regardless of
# where the model puts it, but see extract_output_value: only the LAST
# match counts, matching "end your reply with... on its own final line".
_RESULT_LINE_RE = re.compile(r"^RESULT:\s*(.*?)\s*$", re.MULTILINE)

EXTRACTION_INSTRUCTION = (
    "\n\nAfter completing the above, end your reply with the result on its "
    "own final line, prefixed exactly with 'RESULT: ' (e.g. 'RESULT: 42' or "
    "'RESULT: none found'). This line is parsed programmatically -- write "
    "nothing after it."
)


def parse_output_binding(instruction: str) -> Tuple[str, Optional[str]]:
    """Split a step instruction into (read_only_text, output_var_or_None).

    `→ {{name}}` (or `-> {{name}}`) at the end of the instruction declares a
    write; the clause is stripped from what's returned as read_only_text.
    Absent a write clause, returns the instruction unchanged and None.
    """
    match = _OUTPUT_BINDING_RE.search(instruction)
    if not match:
        return instruction, None
    output_var = match.group(1)
    read_text = instruction[: match.start()].rstrip()
    return read_text, output_var


def extract_output_value(response_text: str) -> Optional[str]:
    """Pull a step's declared output value from its final 'RESULT: ...' line.

    Returns None when no such line is present -- callers must treat that as
    "the step declared a write but produced nothing" (pause), never as an
    empty-string value. Takes the LAST match if the model's reasoning
    happens to mention 'RESULT:' earlier in the text; the instruction asks
    for it on the final line, so the last occurrence is authoritative.
    """
    matches = _RESULT_LINE_RE.findall(response_text or "")
    if not matches:
        return None
    value = matches[-1].strip()
    return value or None


def strip_result_line(response_text: str) -> str:
    """Remove the trailing 'RESULT: ...' line from text shown to a user.

    The line is an internal parsing convention (see EXTRACTION_INSTRUCTION);
    a builder-mode chat or a run-mode response shouldn't display it.
    Idempotent -- a no-op on text with no RESULT line.
    """
    if response_text is None:
        return response_text
    return _RESULT_LINE_RE.sub("", response_text).rstrip()


def is_read_only_tool_name(tool_name: str) -> bool:
    """Whether a tool's name matches one of the read-only prefixes."""
    return tool_name.startswith(READ_ONLY_TOOL_PREFIXES)


def filter_tools_for_step(
    tools: List[Dict[str, Any]], allow_write: bool
) -> List[Dict[str, Any]]:
    """Narrow a tool-declaration list to what one skill step may call.

    `allow_write=False` (the default for every step -- see ParsedStep) keeps
    only read-only-prefixed tools. `allow_write=True` is an explicit,
    per-step opt-in that returns every tool unfiltered. Tools missing a
    "name" key are dropped rather than risking an unintended match.
    """
    if allow_write:
        return list(tools)
    return [tool for tool in tools if is_read_only_tool_name(tool.get("name", ""))]


__all__ = [
    "EXTRACTION_INSTRUCTION",
    "READ_ONLY_TOOL_PREFIXES",
    "extract_output_value",
    "filter_tools_for_step",
    "is_read_only_tool_name",
    "parse_output_binding",
    "strip_result_line",
]
