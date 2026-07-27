"""Jira alert issue-type discovery and guarded LLM selection.

The selector never lets a model invent an issue type: it receives the
project's current create metadata and may return only an advertised id.  A
caller can always use :meth:`fallback` when Jira metadata or the LLM is
unavailable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class JiraIssueType:
    id: str
    name: str
    description: str = ""
    required_fields: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class IssueTypeSelection:
    issue_type: JiraIssueType
    decided_by: str
    reason: str


def normalize_issue_types(payload: Any) -> List[JiraIssueType]:
    """Return creatable type metadata across Jira's supported response shapes."""
    if isinstance(payload, dict):
        values = payload.get("values") or payload.get("issueTypes") or []
    elif isinstance(payload, list):
        values = payload
    else:
        return []
    result: List[JiraIssueType] = []
    for item in values:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        field_map = item.get("fields") or {}
        if not isinstance(field_map, dict):
            field_map = {}
        result.append(
            JiraIssueType(
                id=str(item["id"]),
                name=str(item["name"]),
                description=str(item.get("description") or ""),
                required_fields=tuple(
                    str(field_id) for field_id, field in field_map.items()
                    if isinstance(field, dict) and field.get("required")
                ),
                fields=tuple(str(field_id) for field_id in field_map),
            )
        )
    return result


class JiraIssueTypeSelector:
    """Fetches a project's types and asks the configured LLM to choose one."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Dict[str, str],
        project_key: str,
        model: str,
        cache_ttl_seconds: float = 900.0,
        get_session: Callable[[], Any],
        gateway: Optional[Any] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = headers
        self._project_key = project_key
        self._model = model
        self._cache_ttl_seconds = cache_ttl_seconds
        self._get_session = get_session
        self._gateway = gateway
        self._cached_types: List[JiraIssueType] = []
        self._cached_at = 0.0

    async def available_types(self) -> List[JiraIssueType]:
        now = time.monotonic()
        if self._cached_types and now - self._cached_at < self._cache_ttl_seconds:
            return self._cached_types
        url = f"{self._base_url}/rest/api/3/issue/createmeta/{self._project_key}/issuetypes"
        try:
            session = self._get_session()
            async with session.get(
                url, headers=self._headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status != 200:
                    LOGGER.warning("Jira issue-type metadata failed: HTTP %s", response.status)
                    return []
                types = normalize_issue_types(await response.json())
        except Exception:
            LOGGER.warning("Unable to fetch Jira issue-type metadata", exc_info=True)
            return []
        # The list endpoint intentionally keeps its response small. Fetch the
        # field contract for each type so callers can reject a selection whose
        # required fields cannot be populated safely.
        detailed: List[JiraIssueType] = []
        for issue_type in types:
            detail_url = (
                f"{url}/{issue_type.id}"
            )
            try:
                session = self._get_session()
                async with session.get(
                    detail_url, headers=self._headers, timeout=aiohttp.ClientTimeout(total=15)
                ) as response:
                    if response.status == 200:
                        parsed = normalize_issue_types({"values": [await response.json()]})
                        detailed.append(parsed[0] if parsed else issue_type)
                    else:
                        detailed.append(issue_type)
            except Exception:
                detailed.append(issue_type)
        self._cached_types = detailed
        self._cached_at = now
        return detailed

    async def select(
        self,
        *,
        summary: str,
        description: str,
        requested_type: Optional[str] = None,
    ) -> Optional[IssueTypeSelection]:
        types = await self.available_types()
        if not types:
            return None
        by_id = {item.id: item for item in types}
        if requested_type and requested_type in by_id:
            return IssueTypeSelection(by_id[requested_type], "caller", "validated requested type")
        catalogue = [
            {"id": item.id, "name": item.name, "description": item.description}
            for item in types
        ]
        prompt = (
            "Choose the single most appropriate Jira issue type for this alert. "
            "Return JSON only: {\"issue_type_id\": \"...\", \"reason\": \"...\"}. "
            "issue_type_id must be copied exactly from the catalogue.\n\n"
            f"Catalogue: {json.dumps(catalogue)}\n"
            f"Alert summary: {summary}\nAlert details: {description[:4000]}"
        )
        try:
            # Keep LLM providers lazy: every ticket service imports this
            # module, while only a configured Jira alert creation needs an
            # LLM gateway.
            from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway

            gateway = self._gateway or get_default_generation_gateway(default_model=self._model)
            response = await gateway.generate(
                [LLMMessage(role="user", text=prompt)],
                GenerationOptions(model=self._model, temperature=0.0, response_format="json"),
            )
            raw = json.loads(response.text or "{}")
            selected = by_id.get(str(raw.get("issue_type_id") or ""))
            if selected is None:
                LOGGER.warning("Jira type selector returned a non-creatable issue type")
                return None
            return IssueTypeSelection(selected, "llm", str(raw.get("reason") or ""))
        except Exception:
            LOGGER.warning("Jira issue-type selection failed", exc_info=True)
            return None

    @staticmethod
    def fallback(types: List[JiraIssueType], fallback_name: str) -> Optional[IssueTypeSelection]:
        target = fallback_name.strip().lower()
        for item in types:
            if item.name.strip().lower() == target:
                return IssueTypeSelection(item, "fallback", f"configured fallback {item.name}")
        return None
