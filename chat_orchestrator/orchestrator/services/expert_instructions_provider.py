"""Provider for expert-specific instructions from Google Docs.

Parses a single Google Doc containing all expert definitions with
system instructions, tools, packet types, and workflows.

Doc ID is set via the EXPERT_INSTRUCTIONS_DOC_ID environment variable.

Expected doc structure:
    # Shared Components
    ## Capabilities
    ## Shapes

    # Expert: grid_analyst
    ## System Instructions
    ## Tools
    ## Packet Types
    ### grid_analysis
    #### Workflow
    ### kpi_report
    #### Workflow

Usage:
    from orchestrator.services.expert_instructions_provider import (
        ExpertInstructionsProvider,
        ExpertConfig,
    )

    provider = ExpertInstructionsProvider()

    # Get a specific expert
    config = await provider.get_expert_config("grid_analyst")

    # Get all expert IDs
    expert_ids = await provider.get_all_expert_ids()

    # Find which expert handles a packet type
    expert_id = await provider.get_expert_for_packet_type("grid_analysis")
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown_sections
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

_INSTRUCTIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "instructions"
)


def _load_fallback_expert_instructions() -> Optional[Dict[str, str]]:
    path = os.path.join(_INSTRUCTIONS_DIR, "expert_instructions.md")
    if not os.path.exists(path):
        return None
    try:
        import re

        text = open(path).read()
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
        # Parse into sections matching fetch_google_doc_markdown_sections output format
        sections: Dict[str, str] = {}
        current_key: Optional[str] = None
        current_lines: list[str] = []
        for line in text.splitlines():
            m = re.match(r"^#\s+(.+)", line)
            if m:
                if current_key and current_lines:
                    sections[current_key] = "\n".join(current_lines).strip()
                current_key = m.group(1).strip().lower().replace(" ", "_")
                current_lines = []
            else:
                if current_key is not None:
                    current_lines.append(line)
        if current_key and current_lines:
            sections[current_key] = "\n".join(current_lines).strip()
        LOGGER.info(f"Loaded fallback expert instructions ({len(sections)} sections)")
        return sections or None
    except Exception as e:
        LOGGER.error(f"Failed to load fallback expert instructions: {e}")
        return None


# Module-level cache (1 hour TTL)
_expert_cache: Dict[str, "ExpertConfig"] = {}
_cache_timestamp: float = 0
CACHE_TTL_SECONDS = 3600  # 1 hour


@dataclass
class ExpertConfig:
    """Configuration for an expert parsed from the Google Doc."""

    expert_id: str
    display_name: str
    system_instructions: str
    tools: List[str]
    packet_types: List[str]
    workflows: Dict[str, List[str]]  # packet_type -> [step1, step2, ...]
    capabilities: List[str]
    raw_sections: Dict[str, str] = field(default_factory=dict)
    model: Optional[str] = None  # Optional model override (e.g., "gemini-3-flash")
    settings: Dict[str, Any] = field(default_factory=dict)  # Parsed from ### Settings
    expert_type: str = "stateless"  # "stateless", "persistent", or "user_startable"
    anchor_entity_type: Optional[str] = None  # e.g., "grid" for persistent experts
    wake_schedule: Optional[str] = None  # Cron expression (UTC) for periodic wakes
    triggers: List[str] = field(
        default_factory=list
    )  # Natural language triggers for user_startable
    required_inputs: List[str] = field(default_factory=list)  # Required inputs for user_startable

    @property
    def is_persistent(self) -> bool:
        """Whether this is a persistent (long-running) expert."""
        return self.expert_type in ("persistent", "user_startable")

    @property
    def is_user_startable(self) -> bool:
        """Whether users can instantiate this expert as an agent."""
        return self.expert_type == "user_startable"

    def get_workflow(self, packet_type: str) -> List[str]:
        """Get workflow steps for a packet type.

        Args:
            packet_type: Type of packet

        Returns:
            List of workflow step definitions, or default ["execute"] if not found
        """
        return self.workflows.get(packet_type, ["[llm] execute - Execute the task"])

    @property
    def resumable(self) -> bool:
        """Whether this expert's work can be resumed.

        Resumable experts show: Run new / Resume / Cancel
        Non-resumable experts show: Run new / Cancel
        """
        return bool(self.settings.get("resumable", False))


class ExpertInstructionsProvider:
    """Provides expert configurations from Google Docs.

    Follows the caching pattern from artifacts_provider.py with module-level
    TTL cache for performance.
    """

    DEFAULT_EXPERT_DOC_ID = ""  # Set EXPERT_INSTRUCTIONS_DOC_ID env var; falls back to bundled file

    def __init__(self, doc_id: Optional[str] = None):
        """Initialize the provider.

        Args:
            doc_id: Google Doc ID containing expert definitions.
                   Falls back to EXPERT_INSTRUCTIONS_DOC_ID env var,
                   then to DEFAULT_EXPERT_DOC_ID.
        """
        self.doc_id = (
            doc_id or os.getenv("EXPERT_INSTRUCTIONS_DOC_ID") or self.DEFAULT_EXPERT_DOC_ID
        )

    async def get_expert_config(self, expert_id: str) -> Optional[ExpertConfig]:
        """Get configuration for a specific expert.

        Args:
            expert_id: Expert identifier (e.g., "grid_analyst")

        Returns:
            ExpertConfig if found, None otherwise
        """
        experts = await self._fetch_all_experts()
        return experts.get(expert_id)

    async def get_all_expert_ids(self) -> List[str]:
        """Get list of all available expert IDs.

        Returns:
            List of expert identifier strings
        """
        experts = await self._fetch_all_experts()
        return list(experts.keys())

    async def get_all_experts(self) -> Dict[str, ExpertConfig]:
        """Get all expert configurations.

        Returns:
            Dictionary mapping expert IDs to ExpertConfig objects
        """
        return await self._fetch_all_experts()

    async def get_expert_for_packet_type(self, packet_type: str) -> Optional[str]:
        """Find which expert handles a given packet type.

        Args:
            packet_type: Type of work packet

        Returns:
            Expert ID if found, None otherwise
        """
        experts = await self._fetch_all_experts()
        for expert_id, config in experts.items():
            if packet_type in config.packet_types:
                return expert_id
        return None

    async def get_experts_with_capability(self, capability: str) -> List[str]:
        """Find all experts with a specific capability.

        Args:
            capability: Capability to search for

        Returns:
            List of expert IDs with the capability
        """
        experts = await self._fetch_all_experts()
        return [
            expert_id for expert_id, config in experts.items() if capability in config.capabilities
        ]

    async def _fetch_all_experts(self) -> Dict[str, ExpertConfig]:
        """Fetch and parse all expert configurations.

        Uses existing Google Doc converter with start_section='shared components'
        to skip the introduction section before the Shared Components header.

        Returns cached results if cache is still valid (1 hour TTL).

        Returns:
            Dictionary mapping expert IDs to ExpertConfig objects
        """
        global _expert_cache, _cache_timestamp

        # Check cache
        if _expert_cache and (time.time() - _cache_timestamp) < CACHE_TTL_SECONDS:
            return _expert_cache

        try:
            sections = None
            if self.doc_id:
                # Reuse existing doc fetcher - start from "Shared Components" section
                # This ignores any introduction content before that header
                sections = fetch_google_doc_markdown_sections(
                    self.doc_id, start_section="shared components"
                )
                if not sections:
                    LOGGER.warning(
                        f"Failed to fetch expert instructions doc {self.doc_id} - trying fallback file"
                    )

            if not sections:
                sections = _load_fallback_expert_instructions()
            if not sections:
                LOGGER.error("No expert instructions available")
                return _expert_cache if _expert_cache else {}

            experts = self._parse_expert_sections(sections)

            _expert_cache = experts
            _cache_timestamp = time.time()

            LOGGER.info(f"Loaded {len(experts)} expert configurations from Google Doc")
            return experts

        except Exception as e:
            LOGGER.error(f"Failed to fetch expert instructions: {e}")
            # Return cached if available
            if _expert_cache:
                LOGGER.warning("Using stale expert cache")
                return _expert_cache
            return {}

    def _parse_expert_sections(self, sections: Dict[str, str]) -> Dict[str, ExpertConfig]:
        """Parse doc sections into ExpertConfig objects.

        Expected doc structure (after parsing by fetch_google_doc_markdown_sections):
        - shared_components: Shared capabilities and shapes
        - expert:_grid_analyst: Grid analyst expert section
        - expert:_report_writer: Report writer expert section

        Args:
            sections: Dictionary from fetch_google_doc_markdown_sections

        Returns:
            Dictionary mapping expert IDs to ExpertConfig objects
        """
        experts: Dict[str, ExpertConfig] = {}
        shared_capabilities: List[str] = []

        # Parse shared components first
        shared_key = self._find_section_key(sections, "shared")
        if shared_key:
            shared_text = sections[shared_key]
            # Extract capabilities section
            shared_capabilities = self._extract_subsection_list(shared_text, "capabilities")

        # Parse each expert section
        for key, content in sections.items():
            # Skip disabled experts (strikethrough in Google Docs → ~~text~~ in markdown)
            if "~~" in key:
                # Extract expert name for logging (remove strikethrough markers)
                disabled_name = key.replace("~~", "").strip()
                LOGGER.info(f"Skipping disabled expert (strikethrough): {disabled_name}")
                continue

            # Look for expert sections (various naming conventions)
            expert_id = None

            if key.startswith("expert:_"):
                expert_id = key.replace("expert:_", "").strip()
            elif key.startswith("expert_"):
                expert_id = key.replace("expert_", "").strip()
            elif ":_" in key and "expert" in key.lower():
                # Handle "Expert: grid_analyst" becoming "expert:_grid_analyst"
                expert_id = key.split(":_")[-1].strip()

            if expert_id:
                config = self._parse_single_expert(expert_id, content, shared_capabilities)
                if config:
                    experts[expert_id] = config

        return experts

    def _find_section_key(self, sections: Dict[str, str], prefix: str) -> Optional[str]:
        """Find a section key by prefix (case-insensitive).

        Args:
            sections: Section dictionary
            prefix: Prefix to search for

        Returns:
            Matching key or None
        """
        prefix_lower = prefix.lower()
        for key in sections.keys():
            if key.lower().startswith(prefix_lower):
                return key
        return None

    def _parse_single_expert(
        self,
        expert_id: str,
        content: str,
        shared_capabilities: List[str],
    ) -> Optional[ExpertConfig]:
        """Parse a single expert's section.

        Args:
            expert_id: Expert identifier
            content: Raw content of expert section
            shared_capabilities: Shared capabilities from shared components

        Returns:
            ExpertConfig or None if parsing fails
        """
        try:
            # Split into subsections by ## headers
            subsections = self._split_into_subsections(content)

            # Extract components
            display_name = expert_id.replace("_", " ").title()

            # System instructions - look for various naming conventions
            system_instructions = (
                subsections.get("system_instructions", "")
                or subsections.get("system instructions", "")
                or subsections.get("instructions", "")
            )

            # Tools list
            tools_text = subsections.get("tools", "") or subsections.get("available_tools", "")
            tools = self._parse_list(tools_text)

            # Packet types
            packet_types_text = subsections.get("packet_types", "") or subsections.get(
                "packet types", ""
            )
            packet_types = self._parse_list(packet_types_text)

            # Parse workflows - extract directly from raw content since
            # subsection parsing doesn't preserve parent context well
            workflows: Dict[str, List[str]] = {}
            workflows = self._extract_workflows_from_content(content, packet_types)

            # Expert-specific capabilities (add to shared)
            expert_capabilities = self._parse_list(subsections.get("capabilities", ""))
            all_capabilities = shared_capabilities + expert_capabilities

            # Parse ### Settings section (key: value pairs)
            settings = self._parse_settings(subsections.get("settings", ""))

            # Model can come from ### Settings or legacy ### Model section.
            # Supports env var references: GEMINI_MODEL, GEMINI_AGENT_PRO_MODEL,
            # GEMINI_FALLBACK_MODEL, VERIFICATION_MODEL etc.
            model = settings.get("model")
            if not model:
                # Fallback to legacy ### Model section
                model_raw = subsections.get("model", "")
                if model_raw:
                    for line in model_raw.split("\n"):
                        line = line.strip()
                        if line and not line.startswith("-") and not line.startswith("*"):
                            model = line
                            break
            # Resolve env var references (e.g., "GEMINI_AGENT_PRO_MODEL", "VERIFICATION_MODEL")
            # Any ALL_CAPS_WITH_UNDERSCORES value is treated as an env var name
            if model and model == model.upper() and "_" in model:
                resolved = os.getenv(model)
                if resolved:
                    LOGGER.info(f"Resolved model variable {model} → {resolved}")
                    model = resolved

            # Expert type (## Type)
            expert_type = (
                subsections.get("type", "").strip().lower().replace(" ", "_") or "stateless"
            )
            if expert_type not in ("stateless", "persistent", "user_startable"):
                expert_type = "stateless"

            anchor_entity_type = subsections.get("anchor_entity", "").strip().lower() or None

            wake_schedule = subsections.get("wake_schedule", "").strip() or None
            # Strip inline comments from cron expressions
            if wake_schedule and "#" in wake_schedule:
                wake_schedule = wake_schedule.split("#")[0].strip()

            # User-startable agent fields (## Triggers, ## Required Inputs)
            triggers: List[str] = []
            triggers_raw = subsections.get("triggers", "")
            if triggers_raw:
                triggers = [
                    line.strip().lstrip("- *")
                    for line in triggers_raw.split("\n")
                    if line.strip() and line.strip().lstrip("- *")
                ]

            required_inputs: List[str] = []
            inputs_raw = subsections.get("required_inputs", subsections.get("required inputs", ""))
            if inputs_raw:
                required_inputs = [
                    line.strip().lstrip("- *")
                    for line in inputs_raw.split("\n")
                    if line.strip() and line.strip().lstrip("- *")
                ]

            LOGGER.info(
                f"Parsed expert {expert_id}: model={model!r}, type={expert_type}, "
                f"settings={settings}, subsection_keys={list(subsections.keys())}"
            )

            return ExpertConfig(
                expert_id=expert_id,
                display_name=display_name,
                system_instructions=system_instructions.strip(),
                tools=tools,
                packet_types=packet_types,
                workflows=workflows,
                capabilities=all_capabilities,
                raw_sections=subsections,
                model=model,
                settings=settings,
                expert_type=expert_type,
                anchor_entity_type=anchor_entity_type,
                wake_schedule=wake_schedule,
                triggers=triggers,
                required_inputs=required_inputs,
            )

        except Exception as e:
            LOGGER.error(f"Failed to parse expert {expert_id}: {e}")
            return None

    def _split_into_subsections(self, content: str) -> Dict[str, str]:
        """Split content into subsections by ## headers.

        Args:
            content: Raw section content

        Returns:
            Dictionary mapping subsection names to content
        """
        subsections: Dict[str, str] = {}
        current_section = "header"
        current_content: List[str] = []

        for line in content.split("\n"):
            stripped = line.strip()

            # Check for ## header (but not ### or deeper)
            if stripped.startswith("## ") and not stripped.startswith("### "):
                # Save previous section
                if current_content:
                    subsections[current_section] = "\n".join(current_content).strip()

                # Start new section
                section_name = stripped[3:].strip().lower().replace(" ", "_")
                current_section = section_name
                current_content = []

            # Check for ### header (workflow or packet subsection)
            elif stripped.startswith("### "):
                # Save previous section
                if current_content:
                    subsections[current_section] = "\n".join(current_content).strip()

                # Start new section at ### level
                section_name = stripped[4:].strip().lower().replace(" ", "_")
                current_section = section_name
                current_content = []

            else:
                current_content.append(line)

        # Save last section
        if current_content:
            subsections[current_section] = "\n".join(current_content).strip()

        return subsections

    def _extract_subsection_list(self, content: str, subsection: str) -> List[str]:
        """Extract a list from a named subsection.

        Args:
            content: Raw section content
            subsection: Subsection name to find

        Returns:
            List of items from the subsection
        """
        subsections = self._split_into_subsections(content)
        text = subsections.get(subsection, "")
        return self._parse_list(text)

    def _parse_settings(self, settings_text: str) -> Dict[str, Any]:
        """Parse key: value pairs from a settings section.

        Expected format (resilient to variations):
            model: gemini-2.5-flash
            Resumable: True
            some_number: 42

        Handles:
        - Case variations: "Resumable", "RESUMABLE", "resumable" all work
        - Boolean variations: true/false, yes/no, on/off, enabled/disabled, 1/0
        - Numbers: integers and floats
        - Spaces in keys become underscores

        Args:
            settings_text: Raw text from ### Settings section

        Returns:
            Dict of setting name -> value (with type coercion)
        """
        settings: Dict[str, Any] = {}
        if not settings_text:
            return settings

        # Boolean true/false values (case-insensitive)
        TRUE_VALUES = {"true", "yes", "on", "enabled", "1"}
        FALSE_VALUES = {"false", "no", "off", "disabled", "0"}

        for line in settings_text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue

            # Remove list markers
            line = line.lstrip("-*• ").strip()

            # Parse key: value
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower().replace(" ", "_")
                value = value.strip()
                value_lower = value.lower()

                # Type coercion
                if value_lower in TRUE_VALUES:
                    settings[key] = True
                elif value_lower in FALSE_VALUES:
                    settings[key] = False
                elif value.lstrip("-").isdigit():
                    settings[key] = int(value)
                else:
                    # Try float
                    try:
                        settings[key] = float(value)
                    except ValueError:
                        settings[key] = value

        return settings

    def _is_commented_step(self, line: str) -> bool:
        """Check if a workflow step line is commented out.

        Supports comment prefixes:
        - // (C-style)
        - -- (SQL-style)

        These can appear at the start of the line or after list markers.

        Args:
            line: The line to check (should be stripped)

        Returns:
            True if the line is commented out
        """
        # Remove list markers first (numbers, dashes, asterisks)
        content = line.lstrip("0123456789.-* ")
        return content.startswith("//") or content.startswith("--")

    def _split_merged_workflow_steps(self, text: str) -> List[str]:
        """Split workflow steps that may be merged onto one line.

        Google Docs sometimes merges lines together, resulting in:
        "[llm] step1 - desc[function:handler] - desc[llm] step2"

        This splits them back into individual steps.

        Args:
            text: Text that may contain multiple merged steps

        Returns:
            List of individual step strings
        """
        import re

        # Split on [llm] or [function: markers, keeping the delimiter
        # Pattern matches the start of a new step
        parts = re.split(r"(?=\[llm\]|\[function:)", text)

        steps = []
        for part in parts:
            part = part.strip()
            if part and (part.startswith("[llm]") or part.startswith("[function:")):
                steps.append(part)

        return steps

    def _extract_workflow(self, content: str) -> List[str]:
        """Extract workflow steps from content.

        Looks for a "### Workflow" or "#### Workflow" section, or numbered list.
        Steps can be commented out using // or -- prefixes.

        Handles multiple formats:
        - Numbered: "1. [llm] step_name - description"
        - Bulleted: "- [llm] step_name - description"
        - Plain: "[llm] step_name - description"
        - Merged: "[llm] step1[function:x] step2" (Google Docs artifact)

        Args:
            content: Section content that may contain workflow

        Returns:
            List of workflow step definitions (excluding commented steps)
        """
        lines = content.split("\n")
        in_workflow = False
        steps: List[str] = []
        workflow_text_lines: List[str] = []

        for line in lines:
            stripped = line.strip()

            # Check for workflow header
            if "workflow" in stripped.lower() and (
                stripped.startswith("###") or stripped.startswith("####")
            ):
                in_workflow = True
                continue

            # Once in workflow section, collect content
            if in_workflow:
                # Stop at next header
                if stripped.startswith("#"):
                    break

                # Skip commented-out steps
                if self._is_commented_step(stripped):
                    LOGGER.debug(f"Skipping commented workflow step: {stripped[:50]}...")
                    continue

                # Skip empty lines
                if not stripped:
                    continue

                workflow_text_lines.append(stripped)

        # Process collected workflow lines
        for line in workflow_text_lines:
            # Remove numbering/bullets
            clean_line = line.lstrip("0123456789.-* ")

            # Check if this line has multiple steps merged together
            if "[llm]" in clean_line or "[function:" in clean_line:
                # Count step markers to detect merging
                step_markers = clean_line.count("[llm]") + clean_line.count("[function:")
                if step_markers > 1:
                    # Multiple steps on one line - split them
                    split_steps = self._split_merged_workflow_steps(clean_line)
                    steps.extend(split_steps)
                elif clean_line.startswith("[llm]") or clean_line.startswith("[function:"):
                    steps.append(clean_line)
                else:
                    # Has marker but doesn't start with it - try splitting anyway
                    split_steps = self._split_merged_workflow_steps(clean_line)
                    steps.extend(split_steps)
            elif clean_line:
                # Plain text step (no markers) - still add it
                steps.append(clean_line)

        # If no workflow section found, try finding steps in full content
        if not steps:
            # Collect all text and try to find step markers
            full_text = "\n".join(lines)

            # Look for [llm] or [function: markers anywhere
            if "[llm]" in full_text or "[function:" in full_text:
                steps = self._split_merged_workflow_steps(full_text)

        # Final fallback: look for numbered list
        if not steps:
            for line in lines:
                stripped = line.strip()
                if self._is_commented_step(stripped):
                    continue
                if stripped and stripped[0].isdigit() and "." in stripped[:3]:
                    step = stripped.split(".", 1)[-1].strip()
                    if step:
                        steps.append(step)

        return steps

    def _extract_workflows_from_content(
        self, content: str, packet_types: List[str]
    ) -> Dict[str, List[str]]:
        """Extract workflows for each packet type from raw content.

        The document has structure like:
            ## Packet: grid_analysis
            ### Workflow
            [llm] step1 - description
            [function:handler] - description

            ## Packet: kpi_report
            ### Workflow
            [llm] step1 - description

        This method uses regex to find each packet section and extract its workflow.

        Args:
            content: Raw expert section content
            packet_types: List of packet types to look for

        Returns:
            Dictionary mapping packet type to workflow steps
        """
        import re

        workflows: Dict[str, List[str]] = {}

        # Pattern to find packet sections: ## Packet: name or ## Packet:_name
        packet_pattern = re.compile(
            r"##\s*Packet:\s*(\w+)\s*\n(.*?)(?=##\s*Packet:|$)",
            re.IGNORECASE | re.DOTALL,
        )

        for match in packet_pattern.finditer(content):
            ptype = match.group(1).lower().strip()
            packet_content = match.group(2)

            if ptype in packet_types or ptype.replace("_", "") in packet_types:
                # Normalize the packet type name
                normalized_ptype = ptype if ptype in packet_types else ptype.replace("_", "")

                # Extract workflow from this packet's content
                workflow_steps = self._extract_workflow(packet_content)
                if workflow_steps:
                    workflows[normalized_ptype] = workflow_steps

        return workflows

    def _parse_list(self, text: str) -> List[str]:
        """Parse a markdown list or plain text list into items.

        Handles multiple formats:
        - Bullet points: `- item` or `* item`
        - Numbered lists: `1. item`
        - Plain text: One item per line (for Google Docs without formatting)

        Args:
            text: Text with list items

        Returns:
            List of items (stripped of bullets/numbers)
        """
        items: List[str] = []
        for line in text.strip().split("\n"):
            line = line.strip()

            # Skip empty lines
            if not line:
                continue

            # Skip headers (lines starting with #)
            if line.startswith("#"):
                continue

            # Handle bullet points
            if line.startswith("- ") or line.startswith("* "):
                items.append(line[2:].strip())

            # Handle numbered lists
            elif line[0].isdigit() and "." in line[:3]:
                # Split on first period after number
                parts = line.split(".", 1)
                if len(parts) > 1:
                    items.append(parts[1].strip())

            # Handle plain text lines (common in Google Docs)
            else:
                # Skip lines that look like descriptions or prose (contain spaces and are long)
                # Items are typically single words or short phrases
                if len(line) < 100 and not line.endswith(":"):
                    items.append(line)

        return items


def clear_expert_cache():
    """Clear the module-level expert cache.

    Useful for testing or forcing a refresh.
    """
    global _expert_cache, _cache_timestamp
    _expert_cache = {}
    _cache_timestamp = 0


__all__ = [
    "ExpertInstructionsProvider",
    "ExpertConfig",
    "clear_expert_cache",
]
