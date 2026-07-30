"""Jira issue-type discovery and guarded LLM selection.

The selector never lets a model invent an issue type: it receives the
project's current create metadata and may return only an advertised id.  A
caller can always use :meth:`fallback` when Jira metadata or the LLM is
unavailable.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

import aiohttp

from shared.prompts import PROMPTS
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)
_ISSUE_TYPE_METADATA_TIMEOUT_SECONDS = 5.0
_ISSUE_TYPE_SELECTION_TIMEOUT_SECONDS = 3.0
_ISSUE_TYPE_FIELDS_PAGE_SIZE = 50


@dataclass(frozen=True)
class JiraFieldOption:
    """One Jira option advertised for a create-metadata field."""

    id: str
    value: str


@dataclass(frozen=True)
class JiraFieldDefinition:
    """The create contract for a single Jira field."""

    id: str
    name: str
    required: bool = False
    allowed_values: tuple[JiraFieldOption, ...] = ()


@dataclass(frozen=True)
class JiraIssueType:
    """A Jira issue type together with its complete create-field contract."""

    id: str
    name: str
    description: str = ""
    fields: tuple[JiraFieldDefinition, ...] = ()

    def field(self, field_id: str) -> Optional[JiraFieldDefinition]:
        """Return a field by Jira's stable field ID, if it is advertised."""
        return next((field for field in self.fields if field.id == field_id), None)

    @property
    def required_fields(self) -> tuple[str, ...]:
        """Compatibility view of the required field IDs."""
        return tuple(field.id for field in self.fields if field.required)


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
        fields: List[JiraFieldDefinition] = []
        for field_id, field in field_map.items():
            if not isinstance(field, dict):
                continue
            allowed_values = field.get("allowedValues") or []
            options: List[JiraFieldOption] = []
            if isinstance(allowed_values, list):
                for option in allowed_values:
                    if not isinstance(option, dict) or option.get("id") is None:
                        continue
                    value = option.get("value")
                    if value is None:
                        continue
                    options.append(JiraFieldOption(id=str(option["id"]), value=str(value)))
            fields.append(
                JiraFieldDefinition(
                    id=str(field_id),
                    name=str(field.get("name") or field_id),
                    required=bool(field.get("required")),
                    allowed_values=tuple(options),
                )
            )
        result.append(
            JiraIssueType(
                id=str(item["id"]),
                name=str(item["name"]),
                description=str(item.get("description") or ""),
                fields=tuple(fields),
            )
        )
    return result


def _normalize_field_page(payload: Any) -> Optional[tuple[JiraFieldDefinition, ...]]:
    """Parse one Jira issue-type field-metadata page.

    Jira's detail endpoint returns fields as list items identified by
    ``fieldId``; it does not repeat the surrounding issue type's id/name.
    ``None`` means the response was not a valid field-contract page, so the
    caller must not advertise that issue type as safely creatable.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("fields"), list):
        return None
    fields: List[JiraFieldDefinition] = []
    for field in payload["fields"]:
        if not isinstance(field, dict) or not field.get("fieldId"):
            return None
        allowed_values = field.get("allowedValues") or []
        if not isinstance(allowed_values, list):
            return None
        options: List[JiraFieldOption] = []
        for option in allowed_values:
            if not isinstance(option, dict) or option.get("id") is None:
                continue
            value = option.get("value")
            if value is None:
                value = option.get("name")
            if value is None:
                continue
            options.append(JiraFieldOption(id=str(option["id"]), value=str(value)))
        field_id = str(field["fieldId"])
        fields.append(
            JiraFieldDefinition(
                id=field_id,
                name=str(field.get("name") or field_id),
                required=bool(field.get("required")),
                allowed_values=tuple(options),
            )
        )
    return tuple(fields)


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
        metadata_timeout_seconds: float = _ISSUE_TYPE_METADATA_TIMEOUT_SECONDS,
        llm_timeout_seconds: float = _ISSUE_TYPE_SELECTION_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = headers
        self._project_key = project_key
        self._model = model
        self._cache_ttl_seconds = cache_ttl_seconds
        self._get_session = get_session
        self._gateway = gateway
        self._metadata_timeout_seconds = metadata_timeout_seconds
        self._llm_timeout_seconds = llm_timeout_seconds
        self._cached_types: List[JiraIssueType] = []
        self._cached_at = 0.0

    async def available_types(self) -> List[JiraIssueType]:
        now = time.monotonic()
        if self._cached_types and now - self._cached_at < self._cache_ttl_seconds:
            return self._cached_types
        url = f"{self._base_url}/rest/api/3/issue/createmeta/{self._project_key}/issuetypes"
        try:
            types = await asyncio.wait_for(
                self._fetch_issue_type_list(url),
                timeout=self._metadata_timeout_seconds,
            )
        except Exception:
            LOGGER.warning("Unable to fetch Jira issue-type metadata", exc_info=True)
            return []
        if not types:
            return []

        elapsed = time.monotonic() - now
        remaining = max(0.0, self._metadata_timeout_seconds - elapsed)
        tasks = [
            asyncio.create_task(self._fetch_issue_type_detail(url, issue_type))
            for issue_type in types
        ]
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        detailed: List[JiraIssueType] = []
        for task in tasks:
            if task not in done:
                continue
            try:
                issue_type = task.result()
            except Exception:
                LOGGER.warning("Unable to fetch Jira issue-type field metadata", exc_info=True)
                continue
            if issue_type is not None:
                detailed.append(issue_type)
        self._cached_types = detailed
        self._cached_at = now
        return detailed

    async def _fetch_issue_type_list(self, url: str) -> List[JiraIssueType]:
        start_at = 0
        issue_types: List[JiraIssueType] = []
        while True:
            session = self._get_session()
            async with session.get(
                url,
                headers=self._headers,
                params={"startAt": start_at, "maxResults": _ISSUE_TYPE_FIELDS_PAGE_SIZE},
                timeout=aiohttp.ClientTimeout(total=self._metadata_timeout_seconds),
            ) as response:
                if response.status != 200:
                    LOGGER.warning("Jira issue-type metadata failed: HTTP {}", response.status)
                    return []
                payload = await response.json()
            if not isinstance(payload, dict):
                return []
            raw_values = payload.get("values")
            if raw_values is None:
                raw_values = payload.get("issueTypes")
            if not isinstance(raw_values, list):
                return []
            issue_types.extend(normalize_issue_types(payload))

            has_paging = any(
                key in payload for key in ("startAt", "maxResults", "total", "isLast")
            )
            if not has_paging:
                break
            page_start = payload.get("startAt", start_at)
            total = payload.get("total")
            if not isinstance(page_start, int):
                return []
            if isinstance(total, int):
                if page_start + len(raw_values) >= total:
                    break
            elif payload.get("isLast") is True:
                break
            else:
                page_size = payload.get("maxResults", _ISSUE_TYPE_FIELDS_PAGE_SIZE)
                if not isinstance(page_size, int) or page_size <= 0:
                    return []
                if len(raw_values) < page_size:
                    break
            if not raw_values:
                return []
            start_at = page_start + len(raw_values)
        return issue_types

    async def _fetch_issue_type_detail(
        self, list_url: str, issue_type: JiraIssueType
    ) -> Optional[JiraIssueType]:
        detail_url = f"{list_url}/{issue_type.id}"
        start_at = 0
        fields: List[JiraFieldDefinition] = []
        while True:
            session = self._get_session()
            async with session.get(
                detail_url,
                headers=self._headers,
                params={"startAt": start_at, "maxResults": _ISSUE_TYPE_FIELDS_PAGE_SIZE},
                timeout=aiohttp.ClientTimeout(total=self._metadata_timeout_seconds),
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json()
            page_fields = _normalize_field_page(payload)
            if page_fields is None:
                return None
            fields.extend(page_fields)

            page_start = payload.get("startAt", start_at)
            total = payload.get("total")
            if not isinstance(page_start, int):
                return None
            if isinstance(total, int):
                if page_start + len(page_fields) >= total:
                    break
            elif payload.get("isLast") is True:
                break
            elif len(page_fields) < _ISSUE_TYPE_FIELDS_PAGE_SIZE:
                break
            if not page_fields:
                return None
            start_at = page_start + len(page_fields)

        return JiraIssueType(
            id=issue_type.id,
            name=issue_type.name,
            description=issue_type.description,
            fields=tuple(fields),
        )

    async def select(
        self,
        *,
        summary: str,
        description: str,
        requested_type: Optional[str] = None,
        operational_context: Optional[Dict[str, Any]] = None,
        candidate_types: Sequence[JiraIssueType] | None = None,
    ) -> Optional[IssueTypeSelection]:
        types = list(candidate_types) if candidate_types is not None else await self.available_types()
        if not types:
            return None
        by_id = {item.id: item for item in types}
        if requested_type and requested_type in by_id:
            return IssueTypeSelection(by_id[requested_type], "caller", "validated requested type")
        catalogue = [
            {"id": item.id, "name": item.name, "description": item.description}
            for item in types
        ]
        operational_context_block = (
            f"\nLive grid telemetry: {json.dumps(operational_context, default=str)}"
            if operational_context
            else ""
        )
        prompt = PROMPTS.text(
            "ticketing.jira_issue_types",
            catalogue_json=json.dumps(catalogue),
            summary=summary,
            description=description[:4000],
            operational_context_block=operational_context_block,
        )
        try:
            # Keep LLM providers lazy: every ticket service imports this
            # module, while only a configured Jira ticket creation needs an
            # LLM gateway.
            from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway

            gateway = self._gateway or get_default_generation_gateway(default_model=self._model)
            response = await asyncio.wait_for(
                gateway.generate(
                    [LLMMessage(role="user", text=prompt)],
                    GenerationOptions(model=self._model, temperature=0.0, response_format="json"),
                ),
                timeout=self._llm_timeout_seconds,
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
