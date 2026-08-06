"""detect_contradictions now sources staff.system via the shared prompt
library instead of os.getenv("STAFF_SUPPORT_DOC_ID") + a direct Google Doc
fetch, and truncates the resolved text to SYSTEM_INSTRUCTIONS_CONTEXT_CHARS
exactly once.

Previously this was two separate [:1000] slices -- one where the text was
fetched, one where it was consumed -- so deleting only one would have
changed nothing. Both were replaced with a single named constant applied at
the fetch site.
"""

import asyncio
import importlib
import inspect

# Both `from ...ingestion_expert import detect_contradictions` and
# `import ...ingestion_expert.detect_contradictions as dc` resolve via
# attribute lookup on the parent package, which that package's __init__.py
# rebinds to the step *function* re-exported under the same name (shadowing
# the submodule). importlib.import_module reaches the module directly via
# sys.modules instead.
dc = importlib.import_module(
    "orchestrator.experts.handlers.ingestion_expert.detect_contradictions"
)


def test_fetch_source_has_no_env_var_gate():
    """Belt-and-suspenders on top of the repo-wide guardrail in
    shared/tests/test_prompt_single_resolution.py -- names this exact
    function."""
    source = inspect.getsource(dc._fetch_system_instructions_snippet)
    assert "STAFF_SUPPORT_DOC_ID" not in source
    assert "fetch_google_doc_markdown" not in source


def test_truncation_happens_exactly_once_at_the_named_constant():
    """The bundled staff.system prompt (~29.5k chars) exceeds the cap, so a
    real (non-mocked) resolve proves truncation actually engages, not just
    that the constant exists."""
    snippet = asyncio.run(dc._fetch_system_instructions_snippet())
    assert snippet is not None
    assert len(snippet) == dc.SYSTEM_INSTRUCTIONS_CONTEXT_CHARS


def test_build_existing_knowledge_text_does_not_re_truncate():
    """_build_existing_knowledge_text used to slice [:1000] again on top of
    the fetch-site truncation. Feed it text already at the cap and confirm
    every character survives into the formatted block -- a second silent
    truncation would cut this down further."""
    already_at_cap = "x" * dc.SYSTEM_INSTRUCTIONS_CONTEXT_CHARS
    text = dc._build_existing_knowledge_text([], already_at_cap)
    assert already_at_cap in text


def test_none_system_instructions_produces_no_system_block():
    text = dc._build_existing_knowledge_text([], None)
    assert "System Instructions" not in text
