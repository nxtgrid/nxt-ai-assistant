"""Per-prompt, per-group access control for the prompt library.

Membership lives in env-var whitelists, parsed exactly like the four lists in
``anansi_app/grid_app/lib/perms.py`` so the app has one RBAC story. Bindings
live per prompt, in frontmatter (default) or the DB override layer.

This module governs the admin UI and the write API only. It is never consulted
on the render path: ``PROMPTS.render`` serves users who are not logged into the
admin app at all, and these whitelists fail closed.
"""

from __future__ import annotations

import os
import re

from shared.prompts.spec import PromptSpec
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

GROUP_ENV_VARS = {
    "ops": "PROMPT_EDITORS_OPS",
    "eng": "PROMPT_EDITORS_ENG",
    "admin": "PROMPT_ADMINS",
}


def _parse(env_name: str) -> set[str]:
    raw = os.getenv(env_name, "")
    if not raw:
        return set()
    return {e.strip().lower() for e in re.split(r"[,;\n]+", raw) if e.strip()}


def _dev_bypass() -> bool:
    return os.getenv("GRID_DESIGN_DEV_NO_AUTH", "").lower() in ("1", "true", "yes")


def groups_for(email: str) -> set[str]:
    """Every group this email belongs to."""
    email = (email or "").lower()
    if not email:
        return set()
    return {name for name, var in GROUP_ENV_VARS.items() if email in _parse(var)}


def is_prompt_admin(email: str) -> bool:
    return "admin" in groups_for(email)


def _allows(spec: PromptSpec, verb: str, email: str) -> bool:
    # A PR-only prompt is PR-only. `overridable: false` beats every grant,
    # admin included.
    if verb in ("edit", "publish") and not spec.overridable:
        return False
    if _dev_bypass():
        return True
    # `admin` is implicit and never listed in a prompt's own frontmatter.
    if is_prompt_admin(email):
        return True
    bound = set(getattr(spec.access, verb))
    return bool(bound & groups_for(email))


def can_view_prompt(spec: PromptSpec, email: str) -> bool:
    if _dev_bypass() or is_prompt_admin(email):
        return True
    return bool(set(spec.access.view) & groups_for(email))


def can_edit_prompt(spec: PromptSpec, email: str) -> bool:
    allowed = _allows(spec, "edit", email)
    if not allowed:
        LOGGER.info(f"Prompt edit denied: {email or '<anonymous>'} on '{spec.id}'")
    return allowed


def can_publish_prompt(spec: PromptSpec, email: str) -> bool:
    allowed = _allows(spec, "publish", email)
    if not allowed:
        LOGGER.info(f"Prompt publish denied: {email or '<anonymous>'} on '{spec.id}'")
    return allowed
