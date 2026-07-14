from orchestrator.graphs.nodes.expert_handler import _build_lpp_packet_inputs


def test_packet_inputs_anchor_route():
    inputs = _build_lpp_packet_inputs(
        packet_type="light_preliminary_package",
        effective_request='/lpp anchor:6.8244,4.5909 name:"Commville"',
        expert_command="/lpp",
        key_entity=None,
        args='anchor:6.8244,4.5909 name:"Commville"',
    )
    assert inputs["latitude"] == "6.8244"
    assert inputs["longitude"] == "4.5909"
    assert inputs["community_name"] == "Commville"
    assert "site_name" not in inputs


def test_packet_inputs_bare_coords_route():
    inputs = _build_lpp_packet_inputs(
        packet_type="light_preliminary_package",
        effective_request="/lpp 6.8244, 4.5909",
        expert_command="/lpp",
        key_entity="6.8244",
        args="6.8244, 4.5909",
    )
    assert inputs["latitude"] == "6.8244"
    assert inputs["longitude"] == "4.5909"
    assert "site_name" not in inputs


def test_packet_inputs_embedded_coords_route():
    inputs = _build_lpp_packet_inputs(
        packet_type="light_preliminary_package",
        effective_request=(
            "/lpp create an LPP for the site located at 9.3947551,9.3176320 using Deye technology"
        ),
        expert_command="/lpp",
        key_entity="for the site",
        args="create an LPP for the site located at 9.3947551,9.3176320 using Deye technology",
    )
    assert inputs["latitude"] == "9.3947551"
    assert inputs["longitude"] == "9.3176320"
    assert inputs["technology_family"] == "deye"
    assert "site_name" not in inputs


def test_packet_inputs_preserve_technology_from_original_nl_request():
    inputs = _build_lpp_packet_inputs(
        packet_type="light_preliminary_package",
        effective_request="/lpp 9.3947551,9.3176320",
        expert_command="/lpp 9.3947551,9.3176320",
        key_entity="9.3947551,9.3176320",
        args="9.3947551,9.3176320",
        raw_request=(
            "Can you create an LPP for the site located at "
            "9.3947551,9.3176320 using Deye technology?"
        ),
    )
    assert inputs["latitude"] == "9.3947551"
    assert inputs["longitude"] == "9.3176320"
    assert inputs["technology_family"] == "deye"
    assert "using Deye technology" in inputs["raw_request"]


def test_packet_inputs_submission_route_unchanged():
    inputs = _build_lpp_packet_inputs(
        packet_type="light_preliminary_package",
        effective_request="/lpp ExampleSite",
        expert_command="/lpp",
        key_entity="ExampleSite",
        args="ExampleSite",
    )
    assert inputs["site_name"] == "ExampleSite"
    assert "latitude" not in inputs
