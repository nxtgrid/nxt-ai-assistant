from orchestrator.services.command_parser import parse_lpp_anchor_args, parse_lpp_technology_family


def test_parse_anchor_full():
    parsed = parse_lpp_anchor_args('anchor:6.8244,4.5909 name:"Test Community"')
    assert parsed == {
        "latitude": "6.8244",
        "longitude": "4.5909",
        "community_name": "Test Community",
    }


def test_parse_anchor_no_name():
    parsed = parse_lpp_anchor_args("anchor:6.8244,4.5909")
    assert parsed["latitude"] == "6.8244"
    assert parsed["longitude"] == "4.5909"
    assert parsed.get("community_name") in (None, "")


def test_parse_bare_coords_no_space():
    parsed = parse_lpp_anchor_args("6.8244,4.5909")
    assert parsed == {"latitude": "6.8244", "longitude": "4.5909"}


def test_parse_bare_coords_with_space():
    parsed = parse_lpp_anchor_args("6.8244, 4.5909")
    assert parsed == {"latitude": "6.8244", "longitude": "4.5909"}


def test_parse_embedded_coords_from_natural_language_lpp_request():
    parsed = parse_lpp_anchor_args(
        "Can you create an LPP for the site located at 9.3947551,9.3176320 using Deye technology?"
    )
    assert parsed == {"latitude": "9.3947551", "longitude": "9.3176320"}


def test_parse_negative_bare_coords():
    parsed = parse_lpp_anchor_args("-1.5, 36.8")
    assert parsed == {"latitude": "-1.5", "longitude": "36.8"}


def test_parse_anchor_absent_returns_none():
    assert parse_lpp_anchor_args("ExampleSite") is None


def test_parse_site_names_not_treated_as_coords():
    assert parse_lpp_anchor_args("Site1, Site2") is None


def test_parse_out_of_range_falls_through_to_submission():
    assert parse_lpp_anchor_args("12, 5000") is None


def test_parse_lpp_technology_family_deye():
    assert (
        parse_lpp_technology_family(
            "Can you create an LPP for 9.3947551,9.3176320 using Deye technology?"
        )
        == "deye"
    )


def test_parse_lpp_technology_family_victron():
    assert parse_lpp_technology_family("create an LPP with Victron container design") == "victron"


def test_parse_lpp_technology_family_absent_returns_none():
    assert parse_lpp_technology_family("create an LPP for ExampleSite") is None
