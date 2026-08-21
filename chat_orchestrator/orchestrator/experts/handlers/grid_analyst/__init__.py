"""Grid analyst step handlers.

Handlers for the grid_analyst expert's workflow steps:
- fetch_month_metrics: Get metrics from Grafana for one grid
- fetch_multi_grid_metrics: Get metrics from Grafana for multiple grids (KPI reports)
- analyze_failures_loop: Analyze alerts and failures
- categorize_issues: Group analyzed failures by type/severity
- create_analysis_doc: Generate a single-grid analysis Google Doc report
- create_kpi_doc: Generate a multi-grid KPI Google Doc report
- calculate_kpi_values: Compute per-grid/aggregate KPIs from fetched metrics

Docstring and __all__ previously named only 3 of these 7 -- a documentation
staleness only (fixed alongside Phase 10 of docs/superpowers/plans/
2026-08-20-expert-steps-as-skill-tools.md's contract work), not a functional
bug: @register_step runs as an import-time side effect on each of
analyze_failures.py/create_report.py/fetch_metrics.py regardless of which of
their names this file re-exports, so all 7 were always live-registered (see
the live-registry measurement in that plan's Phase 10 section).
"""

from orchestrator.experts.handlers.grid_analyst.analyze_failures import (
    analyze_failures_loop,
    categorize_issues,
)
from orchestrator.experts.handlers.grid_analyst.create_report import (
    calculate_kpi_values,
    create_analysis_doc,
    create_kpi_doc,
)
from orchestrator.experts.handlers.grid_analyst.fetch_metrics import (
    fetch_month_metrics,
    fetch_multi_grid_metrics,
)

__all__ = [
    "fetch_month_metrics",
    "fetch_multi_grid_metrics",
    "analyze_failures_loop",
    "categorize_issues",
    "create_analysis_doc",
    "create_kpi_doc",
    "calculate_kpi_values",
]
