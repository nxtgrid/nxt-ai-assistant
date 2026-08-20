"""Sheet values -> markdown table."""

from shared.utils.gdrive_doc_fetcher import rows_to_markdown_table


def test_a_simple_sheet_becomes_a_markdown_table():
    table = rows_to_markdown_table([["Code", "Meaning"], ["E01", "Undervoltage"]])

    assert table == (
        "| Code | Meaning |\n"
        "| --- | --- |\n"
        "| E01 | Undervoltage |"
    )


def test_ragged_rows_are_padded_to_the_header_width():
    """Sheets omits trailing empty cells; an unpadded row renders as garbage."""
    table = rows_to_markdown_table([["A", "B", "C"], ["1"]])

    assert table.splitlines()[-1] == "| 1 |  |  |"


def test_cells_wider_than_the_header_are_dropped():
    table = rows_to_markdown_table([["A"], ["1", "2", "3"]])

    assert table.splitlines()[-1] == "| 1 |"


def test_pipes_in_a_cell_are_escaped():
    table = rows_to_markdown_table([["A"], ["x|y"]])

    assert "x\\|y" in table


def test_newlines_in_a_cell_become_spaces():
    table = rows_to_markdown_table([["A"], ["line1\nline2"]])

    assert "| line1 line2 |" in table


def test_fully_blank_rows_are_dropped():
    table = rows_to_markdown_table([["A"], ["", "  "], ["1"]])

    assert table.splitlines() == ["| A |", "| --- |", "| 1 |"]


def test_an_empty_sheet_returns_empty_string():
    assert rows_to_markdown_table([]) == ""
    assert rows_to_markdown_table([[""]]) == ""


def test_a_row_cap_truncates_and_says_so():
    rows = [["N"]] + [[str(i)] for i in range(10)]

    table = rows_to_markdown_table(rows, max_rows=3)

    assert "_(truncated: showing first 3 of 10 rows)_" in table
    assert "| 3 |" not in table


def test_an_untruncated_table_has_no_footer():
    table = rows_to_markdown_table([["N"], ["1"]], max_rows=50)

    assert "truncated" not in table


def test_a_char_cap_drops_whole_rows_never_partial_ones():
    rows = [["N"]] + [[f"value-{i:03d}"] for i in range(100)]

    table = rows_to_markdown_table(rows, max_rows=100, max_chars=200)

    body = [ln for ln in table.splitlines() if ln.startswith("| value-")]
    assert body, "expected at least one data row to survive"
    assert all(ln.endswith(" |") for ln in body)
    assert "truncated" in table
