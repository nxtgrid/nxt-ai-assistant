"""Create analysis report step handler.

This handler creates a Google Doc report with the analysis results.
"""

from typing import Any, Dict, List

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@register_step("create_analysis_doc")
async def create_analysis_doc(context: StepContext) -> StepResult:
    """Generate Google Doc with full analysis report.

    Compiles all analysis results into a structured document
    using the google_docs_create MCP tool.

    Args:
        context: Step execution context

    Returns:
        StepResult with document reference
    """
    grid_ref = context.get_input("grid", {})
    grid_name = grid_ref.get("grid_name", "Unknown Grid")
    time_range = context.get_input("time_range", {})

    if not context.mcp_executor:
        return StepResult.failure("MCP executor not available")

    # Gather results from previous steps
    metrics = context.get_previous_result("fetch_month_metrics") or {}
    failures = context.get_previous_result("analyze_failures_loop") or {}

    # Get key findings from state
    key_findings = context.get_state("key_findings", [])

    # Build report content
    report_sections = _build_report_sections(
        grid_name=grid_name,
        time_range=time_range,
        metrics=metrics,
        failures=failures,
        key_findings=key_findings,
    )

    # Create document title
    from datetime import datetime

    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    doc_title = f"Grid Analysis: {grid_name} - {date_str}"

    try:
        # Call google_docs_create tool
        result = await context.mcp_executor.call_tool(
            "google_docs_create",
            {
                "title": doc_title,
                "content": report_sections,
            },
        )

        # Extract document URL from result
        doc_url = None
        doc_id = None

        if isinstance(result, dict):
            doc_url = result.get("url") or result.get("document_url")
            doc_id = result.get("id") or result.get("document_id")

        return StepResult(
            data={
                "document_title": doc_title,
                "document_id": doc_id,
                "document_url": doc_url,
                "sections_included": list(report_sections.keys()),
            },
            state_updates={
                "report_created": True,
            },
            progress_message=f"Created analysis report: {doc_title}",
        )

    except Exception as e:
        LOGGER.error(f"Failed to create analysis doc: {e}")
        # Don't fail the whole workflow - return what we have
        return StepResult(
            data={
                "document_error": str(e),
                "report_content": report_sections,
            },
            state_updates={
                "report_created": False,
                "report_error": str(e),
            },
            progress_message="Failed to create document, analysis available in response",
        )


def _build_report_sections(
    grid_name: str,
    time_range: Dict[str, Any],
    metrics: Dict[str, Any],
    failures: Dict[str, Any],
    key_findings: List[str],
) -> Dict[str, str]:
    """Build report sections from analysis data.

    Args:
        grid_name: Name of the grid
        time_range: Analysis time range
        metrics: Metrics data from fetch step
        failures: Failure analysis results
        key_findings: List of key findings

    Returns:
        Dictionary mapping section names to content
    """
    sections = {}

    # Executive Summary
    summary_lines = [
        f"Analysis of {grid_name} grid performance.",
        "",
        "## Key Findings",
    ]
    if key_findings:
        for finding in key_findings[:5]:  # Top 5 findings
            summary_lines.append(f"- {finding}")
    else:
        summary_lines.append("- No critical issues identified")

    sections["Executive Summary"] = "\n".join(summary_lines)

    # Time Period
    start = time_range.get("start_date", "N/A")
    end = time_range.get("end_date", "N/A")
    sections["Analysis Period"] = f"From: {start}\nTo: {end}"

    # Metrics Summary
    metrics_lines = ["## Performance Metrics", ""]
    if metrics.get("battery_metrics"):
        metrics_lines.append("### Battery Performance")
        metrics_lines.append(_format_metrics(metrics["battery_metrics"]))
    if metrics.get("solar_metrics"):
        metrics_lines.append("### Solar Generation")
        metrics_lines.append(_format_metrics(metrics["solar_metrics"]))

    sections["Performance Metrics"] = "\n".join(metrics_lines)

    # Issues and Alerts
    issues_lines = ["## Issues Identified", ""]
    failure_count = failures.get("failure_count", 0)
    successful = failures.get("successful_analyses", 0)

    if failure_count > 0:
        issues_lines.append(f"Analyzed {successful} of {failure_count} alerts.")
        issues_lines.append("")

        for failure in (failures.get("failures_analyzed") or [])[:10]:
            alert = failure.get("alert", {})
            analysis = failure.get("analysis") or {}

            alert_type = alert.get("type", "Unknown")
            timestamp = alert.get("timestamp", "")
            summary = analysis.get("summary") or analysis.get("finding") or "No analysis available"

            issues_lines.append(f"### {alert_type}")
            issues_lines.append(f"Time: {timestamp}")
            issues_lines.append(f"Analysis: {summary}")
            issues_lines.append("")
    else:
        issues_lines.append("No issues identified during the analysis period.")

    sections["Issues"] = "\n".join(issues_lines)

    # Recommendations
    rec_lines = ["## Recommendations", ""]
    if key_findings:
        rec_lines.append("Based on the analysis:")
        for i, finding in enumerate(key_findings[:3], 1):
            rec_lines.append(f"{i}. Address: {finding}")
    else:
        rec_lines.append("Continue monitoring - no immediate actions required.")

    sections["Recommendations"] = "\n".join(rec_lines)

    return sections


def _format_metrics(metrics: Any) -> str:
    """Format metrics data for display.

    Args:
        metrics: Metrics data (dict or other)

    Returns:
        Formatted string
    """
    if isinstance(metrics, dict):
        lines = []
        for key, value in metrics.items():
            if isinstance(value, (int, float)):
                lines.append(f"- {key}: {value:.2f}")
            else:
                lines.append(f"- {key}: {value}")
        return "\n".join(lines) if lines else "No data available"
    return str(metrics)[:500]


@register_step("create_kpi_doc")
async def create_kpi_doc(context: StepContext) -> StepResult:
    """Create a KPI report document for multiple grids.

    Args:
        context: Step execution context

    Returns:
        StepResult with document reference
    """
    grids = context.get_input("grids", [])
    time_range = context.get_input("time_range", {})
    report_type = context.get_input("report_type", "weekly")

    if not context.mcp_executor:
        return StepResult.failure("MCP executor not available")

    # Get metrics from previous step
    metrics_result = context.get_previous_result("fetch_multi_grid_metrics") or {}
    grid_metrics = metrics_result.get("grid_metrics", {})

    # Build document title
    from datetime import datetime

    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    grid_names = [g.get("grid_name") for g in grids if g.get("grid_name")]
    grid_label = ", ".join(grid_names[:3])
    if len(grid_names) > 3:
        grid_label += f" (+{len(grid_names) - 3} more)"

    doc_title = f"KPI Report ({report_type.title()}): {grid_label} - {date_str}"

    # Build report content
    content_lines = [
        f"# {report_type.title()} KPI Report",
        f"Generated: {date_str}",
        "",
        "## Overview",
        f"Grids included: {len(grid_metrics)}",
        f"Period: {time_range.get('start_date')} to {time_range.get('end_date')}",
        "",
    ]

    for grid_name, metrics in grid_metrics.items():
        content_lines.append(f"## {grid_name}")
        content_lines.append(_format_metrics(metrics))
        content_lines.append("")

    try:
        result = await context.mcp_executor.call_tool(
            "google_docs_create",
            {
                "title": doc_title,
                "content": "\n".join(content_lines),
            },
        )

        doc_url = None
        doc_id = None

        if isinstance(result, dict):
            doc_url = result.get("url") or result.get("document_url")
            doc_id = result.get("id") or result.get("document_id")

        return StepResult(
            data={
                "document_title": doc_title,
                "document_id": doc_id,
                "document_url": doc_url,
            },
            state_updates={
                "report_created": True,
            },
            progress_message=f"Created KPI report: {doc_title}",
        )

    except Exception as e:
        LOGGER.error(f"Failed to create KPI doc: {e}")
        return StepResult(
            data={
                "document_error": str(e),
                "report_content": "\n".join(content_lines),
            },
            state_updates={
                "report_created": False,
            },
            progress_message="Failed to create document",
        )


@register_step("calculate_kpi_values")
async def calculate_kpi_values(context: StepContext) -> StepResult:
    """Calculate KPI values from fetched metrics.

    Computes uptime, generation efficiency, and other KPIs.

    Args:
        context: Step execution context

    Returns:
        StepResult with calculated KPIs
    """
    metrics_result = context.get_previous_result("fetch_multi_grid_metrics") or {}
    grid_metrics = metrics_result.get("grid_metrics", {})

    kpis = {}

    for grid_name, metrics in grid_metrics.items():
        grid_kpis = {
            "grid_name": grid_name,
            "uptime_percent": _calculate_uptime(metrics),
            "avg_soc": _calculate_avg_soc(metrics),
            "total_generation_kwh": _calculate_generation(metrics),
            "alert_count": _count_alerts(metrics),
        }
        kpis[grid_name] = grid_kpis

    # Calculate aggregate KPIs
    all_uptimes = [k.get("uptime_percent", 0) for k in kpis.values()]
    aggregate = {
        "avg_uptime": sum(all_uptimes) / len(all_uptimes) if all_uptimes else 0,
        "total_grids": len(kpis),
        "grids_above_95_uptime": len([u for u in all_uptimes if u >= 95]),
    }

    return StepResult(
        data={
            "grid_kpis": kpis,
            "aggregate_kpis": aggregate,
        },
        state_updates={
            "kpis_calculated": True,
        },
        progress_message=f"Calculated KPIs for {len(kpis)} grids",
    )


def _calculate_uptime(metrics: Dict[str, Any]) -> float:
    """Calculate uptime percentage from metrics."""
    if isinstance(metrics, dict):
        return float(metrics.get("uptime", 99.0))
    return 99.0


def _calculate_avg_soc(metrics: Dict[str, Any]) -> float:
    """Calculate average state of charge from metrics."""
    if isinstance(metrics, dict):
        return float(metrics.get("avg_soc", 75.0))
    return 75.0


def _calculate_generation(metrics: Dict[str, Any]) -> float:
    """Calculate total generation from metrics."""
    if isinstance(metrics, dict):
        return float(metrics.get("total_generation", 0.0))
    return 0.0


def _count_alerts(metrics: Dict[str, Any]) -> int:
    """Count alerts from metrics."""
    if isinstance(metrics, dict):
        alerts = metrics.get("alerts", [])
        if isinstance(alerts, list):
            return len(alerts)
    return 0
