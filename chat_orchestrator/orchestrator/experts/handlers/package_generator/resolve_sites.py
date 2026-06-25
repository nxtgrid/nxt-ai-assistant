"""Resolve and validate site names for multi-site LPP execution.

Parses comma-separated site names, fuzzy-matches each against Auth DB,
deduplicates, and stores validated sites in state for downstream handlers.

Follows the GTR pattern from resolve_grid_sheets.py: validate upfront,
fail fast if any name is unresolvable.
"""

import os
from typing import Any

import asyncpg

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.auth.auth_service import STAFF_ORG_ID as _STAFF_ORG_ID
from shared.utils.grid_matcher import find_best_grid_match, parse_multi_site_args
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


async def _fetch_site_names_for_org(
    org_id: int | None,
) -> list[dict[str, Any]]:
    """Fetch valid site names from pd_site_submissions using asyncpg.

    Filters by organization_id when available. Uses AUTH_DB_* env vars
    with SSL as required by CLAUDE.md.

    Returns:
        List of dicts with id, site_name
    """
    host = os.getenv("AUTH_DB_HOST")
    if not host:
        raise RuntimeError("AUTH_DB_HOST not configured")

    conn = await asyncpg.connect(
        host=host,
        port=int(os.getenv("AUTH_DB_PORT", "5432")),
        database=os.getenv("AUTH_DB_NAME", "postgres"),
        user=os.getenv("AUTH_DB_USER"),
        password=os.getenv("AUTH_DB_PASSWORD"),
        ssl="require",
        statement_cache_size=0,
    )
    try:
        if org_id is not None:
            rows = await conn.fetch(
                """SELECT id, site_name
                   FROM pd_site_submissions
                   WHERE site_name IS NOT NULL
                     AND deleted_at IS NULL
                     AND organization_id = $1
                   ORDER BY site_name""",
                org_id,
            )
        else:
            rows = await conn.fetch(
                """SELECT id, site_name
                   FROM pd_site_submissions
                   WHERE site_name IS NOT NULL AND deleted_at IS NULL
                   ORDER BY site_name"""
            )
        return [{"id": row["id"], "site_name": row["site_name"]} for row in rows]
    finally:
        await conn.close()


def _match_site_names(
    requested_names: list[str],
    valid_sites: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Match requested names against valid sites using fuzzy matching.

    Args:
        requested_names: User-provided site names (possibly misspelled)
        valid_sites: All valid sites from the database

    Returns:
        Tuple of (matched sites, unmatched names)
    """
    valid_names = [s["site_name"] for s in valid_sites]
    name_to_site: dict[str, dict[str, Any]] = {}
    for s in valid_sites:
        key = s["site_name"].lower().strip()
        if key not in name_to_site:
            name_to_site[key] = s

    matched = []
    unmatched = []

    for name in requested_names:
        name_lower = name.lower().strip()
        if name_lower in name_to_site:
            matched.append(name_to_site[name_lower])
            continue

        fuzzy_match, was_fuzzy, score = find_best_grid_match(name, valid_names)
        if fuzzy_match:
            match_lower = fuzzy_match.lower().strip()
            if match_lower in name_to_site:
                site = name_to_site[match_lower]
                if was_fuzzy:
                    LOGGER.info(f"Fuzzy matched '{name}' -> '{fuzzy_match}' (score: {score}%)")
                matched.append(site)
            else:
                unmatched.append(name)
        else:
            # Substring fallback: if input is a suffix/substring of exactly one valid name.
            # Handles "Yashikira" → "Test Yashikira" where token_sort_ratio scores ~78%.
            # Minimum 5 chars prevents short fragments (e.g. "ure") from spuriously
            # matching unrelated sites that happen to share a substring.
            substr_matches = (
                [k for k in name_to_site if name_lower in k] if len(name_lower) >= 5 else []
            )
            if len(substr_matches) == 1:
                site = name_to_site[substr_matches[0]]
                LOGGER.info(
                    f"Substring matched '{name}' -> '{site['site_name']}' "
                    f"(input is substring of site name)"
                )
                matched.append(site)
            else:
                if len(substr_matches) > 1:
                    LOGGER.warning(
                        f"Ambiguous substring match for '{name}': "
                        f"{[name_to_site[k]['site_name'] for k in substr_matches]}"
                    )
                unmatched.append(name)

    return matched, unmatched


@register_step("resolve_sites")
async def resolve_sites(context: StepContext) -> StepResult:
    """Validate and resolve site names for multi-site execution.

    Parses comma-separated args, fuzzy-matches each against Auth DB
    (filtered by org), deduplicates, and stores sites_to_process in state.

    Fails fast if ANY name is unresolvable.
    """
    # Community (Route B) resolves its own geo via resolve_community_site; nothing to do here.
    if context.get_state("geo_source") == "community":
        return StepResult(
            data={"sites_to_process": []},
            state_updates={},
            progress_message="Community route — site resolution handled upstream.",
        )

    args = context.get_input("args") or context.get_input("site_name") or ""
    if not args:
        return StepResult.failure(
            "No site name provided. Usage: /lpp SiteName or /lpp Site1, Site2"
        )

    site_names = parse_multi_site_args(str(args))
    if not site_names:
        return StepResult.failure("No valid site names found in input.")

    if len(site_names) > 1:
        await context.send_progress_to_user(f"Validating {len(site_names)} site name(s)...")

    org_id = context.effective_org_id

    # Staff org can access all sites; customers are filtered to their org
    query_org_id = None if org_id == _STAFF_ORG_ID else org_id

    try:
        valid_sites = await _fetch_site_names_for_org(query_org_id)
    except Exception as e:
        LOGGER.exception(f"Failed to fetch sites from Auth DB: {e}")
        return StepResult.failure("Could not validate site names right now. Please try again.")

    if not valid_sites:
        return StepResult.failure("No sites found in database.")

    matched, unmatched = _match_site_names(site_names, valid_sites)

    if unmatched:
        return StepResult.failure(
            f"Could not match site(s): {', '.join(unmatched)}. "
            "Please check the spelling and try again."
        )

    # Deduplicate (preserve order of first occurrence)
    sites_to_process = []
    seen: set[str] = set()
    for site in matched:
        key = site["site_name"].lower().strip()
        if key not in seen:
            seen.add(key)
            sites_to_process.append(
                {
                    "name": site["site_name"],
                    "id": site["id"],
                }
            )

    primary = sites_to_process[0]

    LOGGER.info(
        f"Resolved {len(sites_to_process)} site(s): {[s['name'] for s in sites_to_process]}"
    )

    return StepResult(
        data={"sites_to_process": sites_to_process},
        state_updates={
            "sites_to_process": sites_to_process,
            "site_name": primary["name"],
            "site_id": primary["id"],
        },
        progress_message=f"Validated {len(sites_to_process)} site(s): "
        + ", ".join(s["name"] for s in sites_to_process),
    )
