"""Shared form schemas and constants for Telegram Mini App integration.

Decoupled from router.py so workflow_executor can import without
depending on the API layer.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Dict

from pydantic import BaseModel

# Sentinel value used as user_input when resuming a workflow after
# a Mini App form submission.  The actual values are stored in
# pending_param_overrides — this sentinel simply unblocks the executor.
FORM_SUBMITTED_SENTINEL = "__form_submitted__"

# Form definitions: map form_type → list of field descriptors.
# Each field has: key, label, type, and optional step/min/max/suffix.
FORM_SCHEMAS: Dict[str, list] = {
    "design_params": [
        {
            "key": "editable_total_kwp",
            "label": "Total kWp",
            "type": "number",
            "step": 0.1,
            "min": 0,
            "suffix": "kWp",
        },
        {
            "key": "editable_total_kwh",
            "label": "Total kWh",
            "type": "number",
            "step": 0.1,
            "min": 0,
            "suffix": "kWh",
        },
        {
            "key": "editable_total_buildings",
            "label": "Total Buildings",
            "type": "number",
            "step": 1,
            "min": 1,
        },
        {
            "key": "editable_served_building_count",
            "label": "Served Buildings",
            "type": "number",
            "step": 1,
            "min": 1,
        },
    ],
}


def validate_form_values(form_type: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Validate submitted form values against the schema.

    Args:
        form_type: Key into FORM_SCHEMAS
        values: User-submitted key→value dict

    Returns:
        Validated (and coerced) values dict

    Raises:
        ValueError: If form_type unknown, unknown keys, or invalid values
    """
    schema = FORM_SCHEMAS.get(form_type)
    if not schema:
        raise ValueError(f"Unknown form type: {form_type}")

    allowed_keys = {f["key"] for f in schema}
    unknown = set(values.keys()) - allowed_keys
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}")

    validated: Dict[str, Any] = {}
    for field in schema:
        key = field["key"]
        if key not in values:
            continue

        raw = values[key]
        field_type = field.get("type", "text")

        if field_type == "number":
            try:
                val = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{field['label']}: expected a number")
            if "min" in field and val < field["min"]:
                raise ValueError(f"{field['label']}: must be >= {field['min']}")
            if "max" in field and val > field["max"]:
                raise ValueError(f"{field['label']}: must be <= {field['max']}")
            # Preserve int when step is 1
            if field.get("step") == 1:
                val = int(val)
            validated[key] = val
        else:
            validated[key] = str(raw)

    return validated


def _ensure_https(url: str) -> str:
    """Force HTTPS scheme. Telegram Mini Apps require HTTPS and macOS ATS blocks HTTP."""
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url


def build_mini_app_url(packet_id: str, form_type: str) -> str | None:
    """Build the full Mini App URL for a given packet and form type.

    Returns None if MINI_APP_BASE_URL is not configured.
    """
    base_url = os.getenv("MINI_APP_BASE_URL", "").rstrip("/")
    if not base_url:
        return None
    base_url = _ensure_https(base_url)
    # Trailing slash is required: without it, Starlette's StaticFiles issues a
    # 307 redirect to add the slash, and behind a TLS-terminating reverse proxy
    # (DigitalOcean / Cloudflare) that redirect uses http://, which browsers
    # block as mixed content when loaded in an iframe on Telegram Web.
    return f"{base_url}/?packet_id={packet_id}&form_type={form_type}"


class StateEntry(BaseModel):
    """Single state value for the View State mini app."""

    key: str
    label: str
    value: Any


class WorkflowStepProgress(BaseModel):
    """Single workflow step for progress display."""

    name: str
    description: str
    status: str


class StateDataResponse(BaseModel):
    """Response for GET /api/mini-app/state-data."""

    packet_id: str
    packet_title: str
    packet_type: str
    packet_status: str
    state: list[StateEntry]
    workflow_steps: list[WorkflowStepProgress]
    is_stale: bool = False
    stale_minutes: int | None = None


def _sign_identifier(identifier: str) -> str:
    """Generate a short HMAC signature for any identifier.

    Used as fallback auth for read-only state views so they work
    on Telegram Desktop/Web where initData may be unavailable.
    """
    secret = os.getenv("TELEGRAM_BOT_TOKEN", "fallback").encode()
    sig = hmac.new(secret, identifier.encode(), hashlib.sha256).hexdigest()[:16]
    return sig


def verify_signature(identifier: str, sig: str) -> bool:
    """Verify an identifier signature (constant-time comparison)."""
    expected = _sign_identifier(identifier)
    return hmac.compare_digest(expected, sig)


def build_view_state_url(packet_id: str) -> str | None:
    """Build the Mini App URL for the read-only state view.

    Includes an HMAC signature so the state view works on Telegram
    Desktop/Web where initData may be empty or unavailable.
    """
    base_url = os.getenv("MINI_APP_BASE_URL", "").rstrip("/")
    if not base_url:
        return None
    base_url = _ensure_https(base_url)
    sig = _sign_identifier(packet_id)
    return f"{base_url}/?packet_id={packet_id}&view=state&sig={sig}"


def build_agent_state_url(instance_id: str) -> str | None:
    """Build the Mini App URL for an agent instance's read-only state view.

    Same HMAC pattern as work packet View State but with instance_id.
    """
    base_url = os.getenv("MINI_APP_BASE_URL", "").rstrip("/")
    if not base_url:
        return None
    base_url = _ensure_https(base_url)
    sig = _sign_identifier(instance_id)
    return f"{base_url}/?instance_id={instance_id}&view=agent_state&sig={sig}"
