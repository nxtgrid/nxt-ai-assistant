"""Alert facts, subject normalization, and signature derivation for /notify
smart ticket correlation.

Pure functions only -- no I/O, no flags, no LLM calls. These are the building
blocks ``AlertCorrelator`` (correlator.py) uses to decide whether an incoming
alert is a brand-new issue, an amend of an existing ticket (a different
component of the same underlying problem), or an exact re-fire of one
already recorded.

Signature design (see the plan's "Alert signature" section):
    signature = sha1(f"{grid}|{component_kind}|{normalize_subject(subject, component_key)}")[:16]

The signature deliberately EXCLUDES the component key -- ``MPPT A3`` and
``MPPT A7`` firing on the same grid must produce the *same* signature (so
they surface as amend candidates for each other), while the deterministic
duplicate check in the correlator compares the full ``(signature,
component_key)`` pair to tell a genuine re-fire (same key) apart from a new
affected component (different key, same underlying issue shape).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from pydantic import BaseModel, Field

_SEVERITY_PATTERN = re.compile(r"\burgent\b", re.IGNORECASE)
_WARNING_PATTERN = re.compile(r"\bwarning\b", re.IGNORECASE)

_LEADING_MARKER = re.compile(r"^\s*!\s*(?:warning|urgent)\s*:\s*", re.IGNORECASE)
_LEADING_BANG = re.compile(r"^\s*!\s*")
_ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(:\d{2})?(\.\d+)?z?", re.IGNORECASE)
_PERCENTAGE = re.compile(r"\d+(?:\.\d+)?\s*%")
_VOLTAGE = re.compile(r"\d+(?:\.\d+)?\s*v\b", re.IGNORECASE)
_ANY_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_WHITESPACE = re.compile(r"\s+")

# Mirrors n8n's Build Alert Actions1 regexes exactly (see the plan's
# "Current architecture (n8n side)" section) -- a 16-hex id is a base
# station, a 9-digit id is a DCU.
_MPPT_PATTERN = re.compile(r"mppt\s+([A-Za-z0-9]+)\s*\[(.*?)\]", re.IGNORECASE)
_DCU_PATTERN = re.compile(r"dcu\s+(\d{9}|[a-fA-F0-9]{16})", re.IGNORECASE)


class AlertFacts(BaseModel):
    """Structured facts about one incoming alert, extending ``NotifyRequest``.

    Every field is optional and independently derivable (see
    ``enrich_alert_facts``) -- n8n may supply some directly (post-Task-13
    cutover) and leave the rest blank for Anansi to fill in.
    """

    subject: str = ""
    alert_type: str = ""
    details: str = ""
    severity: str = ""
    component_kind: str = ""
    component_key: str = ""
    component_label: str = ""
    signature: str = ""
    fired_at: str = ""
    rule_id: str = ""
    raw: Dict[str, Any] = Field(default_factory=dict)


def derive_severity(subject: str) -> str:
    """"urgent" | "warning" | "" from the n8n "! Urgent:"/"! Warning:" convention."""
    if _SEVERITY_PATTERN.search(subject or ""):
        return "urgent"
    if _WARNING_PATTERN.search(subject or ""):
        return "warning"
    return ""


def derive_component(subject: str, text: str = "") -> Tuple[str, str, str]:
    """Extract (kind, key, label) from an alert's subject/body text.

    Searches ``subject`` first, then ``text`` (n8n runs these regexes against
    the alert body, "activeText" -- searching the subject too is a superset,
    matching whichever field the pattern actually appears in). Returns
    ``("", "", "")`` when nothing matches -- callers must treat that as "no
    identifiable component" (e.g. a grid-level alert), not an error.
    """
    for haystack in (subject or "", text or ""):
        mppt_match = _MPPT_PATTERN.search(haystack)
        if mppt_match:
            key = mppt_match.group(1)
            return "mppt", key, f"MPPT {key}"

        dcu_match = _DCU_PATTERN.search(haystack)
        if dcu_match:
            key = dcu_match.group(1)
            kind = "base_station" if len(key) > 10 else "dcu"
            label = "Base Station" if kind == "base_station" else "DCU"
            return kind, key, f"{label} {key}"

    return "", "", ""


def normalize_subject(subject: str, component_key: str = "") -> str:
    """Canonicalize a subject line for signature comparison.

    Strips the leading "! Warning:"/"! Urgent:" marker and a trailing "!",
    lowercases, removes the component key (if given) so different components
    of the same issue shape normalize identically, masks numbers/percentages/
    voltages/ISO timestamps to "#", and collapses whitespace.
    """
    text = subject or ""
    text = _LEADING_MARKER.sub("", text)
    text = _LEADING_BANG.sub("", text)
    text = text.rstrip("!").strip()
    text = text.lower()

    if component_key:
        text = re.sub(rf"\b{re.escape(component_key.lower())}\b", "", text)

    # Order matters: mask the more specific shapes before the generic number
    # mask consumes their digits piecemeal.
    text = _ISO_TIMESTAMP.sub("#", text)
    text = _PERCENTAGE.sub("#", text)
    text = _VOLTAGE.sub("#", text)
    text = _ANY_NUMBER.sub("#", text)

    text = _WHITESPACE.sub(" ", text).strip()
    return text


def derive_signature(
    grid_name: str, component_kind: str, subject: str, component_key: str = ""
) -> str:
    """Stable fingerprint for grouping alerts of the same shape on the same grid.

    Deliberately excludes ``component_key`` from the hashed material -- see
    the module docstring.
    """
    normalized = normalize_subject(subject, component_key=component_key)
    material = f"{grid_name}|{component_kind}|{normalized}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def enrich_alert_facts(alert: AlertFacts, grid_name: str) -> AlertFacts:
    """Fill in blank severity/component/signature/fired_at fields.

    Never overwrites a field the caller already populated -- n8n (post
    cutover) may supply some of these directly; this only covers what's
    still blank.
    """
    severity = alert.severity or derive_severity(alert.subject)

    component_kind, component_key, component_label = (
        alert.component_kind,
        alert.component_key,
        alert.component_label,
    )
    if not component_kind:
        component_kind, component_key, component_label = derive_component(
            alert.subject, alert.details
        )

    signature = alert.signature or derive_signature(
        grid_name=grid_name,
        component_kind=component_kind,
        subject=alert.subject,
        component_key=component_key,
    )

    fired_at = alert.fired_at or datetime.now(timezone.utc).isoformat()

    return alert.model_copy(
        update={
            "severity": severity,
            "component_kind": component_kind,
            "component_key": component_key,
            "component_label": component_label,
            "signature": signature,
            "fired_at": fired_at,
        }
    )
