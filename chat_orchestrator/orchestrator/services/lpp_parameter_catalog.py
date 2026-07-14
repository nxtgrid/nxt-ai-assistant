"""Canonical parameter catalog for Light Preliminary Package requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable

import orchestrator.experts.handlers.package_generator  # noqa: F401
from orchestrator.experts.step_registry import get_step_contract

LPP_CONFIGURABLE_STEPS: tuple[str, ...] = (
    "generate_distribution_layout",
    "generate_powerplant_design",
    "generate_site_layout",
    "populate_lpp_cells",
)


@dataclass(frozen=True)
class LPPParameterDef:
    name: str
    step_name: str
    param_type: str
    description: str
    synonyms: tuple[str, ...]
    required: bool
    default: object = None


def _defs_for_step(step_name: str) -> dict[str, LPPParameterDef]:
    contract = get_step_contract(step_name)
    if contract is None:
        return {}
    out: dict[str, LPPParameterDef] = {}
    for param in contract.params:
        out[param.name] = LPPParameterDef(
            name=param.name,
            step_name=step_name,
            param_type=param.param_type,
            description=param.description,
            synonyms=tuple(param.synonyms),
            required=param.required,
            default=param.default,
        )
    return out


def get_lpp_parameter_catalog() -> Dict[str, Dict[str, LPPParameterDef]]:
    return {step_name: _defs_for_step(step_name) for step_name in LPP_CONFIGURABLE_STEPS}


def get_lpp_parameter_names() -> set[str]:
    names: set[str] = set()
    for params in get_lpp_parameter_catalog().values():
        names.update(params)
    return names


def iter_lpp_parameters(steps: Iterable[str] | None = None) -> Iterable[LPPParameterDef]:
    selected = set(steps or LPP_CONFIGURABLE_STEPS)
    for step_name, params in get_lpp_parameter_catalog().items():
        if step_name not in selected:
            continue
        yield from params.values()


def format_lpp_supported_parameters(steps: list[str] | None = None) -> str:
    selected = set(steps or LPP_CONFIGURABLE_STEPS)
    lines = ["LPP configurable parameters:"]
    for step_name in LPP_CONFIGURABLE_STEPS:
        if step_name not in selected:
            continue
        params = _defs_for_step(step_name)
        if not params:
            continue
        lines.append(f"\n{step_name}:")
        for param in params.values():
            alias_text = f" Aliases: {', '.join(param.synonyms)}." if param.synonyms else ""
            default_text = f" Default: {param.default}." if param.default is not None else ""
            lines.append(
                f"- {param.name} ({param.param_type}): {param.description}"
                f"{default_text}{alias_text}"
            )
    return "\n".join(lines)


__all__ = [
    "LPPParameterDef",
    "LPP_CONFIGURABLE_STEPS",
    "format_lpp_supported_parameters",
    "get_lpp_parameter_catalog",
    "get_lpp_parameter_names",
    "iter_lpp_parameters",
]
