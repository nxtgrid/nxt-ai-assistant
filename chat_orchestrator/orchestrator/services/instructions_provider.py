"""
Instructions Provider with Role-Based Retrieval

This service retrieves system instructions and prompts based on user roles,
context, and entity types. Instructions can be optimized via DSPy in the future.

The service:
1. Retrieves role-specific instructions from Supabase
2. Retrieves entity-specific instructions (for grids, meters, etc.)
3. Composes final system instruction for Gemini
4. Can be optimized via DSPy based on feedback
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from pydantic import BaseModel

from orchestrator.models.schemas import EntityContext, UserContext
from shared.auth import get_auth_service
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

_INSTRUCTIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "instructions"
)

# Organization ID for internal staff (hardcoded)
INTERNAL_ORG_ID = int(os.getenv("STAFF_ORG_ID", "2"))

# ── Staff Groups Registry ─────────────────────────────────────────────
# Populated from the "# Staff Groups" section of the staff instructions doc.
# Same cache lifecycle as the rest of the doc (1-hour TTL via artifacts_provider).
_staff_groups: Dict[str, Dict[str, Any]] = {}  # chat_id -> {"name": str, "purposes": list[str]}


def _load_fallback_instructions(filename: str) -> Optional[Dict[str, str]]:
    """Load bundled fallback instruction file and parse into sections dict."""
    path = os.path.join(_INSTRUCTIONS_DIR, filename)
    if not os.path.exists(path):
        return None
    try:
        text = open(path).read()
        # Strip HTML comment header
        text = __import__("re").sub(r"<!--.*?-->", "", text, flags=__import__("re").DOTALL).strip()
        # Split on "# Section Title" headings
        import re

        sections: Dict[str, str] = {}
        current_key = "system_instructions"
        current_lines: list[str] = []
        for line in text.splitlines():
            m = re.match(r"^#\s+(.+)", line)
            if m:
                if current_lines:
                    sections[current_key] = "\n".join(current_lines).strip()
                current_key = m.group(1).strip().lower().replace(" ", "_")
                current_lines = []
            else:
                current_lines.append(line)
        if current_lines:
            sections[current_key] = "\n".join(current_lines).strip()
        LOGGER.info(f"Loaded fallback instructions from {filename} ({len(sections)} sections)")
        return sections or None
    except Exception as e:
        LOGGER.error(f"Failed to load fallback instructions {filename}: {e}")
        return None


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


class Instruction(BaseModel):
    """Represents a system instruction."""

    content: str
    priority: int = 0  # Higher priority = included first
    context_type: str = "general"  # general, role, entity, task
    metadata: Dict[str, Any] = {}


class InstructionsProvider:
    """
    Retrieves and composes system instructions based on user and context.

    Instructions are stored in Supabase instructions table with fields:
    - id: UUID
    - content: TEXT (instruction content)
    - roles: TEXT[] (which roles this applies to)
    - entity_types: TEXT[] (grid, meter, customer, etc.)
    - priority: INTEGER (higher = more important)
    - context_type: TEXT (general, role, entity, task)
    - is_active: BOOLEAN
    - created_at, updated_at: TIMESTAMP
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

        # Default instructions (fallback if database is not configured)
        self._default_instructions = {
            "general": """You are a helpful AI assistant with access to various tools and data sources.

Key principles:
- Always verify user permissions before accessing data
- Be concise and professional
- Ask for clarification when needed
- Cite sources when providing factual information
- Acknowledge limitations when you're unsure
""",
            "admin": """You have administrative access to all systems and data.

Additional responsibilities:
- Monitor system health and usage
- Assist with user management and permissions
- Provide insights on overall system performance
""",
            "developer": """You have access to codebase, logs, and development tools.

Focus areas:
- Help with debugging and troubleshooting
- Provide code insights and suggestions
- Monitor application health
""",
            "customer_support": """You assist with customer inquiries and support.

Focus areas:
- Help customers understand their data
- Investigate customer-reported issues
- Provide clear explanations of meter readings and grid status
""",
        }

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

        Uses section parsing from Google Docs:
        - "System Instructions" section → system instructions
        - All other sections → initial context message

        Priority order:
        1. Google Doc (if CUSTOMER_SUPPORT_DOC_ID is set)
        2. Supabase system instructions
        3. Default fallback

        Returns:
            Tuple of (system_instructions, context_message)
            - system_instructions: Goes to the provider system-instruction channel
            - context_message: Goes as first user message (or None)
        """
        try:
            # Import artifacts provider
            from orchestrator.services.artifacts_provider import ArtifactsProvider

            artifacts_provider = ArtifactsProvider(
                supabase_url=self._supabase_url,
                supabase_key=self._supabase_key,
            )

            # CUSTOMER MODE: Google Doc preferred, falls back to bundled file
            doc_id = os.getenv("CUSTOMER_SUPPORT_DOC_ID", "").strip()
            sections = None

            if doc_id:
                LOGGER.info(f"Fetching customer instructions from Google Doc: {doc_id}")
                sections = artifacts_provider._fetch_google_doc_sections(doc_id)
                if not sections:
                    LOGGER.warning(
                        f"Failed to fetch Google Doc {doc_id} - falling back to bundled instructions"
                    )

            if not sections:
                sections = _load_fallback_instructions("customer_instructions.md")
                if not sections:
                    raise ValueError(
                        "No customer instructions available — set CUSTOMER_SUPPORT_DOC_ID or ensure "
                        "chat_orchestrator/instructions/customer_instructions.md exists"
                    )

            # Extract system instructions section
            system_instructions = sections.get("system_instructions", "")

            if not system_instructions:
                source = f"Google Doc {doc_id}" if doc_id else "fallback instructions file"
                error_msg = f"No 'System Instructions' section found in {source}"
                LOGGER.error(error_msg)
                raise ValueError(error_msg)

            # Compose context from all other sections
            context_parts = []
            examples_section = None
            examples_section_name = None

            # First pass: collect all sections except examples
            for section_name, section_content in sections.items():
                if section_name == "system_instructions" or not section_content.strip():
                    continue

                # Save examples section for later (try multiple possible names)
                if section_name in ["example_conversations", "examples", "example"]:
                    examples_section = section_content
                    examples_section_name = section_name
                else:
                    display_name = section_name.replace("_", " ").title()
                    context_parts.append(f"# {display_name}\n\n{section_content}")

            # Add examples section, truncating if needed to stay under 5000 words
            MAX_EXAMPLES_WORDS = 5000
            if examples_section:
                examples_words = len(examples_section.split())

                if examples_words > MAX_EXAMPLES_WORDS:
                    # Truncate examples to fit within limit
                    words = examples_section.split()[:MAX_EXAMPLES_WORDS]
                    truncated_examples = " ".join(words)
                    display_name = examples_section_name.replace("_", " ").title()
                    context_parts.append(
                        f"# {display_name}\n\n{truncated_examples}\n\n"
                        f"[Truncated: showing {MAX_EXAMPLES_WORDS}/{examples_words} words]"
                    )
                    LOGGER.warning(
                        f"Truncated examples section from {examples_words} to {MAX_EXAMPLES_WORDS} words"
                    )
                else:
                    # Examples fit within limit
                    display_name = examples_section_name.replace("_", " ").title()
                    context_parts.append(f"# {display_name}\n\n{examples_section}")

            context_message = "\n\n".join(context_parts) if context_parts else None

            # Truncate context if it exceeds MAX_CONTEXT_CHARS
            original_context_len = len(context_message) if context_message else 0
            if context_message and len(context_message) > MAX_CONTEXT_CHARS:
                context_message = context_message[:MAX_CONTEXT_CHARS]
                # Find last complete sentence or paragraph
                last_newline = context_message.rfind("\n\n")
                if last_newline > MAX_CONTEXT_CHARS * 0.8:  # Only use if not too much is lost
                    context_message = context_message[:last_newline]
                context_message += "\n\n[Context truncated due to size limits]"
                LOGGER.warning(
                    f"Truncated context message from {original_context_len} to "
                    f"{len(context_message)} chars"
                )

            final_word_count = len(system_instructions.split())
            if context_message:
                final_word_count += len(context_message.split())

            LOGGER.info(
                f"Loaded customer instructions from Google Doc: "
                f"system_instructions={len(system_instructions)} chars, "
                f"context_sections={len(context_parts)}, "
                f"context_chars={original_context_len}, "
                f"total_words={final_word_count}"
            )

            return system_instructions, context_message

        except ValueError as e:
            # ValueError indicates configuration or Google Doc errors - re-raise to fail fast
            LOGGER.exception(f"Critical error loading customer instructions: {e}")
            raise
        except Exception as e:
            # Other unexpected errors - also fail in customer mode (strict)
            LOGGER.exception(f"Unexpected error loading customer instructions: {e}")
            raise ValueError(f"Failed to load customer instructions: {e}") from e

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
        Get staff instructions from Google Doc with section parsing.

        Same pattern as customer mode:
        - "System Instructions" section → system instructions
        - All other sections → context message

        Returns:
            Tuple of (system_instructions, context_message)
            - system_instructions: Goes to the provider system-instruction channel
            - context_message: Goes as first user message (or None)
        """
        try:
            from orchestrator.services.artifacts_provider import ArtifactsProvider

            artifacts_provider = ArtifactsProvider(
                supabase_url=self._supabase_url,
                supabase_key=self._supabase_key,
            )

            # STAFF MODE: Google Doc preferred, falls back to bundled file
            doc_id = os.getenv("STAFF_SUPPORT_DOC_ID", "").strip()
            sections = None

            if doc_id:
                LOGGER.info(f"Fetching staff instructions from Google Doc: {doc_id}")
                sections = artifacts_provider._fetch_google_doc_sections(doc_id)
                if not sections:
                    LOGGER.warning(
                        f"Failed to fetch Google Doc {doc_id} - falling back to bundled instructions"
                    )

            if not sections:
                sections = _load_fallback_instructions("staff_instructions.md")
                if not sections:
                    LOGGER.error("No staff instructions available - set STAFF_SUPPORT_DOC_ID")
                    return self._default_instructions["general"], None

            if sections:
                # Parse and cache staff groups (skip from LLM context)
                global _staff_groups
                staff_groups_content = sections.pop("staff_groups", "")
                if staff_groups_content:
                    _staff_groups = _parse_staff_groups(staff_groups_content)
                    LOGGER.info(f"Loaded {len(_staff_groups)} staff group(s) from doc")

                # Extract system instructions section
                system_instructions = sections.get("system_instructions", "")

                # Compose context from all other sections
                context_parts = []
                examples_section = None
                examples_section_name = None

                # First pass: collect all sections except examples
                for section_name, section_content in sections.items():
                    if section_name == "system_instructions" or not section_content.strip():
                        continue

                    # Save examples section for later (try multiple possible names)
                    if section_name in ["example_conversations", "examples", "example"]:
                        examples_section = section_content
                        examples_section_name = section_name
                    else:
                        display_name = section_name.replace("_", " ").title()
                        context_parts.append(f"# {display_name}\n\n{section_content}")

                # Add examples section, truncating if needed to stay under 5000 words
                MAX_EXAMPLES_WORDS = 5000
                if examples_section:
                    examples_words = len(examples_section.split())

                    if examples_words > MAX_EXAMPLES_WORDS:
                        # Truncate examples to fit within limit
                        words = examples_section.split()[:MAX_EXAMPLES_WORDS]
                        truncated_examples = " ".join(words)
                        display_name = examples_section_name.replace("_", " ").title()
                        context_parts.append(
                            f"# {display_name}\n\n{truncated_examples}\n\n"
                            f"[Truncated: showing {MAX_EXAMPLES_WORDS}/{examples_words} words]"
                        )
                        LOGGER.warning(
                            f"Truncated examples section from {examples_words} to {MAX_EXAMPLES_WORDS} words"
                        )
                    else:
                        # Examples fit within limit
                        display_name = examples_section_name.replace("_", " ").title()
                        context_parts.append(f"# {display_name}\n\n{examples_section}")

                context_message = "\n\n".join(context_parts) if context_parts else None

                # Truncate context if it exceeds MAX_CONTEXT_CHARS
                original_context_len = len(context_message) if context_message else 0
                if context_message and len(context_message) > MAX_CONTEXT_CHARS:
                    context_message = context_message[:MAX_CONTEXT_CHARS]
                    # Find last complete sentence or paragraph
                    last_newline = context_message.rfind("\n\n")
                    if last_newline > MAX_CONTEXT_CHARS * 0.8:  # Only use if not too much is lost
                        context_message = context_message[:last_newline]
                    context_message += "\n\n[Context truncated due to size limits]"
                    LOGGER.warning(
                        f"Truncated staff context message from {original_context_len} to "
                        f"{len(context_message)} chars"
                    )

                LOGGER.info(
                    f"Loaded staff instructions from Google Doc: "
                    f"system_instructions={len(system_instructions)} chars, "
                    f"context_sections={len(context_parts)}, "
                    f"context_chars={original_context_len}"
                )

                if system_instructions:
                    return system_instructions, context_message
                else:
                    LOGGER.warning(
                        "No 'System Instructions' section found in Google Doc, using default"
                    )
                    return self._default_instructions["general"], context_message
            else:
                LOGGER.error(f"Failed to fetch Google Doc {doc_id}")
                return self._default_instructions["general"], None

        except Exception as e:
            LOGGER.exception(f"Error fetching staff instructions: {e}")
            return self._default_instructions["general"], None

    async def get_verification_instructions(self) -> Optional[str]:
        """
        Get verification criteria from Google Doc for LLM-as-judge.

        Used to verify customer-facing responses before sending.
        Returns the full document content as verification instructions.

        Returns:
            Verification instructions string, or None if not configured
        """
        doc_id = os.getenv("VERIFICATION_DOC_ID", "").strip()
        if not doc_id:
            LOGGER.debug("VERIFICATION_DOC_ID not set - verification disabled")
            return None

        try:
            from orchestrator.services.artifacts_provider import ArtifactsProvider

            artifacts_provider = ArtifactsProvider(
                supabase_url=self._supabase_url,
                supabase_key=self._supabase_key,
            )

            LOGGER.info(f"Fetching verification instructions from Google Doc: {doc_id}")

            # Fetch sections and combine into single instruction string
            sections = artifacts_provider._fetch_google_doc_sections(doc_id)

            if not sections:
                LOGGER.error(f"Failed to fetch verification Google Doc {doc_id}")
                return None

            # Combine all sections into verification instructions
            parts = []
            for section_name, section_content in sections.items():
                if section_content.strip():
                    display_name = section_name.replace("_", " ").title()
                    parts.append(f"# {display_name}\n\n{section_content}")

            verification_instructions = "\n\n".join(parts)

            LOGGER.info(
                f"Loaded verification instructions: {len(verification_instructions)} chars, "
                f"{len(parts)} sections"
            )

            return verification_instructions

        except Exception as e:
            LOGGER.exception(f"Error fetching verification instructions: {e}")
            return None

    async def get_troubleshooting_procedures(self) -> Optional[str]:
        """Get common instructions shared between customer and staff modes.

        Fetches the troubleshooting procedures Google Doc, parses sections starting from
        "Troubleshooting Steps For Common Issues", and returns combined markdown.
        """
        doc_id = os.getenv("TROUBLESHOOTING_PROCEDURES_DOC_ID", "").strip()
        if not doc_id:
            LOGGER.debug("TROUBLESHOOTING_PROCEDURES_DOC_ID not set - no common instructions")
            return None

        try:
            from orchestrator.services.artifacts_provider import ArtifactsProvider

            artifacts_provider = ArtifactsProvider(
                supabase_url=self._supabase_url,
                supabase_key=self._supabase_key,
            )

            LOGGER.info(f"Fetching troubleshooting procedures from Google Doc: {doc_id}")
            sections = artifacts_provider._fetch_google_doc_sections(
                doc_id,
                start_section="troubleshooting steps for common issues",
            )

            if not sections:
                LOGGER.error(f"Failed to fetch troubleshooting procedures Google Doc {doc_id}")
                return None

            # Combine all sections into single markdown string
            parts = []
            for section_name, section_content in sections.items():
                if section_content.strip():
                    display_name = section_name.replace("_", " ").title()
                    parts.append(f"# {display_name}\n\n{section_content}")

            troubleshooting_procedures = "\n\n".join(parts)
            LOGGER.info(
                f"Loaded troubleshooting procedures: {len(troubleshooting_procedures)} chars, "
                f"{len(parts)} sections"
            )
            return troubleshooting_procedures

        except Exception as e:
            LOGGER.exception(f"Error fetching troubleshooting procedures: {e}")
            return None


__all__ = ["InstructionsProvider", "Instruction"]
