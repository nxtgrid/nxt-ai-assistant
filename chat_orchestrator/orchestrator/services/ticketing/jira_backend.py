"""Jira ``TicketBackend`` implementation using live create metadata.

Ticket creation fetches the configured project's creatable issue types,
selects only a type whose required fields can be populated safely, and builds
the request from that metadata. This keeps ticket creation on one
project-derived path rather than relying on a separate alert profile.
"""

from __future__ import annotations

import base64
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

import aiohttp

from orchestrator.config.settings import get_settings
from shared.config import flag_registry as fr
from shared.utils.logging import get_logger

from .attachment_repository import BUCKET_NAME, EscalationAttachment
from .backend import (
    AttachmentSyncResult,
    TicketBackendError,
    TicketCreateRequest,
    TicketResult,
    TicketStatus,
    TicketSummary,
)
from .jira_issue_payload import (
    JiraCreateContext,
    build_issue_payload,
    compatible_issue_types,
    incompatible_issue_type_reason,
)
from .jira_issue_types import IssueTypeSelection, JiraIssueType, JiraIssueTypeSelector

LOGGER = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level shared resources (moved from escalation_service.py unchanged)
# ---------------------------------------------------------------------------

# Shared aiohttp session for all Jira API calls (avoids per-request TCP setup).
# Created lazily on first use; replaced when closed.
_jira_session: Optional[aiohttp.ClientSession] = None


def _get_jira_session() -> aiohttp.ClientSession:
    global _jira_session
    if _jira_session is None or _jira_session.closed:
        _jira_session = aiohttp.ClientSession()
    return _jira_session


# TTL cache for Jira organization list (changes rarely -- max one fetch per 30 min).
_jira_orgs_cache: List[Dict[str, Any]] = []
_jira_orgs_cache_time: Optional[float] = None
_JIRA_ORGS_TTL: float = 1800.0  # 30 minutes
_URGENT_ALERT_SUMMARY = re.compile(r"^\s*!\s*urgent\s*:", re.IGNORECASE)


def _adf_to_text(adf: Any, _depth: int = 0, _max_depth: int = 50) -> str:
    """Extract plain text from an Atlassian Document Format node (recursive, depth-limited).

    Sibling blocks (``paragraph``, ``bulletList``) are joined with ``\\n``,
    ``hardBreak`` becomes ``\\n``, and a ``listItem`` is prefixed with
    ``"- "`` -- the inverse of ``_text_to_adf`` for whatever shape that
    function actually produces. A single plain paragraph (no bullets, no
    line breaks) still returns exactly its own text, unchanged from before
    this function supported multi-block docs.
    """
    if _depth > _max_depth or not adf or not isinstance(adf, dict):
        return ""
    node_type = adf.get("type")
    if node_type == "text":
        return str(adf.get("text", ""))
    if node_type == "hardBreak":
        return "\n"
    children = adf.get("content", []) or []
    if node_type == "listItem":
        inner = "".join(_adf_to_text(child, _depth + 1, _max_depth) for child in children)
        return f"- {inner}"
    if node_type in ("doc", "bulletList"):
        return "\n".join(_adf_to_text(child, _depth + 1, _max_depth) for child in children)
    # paragraph (and any other/unknown block type): concatenate inline content directly.
    return "".join(_adf_to_text(child, _depth + 1, _max_depth) for child in children)


def _text_to_adf(text: str) -> Dict[str, Any]:
    """Convert plain text into an ADF doc -- consecutive plain lines become
    one ``paragraph`` (joined by ``hardBreak``), consecutive ``"- "``-prefixed
    lines become a ``bulletList``, interleaved in whatever order they occur.
    A single plain line/paragraph (this module's original use -- escalation
    ticket descriptions, plain alert comments) degrades to exactly the old
    single-paragraph shape. Inverse of ``_adf_to_text``.
    """
    lines = text.split("\n")
    blocks: List[Dict[str, Any]] = []
    para_lines: List[str] = []
    bullet_lines: List[str] = []

    def flush_paragraph() -> None:
        if not para_lines:
            return
        content: List[Dict[str, Any]] = []
        for i, line in enumerate(para_lines):
            if i > 0:
                content.append({"type": "hardBreak"})
            if line:
                content.append({"type": "text", "text": line})
        blocks.append({"type": "paragraph", "content": content})
        para_lines.clear()

    def flush_bullets() -> None:
        if not bullet_lines:
            return
        blocks.append(
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": item}]}
                        ],
                    }
                    for item in bullet_lines
                ],
            }
        )
        bullet_lines.clear()

    for line in lines:
        if line.startswith("- "):
            flush_paragraph()
            bullet_lines.append(line[2:])
        else:
            flush_bullets()
            para_lines.append(line)
    flush_paragraph()
    flush_bullets()

    if not blocks:
        blocks = [{"type": "paragraph", "content": []}]

    return {"type": "doc", "version": 1, "content": blocks}


def _slugify_grid(grid_name: str) -> str:
    """Lowercase, alnum-and-hyphen slug used for the backend-independent
    ``grid-<slug>`` label -- lets ``find_open_by_grid`` locate a grid's open
    tickets by label alone, without depending on a project-specific custom
    grid field."""
    slug = re.sub(r"[^a-z0-9]+", "-", grid_name.strip().lower()).strip("-")
    return slug or "unknown"


class JiraTicketBackend:
    """Ticket backend backed by Jira (REST API v3 + Jira Service Management)."""

    name = "jira"

    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        project_key: Optional[str] = None,
        issue_type: Optional[str] = None,
        get_storage_client: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        # Same env var names/defaults as EscalationService.__init__ -- this is now
        # a standalone class, not relying on EscalationService's state.
        self._jira_base_url = base_url if base_url is not None else os.getenv("JIRA_BASE_URL", "")
        self._jira_email = email if email is not None else os.getenv("JIRA_USERNAME", "")
        self._jira_api_token = (
            api_token if api_token is not None else os.getenv("JIRA_API_TOKEN", "")
        )
        self._jira_project_key = (
            project_key if project_key is not None else os.getenv("JIRA_PROJECT_KEY", "OPS")
        )
        self._jira_issue_type = (
            issue_type if issue_type is not None else os.getenv("JIRA_ISSUE_TYPE", "Task")
        )
        self._get_storage_client = get_storage_client

        # Cached (TTL) health probe state for is_available().
        self._probe_cache_ok: bool = False
        self._probe_cache_time: float = 0.0
        self._issue_type_selector: Optional[JiraIssueTypeSelector] = None

    # ------------------------------------------------------------------
    # TicketBackend Protocol
    # ------------------------------------------------------------------

    def has_credentials(self) -> bool:
        """True when Jira base URL + auth are configured (no network call)."""
        return bool(self._jira_base_url and self._jira_email and self._jira_api_token)

    async def is_available(self) -> bool:
        """Credentials configured AND a cached (TTL) cheap probe succeeded."""
        if not self.has_credentials():
            return False

        ttl = float(fr.get("JIRA_HEALTHCHECK_TTL_SECONDS"))
        now = time.monotonic()
        if now - self._probe_cache_time < ttl:
            return self._probe_cache_ok

        ok = await self._probe_myself()
        self._probe_cache_ok = ok
        self._probe_cache_time = now
        return ok

    async def _probe_myself(self) -> bool:
        """Cheap GET /rest/api/3/myself probe used by is_available().

        Both failure paths are logged at WARNING (not DEBUG): this probe's
        cached result silently steers ``auto`` mode away from Jira with no
        other signal, so a non-200 (e.g. an expired API token) or a transport
        failure must be visible at the deployment's normal log level, not
        require enabling DEBUG after the fact.
        """
        try:
            session = _get_jira_session()
            async with session.get(
                f"{self._jira_base_url}/rest/api/3/myself",
                headers=self._jira_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    LOGGER.warning("Jira health probe returned HTTP {}", resp.status)
                return resp.status == 200
        except Exception as e:
            LOGGER.warning("Jira health probe failed: {}", e)
            return False

    async def create_ticket(self, req: TicketCreateRequest) -> TicketResult:
        assignee_account_id = None
        if req.assignee_email:
            assignee_account_id = await self._resolve_jira_account_id(
                req.assignee_email, self._jira_auth_headers()
            )
        organization_id = None
        if req.organization_short_name:
            organization_id = await self._resolve_jira_org_id(req.organization_short_name)

        labels = list(req.labels or [])
        if req.grid_name:
            grid_label = f"grid-{_slugify_grid(req.grid_name)}"
            if grid_label not in labels:
                labels.append(grid_label)
        is_urgent_alert = (
            req.source == "notify"
            and (
                req.severity.strip().casefold() == "urgent"
                or bool(_URGENT_ALERT_SUMMARY.match(req.summary))
            )
        )
        available_types = await self._type_selector().available_types()
        highest_priority_id = None
        if is_urgent_alert and any(
            issue_type.field("priority") is not None for issue_type in available_types
        ):
            highest_priority_id = await self.resolve_priority_id("highest")
        reporter_account_id = None
        if any(
            (field := issue_type.field("reporter")) is not None and field.required
            for issue_type in available_types
        ):
            # Jira Service Management projects commonly require a reporter on
            # every issue type. Customers aren't Jira users, so the bot's own
            # account is the only reporter identity this integration can
            # always supply -- without it, no issue type is ever compatible.
            reporter_account_id = await self._resolve_own_account_id()
        context = JiraCreateContext(
            project_key=self._jira_project_key,
            summary=req.summary,
            description=req.description,
            labels=labels,
            grid_name=req.grid_name,
            assignee_account_id=assignee_account_id,
            organization_id=organization_id,
            priority_id=highest_priority_id,
            reporter_account_id=reporter_account_id,
        )
        compatible = compatible_issue_types(context, available_types)
        selection = await self._choose_issue_type(req, compatible)
        if selection is None:
            raise TicketBackendError(
                self._describe_no_compatible_type(context, available_types)
            )
        payload = build_issue_payload(context, selection.issue_type)
        if payload is None:
            reason = incompatible_issue_type_reason(context, selection.issue_type)
            detail = f": missing required field {reason!r}" if reason else ""
            raise TicketBackendError(
                f"Jira selected an incompatible issue type {selection.issue_type.name!r}{detail}"
            )
        return await self._post_issue_payload(payload, selection)

    def _describe_no_compatible_type(
        self, context: JiraCreateContext, available_types: Sequence[JiraIssueType]
    ) -> str:
        """Diagnostic detail for "Jira cannot supply a compatible issue type".

        Distinguishes "Jira returned no issue types for this project" (a
        metadata-fetch problem, already warned about separately) from "every
        returned type is missing a required field this integration can't
        populate" -- and for the latter, names the blocking field per type so
        the fix is a Jira project-config change, not a log-archaeology
        session.
        """
        if not available_types:
            return (
                "Jira cannot supply a compatible issue type: no issue types available "
                f"from Jira (project {self._jira_project_key!r})"
            )
        reasons = [
            f"{issue_type.name!r} missing required field {reason!r}"
            for issue_type in available_types
            if (reason := incompatible_issue_type_reason(context, issue_type)) is not None
        ]
        detail = "; ".join(reasons) if reasons else "no issue types returned usable field metadata"
        return f"Jira cannot supply a compatible issue type ({detail})"

    def _type_selector(self) -> JiraIssueTypeSelector:
        if self._issue_type_selector is None:
            self._issue_type_selector = JiraIssueTypeSelector(
                base_url=self._jira_base_url,
                headers=self._jira_auth_headers(),
                project_key=self._jira_project_key,
                model=get_settings().gemini.model,
                get_session=_get_jira_session,
            )
        return self._issue_type_selector

    async def _choose_issue_type(
        self, req: TicketCreateRequest, compatible: Sequence[JiraIssueType]
    ) -> IssueTypeSelection | None:
        if req.source == "notify":
            return await self._type_selector().select(
                summary=req.summary,
                description=req.description,
                requested_type=req.ticket_type,
                operational_context=req.llm_context,
                candidate_types=compatible,
            ) or self._fallback_compatible_type(compatible)
        return self._fallback_compatible_type(compatible)

    def _fallback_compatible_type(
        self, compatible: Sequence[JiraIssueType]
    ) -> IssueTypeSelection | None:
        configured_name = self._jira_issue_type.strip().casefold()
        for issue_type in compatible:
            if issue_type.name.strip().casefold() == configured_name:
                return IssueTypeSelection(
                    issue_type, "fallback", f"configured fallback {issue_type.name}"
                )
        if compatible:
            return IssueTypeSelection(compatible[0], "fallback", "first compatible type")
        return None

    async def _post_issue_payload(
        self, payload: Dict[str, Any], selection: IssueTypeSelection
    ) -> TicketResult:
        url = f"{self._jira_base_url}/rest/api/3/issue"
        try:
            session = _get_jira_session()
            async with session.post(
                url,
                json=payload,
                headers=self._jira_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status not in (200, 201):
                    body = await response.text()
                    raise TicketBackendError(
                        f"Jira ticket creation failed: HTTP {response.status}: {body}"
                    )
                result = await response.json()
        except TicketBackendError:
            raise
        except Exception as exc:
            raise TicketBackendError(f"Jira ticket creation failed: {exc}") from exc

        key = result.get("key")
        if not key:
            raise TicketBackendError("Jira ticket creation failed: response has no key")
        LOGGER.info("Created Jira ticket: {}", key)
        return TicketResult(
            ref=key,
            backend="jira",
            url=f"{self._jira_base_url}/browse/{key}" if self._jira_base_url else None,
            ticket_type=selection.issue_type.name,
        )

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        # `public` is accepted for Protocol parity with the internal backend
        # (which distinguishes public/internal comments); Jira comments here
        # are always visible in Jira the same way _add_jira_comment always
        # posted them -- unchanged from the original behavior.
        return await self._add_jira_comment(ref, body)

    async def get_status(self, ref: str) -> Optional[TicketStatus]:
        fields = await self._fetch_jira_issue_fields(ref)
        if fields is None:
            return None
        return TicketStatus(
            summary=fields.get("summary", ""),
            is_done=bool(fields.get("is_done", False)),
            raw_status=fields.get("raw_status", ""),
        )

    async def transition_to_done(self, ref: str) -> None:
        await self._transition_jira_to_done(ref)

    async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
        return await self._search_jira_for_escalation(mapping_id)

    async def update_ticket(
        self,
        ref: str,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        priority_id: Optional[str] = None,
    ) -> bool:
        """PUT summary/description/priority onto an existing issue.

        Never raises -- returns False on any HTTP error or transport failure,
        same fail-quiet contract as ``add_comment``/``transition_to_done``.
        """
        if not self._jira_base_url:
            return False
        fields: Dict[str, Any] = {}
        if summary is not None:
            fields["summary"] = summary
        if description is not None:
            fields["description"] = _text_to_adf(description)
        if priority_id is not None:
            resolved_priority_id = await self.resolve_priority_id(priority_id)
            if resolved_priority_id is not None:
                fields["priority"] = {"id": resolved_priority_id}
        if not fields:
            return True

        url = f"{self._jira_base_url}/rest/api/3/issue/{ref}"
        try:
            session = _get_jira_session()
            async with session.put(
                url,
                json={"fields": fields},
                headers=self._jira_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status in (200, 204):
                    return True
                body = await resp.text()
                LOGGER.warning("Failed to update Jira issue {}: HTTP {} -- {}", ref, resp.status, body)
                return False
        except Exception:
            LOGGER.warning("Error updating Jira issue {}", ref, exc_info=True)
            return False

    async def resolve_priority_id(self, priority_id: str) -> Optional[str]:
        """Resolve the ``highest`` sentinel from Jira's live priority catalogue.

        Explicit Jira ids pass through unchanged. Catalogue failures and a
        missing standard ``Highest`` entry return ``None`` so priority remains
        an optional field and can never block filing or updating a ticket.
        """
        if priority_id.strip().casefold() != "highest":
            return priority_id
        if not self._jira_base_url:
            return None

        url = f"{self._jira_base_url}/rest/api/3/priority"
        try:
            session = _get_jira_session()
            async with session.get(
                url,
                headers=self._jira_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    LOGGER.warning(
                        "Failed to discover Jira Highest priority: HTTP {}",
                        response.status,
                    )
                    return None
                priorities = await response.json()
        except Exception:
            LOGGER.warning("Failed to discover Jira Highest priority", exc_info=True)
            return None

        if not isinstance(priorities, list):
            return None
        for priority in priorities:
            if not isinstance(priority, dict):
                continue
            if str(priority.get("name") or "").strip().casefold() != "highest":
                continue
            resolved_id = priority.get("id")
            return str(resolved_id) if resolved_id is not None else None
        return None

    async def find_open_by_grid(self, grid_name: str, limit: int = 20) -> List[TicketSummary]:
        """Search for open issues carrying this grid's ``grid-<slug>`` label.

        Label-based rather than a project-specific custom grid field, so
        every ticket created through the metadata-aware path is searchable
        without relying on a static field configuration.
        """
        if not self._jira_base_url or not self._jira_project_key:
            return []
        slug = _slugify_grid(grid_name)
        jql = (
            f'project = "{self._jira_project_key}" AND statusCategory != Done '
            f'AND labels = "grid-{slug}" ORDER BY created DESC'
        )
        url = f"{self._jira_base_url}/rest/api/3/search/jql"
        try:
            session = _get_jira_session()
            async with session.get(
                url,
                params={
                    "jql": jql,
                    "fields": "summary,description,status,created,labels",
                    "maxResults": str(limit),
                },
                headers=self._jira_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:1000]
                    LOGGER.warning(
                        "Jira search failed for grid {!r}: HTTP {} -- {}", grid_name, resp.status, body
                    )
                    return []
                data = await resp.json()
        except Exception:
            LOGGER.warning("Error searching Jira for grid {!r}", grid_name, exc_info=True)
            return []

        results: List[TicketSummary] = []
        for issue in data.get("issues", []):
            fields = issue.get("fields", {}) or {}
            status_field = fields.get("status", {}) or {}
            status_category = status_field.get("statusCategory", {}).get("key", "")
            results.append(
                TicketSummary(
                    ref=issue.get("key", ""),
                    backend="jira",
                    summary=fields.get("summary") or "",
                    description=_adf_to_text(fields.get("description")),
                    status=status_field.get("name", ""),
                    is_done=(status_category == "done"),
                    created_at=fields.get("created"),
                    labels=fields.get("labels") or [],
                )
            )
        return results

    # ------------------------------------------------------------------
    # Jira REST helpers (moved from EscalationService unchanged)
    # ------------------------------------------------------------------

    def _jira_auth_headers(self) -> Dict[str, str]:
        """Return Basic-auth + JSON headers for Jira API calls (cached per instance)."""
        if not hasattr(self, "_cached_jira_auth_header"):
            auth_b64 = base64.b64encode(
                f"{self._jira_email}:{self._jira_api_token}".encode("ascii")
            ).decode("ascii")
            self._cached_jira_auth_header = f"Basic {auth_b64}"
        return {
            "Authorization": self._cached_jira_auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _download_attachment_bytes(self, storage_path: str) -> Optional[bytes]:
        if self._get_storage_client is None:
            return None
        try:
            client = self._get_storage_client()
        except Exception:
            LOGGER.warning(
                "get_storage_client() raised -- skipping download for attachment {}",
                storage_path,
                exc_info=True,
            )
            return None
        if client is None:
            return None
        try:
            return client.storage.from_(BUCKET_NAME).download(storage_path)
        except Exception:
            LOGGER.warning(
                "Failed to download attachment {} from storage", storage_path, exc_info=True
            )
            return None

    async def _upload_jira_attachment(
        self, issue_key: str, filename: str, content: bytes, mime_type: str
    ) -> Optional[str]:
        """POST one file to Jira's attachment endpoint. Returns the Jira
        attachment id on success, None on any failure (never raises).

        Jira's attachment endpoint requires multipart/form-data and the
        X-Atlassian-Token header -- unlike every other Jira call in this
        class, it must NOT send Content-Type: application/json, so this
        builds its own headers rather than reusing _jira_auth_headers().
        """
        url = f"{self._jira_base_url}/rest/api/3/issue/{issue_key}/attachments"
        headers = {
            "Authorization": self._jira_auth_headers()["Authorization"],
            "X-Atlassian-Token": "no-check",
        }
        form = aiohttp.FormData()
        form.add_field("file", content, filename=filename, content_type=mime_type)
        try:
            session = _get_jira_session()
            async with session.post(
                url, data=form, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status not in (200, 201):
                    body = await response.text()
                    LOGGER.warning(
                        "Jira attachment upload failed for {}: HTTP {}: {}",
                        issue_key,
                        response.status,
                        body,
                    )
                    return None
                result = await response.json()
                entries = result if isinstance(result, list) else []
                if not entries or not entries[0].get("id"):
                    LOGGER.warning(
                        "Jira attachment upload for {} returned no attachment id", issue_key
                    )
                    return None
                return str(entries[0]["id"])
        except Exception:
            LOGGER.warning("Jira attachment upload failed for {}", issue_key, exc_info=True)
            return None

    async def add_attachments(
        self, ticket_ref: str, attachments: List[EscalationAttachment]
    ) -> List[AttachmentSyncResult]:
        results: List[AttachmentSyncResult] = []
        for attachment in attachments:
            if attachment.jira_attachment_id:
                continue
            content = self._download_attachment_bytes(attachment.storage_path)
            if content is None:
                continue
            filename = attachment.storage_path.rsplit("/", 1)[-1]
            jira_attachment_id = await self._upload_jira_attachment(
                ticket_ref, filename, content, attachment.mime_type
            )
            if jira_attachment_id:
                results.append(
                    AttachmentSyncResult(
                        attachment_id=attachment.id, external_id=jira_attachment_id
                    )
                )
        return results

    async def _resolve_own_account_id(self) -> Optional[str]:
        """Resolve (and cache) this integration's own Jira account id.

        Used as ``reporter`` on issue types that require it -- unlike
        ``assignee_account_id`` (caller-supplied), this is always resolvable
        from ``self._jira_email``, so it's cached for the backend's lifetime
        rather than re-fetched on every ticket.
        """
        if not hasattr(self, "_cached_own_account_id"):
            self._cached_own_account_id = await self._resolve_jira_account_id(
                self._jira_email, self._jira_auth_headers()
            )
        return self._cached_own_account_id

    async def _resolve_jira_account_id(
        self,
        email: str,
        headers: Dict[str, str],
    ) -> Optional[str]:
        """Resolve a JIRA account ID from an email address."""
        try:
            url = f"{self._jira_base_url}/rest/api/3/user/search"
            async with _get_jira_session().get(
                url,
                params={"query": email},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                users = await resp.json()
                for user in users:
                    if user.get("emailAddress", "").lower() == email.lower():
                        return str(user.get("accountId"))
        except Exception as e:
            LOGGER.debug(f"Could not resolve JIRA account for {email}: {e}")
        return None

    async def _fetch_jira_organizations(self) -> List[Dict[str, Any]]:
        """GET all JSM organizations, handling pagination (max 50 per page, 20 pages max).

        Results are cached for 30 minutes to avoid hammering the Jira API on every escalation.
        """
        global _jira_orgs_cache, _jira_orgs_cache_time
        if (
            _jira_orgs_cache_time is not None
            and time.monotonic() - _jira_orgs_cache_time < _JIRA_ORGS_TTL
        ):
            return _jira_orgs_cache

        orgs: List[Dict[str, Any]] = []
        url: Optional[str] = f"{self._jira_base_url}/rest/servicedeskapi/organization"
        headers = self._jira_auth_headers()
        session = _get_jira_session()
        page = 0
        max_pages = 20
        while url and page < max_pages:
            try:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        LOGGER.warning(
                            "Jira org fetch returned HTTP {} -- stopping pagination", resp.status
                        )
                        break
                    data = await resp.json()
            except Exception as e:
                LOGGER.warning("Error fetching Jira organizations (page {}): {}", page, e)
                break
            orgs.extend(data.get("values", []))
            next_url = (
                data.get("_links", {}).get("next") if not data.get("isLastPage", True) else None
            )
            # Validate next URL is on the same Jira host to prevent SSRF
            if next_url and self._jira_base_url and next_url.startswith(self._jira_base_url):
                url = next_url
            else:
                url = None
            page += 1

        _jira_orgs_cache = orgs
        _jira_orgs_cache_time = time.monotonic()
        return orgs

    async def _resolve_jira_org_id(self, org_name: str) -> Optional[str]:
        """Fuzzy-match org_name against Jira's organisation list.

        Returns the Jira org ID as a string, or None if no match.
        """
        from shared.utils.grid_matcher import find_best_grid_match

        try:
            orgs = await self._fetch_jira_organizations()
            name_to_id = {o["name"]: str(o["id"]) for o in orgs}
            matched_name, _, _score = find_best_grid_match(org_name, list(name_to_id.keys()))
            return name_to_id[matched_name] if matched_name else None
        except Exception as e:
            LOGGER.warning("Could not resolve Jira org for '{}': {}", org_name, e)
            return None

    async def _add_jira_comment(self, issue_key: str, body: str) -> bool:
        """Post a plain-text comment to an existing Jira issue. Returns True on success."""
        if not self._jira_base_url:
            return False
        try:
            headers = self._jira_auth_headers()
            url = f"{self._jira_base_url}/rest/api/3/issue/{issue_key}/comment"
            payload = {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}],
                }
            }
            jira_sess = _get_jira_session()
            async with jira_sess.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status in (200, 201):
                    return True
                LOGGER.warning(
                    "Failed to add Jira comment to {}: status={}", issue_key, resp.status
                )
                return False
        except Exception as e:
            LOGGER.warning("Error adding Jira comment to {}: {}", issue_key, e)
            return False

    async def _transition_jira_to_done(self, issue_key: str) -> None:
        """Transition a Jira issue to Done from whatever status it currently has.

        Fetches available transitions for the issue and picks the first one
        whose target status is "Done" (statusCategory key "done"). This mirrors
        how handle_jira_issue_updated detects closures from Jira -- no hardcoded
        transition IDs required, works regardless of current workflow state.

        Non-blocking -- failures are logged but never raised.
        """
        transitions_url = f"{self._jira_base_url}/rest/api/3/issue/{issue_key}/transitions"
        try:
            session = _get_jira_session()
            headers = self._jira_auth_headers()

            # 1. Fetch available transitions for this issue's current state
            async with session.get(
                transitions_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    LOGGER.warning(
                        "Could not fetch transitions for {}: HTTP {} -- {}",
                        issue_key,
                        resp.status,
                        body,
                    )
                    return
                data = await resp.json()

            # 2. Find the transition that leads to "Done" status category
            transition_id = None
            for t in data.get("transitions", []):
                to_status = t.get("to", {})
                category_key = to_status.get("statusCategory", {}).get("key", "")
                status_name = to_status.get("name", "")
                if category_key == "done" or status_name in ("Done", "Closed"):
                    transition_id = t["id"]
                    break

            if not transition_id:
                LOGGER.warning(
                    "No 'Done' transition available for {} -- already closed or workflow mismatch",
                    issue_key,
                )
                return

            # 3. Execute the transition
            async with session.post(
                transitions_url,
                json={"transition": {"id": transition_id}},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    LOGGER.warning(
                        "Jira transition failed for {}: HTTP {} -- {}",
                        issue_key,
                        resp.status,
                        body,
                    )
                else:
                    LOGGER.info(
                        "Transitioned Jira {} to Done (transition {})", issue_key, transition_id
                    )

        except Exception:
            LOGGER.warning("Error transitioning Jira {} to Done", issue_key, exc_info=True)

    async def _fetch_jira_issue_fields(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Fetch summary and status category for a Jira issue.

        Returns {"summary": str, "is_done": bool, "raw_status": str} or None on
        error/not-found. ``raw_status`` is additive (the Jira status name, e.g.
        "In Progress") for TicketStatus -- the original inline version of this
        helper only returned summary/is_done since that's all its one caller
        (EscalationService) needed.
        """
        if not self._jira_base_url:
            return None
        url = f"{self._jira_base_url}/rest/api/3/issue/{issue_key}?fields=summary,status"
        try:
            session = _get_jira_session()
            async with session.get(
                url,
                headers=self._jira_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    LOGGER.debug("Jira fetch {} returned HTTP {}", issue_key, resp.status)
                    return None
                data = await resp.json()
            fields = data.get("fields", {})
            status_field = fields.get("status", {})
            status_category = status_field.get("statusCategory", {}).get("key", "")
            return {
                "summary": fields.get("summary", ""),
                "is_done": status_category == "done",
                "raw_status": status_field.get("name", ""),
            }
        except Exception:
            LOGGER.debug("Error fetching Jira issue fields for {}", issue_key, exc_info=True)
            return None

    async def _search_jira_for_escalation(self, mapping_id: str) -> Optional[str]:
        """Search Jira for an existing ticket filed for this escalation mapping.

        Tickets are tagged with label "escalation-{mapping_id[:8]}" at creation.
        Returns the issue key if found, None otherwise.
        """
        if not self._jira_base_url or not self._jira_project_key:
            return None
        label = f"escalation-{mapping_id[:8]}"
        jql = f'project = "{self._jira_project_key}" AND labels = "{label}" ORDER BY created DESC'
        url = f"{self._jira_base_url}/rest/api/3/search/jql"
        try:
            session = _get_jira_session()
            async with session.get(
                url,
                params={"jql": jql, "fields": "summary,status", "maxResults": "1"},
                headers=self._jira_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            issues = data.get("issues", [])
            return str(issues[0]["key"]) if issues else None
        except Exception:
            LOGGER.debug("Error searching Jira for escalation {}", mapping_id, exc_info=True)
            return None
