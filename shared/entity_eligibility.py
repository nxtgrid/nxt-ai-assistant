"""Which grids / organizations a scheduled anchor covers.

Lives in `shared` so every image that needs the enumeration actually has it.
It was in orchestrator/experts/entity_fanout.py, which chat_orchestrator's
image ships and anansi_app's does not (see the two Dockerfiles) -- and
anansi_app is where scheduled batch work runs in this deployment.
entity_fanout re-exports both names, so its callers are unaffected and there
is still exactly one enumeration, which is the property that module's own
docstring exists to protect.

Callers are responsible for the safety property this module deliberately
does NOT enforce itself: if get_eligible_entities returns an empty list,
that must be treated as "the Auth DB may be down," not "there are zero
entities" -- skip the tick/run rather than acting on an empty set.
"""

from __future__ import annotations

from typing import Any, Dict, List

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Anchor types with an eligibility query and a metadata shape.
# "anything else stays unsupported" -- callers should treat an entity_type
# outside this set as a hard "cannot fan out", not a silent empty list
# indistinguishable from "no eligible entities".
SUPPORTED_ANCHOR_ENTITY_TYPES = ("grid", "organization")


async def get_eligible_entities(entity_type: str) -> List[Dict[str, Any]]:
    """Get eligible entities for a given anchor_entity_type.

    Empty list means either "genuinely zero eligible entities" or "the
    Auth DB query failed" -- get_eligible_grids_for_agents/
    get_eligible_organizations_for_agents both degrade to [] on error
    rather than raising, so this function can't distinguish the two either.
    That ambiguity is exactly why callers must not treat an empty result as
    license to act on an empty set -- see this module's docstring.
    """
    if entity_type not in SUPPORTED_ANCHOR_ENTITY_TYPES:
        LOGGER.warning(f"No eligibility query registered for entity_type={entity_type}")
        return []

    # Deferred until entity_type is known to be supported -- constructing
    # AuthService opens a real DB connection (requires AUTH_DB_HOST/USER/
    # PASSWORD), which an unsupported entity_type has no reason to pay for.
    from shared.auth.auth_service import get_auth_service

    auth_service = get_auth_service()

    if entity_type == "grid":
        return await auth_service.get_eligible_grids_for_agents()
    return await auth_service.get_eligible_organizations_for_agents()


__all__ = ["SUPPORTED_ANCHOR_ENTITY_TYPES", "get_eligible_entities"]
