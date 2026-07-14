"""User-facing help for LPP configurable parameters."""

from __future__ import annotations

from orchestrator.services.lpp_parameter_catalog import format_lpp_supported_parameters

STEP_ALIASES = {
    "design": "generate_powerplant_design",
    "power plant design": "generate_powerplant_design",
    "generate_powerplant_design": "generate_powerplant_design",
    "layout": "generate_site_layout",
    "site layout": "generate_site_layout",
    "generate_site_layout": "generate_site_layout",
    "distribution": "generate_distribution_layout",
    "distribution layout": "generate_distribution_layout",
    "populate": "populate_lpp_cells",
}


def detect_lpp_parameter_help_request(text: str) -> bool:
    lowered = text.lower()
    has_lpp_context = any(
        phrase in lowered
        for phrase in (
            "lpp",
            "preliminary package",
            "generate_powerplant_design",
            "generate_site_layout",
            "power plant design",
            "site layout",
        )
    )
    asks_params = any(
        phrase in lowered
        for phrase in (
            "what parameters",
            "which parameters",
            "what can i configure",
            "configurable parameters",
            "supported parameters",
            "what inputs",
            "which inputs",
        )
    )
    return has_lpp_context and asks_params


def _requested_steps(text: str) -> list[str] | None:
    lowered = text.lower()
    steps = [step for alias, step in STEP_ALIASES.items() if alias in lowered]
    return list(dict.fromkeys(steps)) or None


def format_lpp_parameter_help(text: str) -> str:
    return format_lpp_supported_parameters(_requested_steps(text))


__all__ = [
    "detect_lpp_parameter_help_request",
    "format_lpp_parameter_help",
]
