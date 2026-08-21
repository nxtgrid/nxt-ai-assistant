"""The value catalogue a comment's free text is matched against.

Replaces populate_cells.py's hand-written _collect_all_available_values()
dict. That function only knew about LPP's four producer steps; this walks
the registry instead, so any expert whose steps declare OutputSpecs gets a
catalogue for free -- which is what makes "supply any Doc or Sheet as a
template" work outside package_generator.

The LLM matches on `description`, not on `path`: a bare key name like
`energy.total_kwp` is not something a template author would write in a
comment, but "the total peak capacity" matches its description.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from orchestrator.experts.step_contracts import StepContract
from orchestrator.experts.step_registry import get_step_contract, get_step_registry

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogueEntry:
    """One resolvable value, with the prose an LLM matches a comment against."""

    path: str
    value: Any
    value_type: str
    description: str
    produced_by: str


def build_catalogue_from(
    contracts: Mapping[str, StepContract],
    accumulated_results: Mapping[str, Any],
    packet_state: Mapping[str, Any],
) -> list[CatalogueEntry]:
    """Pure core: catalogue for a given set of contracts and run state.

    Separated from `build_catalogue` so it is unit-testable without a
    populated global registry.
    """
    entries: list[CatalogueEntry] = []
    for step_name, contract in contracts.items():
        # "Has this step run" is checked per-output, not per-step: a state
        # output is available whenever packet_state has it, independent of
        # whether this step also left a data entry in accumulated_results
        # (a step producing only state -- e.g. resolve_sites' site_id --
        # can genuinely have no data entry at all).
        step_data = accumulated_results.get(step_name)
        for spec in contract.outputs:
            if spec.where == "state":
                if spec.name not in packet_state:
                    continue
                value = packet_state[spec.name]
            else:
                if not isinstance(step_data, Mapping) or spec.name not in step_data:
                    continue
                value = step_data[spec.name]
            if value is None:
                continue
            entries.append(
                CatalogueEntry(
                    path=spec.name,
                    value=value,
                    value_type=spec.value_type,
                    description=spec.description,
                    produced_by=step_name,
                )
            )
    return entries


def build_catalogue(context) -> list[CatalogueEntry]:
    """Catalogue for a live StepContext, from the global handler registry."""
    registry = get_step_registry()
    contracts: dict[str, StepContract] = {}
    for name in registry.list_handlers():
        contract = get_step_contract(name)
        if contract is not None and contract.outputs:
            contracts[name] = contract
    return build_catalogue_from(
        contracts=contracts,
        accumulated_results=context.accumulated_results or {},
        packet_state=context.packet_state or {},
    )


def render_catalogue(entries: list[CatalogueEntry]) -> str:
    """The catalogue as a prompt block: path, type, description, current value."""
    lines = []
    for e in entries:
        value_repr = json.dumps(e.value, default=str)
        if len(value_repr) > 120:
            value_repr = value_repr[:117] + "..."
        lines.append(f"- {e.path} ({e.value_type}): {e.description} [current value: {value_repr}]")
    return "\n".join(lines)
