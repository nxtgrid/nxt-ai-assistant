from orchestrator.models.work_packets import LPPInputs


def test_lpp_inputs_accept_design_and_layout_parameters():
    inputs = LPPInputs(
        raw_request="create LPP",
        technology_family="deye",
        initial_residential_connections=120,
        initial_business_connections=35,
        wp_per_conn_override=850,
        editable_total_kwp=45.5,
        editable_total_kwh=148.8,
        editable_site_type="ess",
        editable_panel_config="20S2P",
    )

    assert inputs.technology_family == "deye"
    assert inputs.initial_residential_connections == 120
    assert inputs.wp_per_conn_override == 850
