from orchestrator.services.intent_router import _normalize_route, should_try_intent_router


def test_should_try_intent_router_for_workflow_language():
    assert should_try_intent_router("Please prepare the preliminary package for Site Alpha")


def test_should_not_try_intent_router_for_ordinary_chat():
    assert not should_try_intent_router("What is the latest update from the customer?")


def test_normalize_route_accepts_high_confidence_lpp_site():
    route = _normalize_route(
        {
            "should_route_to_expert": True,
            "confidence": 0.91,
            "command": "/lpp",
            "packet_type": "light_preliminary_package",
            "key_entity": "Site Alpha",
            "args": "Site Alpha",
        }
    )
    assert route == {
        "command": "/lpp",
        "packet_type": "light_preliminary_package",
        "key_entity": "Site Alpha",
        "args": "Site Alpha",
    }


def test_normalize_route_converts_lpp_coordinates_to_args():
    route = _normalize_route(
        {
            "should_route_to_expert": True,
            "confidence": 0.94,
            "command": "/lpp",
            "packet_type": "light_preliminary_package",
            "latitude": "9.3947551",
            "longitude": "9.3176320",
        }
    )
    assert route == {
        "command": "/lpp",
        "packet_type": "light_preliminary_package",
        "key_entity": "9.3947551,9.3176320",
        "args": "9.3947551,9.3176320",
    }


def test_normalize_route_rejects_low_confidence():
    assert (
        _normalize_route(
            {
                "should_route_to_expert": True,
                "confidence": 0.4,
                "command": "/lpp",
                "packet_type": "light_preliminary_package",
                "key_entity": "Site Alpha",
                "args": "Site Alpha",
            }
        )
        is None
    )
