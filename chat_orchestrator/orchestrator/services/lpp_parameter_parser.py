"""Deterministic natural-language parser for LPP configurable parameters."""

from __future__ import annotations

import re
from typing import Any

from orchestrator.services.command_parser import parse_lpp_technology_family
from orchestrator.services.lpp_parameter_catalog import get_lpp_parameter_names

_NUMBER = r"(\d+(?:,\d{3})*(?:\.\d+)?)"


def _put_allowed(params: dict[str, Any], key: str, value: Any) -> None:
    if key in get_lpp_parameter_names() and value is not None:
        params[key] = value


def _number(value: str) -> float:
    return float(value.replace(",", ""))


def _int_number(value: str) -> int:
    return int(round(_number(value)))


def _coerce_number(raw: str) -> int | float:
    return _number(raw) if "." in raw else _int_number(raw)


def parse_lpp_request_parameters(text: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if not text:
        return params

    lowered = text.lower()

    family = parse_lpp_technology_family(text)
    if family:
        _put_allowed(params, "technology_family", family)

    patterns = (
        (
            "initial_residential_connections",
            rf"{_NUMBER}\s+(?:residential|household)\s+(?:connections|buildings|customers)",
        ),
        (
            "initial_business_connections",
            rf"{_NUMBER}\s+(?:business|commercial|non[- ]?residential)\s+(?:connections|buildings|customers)",
        ),
        (
            "wp_per_conn_override",
            rf"{_NUMBER}\s*(?:wp\s*/\s*conn|wp\s+per\s+conn|wp\s+per\s+connection|watts\s+per\s+connection)",
        ),
        ("editable_total_kwp", rf"(?:target\s+)?{_NUMBER}\s*kwp\b"),
        ("editable_total_kwh", rf"(?:target\s+)?{_NUMBER}\s*kwh\b"),
        ("anchor_load_kw", rf"{_NUMBER}\s*kw\s+(?:anchor|pue)\s+load"),
        ("pue_hours_per_day", rf"{_NUMBER}\s*(?:hours|hrs)\s*(?:/|per)?\s*day"),
        ("target_tariff_usd", rf"(?:tariff|price)\s+(?:of\s+)?(?:usd\s*)?{_NUMBER}"),
        ("target_tariff_usd", rf"{_NUMBER}\s*(?:usd|dollars?)\s*(?:/|per)?\s*kwh"),
        ("num_poc_teams", rf"{_NUMBER}\s+(?:poc|meter(?:ing)?)\s+teams?"),
        (
            "initial_3phase_connections",
            rf"{_NUMBER}\s+(?:3[- ]?phase|three[- ]?phase)\s+(?:connections|customers|buildings)",
        ),
    )
    for key, pattern in patterns:
        match = re.search(pattern, lowered, re.IGNORECASE)
        if not match:
            continue
        _put_allowed(params, key, _coerce_number(match.group(1)))

    if re.search(r"\b(force|use|make)\s+3[- ]?phase\b|\b3[- ]?phase\s+design\b", lowered):
        _put_allowed(params, "force_3phase", True)

    if re.search(r"\b(no|without|not)\s+(?:nigeria\s+)?dares\b|\bregulation\s*[:=]\s*none\b", lowered):
        _put_allowed(params, "regulation_constraint", "None")
    elif re.search(r"\bdares\b", lowered):
        _put_allowed(params, "regulation_constraint", "Nigeria - DARES")

    if re.search(r"\bess\s+(?:layout|site\s+type)\b", lowered):
        _put_allowed(params, "editable_site_type", "ess")
    elif (
        re.search(r"\bvictron\s+(?:layout|site\s+type|container)\b", lowered)
        and params.get("technology_family") != "deye"
    ):
        _put_allowed(params, "editable_site_type", "victron")

    panel = re.search(r"\b(\d+s\d+p)\b", text, re.IGNORECASE)
    if panel:
        _put_allowed(params, "editable_panel_config", panel.group(1).upper())

    return params


def merge_lpp_parameter_inputs(*texts: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for text in texts:
        merged.update(parse_lpp_request_parameters(text))
    return merged


__all__ = ["merge_lpp_parameter_inputs", "parse_lpp_request_parameters"]
