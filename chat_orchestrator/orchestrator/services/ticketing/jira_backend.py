"""Jira ticket backend.

The Jira REST helpers here are moved out of ``EscalationService`` verbatim
(same HTTP calls, same payload shapes, same fallback/retry behavior) so they
can be used through the ``TicketBackend`` Protocol instead of being inline
methods on the escalation service. ``escalation_service.py`` itself is left
untouched for now -- a later task rewires it to call through
``TicketService`` instead of these methods directly.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp

from shared.config import flag_registry as fr
from shared.utils.logging import get_logger

from .backend import TicketBackendError, TicketCreateRequest, TicketResult, TicketStatus

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
_jira_orgs_cache_time: float = 0.0
_JIRA_ORGS_TTL: float = 1800.0  # 30 minutes


def _adf_to_text(adf: Any, _depth: int = 0, _max_depth: int = 50) -> str:
    """Extract plain text from an Atlassian Document Format node (recursive, depth-limited)."""
    if _depth > _max_depth or not adf or not isinstance(adf, dict):
        return ""
    if adf.get("type") == "text":
        return str(adf.get("text", ""))
    return "".join(_adf_to_text(child, _depth + 1, _max_depth) for child in adf.get("content", []))


class JiraTicketBackend:
    """Ticket backend backed by Jira (REST API v3 + Jira Service Management)."""

    name = "jira"

    # JIRA Grid field (customfield_10057) option IDs -- required select field.
    # Fallback used when grid cannot be resolved from escalation context.
    JIRA_GRID_FALLBACK_OPTION_ID = "10315"  # "Software"

    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        project_key: Optional[str] = None,
        issue_type: Optional[str] = None,
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

        # Cached (TTL) health probe state for is_available().
        self._probe_cache_ok: bool = False
        self._probe_cache_time: float = 0.0

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
        """Cheap GET /rest/api/3/myself probe used by is_available()."""
        try:
            session = _get_jira_session()
            async with session.get(
                f"{self._jira_base_url}/rest/api/3/myself",
                headers=self._jira_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status == 200
        except Exception as e:
            LOGGER.debug("Jira health probe failed: %s", e)
            return False

    async def create_ticket(self, req: TicketCreateRequest) -> TicketResult:
        result = await self._create_jira_ticket(
            summary=req.summary,
            description=req.description,
            grid_name=req.grid_name,
            assignee_email=req.assignee_email,
            organization_short_name=req.organization_short_name,
            labels=req.labels or None,
        )
        if not result.get("success"):
            raise TicketBackendError(f"Jira ticket creation failed: {result.get('error')}")
        key = result["key"]
        url = f"{self._jira_base_url}/browse/{key}" if self._jira_base_url else None
        return TicketResult(ref=key, backend="jira", url=url)

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

    async def _resolve_jira_grid_option(
        self,
        grid_name: Optional[str],
        headers: Dict[str, str],
    ) -> Dict[str, str]:
        """Resolve a grid name to a JIRA customfield_10057 option.

        Fetches allowed values from JIRA create metadata, fuzzy-matches the
        grid name, and returns ``{"id": "<option_id>"}``.  Falls back to the
        ``Software`` option when no match is found or when *grid_name* is None.
        """
        fallback = {"id": self.JIRA_GRID_FALLBACK_OPTION_ID}
        if not grid_name:
            return fallback

        try:
            meta_url = (
                f"{self._jira_base_url}/rest/api/3/issue/createmeta"
                f"/{self._jira_project_key}/issuetypes"
            )
            session = _get_jira_session()
            # Find Task issue type ID
            async with session.get(
                meta_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    LOGGER.warning(f"Could not fetch issue types: {resp.status}")
                    return fallback
                type_data = await resp.json()

            task_type_id = None
            for it in type_data.get("issueTypes", type_data.get("values", [])):
                if it.get("name") == "Task":
                    task_type_id = it.get("id")
                    break
            if not task_type_id:
                return fallback

            # Fetch field metadata for Task type
            fields_url = (
                f"{self._jira_base_url}/rest/api/3/issue/createmeta"
                f"/{self._jira_project_key}/issuetypes/{task_type_id}"
            )
            async with session.get(
                fields_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp2:
                if resp2.status != 200:
                    return fallback
                fields_data = await resp2.json()

            # Find customfield_10057 and match
            for field in fields_data.get("fields", fields_data.get("values", [])):
                fid = field.get("fieldId", field.get("key", ""))
                if fid == "customfield_10057":
                    allowed = field.get("allowedValues", [])
                    # Exact match first (case-insensitive)
                    for opt in allowed:
                        if opt["value"].lower() == grid_name.lower():
                            LOGGER.info(f"Grid '{grid_name}' matched JIRA option id={opt['id']}")
                            return {"id": opt["id"]}
                    # Fuzzy match
                    try:
                        from shared.utils.grid_matcher import find_best_grid_match

                        option_names = [o["value"] for o in allowed]
                        matched, was_fuzzy, score = find_best_grid_match(
                            grid_name, option_names, threshold=80
                        )
                        if matched:
                            for opt in allowed:
                                if opt["value"] == matched:
                                    LOGGER.info(
                                        f"Grid '{grid_name}' fuzzy matched to "
                                        f"'{matched}' (score={score}%) -> id={opt['id']}"
                                    )
                                    return {"id": opt["id"]}
                    except ImportError:
                        pass
                    LOGGER.warning(f"No JIRA grid option matched for '{grid_name}', using fallback")
                    return fallback
        except Exception as e:
            LOGGER.warning(f"Error resolving JIRA grid option: {e}")
        return fallback

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
        if time.monotonic() - _jira_orgs_cache_time < _JIRA_ORGS_TTL:
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
                            "Jira org fetch returned HTTP %d -- stopping pagination", resp.status
                        )
                        break
                    data = await resp.json()
            except Exception as e:
                LOGGER.warning("Error fetching Jira organizations (page %d): %s", page, e)
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
            LOGGER.warning("Could not resolve Jira org for '%s': %s", org_name, e)
            return None

    async def _create_jira_ticket(
        self,
        summary: str,
        description: str,
        grid_name: Optional[str] = None,
        assignee_email: Optional[str] = None,
        organization_short_name: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Create a Jira ticket.

        Args:
            summary: Ticket summary/title
            description: Ticket description
            grid_name: Grid name to match against JIRA options (optional)
            assignee_email: Email to auto-assign the ticket to (optional)
            organization_short_name: Org name for JSM Organizations field (optional)

        Returns:
            Dict with success status and ticket key
        """
        try:
            headers = self._jira_auth_headers()
            url = f"{self._jira_base_url}/rest/api/3/issue"

            # Resolve grid option (required field in OPS project)
            grid_option = await self._resolve_jira_grid_option(grid_name, headers)

            # Resolve assignee account ID
            assignee_account_id = None
            if assignee_email:
                assignee_account_id = await self._resolve_jira_account_id(assignee_email, headers)
                if assignee_account_id:
                    LOGGER.info(f"Will assign ticket to {assignee_email}")
                else:
                    LOGGER.debug(f"Could not resolve JIRA account for {assignee_email}")

            # Build ticket payload
            payload: Dict[str, Any] = {
                "fields": {
                    "project": {"key": self._jira_project_key},
                    "summary": summary,
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": description}],
                            }
                        ],
                    },
                    "issuetype": {"name": self._jira_issue_type},
                    "customfield_10057": grid_option,
                }
            }

            if assignee_account_id:
                payload["fields"]["assignee"] = {"accountId": assignee_account_id}

            if labels:
                payload["fields"]["labels"] = labels

            # Tag the JSM Organizations field (fuzzy-match our org to Jira's org list)
            org_field_id = os.getenv("JIRA_ORGANIZATION_FIELD_ID")
            if organization_short_name and org_field_id:
                jira_org_id = await self._resolve_jira_org_id(organization_short_name)
                if jira_org_id:
                    payload["fields"][org_field_id] = [int(jira_org_id)]
                    LOGGER.info(
                        "Tagged Jira org field %s=%s for org '%s'",
                        org_field_id,
                        jira_org_id,
                        organization_short_name,
                    )

            LOGGER.info(f"JIRA ticket grid option: {grid_option}")

            jira_sess = _get_jira_session()
            async with jira_sess.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response_text = await response.text()

                if response.status in (200, 201):
                    result: Dict[str, Any] = await response.json()
                    jira_key = result.get("key")
                    LOGGER.info(f"Successfully created Jira ticket: {jira_key}")
                    return {
                        "success": True,
                        "key": jira_key,
                        "id": result.get("id"),
                    }

                # Fallback: if issue type is invalid, retry with "Task"
                if (
                    response.status == 400
                    and "issuetype" in response_text
                    and payload["fields"]["issuetype"]["name"] != "Task"
                ):
                    LOGGER.warning(
                        f"Issue type '{payload['fields']['issuetype']['name']}' "
                        f"rejected, retrying with 'Task'"
                    )
                    payload["fields"]["issuetype"]["name"] = "Task"
                    async with jira_sess.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as retry_resp:
                        retry_text = await retry_resp.text()
                        if retry_resp.status in (200, 201):
                            retry_result: Dict[str, Any] = await retry_resp.json()
                            jira_key = retry_result.get("key")
                            LOGGER.info(f"Created Jira ticket with fallback type: {jira_key}")
                            return {
                                "success": True,
                                "key": jira_key,
                                "id": retry_result.get("id"),
                            }
                        LOGGER.error(
                            f"Fallback also failed: status={retry_resp.status}, "
                            f"response={retry_text}"
                        )

                LOGGER.error(
                    f"Failed to create Jira ticket: status={response.status}, "
                    f"response={response_text}"
                )
                return {
                    "success": False,
                    "error": f"Jira API returned {response.status}: {response_text}",
                }

        except Exception as e:
            LOGGER.exception(f"Error creating Jira ticket: {e}")
            return {
                "success": False,
                "error": str(e),
            }

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
                    "Failed to add Jira comment to %s: status=%s", issue_key, resp.status
                )
                return False
        except Exception as e:
            LOGGER.warning("Error adding Jira comment to %s: %s", issue_key, e)
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
                        "Could not fetch transitions for %s: HTTP %s -- %s",
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
                    "No 'Done' transition available for %s -- already closed or workflow mismatch",
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
                        "Jira transition failed for %s: HTTP %s -- %s",
                        issue_key,
                        resp.status,
                        body,
                    )
                else:
                    LOGGER.info(
                        "Transitioned Jira %s to Done (transition %s)", issue_key, transition_id
                    )

        except Exception:
            LOGGER.warning("Error transitioning Jira %s to Done", issue_key, exc_info=True)

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
                    LOGGER.debug("Jira fetch %s returned HTTP %s", issue_key, resp.status)
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
            LOGGER.debug("Error fetching Jira issue fields for %s", issue_key, exc_info=True)
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
        url = f"{self._jira_base_url}/rest/api/3/issue/search"
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
            LOGGER.debug("Error searching Jira for escalation %s", mapping_id, exc_info=True)
            return None
