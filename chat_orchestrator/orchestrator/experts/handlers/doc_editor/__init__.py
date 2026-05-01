"""Google Docs comment-driven and instruction-driven editor.

Processes @anansibot comments in Google Docs, or edits sections
based on chat instructions with high-confidence section matching.
"""

from orchestrator.experts.handlers.doc_editor.process_doc_edits import process_doc_edits

__all__ = ["process_doc_edits"]
