"""
Instructions Provider with Role-Based Retrieval

This service retrieves system instructions and prompts based on user roles,
context, and entity types. Instructions can be optimized via DSPy in the future.

Prompt resolution (bundled default / Google Doc / DB override, in that
order) is delegated to the shared prompt library (``shared.prompts``). This
service composes the customer/staff/troubleshooting/verification prompts
into the two-channel (system instruction, context message) shape the
generation gateway expects, and owns behavior specific to that composition:
staff-group extraction and examples-section reordering.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from pydantic import BaseModel

from orchestrator.models.schemas import EntityContext, UserContext
from shared.auth import get_auth_service
from shared.prompts import PROMPTS
from shared.utils.langfuse_utils import prompt_metadata
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Organization ID for internal staff (hardcoded)
INTERNAL_ORG_ID = int(os.getenv("STAFF_ORG_ID", "2"))

# ── Staff Groups Registry ─────────────────────────────────────────────
# Populated from the "# Staff Groups" section of the staff instructions doc.
# Same cache lifecycle as the rest of the doc (1-hour TTL via the prompt library).
_staff_groups: Dict[str, Dict[str, Any]] = {}  # chat_id -> {"name": str, "purposes": list[str]}


def get_staff_group(chat_id: str) -> Optional[Dict[str, Any]]:
    """Look up a staff group by Telegram chat ID. Returns None if not found."""
    return _staff_groups.get(chat_id)


def get_staff_groups() -> Dict[str, Dict[str, Any]]:
    """Get all configured staff groups. Returns {chat_id: {name, purposes}}."""
    return _staff_groups


def _parse_staff_groups(section_content: str) -> Dict[str, Dict[str, Any]]:
    """Parse the # Staff Groups section into a chat_id-keyed dict.

    Expected format per group:
        ## Group Name
        - chat_id: -100...
        - purpose: tag1, tag2
    """
    groups: Dict[str, Dict[str, Any]] = {}
    current_name: Optional[str] = None
    current_data: Dict[str, Any] = {}

    for line in section_content.split("\n"):
        stripped = line.strip()

        if stripped.startswith("## "):
            # Save previous group
            if current_name and "chat_id" in current_data:
                cid = current_data["chat_id"].strip()
                groups[cid] = {
                    "name": current_name,
                    "purposes": current_data.get("purposes", []),
                }
            current_name = stripped[3:].strip()
            current_data = {}
            continue

        if stripped.startswith("- ") and ":" in stripped:
            key, _, value = stripped[2:].partition(":")
            key = key.strip().lower()
            value = value.strip()

            if key == "chat_id":
                clean_id = value.strip()
                if clean_id.lstrip("-").isdigit():
                    current_data["chat_id"] = clean_id
                else:
                    LOGGER.warning(f"Invalid chat_id '{value}' for staff group '{current_name}'")
            elif key == "purpose":
                current_data["purposes"] = [t.strip() for t in value.split(",") if t.strip()]

    # Save last group
    if current_name and "chat_id" in current_data:
        cid = current_data["chat_id"].strip()
        groups[cid] = {
            "name": current_name,
            "purposes": current_data.get("purposes", []),
        }

    return groups


# Maximum context message size (chars) to prevent token limit issues
# ~30K chars ≈ 7,500 tokens, leaving room for conversation history
MAX_CONTEXT_CHARS = 30000

# Maximum words for an "Examples" style section before truncation. Such a
# section is also moved to the end of the context message so a large
# examples block doesn't crowd out other sections ahead of the overall
# MAX_CONTEXT_CHARS cut.
MAX_EXAMPLES_WORDS = 5000
_EXAMPLES_SECTION_KEYS = ("example_conversations", "examples", "example")

_BLOCK_HEADING = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_BLOCK_SPLIT_PATTERN = re.compile(r"\n\n(?=# )")


def _split_context_blocks(context_text: str) -> list[str]:
    """Split a composed context message back into its '# Title' blocks."""
    return _BLOCK_SPLIT_PATTERN.split(context_text)


def _block_key(block: str) -> str:
    """The section-name key for a '# Title\\n\\n...' block, matching the key
    format sections were originally parsed into (lowercased, spaces to underscores)."""
    match = _BLOCK_HEADING.match(block)
    title = match.group(1).strip() if match else ""
    return title.lower().replace(" ", "_")


def _extract_block(blocks: list[str], key: str) -> tuple[Optional[str], list[str]]:
    """Pull the first block whose title matches `key`. Returns (block_or_None, remaining_blocks)."""
    remaining = []
    found = None
    for block in blocks:
        if found is None and _block_key(block) == key:
            found = block
        else:
            remaining.append(block)
    return found, remaining


def _truncate_examples_block(block: str) -> str:
    """Cap an examples-style block's body at MAX_EXAMPLES_WORDS words."""
    match = re.match(r"^(#\s+.+?)\n\n(.*)\Z", block, re.DOTALL)
    if not match:
        return block
    heading, body = match.group(1), match.group(2)
    words = body.split()
    if len(words) <= MAX_EXAMPLES_WORDS:
        return block
    truncated = " ".join(words[:MAX_EXAMPLES_WORDS])
    LOGGER.warning(f"Truncated examples section from {len(words)} to {MAX_EXAMPLES_WORDS} words")
    return f"{heading}\n\n{truncated}\n\n[Truncated: showing {MAX_EXAMPLES_WORDS}/{len(words)} words]"


def _postprocess_context(
    context_text: Optional[str], *, extract_staff_groups: bool
) -> Optional[str]:
    """Extract staff groups (if requested, removing them from the LLM-bound
    text) and move/truncate an examples-style section to the end."""
    if not context_text:
        return context_text

    blocks = _split_context_blocks(context_text)

    if extract_staff_groups:
        groups_block, blocks = _extract_block(blocks, "staff_groups")
        if groups_block:
            body_match = re.match(r"^#\s+.+?\n\n(.*)\Z", groups_block, re.DOTALL)
            body = body_match.group(1) if body_match else ""
            global _staff_groups
            _staff_groups = _parse_staff_groups(body)
            LOGGER.info(f"Loaded {len(_staff_groups)} staff group(s) from doc")

    examples_block = None
    for key in _EXAMPLES_SECTION_KEYS:
        examples_block, blocks = _extract_block(blocks, key)
        if examples_block:
            break

    if examples_block:
        blocks.append(_truncate_examples_block(examples_block))

    return "\n\n".join(blocks) if blocks else None


def _cap_context(context_text: Optional[str]) -> Optional[str]:
    """Apply the overall context budget, truncating at the last paragraph
    boundary where possible rather than mid-sentence."""
    if not context_text:
        return None
    if len(context_text) <= MAX_CONTEXT_CHARS:
        return context_text

    original_len = len(context_text)
    clipped = context_text[:MAX_CONTEXT_CHARS]
    last_newline = clipped.rfind("\n\n")
    if last_newline > MAX_CONTEXT_CHARS * 0.8:  # Only use if not too much is lost
        clipped = clipped[:last_newline]
    clipped += "\n\n[Context truncated due to size limits]"
    LOGGER.warning(f"Truncated context message from {original_len} to {len(clipped)} chars")
    return clipped


class Instruction(BaseModel):
    """Represents a system instruction."""

    content: str
    priority: int = 0  # Higher priority = included first
    context_type: str = "general"  # general, role, entity, task
    metadata: Dict[str, Any] = {}


class InstructionsProvider:
    """
    Retrieves and composes system instructions based on user and context.

    Prompt content and its resolution order (DB override → Google Doc →
    bundled default) are owned by the shared prompt library. This class
    composes that content into (system_instructions, context_message) and
    owns the customer/staff-specific post-processing described above.
    """

    def __init__(
        self,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
    ):
        """
        Initialize instructions provider.

        Args:
            supabase_url: URL to main Supabase instance
            supabase_key: Service key for Supabase
        """
        self._supabase_url = (
            supabase_url or os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
        )
        self._supabase_key = (
            supabase_key or os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
        )
        self._client = None
        self._last_provenance: Optional[Dict[str, Any]] = None

    def get_last_provenance(self) -> Optional[Dict[str, Any]]:
        """Provenance of the system instructions from the most recent
        get_customer_instructions()/_get_staff_instructions_from_doc() call,
        for callers (prepare_context.py) that want to log or trace it."""
        return self._last_provenance

    def _get_client(self):
        """Get or create Supabase client."""
        if self._client is None and self._supabase_url and self._supabase_key:
            try:
                from supabase import create_client

                self._client = create_client(self._supabase_url, self._supabase_key)
                LOGGER.info("Instructions Supabase client initialized")
            except ImportError:
                LOGGER.error("supabase-py not installed. Install with: pip install supabase")
                return None
        return self._client

    async def is_customer_mode(self, user_email: str) -> bool:
        """
        Check if user should be in customer mode based on organization ID.

        Customer mode is triggered when user's organization_id != STAFF_ORG_ID.
        This protects internal staff mode by only allowing STAFF_ORG_ID members to access it.

        Args:
            user_email: User's email address

        Returns:
            True if customer mode should be used, False for internal staff mode
        """
        try:
            # Use singleton auth service for permission checking
            auth_service = get_auth_service()
            permissions = await auth_service.get_user_permissions(user_email)

            if not permissions.organization_ids:
                LOGGER.warning(
                    f"User {user_email} has no organization_ids, defaulting to customer mode"
                )
                return True  # Safe default: unknown users are customers

            org_id = int(permissions.organization_ids[0])
            is_customer = org_id != INTERNAL_ORG_ID

            LOGGER.info(
                f"User {user_email} org_id={org_id}, "
                f"mode={'customer' if is_customer else 'internal'}"
            )

            return bool(is_customer)

        except Exception as e:
            LOGGER.exception(f"Error checking customer mode for {user_email}: {e}")
            return True  # Safe default: treat as customer on error

    async def get_customer_instructions(self) -> tuple[str, Optional[str]]:
        """
        Get customer-facing system instructions and optional context.

        Returns:
            Tuple of (system_instructions, context_message)
            - system_instructions: Goes to the provider system-instruction channel
            - context_message: Goes as first user message (or None)
        """
        rendered = PROMPTS.render("customer.system")
        self._last_provenance = prompt_metadata(rendered)
        context_message = _postprocess_context(rendered.context_text, extract_staff_groups=False)
        context_message = _cap_context(context_message)

        LOGGER.info(
            f"Loaded customer instructions: {rendered.provenance()}, "
            f"system={len(rendered.system_text)} chars, "
            f"context={len(context_message) if context_message else 0} chars"
        )

        return rendered.system_text, context_message

    async def get_instructions(
        self,
        user_context: UserContext,
        entity_context: Optional[EntityContext] = None,
        task_type: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:
        """
        Get composed system instructions and optional context message.

        Args:
            user_context: User context with roles
            entity_context: Optional entity context (grid, meter, etc.)
            task_type: Optional task type (analysis, reporting, troubleshooting, etc.)

        Returns:
            Tuple of (system_instructions, context_message)
            - system_instructions: Goes to the provider system-instruction channel
            - context_message: Goes as first user message (or None)
        """
        # Use is_staff flag from user_context (already resolved during auth)
        if user_context.is_staff:
            LOGGER.info(
                f"Using INTERNAL/STAFF mode for {user_context.user_email or user_context.user_id}"
            )
            return await self._get_staff_instructions_from_doc()
        else:
            LOGGER.info(
                f"Using CUSTOMER mode for {user_context.user_email or user_context.user_id}"
            )
            return await self.get_customer_instructions()

    async def _get_staff_instructions_from_doc(self) -> tuple[str, Optional[str]]:
        """
        Get staff instructions and optional context.

        Returns:
            Tuple of (system_instructions, context_message)
            - system_instructions: Goes to the provider system-instruction channel
            - context_message: Goes as first user message (or None)
        """
        rendered = PROMPTS.render("staff.system")
        self._last_provenance = prompt_metadata(rendered)
        context_message = _postprocess_context(rendered.context_text, extract_staff_groups=True)
        context_message = _cap_context(context_message)

        LOGGER.info(
            f"Loaded staff instructions: {rendered.provenance()}, "
            f"system={len(rendered.system_text)} chars, "
            f"context={len(context_message) if context_message else 0} chars"
        )

        return rendered.system_text, context_message

    async def get_verification_instructions(self) -> Optional[str]:
        """
        Get verification criteria for LLM-as-judge.

        Used to verify customer-facing responses before sending.

        Returns:
            Verification instructions string.
        """
        return PROMPTS.text("verification.criteria")

    async def get_troubleshooting_procedures(self) -> Optional[str]:
        """Get common instructions shared between customer and staff modes."""
        return PROMPTS.text("troubleshooting.procedures")


__all__ = ["InstructionsProvider", "Instruction"]
