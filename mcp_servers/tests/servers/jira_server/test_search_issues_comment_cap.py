"""Regression test: search_issues_with_comments embeds full comment threads for
every matching issue with no size limit. A broad query against tickets with long
comment histories produced a payload large enough to exceed Gemini's 1,048,576
token context window, which surfaced to the end user as a generic "something
went wrong" error instead of any useful search result.
"""

import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.jira_server.jira_mcp_server import (  # noqa: E402
    _MAX_COMMENT_BODY_CHARS,
    _MAX_COMMENTS_PER_ISSUE,
    _cap_comments_for_context,
)


def _comment(comment_id: int, body: str) -> dict:
    return {"id": comment_id, "author": "someone", "created": "", "updated": "", "body": body}


def test_caps_comment_count_to_most_recent():
    comments = [_comment(i, "short") for i in range(50)]

    capped = _cap_comments_for_context(comments)

    assert len(capped) == _MAX_COMMENTS_PER_ISSUE
    # Keeps the most recent comments (tail of the list), not the oldest.
    assert [c["id"] for c in capped] == list(range(50 - _MAX_COMMENTS_PER_ISSUE, 50))


def test_truncates_long_comment_bodies():
    comments = [_comment(1, "x" * 5000)]

    capped = _cap_comments_for_context(comments)

    assert len(capped[0]["body"]) == _MAX_COMMENT_BODY_CHARS + len("... [truncated]")
    assert capped[0]["body"].endswith("... [truncated]")


def test_leaves_short_comments_and_small_lists_untouched():
    comments = [_comment(1, "hello"), _comment(2, "world")]

    capped = _cap_comments_for_context(comments)

    assert capped == comments


def test_empty_comments_list():
    assert _cap_comments_for_context([]) == []
