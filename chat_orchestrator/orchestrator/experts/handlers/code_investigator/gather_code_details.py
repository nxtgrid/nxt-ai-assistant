"""Gather code details step handler for the Code Investigator expert.

Executes the investigation plan produced by the classify_and_plan LLM step.
Calls relevant MCP tools (codebase search, PRs, database, logs) based on
the plan, then returns structured evidence for the synthesize LLM step.

Each data source is independent — partial failures are recorded in
_unavailable_sources rather than failing the entire step.
"""

import asyncio
import json
import re

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Per-repo configuration for language-aware searching
REPO_CONFIG: dict[str, dict[str, str]] = {
    "skyfox": {
        "file_pattern": "*.ts,*.tsx,*.js,*.jsx",
        "language": "typescript",
    },
    "anansi": {
        "file_pattern": "*.py",
        "language": "python",
    },
}

# Truncation limits to stay within context budget
MAX_CODE_RESULTS = 10
MAX_PR_SUMMARIES = 5
MAX_LOG_ENTRIES = 20


def _parse_tool_result(tool_result: list) -> dict | None:
    """Extract parsed JSON from an MCP tool result.

    Returns the parsed dict, or None if the result indicates an error.
    """
    if not tool_result:
        return None
    text = tool_result[0].text if hasattr(tool_result[0], "text") else str(tool_result[0])
    parsed = json.loads(text) if isinstance(text, str) else text
    if isinstance(parsed, dict) and parsed.get("error"):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _parse_plan_from_llm(context: StepContext) -> dict | None:
    """Extract the investigation plan JSON from the LLM's classify_and_plan output."""
    prev = context.get_previous_result("classify_and_plan")
    if not prev:
        return None

    # The LLM step result may contain a JSON plan in the response text
    raw = prev.get("response") or prev.get("text") or ""
    if isinstance(raw, dict):
        return dict(raw)

    # Try to extract JSON from markdown code blocks or raw text
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group(1))
            return dict(result) if isinstance(result, dict) else None
        except json.JSONDecodeError:
            pass

    # Try raw JSON parse
    try:
        result = json.loads(raw)
        return dict(result) if isinstance(result, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass

    return None


def _determine_repo(context: StepContext) -> str:
    """Determine target repo from the command that triggered this workflow.

    /ayrton -> skyfox, /anansi -> anansi

    The expert_handler stores the original command (e.g. "/ayrton") in
    packet_inputs["parsed_command"].
    """
    parsed_command = context.get_input("parsed_command", "")
    # parsed_command is like "/ayrton why is meter ..." — extract the command name
    if parsed_command:
        command_name = parsed_command.strip().lstrip("/").split(None, 1)[0].lower()
        if command_name == "ayrton":
            return "skyfox"
        elif command_name == "anansi":
            return "anansi"

    # Fallback: check packet goal or state
    goal = context.packet_goal.lower() if context.packet_goal else ""
    if "skyfox" in goal or "ayrton" in goal:
        return "skyfox"

    return "anansi"


async def _search_codebase(context: StepContext, search_terms: list[str], repo: str) -> dict:
    """Search codebase for relevant code patterns."""
    results = []
    config = REPO_CONFIG.get(repo, REPO_CONFIG["anansi"])

    for term in search_terms[:3]:  # Max 3 search terms
        try:
            tool_result = await context.mcp_executor.call_tool(
                "codebase_search_codebase",
                {
                    "query": term,
                    "file_pattern": config["file_pattern"],
                    "max_results": MAX_CODE_RESULTS,
                },
            )
            parsed = _parse_tool_result(tool_result)
            if parsed:
                results.append({"search_term": term, "matches": parsed})
        except Exception as e:
            LOGGER.warning(f"Code search failed for '{term}': {e}")

    return {"search_count": len(results), "results": results[:MAX_CODE_RESULTS]}


async def _search_semantic(context: StepContext, search_terms: list[str], repo: str) -> dict:
    """Semantic code search using vector embeddings."""
    config = REPO_CONFIG.get(repo, REPO_CONFIG["anansi"])
    results = []

    for term in search_terms[:2]:  # Max 2 semantic searches
        try:
            tool_result = await context.mcp_executor.call_tool(
                "codebase_search_code_semantic",
                {
                    "query": term,
                    "max_results": 5,
                    "language_filter": config["language"],
                },
            )
            parsed = _parse_tool_result(tool_result)
            if parsed:
                results.append({"query": term, "matches": parsed})
        except Exception as e:
            LOGGER.warning(f"Semantic search failed for '{term}': {e}")

    return {"search_count": len(results), "results": results}


async def _check_recent_prs(context: StepContext, days: int = 14) -> dict:
    """Get recent production PRs."""
    try:
        tool_result = await context.mcp_executor.call_tool(
            "codebase_get_recent_production_prs",
            {"days": days, "max_prs": MAX_PR_SUMMARIES},
        )
        parsed = _parse_tool_result(tool_result)
        if parsed:
            return parsed
    except Exception as e:
        LOGGER.warning(f"PR check failed: {e}")
    return {"error": "Could not fetch recent PRs"}


async def _investigate_database(context: StepContext, queries: list[str]) -> dict:
    """Run read-only database queries for investigation via the MCP tool."""
    results = []

    for sql_query in queries[:3]:  # Max 3 DB queries
        try:
            tool_result = await context.mcp_executor.call_tool(
                "codebase_investigate_database",
                {"sql_query": sql_query},
            )
            parsed = _parse_tool_result(tool_result)
            if parsed:
                results.append({"query": sql_query, "result": parsed})
            else:
                # Tool returned an error — still record the attempt
                error_text = ""
                if tool_result:
                    raw = (
                        tool_result[0].text
                        if hasattr(tool_result[0], "text")
                        else str(tool_result[0])
                    )
                    try:
                        error_text = json.loads(raw).get("error", raw)
                    except (json.JSONDecodeError, TypeError):
                        error_text = raw
                results.append({"query": sql_query, "error": error_text})
        except Exception as e:
            LOGGER.warning(f"DB investigation failed for query: {e}")
            results.append({"query": sql_query, "error": str(e)})

    return {"query_count": len(results), "results": results}


async def _check_logs(context: StepContext, search_terms: list[str]) -> dict:
    """Search application logs for relevant entries."""
    results = []

    for term in search_terms[:2]:  # Max 2 log searches
        try:
            tool_result = await context.mcp_executor.call_tool(
                "logs_search_logs_semantic",
                {"query": term},
            )
            parsed = _parse_tool_result(tool_result)
            if parsed:
                # Truncate log entries
                if isinstance(parsed, dict) and "results" in parsed:
                    parsed["results"] = parsed["results"][:MAX_LOG_ENTRIES]
                results.append({"search_term": term, "matches": parsed})
        except Exception as e:
            LOGGER.warning(f"Log search failed for '{term}': {e}")

    return {"search_count": len(results), "results": results}


async def _check_operational_data(context: StepContext, entities: dict) -> dict:
    """Fetch operational data for mentioned entities (grids, meters)."""
    results = {}

    grid_names = entities.get("grid_names", [])
    for grid_name in grid_names[:2]:  # Max 2 grids
        try:
            tool_result = await context.mcp_executor.call_tool(
                "customer_customer_get_grid_status",
                {"grid_name": grid_name},
            )
            parsed = _parse_tool_result(tool_result)
            if parsed:
                results[f"grid_{grid_name}"] = parsed
        except Exception as e:
            LOGGER.warning(f"Grid status failed for '{grid_name}': {e}")

    meter_ids = entities.get("meter_ids", [])
    for meter_id in meter_ids[:2]:  # Max 2 meters
        try:
            tool_result = await context.mcp_executor.call_tool(
                "customer_meter_information",
                {"meter_number": str(meter_id)},
            )
            parsed = _parse_tool_result(tool_result)
            if parsed:
                results[f"meter_{meter_id}"] = parsed
        except Exception as e:
            LOGGER.warning(f"Meter info failed for '{meter_id}': {e}")

    return results


@register_step("gather_code_details")
async def gather_code_details(context: StepContext) -> StepResult:
    """Execute the investigation plan from the LLM classify step.

    Gathers ALL available evidence in one step:
    1. Operational data (meter status, grid status) via MCP tools
    2. Database records (orders, meters tables) via investigate_database
    3. Codebase search (semantic + grep) via codebase tools
    4. Recent PRs (last 14 days) via GitHub tools
    5. Logs (if the LLM plan flagged it) via logs tools

    Each source is independent — partial failures return what's available
    with a note about what couldn't be checked. Independent sources are
    gathered concurrently with asyncio.gather.
    """
    await context.send_progress_to_user(
        "Investigating... checking code, database, and logs. This may take a moment."
    )

    repo = _determine_repo(context)
    plan = _parse_plan_from_llm(context)

    evidence: dict = {
        "target_repo": repo,
        "_unavailable_sources": [],
    }

    if not plan:
        # No structured plan from LLM — do a broad investigation
        LOGGER.warning("No structured plan from classify_and_plan, using broad search")
        question = context.get_input("question", "") or context.packet_goal or ""
        plan = {
            "entities": {"meter_ids": [], "grid_names": [], "keywords": []},
            "check_database": False,
            "db_queries": [],
            "check_code": True,
            "code_search_terms": [question] if question else [],
            "check_prs": True,
            "check_logs": False,
            "log_search_terms": [],
        }

    entities = plan.get("entities", {})

    # Build list of concurrent tasks based on plan
    tasks: dict[str, asyncio.Task] = {}

    # 1. Operational data (grids, meters mentioned in question)
    if entities.get("grid_names") or entities.get("meter_ids"):
        tasks["operational_data"] = asyncio.create_task(_check_operational_data(context, entities))

    # 2. Database investigation
    if plan.get("check_database") and plan.get("db_queries"):
        tasks["database"] = asyncio.create_task(_investigate_database(context, plan["db_queries"]))

    # 3. Codebase search (grep + semantic)
    if plan.get("check_code") and plan.get("code_search_terms"):
        search_terms = plan["code_search_terms"]
        tasks["code_search"] = asyncio.create_task(_search_codebase(context, search_terms, repo))
        tasks["semantic_search"] = asyncio.create_task(
            _search_semantic(context, search_terms, repo)
        )

    # 4. Recent PRs
    if plan.get("check_prs", True):
        tasks["recent_prs"] = asyncio.create_task(_check_recent_prs(context, days=14))

    # 5. Logs (conditional)
    if plan.get("check_logs") and plan.get("log_search_terms"):
        tasks["logs"] = asyncio.create_task(_check_logs(context, plan["log_search_terms"]))

    # Await all tasks concurrently, collecting results and errors
    if tasks:
        done = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for key, result in zip(tasks.keys(), done):
            if isinstance(result, Exception):
                LOGGER.error(f"{key} check failed: {result}")
                evidence["_unavailable_sources"].append(key)
            else:
                evidence[key] = result

    # Summary of what was gathered
    sources_checked = [
        k
        for k in evidence.keys()
        if k not in ("target_repo", "_unavailable_sources") and evidence[k]
    ]
    evidence["_sources_checked"] = sources_checked
    evidence["_investigation_plan"] = plan

    LOGGER.info(
        f"Investigation complete: {len(sources_checked)} sources checked, "
        f"{len(evidence.get('_unavailable_sources', []))} unavailable"
    )

    return StepResult(
        data=evidence,
        state_updates={"gathered_evidence": True, "target_repo": repo},
        progress_message=f"Checked {len(sources_checked)} data sources",
    )
