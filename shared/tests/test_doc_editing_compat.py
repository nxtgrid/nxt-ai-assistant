"""doc_editing keeps its public surface after the spine extraction.

Both mcp_servers/servers/knowledge_server/knowledge_mcp_server.py and
orchestrator/experts/handlers/doc_editor/process_doc_edits.py import these
names directly; moving the implementation must not move the imports.
"""

from shared.utils import doc_editing
from shared.utils.file_annotations import BOT_MENTION as SPINE_MENTION


def test_public_names_are_still_importable():
    for name in ("scan_comments", "edit_section", "pin_revision",
                 "get_comment_by_id", "generate_replacement_markdown"):
        assert hasattr(doc_editing, name), f"doc_editing.{name} disappeared"


def test_bot_mention_is_the_shared_one_not_a_second_copy():
    assert doc_editing.BOT_MENTION is SPINE_MENTION
