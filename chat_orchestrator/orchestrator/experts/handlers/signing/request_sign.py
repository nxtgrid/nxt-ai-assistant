"""Request signature step handler.

Reusable step: any expert workflow can include [function:request_sign]
to request a signature on a Drive-hosted PDF.

Inputs (from packet_inputs or packet_state.parsed_inputs):
  - document_drive_id: str  — Drive file ID of the PDF to sign
  - signer_hint: str        — Name or @username of the intended signer
  - document_name: str      — (optional) Human-readable name shown in notification

Flow:
  1. Check requesting user has Drive read access to the document.
  2. Resolve signer_hint → accounts row via fuzzy match (WRatio ≥ 85).
     - Single match: proceed.
     - Multiple matches: needs_input with numbered list.
     - No match: failure.
  3. Write signing_* keys into packet_state.
  4. Send Telegram message to signer with inline Mini App button.
  5. Return immediately (fire-and-forget).

Cancel handling: when in needs_input state for signer selection,
cancel words ("cancel", "skip", etc.) set skip_remaining=True.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.auth.auth_service import get_auth_service
from shared.utils.drive_permissions import user_can_access
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger
from shared.utils.telegram_markdown import escape_markdown
from shared.utils.telegram_send import send_telegram_message

LOGGER = get_logger(__name__)

CANCEL_WORDS = {"cancel", "skip", "abort", "quit", "exit", "stop", "no"}
_SELF_SIGN_WORDS = {"me", "i", "myself", "i'll", "ill", "i will", "i want", "i want to"}
_SIGNER_THRESHOLD = 85

_DRIVE_URL_RE = re.compile(r"https?://drive\.google\.com/file/d/([a-zA-Z0-9_-]+)")
_DRIVE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{25,}$")


def _extract_drive_id(text: str) -> str:
    """Extract a Drive file ID from a URL or bare ID string."""
    text = text.strip()
    m = _DRIVE_URL_RE.search(text)
    if m:
        return m.group(1)
    # Bare file ID (no slashes, no spaces, 25+ chars)
    if _DRIVE_ID_RE.match(text):
        return text
    return text  # Return as-is; let the Drive API reject invalid IDs


# Accounts cache: avoid full-table scan on every invocation.
_accounts_cache: list[dict[str, Any]] = []
_accounts_cache_ts: float = 0.0
_ACCOUNTS_CACHE_TTL = 60.0  # seconds


async def _query_accounts(conn: Any) -> list[dict[str, Any]]:
    global _accounts_cache, _accounts_cache_ts
    now = time.monotonic()
    if _accounts_cache and now - _accounts_cache_ts < _ACCOUNTS_CACHE_TTL:
        return _accounts_cache
    rows = await conn.fetch(
        "SELECT telegram_id, full_name, email FROM accounts WHERE deleted_at IS NULL"
    )
    _accounts_cache = [dict(r) for r in rows]
    _accounts_cache_ts = now
    return _accounts_cache


def _resolve_candidates(hint: str, accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return accounts fuzzy-matching hint at WRatio ≥ 85."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        LOGGER.warning("rapidfuzz not installed — signer resolution unavailable")
        return []

    # Strip leading @ for @username-style hints, then match against full_name and email prefix
    clean_hint = hint.lstrip("@")
    scored = []
    for row in accounts:
        email = row.get("email") or ""
        email_prefix = email.split("@")[0] if "@" in email else email
        score = max(
            fuzz.WRatio(clean_hint, row.get("full_name") or ""),
            fuzz.WRatio(clean_hint, email_prefix),
        )
        if score >= _SIGNER_THRESHOLD:
            scored.append({**row, "_score": score})

    scored.sort(key=lambda r: r["_score"], reverse=True)
    return scored


def _build_selection_prompt(candidates: list[dict[str, Any]]) -> str:
    lines = ["Which person did you mean?\n"]
    for i, c in enumerate(candidates, 1):
        name = c.get("full_name") or c.get("email") or "Unknown"
        email = c.get("email") or ""
        label = f"{name} ({email})" if email and email != name else name
        lines.append(f"{i}. {label}")
    lines.append("\nReply with the number, or 'cancel' to abort.")
    return "\n".join(lines)


def _parse_selection(user_input: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        idx = int(user_input.strip()) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]
    except (ValueError, TypeError):
        pass
    return None


async def _send_sign_button(
    signer_telegram_id: str,
    packet_id: str,
    document_hint: str,
    requester_name: str,
    requester_email: str,
) -> None:
    """Send Telegram message to signer with inline Mini App button."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        LOGGER.warning("TELEGRAM_BOT_TOKEN not set — cannot send sign button")
        return

    base_url = os.getenv("MINI_APP_BASE_URL", "").rstrip("/")
    if not base_url:
        LOGGER.warning("MINI_APP_BASE_URL not set — cannot build sign URL")
        return

    if base_url.startswith("http://"):
        base_url = "https://" + base_url[7:]

    sign_url = f"{base_url}/?packet_id={packet_id}&view=sign"

    safe_name = escape_markdown(requester_name)
    safe_email = escape_markdown(requester_email)
    safe_doc = escape_markdown(document_hint)
    text = (
        f"*Signature request from {safe_name}*\n\n"
        f"You have been asked to sign: *{safe_doc}*\n"
        f"Requested by: {safe_name} ({safe_email})\n\n"
        f"Tap to review the document before signing."
    )
    reply_markup = {"inline_keyboard": [[{"text": "Sign document", "web_app": {"url": sign_url}}]]}

    await send_telegram_message(
        bot_token,
        signer_telegram_id,
        text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


@register_step("request_sign")
async def request_sign(context: StepContext) -> StepResult:
    """Request a signature on a Drive PDF.

    Writes signing_* keys to packet_state and sends the signer a
    Telegram message with a Mini App button. Returns immediately.
    """
    # ── Resume path: signer selection ───────────────────────────────────────
    awaiting = context.get_state("awaiting_signer_selection")
    if awaiting:
        user_input = (context.user_input or "").strip().lower()

        if user_input in CANCEL_WORDS:
            return StepResult(
                state_updates={"awaiting_signer_selection": False, "signer_candidates": []},
                skip_remaining=True,
            )

        candidates = context.get_state("signer_candidates") or []

        signer = _parse_selection(user_input, candidates)
        if not signer:
            return StepResult.needs_input(
                _build_selection_prompt(candidates) + "\n\nPlease enter a valid number."
            )

        document_drive_id = (
            context.get_state("pending_document_drive_id")
            or context.get_input("document_drive_id")
            or ""
        )
        return await _complete_request(context, signer, document_drive_id)

    # ── Awaiting signer hint (user provided URL but no signer) ───────────────
    awaiting_signer_hint = context.get_state("awaiting_signer_hint")
    if awaiting_signer_hint:
        user_input = (context.user_input or "").strip()
        lowered = user_input.lower()
        if lowered in CANCEL_WORDS:
            return StepResult(
                state_updates={"awaiting_signer_hint": False},
                skip_remaining=True,
            )
        # Detect self-sign: "me", "I", "I want to sign it", etc.
        if _is_self_sign(lowered):
            return await _resolve_self_signer(
                context, document_drive_id=context.get_state("pending_document_drive_id") or ""
            )
        return await _run_with_inputs(
            context,
            document_drive_id=context.get_state("pending_document_drive_id") or "",
            signer_hint=user_input,
        )

    # ── First-run path ───────────────────────────────────────────────────────
    document_drive_id = context.get_input("document_drive_id")
    signer_hint = context.get_input("signer_hint")

    # Fall back: extract Drive ID from command args (e.g. /sign <url>)
    if not document_drive_id:
        raw_args = context.get_input("args") or context.get_input("raw_request") or ""
        if raw_args:
            document_drive_id = _extract_drive_id(raw_args.split()[0] if raw_args else "")

    if not document_drive_id:
        return StepResult.needs_input(
            "Please provide the Drive file URL or ID of the document to sign.\n"
            "Reply with the Drive link or 'cancel' to abort."
        )

    if not signer_hint:
        return StepResult(
            needs_user_input=True,
            user_prompt="Who should sign this document? Reply with their name or email.",
            state_updates={
                "awaiting_signer_hint": True,
                "pending_document_drive_id": document_drive_id,
            },
        )

    return await _run_with_inputs(context, document_drive_id, signer_hint)


def _is_self_sign(lowered: str) -> bool:
    """Return True if the user's reply indicates they want to sign it themselves."""
    if lowered in _SELF_SIGN_WORDS:
        return True
    # Phrases containing self-sign intent (e.g. "I want to sign it", "I'll sign it myself")
    self_patterns = ("i want to sign", "i'll sign", "i will sign", "i'd sign", "i am signing")
    return any(lowered.startswith(p) or p in lowered for p in self_patterns)


async def _resolve_self_signer(context: StepContext, document_drive_id: str) -> StepResult:
    """Resolve the requester as the signer using their telegram_id."""
    requester_chat_id = context.user_context.chat_id if context.user_context else None
    if not requester_chat_id:
        return StepResult.failure(
            "Couldn't identify your account. Please provide your name instead."
        )

    auth_service = get_auth_service()
    try:
        pool = await auth_service._get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT telegram_id, full_name, email FROM accounts WHERE telegram_id = $1 AND deleted_at IS NULL",
                str(requester_chat_id),
            )
    except Exception as e:
        LOGGER.error("request_sign: DB error resolving self-signer: %s", e)
        return StepResult.failure(sanitize_error_for_user(str(e)))

    if not row:
        return StepResult.failure("Couldn't find your account. Please provide your name instead.")

    return await _complete_request(context, dict(row), document_drive_id)


async def _run_with_inputs(
    context: StepContext, document_drive_id: str, signer_hint: str
) -> StepResult:
    """Validate Drive access, resolve signer, and dispatch signing."""
    # 1. Check requesting user has Drive read access
    can_access = await user_can_access(
        document_drive_id,
        context.effective_email,
        need_write=False,
    )
    if not can_access:
        LOGGER.warning(
            "request_sign: Drive access denied for email=%s file=%s",
            context.effective_email,
            document_drive_id,
        )
        return StepResult.failure(
            "You don't have access to that document. "
            "Make sure it's shared with your account and try again."
        )

    # 2. Resolve signer
    auth_service = get_auth_service()
    try:
        pool = await auth_service._get_db_pool()
        async with pool.acquire() as conn:
            accounts = await _query_accounts(conn)
    except Exception as e:
        LOGGER.error("request_sign: DB error fetching accounts: %s", e)
        return StepResult.failure(sanitize_error_for_user(str(e)))

    candidates = _resolve_candidates(signer_hint, accounts)

    if not candidates:
        return StepResult.failure(
            f"Couldn't find anyone matching '{signer_hint}'. Try their full name or email address.",
        )

    if len(candidates) > 1:
        safe_candidates = [
            {
                "telegram_id": c["telegram_id"],
                "full_name": c.get("full_name") or "",
                "email": c.get("email") or "",
            }
            for c in candidates
        ]
        return StepResult(
            needs_user_input=True,
            user_prompt=_build_selection_prompt(candidates),
            state_updates={
                "awaiting_signer_selection": True,
                "signer_candidates": safe_candidates,
                "pending_document_drive_id": document_drive_id,
            },
        )

    return await _complete_request(context, candidates[0], document_drive_id)


async def _complete_request(
    context: StepContext, signer: dict[str, Any], document_drive_id: str
) -> StepResult:
    """Write signing state and send the button after signer is resolved."""
    document_name = context.get_input("document_name") or ""
    requester_telegram_id = context.user_context.chat_id if context.user_context else None
    requester_email = context.effective_email or ""

    document_hint = document_name or document_drive_id or "document"

    # Use email prefix as display name — avoids a second DB round-trip.
    requester_name = requester_email.split("@")[0] if "@" in requester_email else "A team member"

    signing_state: dict[str, Any] = {
        "signing_document_drive_id": document_drive_id,
        "signing_document_name": document_name,
        "signing_requester_telegram_id": str(requester_telegram_id or ""),
        "signing_requester_name": requester_name,
        "signing_requester_email": requester_email,
        "signing_signer_telegram_id": str(signer.get("telegram_id") or ""),
        "signing_status": "pending",
        # Clear selection state
        "awaiting_signer_selection": False,
        "signer_candidates": [],
    }

    try:
        await _send_sign_button(
            str(signer["telegram_id"]),
            context.packet_id,
            document_hint,
            requester_name=requester_name,
            requester_email=requester_email,
        )
    except Exception as e:
        LOGGER.error("request_sign: failed to send sign button: %s", e)
        return StepResult.failure(sanitize_error_for_user(str(e)))

    signer_name = signer.get("full_name") or signer.get("email") or "the signer"
    return StepResult(
        data={"signer_resolved": signer_name},
        state_updates=signing_state,
        progress_message=f"Signing request sent to {signer_name}.",
    )
