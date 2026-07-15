"""Thread assignment service for conversation disentanglement.

Assigns incoming messages to conversation threads using two-path logic:
- Path A (deterministic): reply chains, commands, explicit new-issue signals, single active thread, zero threads
- Path B (LLM binary): ask Gemini Flash Lite when multiple active threads exist

Fail-open: any exception returns None → downstream uses full unfiltered history.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from orchestrator.models.schemas import ConversationMessage
from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Thread is "active" if it has a message within this window (configurable in minutes)
ACTIVE_THREAD_WINDOW_MINUTES = int(os.getenv("ACTIVE_THREAD_WINDOW_MINUTES", "60"))
ACTIVE_THREAD_WINDOW_SECONDS = ACTIVE_THREAD_WINDOW_MINUTES * 60

# LLM confidence threshold — below this, start a new thread
CONFIDENCE_THRESHOLD = 0.5

# Default model for thread classification
DEFAULT_CLASSIFIER_MODEL = "gemini-2.5-flash-lite"

# Issue type taxonomy — used for thread classification and open-issue queries.
ISSUE_TYPES = ("token", "hps", "meter", "transaction", "commissioning", "other")

# Explicit "new issue" signals — anchored to message start, case-insensitive.
# Matching any of these deterministically creates a new thread (Path A.2.5).
_NEW_ISSUE_SIGNAL_RE = re.compile(
    r"^("
    r"new (issue|problem|topic|question|thread|case)"
    r"|separate (issue|problem|question)"
    r"|different (issue|problem|question)"
    r"|unrelated (issue|problem|question|topic)"
    r"|this is a new (issue|problem|topic)"
    r"|i('m| am) (reporting|raising|opening|starting|logging) a (new|separate|different) (issue|problem|question)"
    r"|i have a (new|different|separate) (issue|problem|question)"
    r"|starting (a )?(new|fresh) (issue|problem|topic|thread)"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class ThreadAssignment:
    """Result of thread assignment."""

    thread_id: str
    is_new: bool = False
    method: str = "unknown"
    confidence: float = 1.0
    issue_type: Optional[str] = None


def _new_thread_id() -> str:
    """Generate a new thread ID."""
    return f"thr_{uuid.uuid4().hex[:12]}"


def _find_by_telegram_msg_id(
    history: List[ConversationMessage], telegram_message_id: int
) -> Optional[ConversationMessage]:
    """Find a message in history by its Telegram message ID."""
    for msg in reversed(history):
        if msg.telegram_message_id == telegram_message_id:
            return msg
    return None


def _get_active_thread_ids(
    history: List[ConversationMessage],
    max_age_seconds: int = ACTIVE_THREAD_WINDOW_SECONDS,
) -> List[str]:
    """Get distinct thread IDs from recent messages.

    Returns thread IDs ordered by most recent first.
    """
    now = datetime.now(timezone.utc)
    seen = set()
    active = []

    for msg in reversed(history):
        if not msg.thread_id:
            continue
        # Check recency via timestamp
        if msg.timestamp:
            try:
                msg_time = datetime.fromisoformat(msg.timestamp.replace("Z", "+00:00"))
                age = (now - msg_time).total_seconds()
                if age > max_age_seconds:
                    continue
            except (ValueError, TypeError):
                pass  # If timestamp parsing fails, include the message
        if msg.thread_id not in seen:
            seen.add(msg.thread_id)
            active.append(msg.thread_id)

    return active


def _get_last_n_messages_for_thread(
    history: List[ConversationMessage], thread_id: str, n: int = 3
) -> List[ConversationMessage]:
    """Get the last N messages belonging to a specific thread."""
    thread_msgs = [m for m in history if m.thread_id == thread_id]
    return thread_msgs[-n:]


def is_thread_disentanglement_enabled() -> bool:
    """Check if thread disentanglement is enabled via environment variable."""
    return os.getenv("THREAD_DISENTANGLEMENT_ENABLED", "false").lower() == "true"


async def classify_issue_type(user_input: str) -> str:
    """Classify the user's first message into one of the ISSUE_TYPES buckets.

    Uses Flash Lite for low-latency classification. Falls back to 'other' on any error.
    """
    model = os.getenv("THREAD_CLASSIFIER_MODEL", DEFAULT_CLASSIFIER_MODEL)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = (
        f"TODAY'S DATE AND TIME: {now_str}\n\n"
        f"Classify this customer support message into exactly one category.\n\n"
        f'Message: "{user_input[:500]}"\n\n'
        f"Categories:\n"
        f"- token: payment token generation, token not received, top-up failures\n"
        f"- hps: HPS power limit, load shedding, high-power service requests\n"
        f"- meter: meter errors, tamper alerts, meter replacement, meter hardware\n"
        f"- transaction: payments, wallet credit, transaction history, refunds\n"
        f"- commissioning: new connection, commissioning failures, meter activation\n"
        f"- other: anything else\n\n"
        f'Return JSON: {{"issue_type": "<one of the categories above>"}}'
    )
    try:
        gateway = get_default_generation_gateway(default_model=model)
        response = await gateway.generate(
            [LLMMessage(role="user", text=prompt)],
            GenerationOptions(
                model=model,
                temperature=0.0,
                max_output_tokens=32,
                response_format="json",
            ),
        )
        if response.text:
            result = json.loads(response.text.strip())
            issue_type = str(result.get("issue_type", "other"))
            if issue_type in ISSUE_TYPES:
                return issue_type
    except Exception as e:
        LOGGER.warning(f"Issue type classification failed, defaulting to 'other': {e}")
    return "other"


class ThreadAssignmentService:
    """Assigns messages to conversation threads."""

    async def assign_thread(
        self,
        user_input: str,
        conversation_history: List[ConversationMessage],
        reply_to_telegram_message_id: Optional[int] = None,
        active_work_packet: Optional[dict] = None,
    ) -> Optional[ThreadAssignment]:
        """Assign the incoming message to a thread.

        Returns None on any failure (fail-open → use full history).
        """
        try:
            return await self._assign(
                user_input=user_input,
                history=conversation_history,
                reply_to_id=reply_to_telegram_message_id,
                active_work_packet=active_work_packet,
            )
        except Exception as e:
            LOGGER.warning(f"Thread assignment failed (fail-open): {e}")
            return None

    async def _assign(
        self,
        user_input: str,
        history: List[ConversationMessage],
        reply_to_id: Optional[int],
        active_work_packet: Optional[dict],
    ) -> ThreadAssignment:
        """Internal assignment logic — Path A then Path B."""

        # Path A.1: Reply-to chain — follow to parent's thread
        if reply_to_id:
            parent = _find_by_telegram_msg_id(history, reply_to_id)
            if parent and parent.thread_id:
                return ThreadAssignment(
                    thread_id=parent.thread_id,
                    method="reply_chain",
                )

        # Path A.2: Slash command → always a new thread
        if user_input.strip().startswith("/"):
            return ThreadAssignment(
                thread_id=_new_thread_id(),
                is_new=True,
                method="command",
            )

        # Path A.2.5: Explicit "new issue" signal → always a new thread.
        # Matched at message start so mid-sentence occurrences don't trigger.
        if _NEW_ISSUE_SIGNAL_RE.match(user_input.strip()):
            LOGGER.info("Explicit new-issue signal detected — creating new thread")
            return ThreadAssignment(
                thread_id=_new_thread_id(),
                is_new=True,
                method="explicit_signal",
            )

        # Path A.3: Active expert workflow awaiting input → workflow's thread
        if active_work_packet:
            packet_state = active_work_packet.get("state", {})
            packet_thread = packet_state.get("thread_id")
            if packet_thread:
                return ThreadAssignment(
                    thread_id=packet_thread,
                    method="active_expert",
                )

        # Path A.4: Count active threads
        active_threads = _get_active_thread_ids(history)

        if len(active_threads) == 0:
            return ThreadAssignment(
                thread_id=_new_thread_id(),
                is_new=True,
                method="first_message",
            )

        if len(active_threads) == 1:
            return ThreadAssignment(
                thread_id=active_threads[0],
                method="single_active",
            )

        # Path B: Multiple active threads → ask LLM
        return await self._classify_with_llm(user_input, active_threads, history)

    async def _classify_with_llm(
        self,
        user_input: str,
        active_threads: List[str],
        history: List[ConversationMessage],
    ) -> ThreadAssignment:
        """Use Gemini Flash Lite for binary classification."""
        model = os.getenv("THREAD_CLASSIFIER_MODEL", DEFAULT_CLASSIFIER_MODEL)

        # Build thread summaries (last 3 messages per thread)
        thread_summaries = []
        for tid in active_threads:
            msgs = _get_last_n_messages_for_thread(history, tid)
            summary_lines = []
            for m in msgs:
                role = m.role
                text = (m.content or "")[:150]
                if m.function_call:
                    text = f"[tool call: {m.function_call.name}]"
                elif m.tool_result:
                    text = f"[tool result: {m.tool_result.name}]"
                summary_lines.append(f"  {role}: {text}")
            thread_summaries.append(f"Thread {tid}:\n" + "\n".join(summary_lines))

        threads_text = "\n\n".join(thread_summaries)

        prompt = f"""You are a conversation thread classifier for a utility chatbot (power grid operations).

Active threads:
{threads_text}

New message: "{user_input}"

Which thread does this message continue, or is it a new topic?

Return JSON: {{"thread_id": "<thread_id or NEW>", "confidence": 0.0-1.0, "reasoning": "<brief>"}}"""

        try:
            gateway = get_default_generation_gateway(default_model=model)
            response = await gateway.generate(
                [LLMMessage(role="user", text=prompt)],
                GenerationOptions(
                    model=model,
                    temperature=0.1,
                    max_output_tokens=128,
                    response_format="json",
                ),
            )

            if not response.text:
                LOGGER.warning("Empty LLM response for thread classification")
                return ThreadAssignment(thread_id=_new_thread_id(), is_new=True, method="llm_empty")

            result = json.loads(response.text.strip())
            chosen_tid = result.get("thread_id", "NEW")
            confidence = float(result.get("confidence", 0.0))

            LOGGER.info(
                f"LLM thread classification: {chosen_tid} "
                f"(confidence={confidence:.2f}, reasoning={result.get('reasoning', '')})"
            )

            if chosen_tid == "NEW" or confidence < CONFIDENCE_THRESHOLD:
                return ThreadAssignment(
                    thread_id=_new_thread_id(),
                    is_new=True,
                    method="llm_new",
                    confidence=confidence,
                )

            # Validate the LLM's answer is a real thread
            if chosen_tid in active_threads:
                return ThreadAssignment(
                    thread_id=chosen_tid,
                    method="llm",
                    confidence=confidence,
                )

            # LLM returned an unknown thread ID — treat as new
            LOGGER.warning(f"LLM returned unknown thread_id '{chosen_tid}', creating new thread")
            return ThreadAssignment(thread_id=_new_thread_id(), is_new=True, method="llm_invalid")

        except Exception as e:
            LOGGER.warning(f"LLM classification failed, creating new thread: {e}")
            return ThreadAssignment(thread_id=_new_thread_id(), is_new=True, method="llm_error")


def assign_passive_thread(
    history: List[ConversationMessage],
    reply_to_telegram_message_id: Optional[int] = None,
) -> str:
    """Deterministic thread assignment for passive (non-bot-directed) messages.

    Uses reply-chain and single-active-thread heuristics only (no LLM).
    Always returns a thread_id (creates a new one as fallback).
    """
    # Reply chain — inherit parent's thread
    if reply_to_telegram_message_id:
        parent = _find_by_telegram_msg_id(history, reply_to_telegram_message_id)
        if parent and parent.thread_id:
            return str(parent.thread_id)

    # Active thread heuristic
    active_threads = _get_active_thread_ids(history)
    if len(active_threads) == 1:
        return active_threads[0]

    # 0 or multiple active threads — create new thread
    return _new_thread_id()


def filter_history_by_thread(
    history: List[ConversationMessage],
    thread_id: str,
) -> List[ConversationMessage]:
    """Filter conversation history to messages belonging to a thread.

    Includes messages that:
    - Match the given thread_id
    - Have NULL thread_id (historical/shared messages — backward compat)

    Also enforces tool call/result pairs: if a function_call message is included,
    its corresponding tool_result stays too (and vice versa).
    """
    if not thread_id:
        return history

    # First pass: include matching or NULL thread_id
    indices = set()
    for i, msg in enumerate(history):
        if msg.thread_id is None or msg.thread_id == thread_id:
            indices.add(i)

    # Second pass: enforce function_call / tool_result pairs
    num = len(history)
    for i in list(indices):
        msg = history[i]
        if msg.function_call and (i + 1) < num:
            next_msg = history[i + 1]
            if next_msg.tool_result:
                indices.add(i + 1)
        elif msg.tool_result and i > 0:
            prev_msg = history[i - 1]
            if prev_msg.function_call:
                indices.add(i - 1)

    return [history[i] for i in sorted(indices)]


__all__ = [
    "ThreadAssignmentService",
    "ThreadAssignment",
    "assign_passive_thread",
    "filter_history_by_thread",
    "is_thread_disentanglement_enabled",
    "classify_issue_type",
    "ISSUE_TYPES",
]
