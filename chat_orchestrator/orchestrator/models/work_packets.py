"""Work packet models using composition pattern.

Shapes are reusable building blocks that can be composed into
packet-specific schemas. This follows "additive composition"
rather than inheritance.

Usage:
    from orchestrator.models.work_packets import (
        WorkPacket,
        PacketStatus,
        GridAnalysisInputs,
        validate_packet_data,
    )

    # Validate inputs for a packet type
    validated = validate_packet_data("grid_analysis", "inputs", data)

    # Create typed packet
    packet: WorkPacket[GridAnalysisInputs, GridAnalysisState, GridAnalysisOutputs]
"""

import os
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar

from pydantic import BaseModel, Field

# =============================================================================
# COMPOSABLE SHAPES (Reusable Building Blocks)
# =============================================================================


class GridReference(BaseModel):
    """Shape: References a specific grid."""

    grid_name: str = Field(description="Human-readable grid name")
    grid_id: Optional[int] = Field(default=None, description="Database grid ID")
    site_id: Optional[str] = Field(default=None, description="VRM site ID for API calls")


class TimeRange(BaseModel):
    """Shape: A time period for analysis."""

    start_date: datetime = Field(description="Start of time range")
    end_date: datetime = Field(description="End of time range")
    timezone: str = Field(
        default=os.getenv("DEFAULT_TIMEZONE", "UTC"), description="Timezone for display"
    )


class ProgressInfo(BaseModel):
    """Shape: Progress tracking for long-running work."""

    percent_complete: int = Field(default=0, ge=0, le=100)
    current_action: Optional[str] = Field(
        default=None, description="What the expert is currently doing"
    )
    steps_total: int = Field(default=0, ge=0)
    steps_done: int = Field(default=0, ge=0)


class ExternalDocRef(BaseModel):
    """Shape: Reference to external document (Google Docs, Jira, etc.)."""

    system: str = Field(description="External system: google_docs, jira, notion")
    doc_id: str = Field(description="Document/issue ID in external system")
    url: Optional[str] = Field(default=None, description="Direct URL to document")
    version: Optional[str] = Field(default=None, description="Version/revision tracking")


class ToolCallRecord(BaseModel):
    """Shape: Record of a tool invocation during workflow."""

    tool_name: str = Field(description="Name of MCP tool called")
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result_summary: Optional[str] = Field(default=None, description="Brief summary of result")
    called_at: datetime = Field(default_factory=datetime.utcnow)
    success: bool = Field(default=True)
    error: Optional[str] = Field(default=None)


# =============================================================================
# PACKET STATUS ENUM
# =============================================================================


class PacketStatus(str, Enum):
    """Status of a work packet."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    AWAITING_INPUT = "awaiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# =============================================================================
# PACKET TYPE: GRID_ANALYSIS
# =============================================================================


class GridAnalysisInputs(BaseModel):
    """Inputs for grid analysis packet."""

    grid: GridReference = Field(description="Grid to analyze")
    time_range: TimeRange = Field(description="Time period to analyze")
    analysis_focus: Optional[str] = Field(
        default="all", description="Focus area: battery, solar, faults, or all"
    )
    include_comparisons: bool = Field(
        default=False, description="Include comparisons to previous period"
    )


class GridAnalysisState(BaseModel):
    """Working state during grid analysis."""

    progress: ProgressInfo = Field(default_factory=ProgressInfo)
    metrics_fetched: bool = Field(default=False)
    alerts_fetched: bool = Field(default=False)
    faults_analyzed: bool = Field(default=False)
    key_findings: List[str] = Field(default_factory=list)
    tool_calls: List[ToolCallRecord] = Field(default_factory=list)
    accumulated_results: Dict[str, Any] = Field(
        default_factory=dict, description="Results from completed steps"
    )


class GridAnalysisOutputs(BaseModel):
    """Outputs from completed grid analysis."""

    summary: str = Field(description="Executive summary of analysis")
    findings: List[str] = Field(default_factory=list, description="Key findings")
    recommendations: List[str] = Field(default_factory=list, description="Recommended actions")
    external_doc: Optional[ExternalDocRef] = Field(
        default=None, description="Link to full report document"
    )
    metrics_snapshot: Optional[Dict[str, Any]] = Field(
        default=None, description="Raw metrics data for reference"
    )


# =============================================================================
# PACKET TYPE: KPI_REPORT
# =============================================================================


class KPIReportInputs(BaseModel):
    """Inputs for KPI report generation."""

    grids: List[GridReference] = Field(description="Grids to include in report")
    time_range: TimeRange = Field(description="Reporting period")
    report_type: str = Field(default="weekly", description="Report type: daily, weekly, monthly")
    sections_requested: List[str] = Field(
        default_factory=lambda: ["overview", "performance", "issues"],
        description="Report sections to generate",
    )


class KPIReportState(BaseModel):
    """Working state during KPI report generation."""

    progress: ProgressInfo = Field(default_factory=ProgressInfo)
    grids_processed: List[str] = Field(default_factory=list)
    sections_completed: List[str] = Field(default_factory=list)
    tool_calls: List[ToolCallRecord] = Field(default_factory=list)
    accumulated_results: Dict[str, Any] = Field(default_factory=dict)


class KPIReportOutputs(BaseModel):
    """Outputs from completed KPI report."""

    report_summary: str = Field(description="Executive summary")
    external_doc: ExternalDocRef = Field(description="Link to full report document")
    highlights: List[str] = Field(default_factory=list, description="Key highlights")


# =============================================================================
# PACKET TYPE: LIGHT_PRELIMINARY_PACKAGE
# =============================================================================


class LPPInputs(BaseModel):
    """Inputs for Light Preliminary Package generation."""

    site_id: Optional[int] = Field(default=None, description="Site submission ID")
    site_name: Optional[str] = Field(default=None, description="Site name to look up")
    technology_family: Optional[str] = Field(
        default=None,
        description="Power plant technology family/architecture: 'victron' or 'deye'",
    )
    raw_request: str = Field(default="", description="Original user request")


class SiteOption(BaseModel):
    """A site option when multiple submissions match."""

    id: int = Field(description="Site submission ID")
    site_name: str = Field(description="Site name")
    created_at: Optional[str] = Field(default=None, description="Submission timestamp")


class LPPState(BaseModel):
    """Working state during LPP generation."""

    progress: ProgressInfo = Field(default_factory=ProgressInfo)
    template_copied: bool = Field(default=False)
    document_id: Optional[str] = Field(default=None)
    document_url: Optional[str] = Field(default=None)
    document_title: Optional[str] = Field(default=None)
    map_generated: bool = Field(default=False)
    map_image_b64: Optional[str] = Field(default=None, description="Base64-encoded map image")
    # Design and BOM generation
    design_generated: bool = Field(default=False)
    design_id: Optional[str] = Field(default=None, description="AppSheet design ID")
    cost_summary: Optional[Dict[str, Any]] = Field(default=None, description="BOM cost summary")
    values_dumped: bool = Field(
        default=False, description="Whether values were dumped to reference columns"
    )
    cells_populated: bool = Field(
        default=False, description="Whether Main Input sheet is populated"
    )
    # For handling multiple site submissions
    awaiting_site_selection: bool = Field(default=False)
    site_options: List[SiteOption] = Field(
        default_factory=list, description="Available site options when multiple match"
    )
    selected_site_id: Optional[int] = Field(
        default=None, description="User-selected site ID when multiple match"
    )
    site_id: Optional[int] = Field(default=None, description="Resolved site ID")
    site_name: Optional[str] = Field(default=None, description="Resolved site name")
    technology_family: Optional[str] = Field(
        default=None,
        description="Power plant technology family/architecture: 'victron' or 'deye'",
    )
    tool_calls: List[ToolCallRecord] = Field(default_factory=list)
    accumulated_results: Dict[str, Any] = Field(default_factory=dict)


class LPPOutputs(BaseModel):
    """Outputs from completed LPP generation."""

    # Summary is always present from workflow
    summary: str = Field(default="", description="Human-readable summary of the work")
    steps_executed: List[str] = Field(
        default_factory=list, description="Names of workflow steps that executed"
    )

    # External doc reference (set if document was created)
    external_doc: Optional[Dict[str, Any]] = Field(
        default=None, description="Reference to created Google Doc/Sheet"
    )

    # Optional direct fields (for backwards compatibility)
    document_url: Optional[str] = Field(default=None, description="URL to the generated document")
    document_title: Optional[str] = Field(
        default=None, description="Final document title with doc code"
    )
    site_statistics: Dict[str, Any] = Field(
        default_factory=dict, description="Site statistics from map generation"
    )


# =============================================================================
# GENERIC WORK PACKET
# =============================================================================

InputT = TypeVar("InputT", bound=BaseModel)
StateT = TypeVar("StateT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class WorkPacket(BaseModel, Generic[InputT, StateT, OutputT]):
    """Generic work packet with typed inputs/state/outputs.

    Usage:
        packet: WorkPacket[GridAnalysisInputs, GridAnalysisState, GridAnalysisOutputs]
    """

    id: str = Field(description="UUID from database")
    packet_id: str = Field(description="Human-readable unique ID")
    packet_type: str = Field(description="Type of work: grid_analysis, kpi_report, etc.")
    packet_title: str = Field(description="Display title")
    packet_goal: str = Field(description="What the expert is trying to achieve")
    assigned_expert: str = Field(description="Expert handling this packet")
    packet_status: PacketStatus = Field(default=PacketStatus.PENDING)
    current_step: Optional[str] = Field(default=None)
    steps_completed: List[str] = Field(default_factory=list)

    packet_inputs: InputT
    packet_state: StateT
    packet_outputs: Optional[OutputT] = None

    external_system: Optional[str] = None
    external_id: Optional[str] = None
    external_url: Optional[str] = None

    organization_id: Optional[int] = None
    requested_by_email: Optional[str] = None
    requested_in_session: Optional[str] = None
    sessions_involved: List[str] = Field(default_factory=list)

    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# =============================================================================
# PACKET TYPE REGISTRY
# =============================================================================

PACKET_TYPE_SCHEMAS: Dict[str, Dict[str, Type[BaseModel]]] = {
    "grid_analysis": {
        "inputs": GridAnalysisInputs,
        "state": GridAnalysisState,
        "outputs": GridAnalysisOutputs,
    },
    "kpi_report": {
        "inputs": KPIReportInputs,
        "state": KPIReportState,
        "outputs": KPIReportOutputs,
    },
    "light_preliminary_package": {
        "inputs": LPPInputs,
        "state": LPPState,
        "outputs": LPPOutputs,
    },
}


def validate_packet_data(packet_type: str, field: str, data: Dict[str, Any]) -> BaseModel:
    """Validate packet data against registered schema.

    Args:
        packet_type: Type of packet (grid_analysis, kpi_report, etc.)
        field: Field to validate (inputs, state, outputs)
        data: Data dictionary to validate

    Returns:
        Validated Pydantic model instance

    Raises:
        ValueError: If packet type or field not found in registry
        ValidationError: If data doesn't match schema
    """
    if packet_type not in PACKET_TYPE_SCHEMAS:
        raise ValueError(f"Unknown packet type: {packet_type}")

    schema = PACKET_TYPE_SCHEMAS[packet_type].get(field)
    if not schema:
        raise ValueError(f"No schema for {packet_type}.{field}")

    return schema.model_validate(data)  # type: ignore[return-value]


def get_initial_state(packet_type: str) -> Dict[str, Any]:
    """Get default initial state for a packet type.

    Args:
        packet_type: Type of packet

    Returns:
        Dictionary with default state values
    """
    if packet_type not in PACKET_TYPE_SCHEMAS:
        return {}

    state_schema = PACKET_TYPE_SCHEMAS[packet_type].get("state")
    if not state_schema:
        return {}

    return state_schema().model_dump()  # type: ignore[no-any-return]


def get_packet_type_info(packet_type: str) -> Optional[Dict[str, type]]:
    """Get schema info for a packet type.

    Args:
        packet_type: Type of packet

    Returns:
        Dictionary with inputs/state/outputs schemas, or None if not found
    """
    return PACKET_TYPE_SCHEMAS.get(packet_type)


__all__ = [
    # Shapes
    "GridReference",
    "TimeRange",
    "ProgressInfo",
    "ExternalDocRef",
    "ToolCallRecord",
    # Status
    "PacketStatus",
    # Grid Analysis
    "GridAnalysisInputs",
    "GridAnalysisState",
    "GridAnalysisOutputs",
    # KPI Report
    "KPIReportInputs",
    "KPIReportState",
    "KPIReportOutputs",
    # Light Preliminary Package
    "LPPInputs",
    "LPPState",
    "LPPOutputs",
    "SiteOption",
    # Generic
    "WorkPacket",
    # Registry
    "PACKET_TYPE_SCHEMAS",
    "validate_packet_data",
    "get_initial_state",
    "get_packet_type_info",
]
