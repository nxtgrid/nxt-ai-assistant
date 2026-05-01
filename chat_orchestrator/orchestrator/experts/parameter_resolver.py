"""Parameter resolution for interactive workflow confirmation.

This module provides expert-level parameter confirmation. Parameters are
defined in the Google Doc expert configuration (### Inputs and ### State
sections), NOT in handler code. This makes the feature general across all
experts and packets without requiring handler code changes.

Resolves current parameter values from multiple sources:
1. User overrides (highest priority)
2. Packet inputs
3. Packet state
4. Previous step results
5. Default values (lowest priority)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from shared.utils.logging import get_logger

if TYPE_CHECKING:
    from orchestrator.experts.step_context import StepContext

LOGGER = get_logger(__name__)


@dataclass
class ParameterDefinition:
    """Definition of a parameter from Google Doc configuration.

    Parsed from ### Inputs or ### State sections in expert definition.
    Format in Google Doc: `field_name: type - Description`
    """

    name: str
    param_type: str = "string"  # string, int, float, bool, list, dict
    description: str = ""
    required: bool = False
    default: Any = None
    editable: bool = True  # Inputs are editable, computed state values are not


@dataclass
class PacketParameterSchema:
    """Schema for a packet's parameters from Google Doc.

    Represents all confirmable parameters for a workflow, parsed from
    the expert's Inputs and State sections in the Google Doc.
    """

    packet_type: str
    parameters: List[ParameterDefinition] = field(default_factory=list)
    description: str = ""


@dataclass
class ResolvedParameter:
    """A resolved parameter with its current value and metadata."""

    name: str
    current_value: Any
    source: str  # "override", "input", "state", "from {step_name}", "default", "unset"
    param_type: str
    description: str
    required: bool
    editable: bool
    is_modified: bool = False  # True if user has overridden this value


def parse_parameters_from_section(
    section_text: str, editable: bool = True
) -> List[ParameterDefinition]:
    """Parse parameter definitions from a Google Doc section.

    Expected format (one per line):
        field_name: type - Description
        another_field: int - Another description

    Also handles simpler formats:
        field_name - Description (assumes string type)
        field_name: type (no description)

    Args:
        section_text: Raw text from ### Inputs or ### State section
        editable: Whether these parameters are user-editable (True for inputs)

    Returns:
        List of ParameterDefinition objects
    """
    parameters: List[ParameterDefinition] = []

    if not section_text:
        return parameters

    # Pattern: field_name: type - Description
    # or: field_name - Description
    # or: field_name: type
    for line in section_text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Remove list markers (-, *, numbers)
        line = re.sub(r"^[-*\d.)\s]+", "", line).strip()
        if not line:
            continue

        # Try to parse: name: type - description
        match = re.match(r"^(\w+)\s*:\s*(\w+)\s*(?:-\s*(.*))?$", line)
        if match:
            name = match.group(1)
            param_type = match.group(2).lower()
            description = match.group(3) or ""
            required = "[required]" in description.lower()
            description = re.sub(r"\[required\]", "", description, flags=re.IGNORECASE).strip()

            parameters.append(
                ParameterDefinition(
                    name=name,
                    param_type=param_type,
                    description=description,
                    required=required,
                    editable=editable,
                )
            )
            continue

        # Try to parse: name - description (assumes string)
        match = re.match(r"^(\w+)\s*-\s*(.+)$", line)
        if match:
            name = match.group(1)
            description = match.group(2)
            required = "[required]" in description.lower()
            description = re.sub(r"\[required\]", "", description, flags=re.IGNORECASE).strip()

            parameters.append(
                ParameterDefinition(
                    name=name,
                    param_type="string",
                    description=description,
                    required=required,
                    editable=editable,
                )
            )
            continue

        # Try to parse: just a name (assumes string, optional)
        match = re.match(r"^(\w+)$", line)
        if match:
            parameters.append(
                ParameterDefinition(
                    name=match.group(1),
                    param_type="string",
                    description="",
                    required=False,
                    editable=editable,
                )
            )

    return parameters


def get_schema_from_packet_state(
    packet_state: Dict[str, Any],
    step_name: str,
    packet_inputs: Optional[Dict[str, Any]] = None,
) -> PacketParameterSchema:
    """Build parameter schema from packet state, showing only editable_ prefixed parameters.

    Editable parameters must be explicitly declared by step handlers using the
    `editable_` prefix in their state_updates. This ensures only intentionally
    user-configurable values are shown for confirmation.

    Example in a step handler:
        return StepResult(
            data={...},
            state_updates={
                "editable_total_buildings": 42,  # Shown to user as "Total Buildings"
                "editable_total_kwp": 15.5,      # Shown to user as "Total Kwp"
                "internal_flag": True,           # NOT shown (no prefix)
            },
        )

    Args:
        packet_state: Current packet state dictionary
        step_name: Name of the step (for display)
        packet_inputs: Original packet inputs (not used - only state with editable_ prefix shown)

    Returns:
        PacketParameterSchema with parameters for editable_ prefixed values only
    """
    parameters: List[ParameterDefinition] = []

    # Only include parameters with editable_ prefix
    EDITABLE_PREFIX = "editable_"

    for key, value in sorted(packet_state.items()):
        # Only show parameters explicitly marked as editable
        if not key.startswith(EDITABLE_PREFIX):
            continue

        # Skip None and empty string values
        if value is None or value == "":
            continue

        # Infer type from value
        param_type = _infer_type(value)

        # For lists, check if it's an enum (list of allowed values)
        # Format: editable_status with value ["pending", "active", "closed"]
        # The first item is the current value, rest are options
        enum_options: Optional[List[str]] = None
        if param_type == "list" and isinstance(value, list) and len(value) > 1:
            # Treat as enum: first value is current, list is all options
            if all(isinstance(v, str) for v in value):
                enum_options = value
                param_type = "enum"

        # Skip complex types that aren't enums
        if param_type in ("dict", "list"):
            continue

        parameters.append(
            ParameterDefinition(
                name=key,  # Keep original key for state updates
                param_type=param_type,
                description=f"Options: {', '.join(enum_options)}" if enum_options else "",
                required=False,
                default=None,
                editable=True,  # All editable_ params are editable by definition
            )
        )

    return PacketParameterSchema(
        packet_type=step_name,
        parameters=parameters,
        description=f"Parameters for {step_name.replace('_', ' ')}",
    )


def _infer_type(value: Any) -> str:
    """Infer parameter type from value."""
    if isinstance(value, bool):
        return "bool"
    elif isinstance(value, int):
        return "int"
    elif isinstance(value, float):
        return "float"
    elif isinstance(value, str):
        return "string"
    elif isinstance(value, list):
        return "list"
    elif isinstance(value, dict):
        return "dict"
    else:
        return "string"


class ParameterResolver:
    """Resolves current parameter values from all sources."""

    def resolve_parameters(
        self,
        schema: PacketParameterSchema,
        context: "StepContext",
    ) -> List[ResolvedParameter]:
        """Resolve current values for all parameters in a schema.

        Resolution priority (highest to lowest):
        1. User overrides from pending_param_overrides in packet_state
        2. Packet inputs (original request data)
        3. Packet state (accumulated during workflow)
        4. Previous step results (if source_hint specifies)
        5. Default values from parameter definition

        Args:
            schema: Packet parameter schema with parameter definitions
            context: Step execution context

        Returns:
            List of ResolvedParameter with current values and sources
        """
        results: List[ResolvedParameter] = []
        overrides = context.get_state("pending_param_overrides", {}) or {}

        for param in schema.parameters:
            # Check for user override first
            if param.name in overrides:
                value = overrides[param.name]
                source = "override"
                is_modified = True
            else:
                value, source = self._resolve_value(param, context)
                is_modified = False

            results.append(
                ResolvedParameter(
                    name=param.name,
                    current_value=value,
                    source=source,
                    param_type=param.param_type,
                    description=param.description,
                    required=param.required,
                    editable=param.editable,
                    is_modified=is_modified,
                )
            )

        return results

    def resolve_from_packet_state(
        self,
        context: "StepContext",
        step_name: str,
    ) -> Tuple[PacketParameterSchema, List[ResolvedParameter]]:
        """Resolve parameters directly from current packet state.

        Shows only editable configuration parameters (strings, numbers),
        not internal status flags or complex objects. If no editable
        parameters are found, returns empty list.

        Args:
            context: Step execution context with packet_state
            step_name: Name of the current step (for display)

        Returns:
            Tuple of (schema, resolved_parameters)
        """
        # Pass both packet_state and packet_inputs to capture user-provided config
        schema = get_schema_from_packet_state(
            context.packet_state,
            step_name,
            packet_inputs=context.packet_inputs,
        )

        if not schema.parameters:
            # No editable parameters - skip confirmation
            return schema, []

        resolved = self.resolve_parameters(schema, context)
        return schema, resolved

    def _resolve_value(
        self,
        param: ParameterDefinition,
        context: "StepContext",
    ) -> Tuple[Any, str]:
        """Resolve value following priority order.

        Args:
            param: Parameter definition
            context: Step execution context

        Returns:
            Tuple of (value, source_description)
        """
        # Check packet_inputs first
        if param.name in context.packet_inputs:
            return context.packet_inputs[param.name], "input"

        # Check parsed_inputs (from LLM parsing steps)
        parsed_inputs = context.packet_state.get("parsed_inputs", {})
        if param.name in parsed_inputs:
            return parsed_inputs[param.name], "parsed"

        # Check packet_state
        if param.name in context.packet_state:
            return context.packet_state[param.name], "state"

        # Check accumulated results for value from previous steps
        for step_name, result in context.accumulated_results.items():
            if isinstance(result, dict) and param.name in result:
                return result[param.name], f"from {step_name}"

        # Use default if available
        if param.default is not None:
            return param.default, "default"

        # Parameter not found anywhere
        return None, "unset"


def get_parameter_resolver() -> ParameterResolver:
    """Get a ParameterResolver instance."""
    return ParameterResolver()


__all__ = [
    "ParameterResolver",
    "ParameterDefinition",
    "PacketParameterSchema",
    "ResolvedParameter",
    "get_parameter_resolver",
    "get_schema_from_packet_state",
    "parse_parameters_from_section",
]
