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

# n8n's original "Build Alert Actions1" pattern only matched "MPPT <id> [<x>]"
# with the bracket adjacent to the id. Real device names put the model between
# them ("Solar Charger - MPPT PNXG ARTN4.50/100/10 [27]") and some alerts have
# no bracket at all, so scan for the first id-shaped token after "MPPT" and
# keep a trailing numeric bracket as an instance discriminator.
#
# The (?!mppt) guard stops the bracket-scan from crossing into a second,
# independent "mppt" mention in the same string (e.g. a prose "MPPT
# performance issue" followed later by a real "MPPT B1 [5]") -- without it,
# the first match's span could swallow the second, legitimate one.
_MPPT_PATTERN = re.compile(
    r"\bmppt\b[\s:\-]+([A-Za-z0-9]+)((?:(?!mppt)[^\[\]]){0,40}\[(\d{1,4})\])?",
    re.IGNORECASE,
)
# Mirrors n8n's Build Alert Actions1 regexes exactly (see the plan's
# "Current architecture (n8n side)" section) -- a 16-hex id is a base
# station, a 9-digit id is a DCU.
_DCU_PATTERN = re.compile(r"dcu\s+(\d{9}|[a-fA-F0-9]{16})", re.IGNORECASE)


def _looks_like_component_id(token: str) -> bool:
    """An id carries a digit or is a short all-caps code -- "performance" is
    prose that happens to follow the word MPPT, "A3"/"PNXG"/"IYYY" are ids."""
    if any(character.isdigit() for character in token):
        return True
    return token.isupper() and 2 <= len(token) <= 12


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
        for mppt_match in _MPPT_PATTERN.finditer(haystack):
            token = mppt_match.group(1)
            if not _looks_like_component_id(token):
                continue
            instance = mppt_match.group(3)
            key = f"{token}#{instance}" if instance else token
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


def same_component(entry: Dict[str, Any], kind: str, key: str) -> bool:
    """Component identity, compared case-insensitively.

    Derived keys come from a regex over alert text; merged keys can come from
    the correlation LLM. The two must compare equal or the same component is
    stored twice and every re-fire looks novel.
    """
    return (
        str(entry.get("kind") or "").strip().casefold() == (kind or "").strip().casefold()
        and str(entry.get("key") or "").strip().casefold() == (key or "").strip().casefold()
    )


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
