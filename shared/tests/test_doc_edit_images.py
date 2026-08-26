"""The anansi-chart:N placeholder protocol."""

from shared.utils.doc_edit_images import substitute_chart_refs


def test_a_reference_becomes_an_inline_base64_image():
    out = substitute_chart_refs("Before\n\n![Power](anansi-chart:1)\n\nAfter", ["QUJD"])
    assert "![Power](base64:QUJD)" in out
    assert "anansi-chart" not in out


def test_references_are_one_based_and_ordered():
    out = substitute_chart_refs(
        "![A](anansi-chart:2)\n\n![B](anansi-chart:1)", ["FIRST", "SECOND"]
    )
    assert "![A](base64:SECOND)" in out
    assert "![B](base64:FIRST)" in out


def test_a_reference_with_no_image_is_dropped_not_left_dangling():
    """A hallucinated reference must not reach Apps Script, which would
    write the literal text 'anansi-chart:3' into the document."""
    out = substitute_chart_refs("Text\n\n![Ghost](anansi-chart:3)\n\nMore", [])
    assert "anansi-chart" not in out
    assert "base64" not in out
    assert "Text" in out and "More" in out


def test_markdown_with_no_images_fetched_is_untouched():
    """No tool call happened (or none returned an image) -- nothing to
    substitute, nothing to append."""
    src = "## Heading\n\n- a\n- b"
    assert substitute_chart_refs(src, []) == src


def test_an_unreferenced_image_is_appended_rather_than_lost():
    """The model fetched a chart and forgot to place it. Appending is better
    than silently discarding work the user asked for."""
    out = substitute_chart_refs("Some prose.", ["QUJD"])
    assert "![Chart](base64:QUJD)" in out
