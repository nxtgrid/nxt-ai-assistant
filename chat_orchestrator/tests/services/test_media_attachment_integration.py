"""End-to-end: escalating with media -> capture -> ticket creation -> attachment linked.

Exercises the full chain through EscalationService.escalate_to_support() and
TicketService.create_ticket() (internal backend) with a single fake Supabase
client shared across both, so the test catches any mismatch between how
capture writes escalation_attachments and how ticket creation reads them
back -- something the per-module unit tests in Tasks 2-9 can't catch since
they each mock their own collaborators.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.escalation_service import EscalationService
from orchestrator.services.ticketing.attachment_repository import AttachmentRepository
from orchestrator.services.ticketing.backend import TicketCreateRequest


class _FakeTable:
    def __init__(self, store: Dict[str, List[Dict[str, Any]]], name: str) -> None:
        self._store = store
        self._name = name
        self._filters: Dict[str, Any] = {}
        self._payload: Dict[str, Any] | None = None
        self._mode = "select"

    def insert(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._payload = payload
        self._mode = "insert"
        return self

    def update(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._payload = payload
        self._mode = "update"
        return self

    def select(self, *_a: Any, **_k: Any) -> "_FakeTable":
        return self

    def eq(self, field: str, value: Any) -> "_FakeTable":
        self._filters[field] = value
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_FakeTable":
        return self

    def order(self, *_a: Any, **_k: Any) -> "_FakeTable":
        return self

    def execute(self) -> MagicMock:
        rows = self._store.setdefault(self._name, [])
        if self._mode == "insert":
            row = {"id": f"{self._name}-{len(rows) + 1}", **self._payload}
            rows.append(row)
            return MagicMock(data=[row])
        if self._mode == "update":
            matched = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
            for row in matched:
                row.update(self._payload)
            return MagicMock(data=matched)
        matched = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
        return MagicMock(data=matched)


class _FakeStorageBucket:
    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}

    def upload(self, path: str, file: bytes, _options: Dict[str, Any]) -> None:
        self.objects[path] = file


class _FakeStorage:
    def __init__(self, bucket: _FakeStorageBucket) -> None:
        self._bucket = bucket

    def from_(self, _name: str) -> _FakeStorageBucket:
        return self._bucket


class _FakeRpcCall:
    """Fakes the internal-ticket ref-allocation RPC. Returns a bare scalar
    string in ``.data``, matching PostgREST's response shape for a function
    that returns a plain (non-set) type rather than SETOF/TABLE -- see
    ``tests/services/ticketing/test_internal_backend.py`` for the same
    convention used by that module's own unit tests."""

    def __init__(self, ref: str) -> None:
        self._ref = ref

    def execute(self) -> MagicMock:
        return MagicMock(data=self._ref)


class _FakeSupabaseRawClient:
    def __init__(self) -> None:
        self._store: Dict[str, List[Dict[str, Any]]] = {}
        self.storage = _FakeStorage(_FakeStorageBucket())
        self._ticket_seq = 0

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(self._store, name)

    def rpc(self, name: str, params: Dict[str, Any]) -> _FakeRpcCall:
        assert name == "next_internal_ticket_ref", f"unexpected rpc: {name}"
        self._ticket_seq += 1
        prefix = (params or {}).get("p_prefix") or "TKT"
        return _FakeRpcCall(f"{prefix}-{self._ticket_seq:06d}")


@pytest.mark.asyncio
async def test_media_from_escalation_reaches_the_internal_ticket() -> None:
    raw_client = _FakeSupabaseRawClient()

    service = EscalationService(
        escalation_chat_id="-100123",
        bot_token="test-token",
        supabase_url="https://example.supabase.co",
        supabase_key="test-key",
    )
    service._send_telegram_message = AsyncMock(
        return_value={"ok": True, "result": {"message_id": 555}}
    )
    service._get_raw_client = MagicMock(return_value=raw_client)
    service.get_escalation_info = AsyncMock(return_value=None)
    service._get_or_create_escalation_topic = AsyncMock(return_value=None)

    wrapper = MagicMock()
    wrapper.save_escalation_mapping = AsyncMock(return_value="mapping-1")
    wrapper._get_client = MagicMock(return_value=raw_client)
    service._get_supabase_client = MagicMock(return_value=wrapper)
    service._resolve_chat_session_uuid = AsyncMock(return_value="session-uuid-1")

    async def fake_download(file_id: str, bot_token: str, max_size_bytes: int):
        return "ZmFrZS1ieXRlcw==", "image/jpeg"  # base64("fake-bytes")

    with patch(
        "orchestrator.services.telegram_transport.download_telegram_photo",
        new=fake_download,
    ):
        escalate_result = await service.escalate_to_support(
            question_summary="Meter sparking, see photo",
            session_id="telegram_abc",
            customer_chat_id="123",
            media_file_ids=[{"type": "image", "file_id": "photo1"}],
        )
    assert escalate_result["success"] is True

    attachments = await AttachmentRepository(client=raw_client).list_by_escalation("mapping-1")
    assert len(attachments) == 1
    assert attachments[0].mime_type == "image/jpeg"
    assert raw_client.storage.from_("escalation-media").objects[attachments[0].storage_path] == (
        b"fake-bytes"
    )

    ticket_result = await service._tickets.create_ticket(
        TicketCreateRequest(
            summary="Meter sparking",
            escalation_mapping_id="mapping-1",
            source="escalation",
        ),
        backend_override="internal",
    )

    linked = await AttachmentRepository(client=raw_client).list_by_escalation("mapping-1")
    assert linked[0].ticket_id == ticket_result.ticket_id
