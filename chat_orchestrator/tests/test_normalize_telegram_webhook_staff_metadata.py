"""_normalize_telegram_webhook must carry forward the staff-group auth
bypass metadata set upstream by async_main's staff-group pre-check (see
handler.py's "STAFF GROUP + UNKNOWN GROUP PRE-CHECK" block, which sets
args["metadata"]["staff_group_auth"] / ["staff_group_organization_id"]
after verifying the sender).

Regression test: the normalized request's "metadata" key is built as a
brand new dict here (telegram_message_id/chat_type/chat_title only), with
no merge of the input args["metadata"] at all. So even a fully-working
staff-group pre-check upstream had its two flags silently dropped before
resolve_auth.py -- the only place that reads them -- ever saw them; only
"_auth_method" was ever explicitly carried through (see the line just above
the final LOGGER.info/return in this function).
"""

from __future__ import annotations

import handler


def _group_reply_args(**metadata_overrides):
    args = {
        "message": {
            "message_id": 1,
            "from": {"id": 111, "username": "staffer", "first_name": "Staffer"},
            "text": "status check",
            "chat": {"id": -1009999, "type": "supergroup", "title": "Internal Ops"},
            "message_thread_id": 7,
        },
    }
    if metadata_overrides:
        args["metadata"] = metadata_overrides
    return args


def test_carries_forward_staff_group_auth_metadata():
    args = _group_reply_args(staff_group_auth=True, staff_group_organization_id=2)
    result = handler._normalize_telegram_webhook(args)
    assert result["metadata"]["staff_group_auth"] is True
    assert result["metadata"]["staff_group_organization_id"] == 2


def test_absent_when_not_set_upstream():
    args = _group_reply_args()
    result = handler._normalize_telegram_webhook(args)
    assert "staff_group_auth" not in result["metadata"]


def test_does_not_clobber_the_freshly_built_metadata_fields():
    args = _group_reply_args(staff_group_auth=True, staff_group_organization_id=2)
    result = handler._normalize_telegram_webhook(args)
    assert result["metadata"]["chat_type"] == "supergroup"
    assert result["metadata"]["telegram_message_id"] == 1
    assert result["metadata"]["chat_title"] == "Internal Ops"


def test_ignores_a_falsy_staff_group_auth():
    """Only an affirmatively-true flag should survive -- an absent or False
    value must not manufacture the key on the normalized side."""
    args = _group_reply_args(staff_group_auth=False)
    result = handler._normalize_telegram_webhook(args)
    assert "staff_group_auth" not in result["metadata"]
