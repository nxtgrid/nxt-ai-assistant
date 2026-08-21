"""File-type-agnostic Drive comment scanning."""

from shared.utils.file_annotations import Annotation, build_thread_instruction, strip_bot_mention


def test_strip_bot_mention_removes_the_mention_and_trims():
    assert strip_bot_mention("@anansi-chatbot fill this in") == "fill this in"
    assert strip_bot_mention("@anansi-chatbot.iam.gserviceaccount.com do it") == "do it"


def test_build_thread_instruction_concatenates_same_author_replies():
    comment = {
        "content": "@anansi-chatbot make it formal",
        "author": {"emailAddress": "a@x.com", "displayName": "Ada"},
        "replies": [
            {"content": "and shorter", "author": {"emailAddress": "a@x.com", "displayName": "Ada"}},
        ],
    }
    assert build_thread_instruction(comment, "Ada") == "make it formal\nand shorter"


def test_build_thread_instruction_attributes_multiple_authors():
    comment = {
        "content": "@anansi-chatbot make it formal",
        "author": {"emailAddress": "a@x.com", "displayName": "Ada"},
        "replies": [
            {"content": "and shorter", "author": {"emailAddress": "b@x.com", "displayName": "Bob"}},
        ],
    }
    result = build_thread_instruction(comment, "Ada")
    assert "[Ada]: make it formal" in result
    assert "[Bob]: and shorter" in result


def test_annotation_carries_quoted_text_and_comment_id():
    ann = Annotation(
        comment_id="c1", quoted_text="{{total_kwp}}", instruction="the peak capacity",
        author_email="a@x.com", created_time="2026-08-21T00:00:00Z",
    )
    assert ann.comment_id == "c1"
    assert ann.quoted_text == "{{total_kwp}}"
