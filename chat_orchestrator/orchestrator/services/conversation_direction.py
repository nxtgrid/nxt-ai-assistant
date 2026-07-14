"""Conversation direction planning for pre-Gemini routing and context choice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from orchestrator.models.schemas import ConversationMessage
from orchestrator.services.intent_router import route_expert_intent
from orchestrator.services.thread_assignment import (
    ThreadAssignmentService,
    filter_history_by_thread,
)


@dataclass
class ConversationDirection:
    """Unified decision about where the current chat turn should go."""

    direction: str = "normal_chat"
    context_scope: str = "session"
    thread_id: Optional[str] = None
    thread_filtered_history: Optional[List[ConversationMessage]] = None
    thread_method: Optional[str] = None
    thread_is_new: bool = False
    thread_confidence: float = 1.0
    issue_type: str = "other"
    expert_route: Optional[Dict[str, str]] = None
    method: str = "deterministic"

    def to_state_updates(self) -> Dict[str, Any]:
        return {
            "conversation_direction": self.direction,
            "conversation_context_scope": self.context_scope,
            "conversation_direction_method": self.method,
            "conversation_issue_type": self.issue_type,
            "planned_expert_route": self.expert_route,
            "thread_id": self.thread_id,
            "thread_filtered_history": self.thread_filtered_history,
            "thread_assignment_method": self.thread_method,
            "thread_assignment_confidence": self.thread_confidence,
            "thread_is_new": self.thread_is_new,
        }


class ConversationDirectionService:
    """Plans thread context and expert intent behind one pre-Gemini interface."""

    async def plan(
        self,
        user_input: str,
        conversation_history: List[ConversationMessage],
        reply_to_telegram_message_id: Optional[int] = None,
        active_work_packet: Optional[dict] = None,
        thread_disentanglement_enabled: bool = False,
    ) -> ConversationDirection:
        expert_route = (
            None if user_input.strip().startswith("/") else await route_expert_intent(user_input)
        )
        direction = "new_expert_workflow" if expert_route else "normal_chat"
        method = "model" if expert_route else "deterministic"
        issue_type = self._issue_type_from_expert_route(expert_route)

        if not thread_disentanglement_enabled:
            return ConversationDirection(
                direction=direction,
                context_scope="session",
                issue_type=issue_type,
                expert_route=expert_route,
                method=method,
            )

        assignment = await ThreadAssignmentService().assign_thread(
            user_input=user_input,
            conversation_history=conversation_history,
            reply_to_telegram_message_id=reply_to_telegram_message_id,
            active_work_packet=active_work_packet,
        )
        if assignment is None:
            return ConversationDirection(
                direction=direction,
                context_scope="session",
                issue_type=issue_type,
                expert_route=expert_route,
                method=method,
            )

        filtered = filter_history_by_thread(conversation_history, assignment.thread_id)
        return ConversationDirection(
            direction=direction,
            context_scope="thread",
            thread_id=assignment.thread_id,
            thread_filtered_history=filtered,
            thread_method=assignment.method,
            thread_is_new=assignment.is_new,
            thread_confidence=assignment.confidence,
            issue_type=assignment.issue_type or issue_type,
            expert_route=expert_route,
            method=method,
        )

    @staticmethod
    def _issue_type_from_expert_route(expert_route: Optional[Dict[str, str]]) -> str:
        if not expert_route:
            return "other"
        packet_type = expert_route.get("packet_type")
        if packet_type == "light_preliminary_package":
            return "lpp"
        if packet_type == "kpi_report":
            return "kpi"
        return "other"


__all__ = ["ConversationDirection", "ConversationDirectionService"]
