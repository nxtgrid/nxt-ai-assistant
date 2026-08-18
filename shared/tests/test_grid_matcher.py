from shared.utils.grid_matcher import find_grid_mention


def test_find_grid_mention_matches_a_name_mentioned_in_a_longer_sentence():
    result = find_grid_mention(
        "The grid in KUDI is down right now", ["Kudi", "Site Alpha"]
    )
    assert result == "Kudi"


def test_find_grid_mention_matches_a_multi_word_name():
    result = find_grid_mention(
        "can someone check site alpha please, the meters are offline",
        ["Kudi", "Site Alpha"],
    )
    assert result == "Site Alpha"


def test_find_grid_mention_returns_none_when_no_candidate_is_mentioned():
    result = find_grid_mention(
        "my meter is broken and I have no power", ["Kudi", "Site Alpha"]
    )
    assert result is None


def test_find_grid_mention_matches_a_typo_that_still_clears_the_threshold():
    # "ste alpha" (missing the "i") scores exactly 90 against "Site Alpha" --
    # right at the threshold, proving this isn't an exact-match-only check.
    result = find_grid_mention(
        "the outage seems to be at ste alpha right now", ["Kudi", "Site Alpha"]
    )
    assert result == "Site Alpha"


def test_find_grid_mention_rejects_an_ambiguous_match():
    # "kudi" alone scores 100 against both candidates -- too close to call.
    result = find_grid_mention("kudi", ["Kudi A", "Kudi B"])
    assert result is None


def test_find_grid_mention_returns_none_for_empty_text():
    assert find_grid_mention("", ["Kudi"]) is None


def test_find_grid_mention_returns_none_for_empty_candidate_list():
    assert find_grid_mention("the grid in kudi is down", []) is None
