"""Cross-tab cell location by quoted comment text."""

from shared.utils.sheet_editing import CellMatch, find_cells_in_grids, index_to_column_letter

GRIDS = {
    "Main Input": [
        ["Total kWp", "{{total_kwp}}"],
        ["Site Name", "{{site_name}}"],
        ["Note", "It&#39;s fine"],
    ],
    "Second": [
        ["{{total_kwp}}"],
    ],
}


def test_index_to_column_letter_handles_multi_letter_columns():
    assert index_to_column_letter(0) == "A"
    assert index_to_column_letter(25) == "Z"
    assert index_to_column_letter(26) == "AA"
    assert index_to_column_letter(27) == "AB"


def test_finds_a_unique_token_in_one_tab():
    # {{site_name}} is GRIDS["Main Input"][1][1] -- row 2, column B (not A;
    # column A on that row holds the label "Site Name").
    matches = find_cells_in_grids(GRIDS, "{{site_name}}")
    assert matches == [CellMatch(tab="Main Input", a1="B2", row=2, column=2)]


def test_finds_a_repeated_token_across_tabs():
    matches = find_cells_in_grids(GRIDS, "{{total_kwp}}")
    assert len(matches) == 2
    assert {(m.tab, m.a1) for m in matches} == {("Main Input", "B1"), ("Second", "A1")}


def test_html_unescapes_before_matching():
    """Spike 0: quotedFileContent is served as text/html."""
    matches = find_cells_in_grids(GRIDS, "It&#39;s fine")
    assert len(matches) == 1
    assert matches[0].a1 == "B3"


def test_html_unescaped_needle_matches_escaped_cell():
    matches = find_cells_in_grids(GRIDS, "It's fine")
    assert len(matches) == 1
    assert matches[0].a1 == "B3"


def test_returns_empty_for_text_not_present():
    assert find_cells_in_grids(GRIDS, "nothing like this") == []


def test_returns_empty_for_empty_needle():
    """An empty cell quotes nothing — never match everything."""
    assert find_cells_in_grids(GRIDS, "") == []
    assert find_cells_in_grids(GRIDS, "   ") == []


# Recorded from Spike 0 against NXT-3235 - GridV Technical Review.
# Comment AAAB0jIG6Kc quoted text that had since been edited (73% similar);
# comment AAABnuBYGB4 matched 14 cells.
STALE_QUOTE = (
    "HPS Hours were 18.6h, falling short of the 22h target and slightly down "
    "from 20.1h the previous month."
)
CURRENT_CELL = (
    "HPS Hours were 19.2h, falling short of the 22h target and slightly down "
    "from 20.2h the previous month."
)


def test_a_stale_quote_finds_nothing_rather_than_the_closest_cell():
    grids = {"2025 Review": [[CURRENT_CELL]]}
    assert find_cells_in_grids(grids, STALE_QUOTE) == []


def test_a_repeated_value_returns_every_match_not_the_first():
    grids = {"Meter Issues": [["To be checked"] for _ in range(14)]}
    matches = find_cells_in_grids(grids, "To be checked")
    assert len(matches) == 14
