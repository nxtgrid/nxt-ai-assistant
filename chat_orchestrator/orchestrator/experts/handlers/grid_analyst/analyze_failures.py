"""Analyze failures step handler - loops through alerts calling MCP tool.

This handler analyzes each alert found in the previous fetch step,
calling the analyze_failures MCP tool for detailed failure analysis.
"""

from typing import Any, Dict, List

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@register_step("analyze_failures_loop")
async def analyze_failures_loop(context: StepContext) -> StepResult:
    """Repeatedly call analyze_failures MCP tool for each alert.

    This is the 'hard logic' that orchestrates multiple tool calls,
    iterating through alerts from the previous fetch step.

    Args:
        context: Step execution context

    Returns:
        StepResult with analyzed failures
    """
    grid_ref = context.get_input("grid", {})
    grid_name = grid_ref.get("grid_name")

    # Get alerts from previous step
    fetch_result = context.get_previous_result("fetch_month_metrics")
    if not fetch_result:
        return StepResult(
            data={"failures_analyzed": [], "failure_count": 0},
            state_updates={"faults_analyzed": True},
            progress_message="No previous fetch results to analyze",
        )

    alerts = fetch_result.get("alerts") or []

    # Handle case where alerts is a tool result dict
    if isinstance(alerts, dict):
        alerts = alerts.get("data", []) or alerts.get("alerts", [])

    if not alerts:
        return StepResult(
            data={"failures_analyzed": [], "failure_count": 0},
            state_updates={"faults_analyzed": True},
            progress_message="No alerts found to analyze",
        )

    LOGGER.info(f"Analyzing {len(alerts)} alerts for {grid_name}")

    if not context.mcp_executor:
        return StepResult.failure("MCP executor not available")

    analyzed_failures: List[Dict[str, Any]] = []
    key_findings: List[str] = []

    for i, alert in enumerate(alerts):
        try:
            # Call MCP analyze_failures tool for each alert
            result = await context.mcp_executor.call_tool(
                "analyze_failures",
                {
                    "grid": grid_name,
                    "alert_id": alert.get("id"),
                    "alert_type": alert.get("type"),
                    "timestamp": alert.get("timestamp"),
                },
            )

            analysis = {
                "alert": alert,
                "analysis": result,
                "index": i,
            }
            analyzed_failures.append(analysis)

            # Extract key finding if present
            if isinstance(result, dict):
                summary = result.get("summary") or result.get("finding")
                if summary:
                    key_findings.append(summary)

        except Exception as e:
            LOGGER.warning(f"Failed to analyze alert {alert.get('id')}: {e}")
            analyzed_failures.append(
                {
                    "alert": alert,
                    "analysis": None,
                    "error": str(e),
                    "index": i,
                }
            )

    successful = len([f for f in analyzed_failures if f.get("analysis")])

    return StepResult(
        data={
            "failures_analyzed": analyzed_failures,
            "failure_count": len(analyzed_failures),
            "successful_analyses": successful,
        },
        state_updates={
            "faults_analyzed": True,
            "key_findings": key_findings,
        },
        progress_message=f"Analyzed {successful}/{len(analyzed_failures)} failures",
    )


@register_step("categorize_issues")
async def categorize_issues(context: StepContext) -> StepResult:
    """Categorize analyzed failures by type and severity.

    Groups failures into categories for easier reporting.

    Args:
        context: Step execution context

    Returns:
        StepResult with categorized issues
    """
    # Get analyzed failures from previous step
    analysis_result = context.get_previous_result("analyze_failures_loop")
    if not analysis_result:
        return StepResult(
            data={"categories": {}},
            progress_message="No failures to categorize",
        )

    failures = analysis_result.get("failures_analyzed", [])

    # Group by type
    categories: Dict[str, List[Dict[str, Any]]] = {
        "battery": [],
        "solar": [],
        "grid": [],
        "communication": [],
        "other": [],
    }

    severity_counts = {
        "critical": 0,
        "warning": 0,
        "info": 0,
    }

    for failure in failures:
        alert = failure.get("alert", {})
        analysis = failure.get("analysis") or {}

        # Determine category
        alert_type = (alert.get("type") or "").lower()
        if "battery" in alert_type or "soc" in alert_type:
            category = "battery"
        elif "solar" in alert_type or "pv" in alert_type:
            category = "solar"
        elif "grid" in alert_type:
            category = "grid"
        elif "comm" in alert_type or "offline" in alert_type:
            category = "communication"
        else:
            category = "other"

        categories[category].append(failure)

        # Count severity
        severity = (analysis.get("severity") or alert.get("severity") or "info").lower()
        if severity in severity_counts:
            severity_counts[severity] += 1

    # Remove empty categories
    categories = {k: v for k, v in categories.items() if v}

    return StepResult(
        data={
            "categories": categories,
            "severity_counts": severity_counts,
            "category_counts": {k: len(v) for k, v in categories.items()},
        },
        state_updates={
            "issues_categorized": True,
        },
        progress_message=f"Categorized into {len(categories)} categories",
    )
