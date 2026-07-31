"""Tests for AttachmentRepository, the sole writer/reader for escalation_attachments."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from orchestrator.services.ticketing.attachment_repository import AttachmentRepository


class _FakeResult:
    def __init__(self, data: List[Dict[str, Any]]) -> None:
        self.data = data


class _FakeTable:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self._filters: Dict[str, Any] = {}
        self._insert_payload: Dict[str, Any] | None = None
        self._update_payload: Dict[str, Any] | None = None

    def insert(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._insert_payload = payload
        return self

    def update(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._update_payload = payload
        return self

    def select(self, *_args: Any, **_kwargs: Any) -> "_FakeTable":
        return self

    def eq(self, field: str, value: Any) -> "_FakeTable":
        self._filters[field] = value
        return self

    def execute(self) -> _FakeResult:
        if self._insert_payload is not None:
            row = {"id": "attachment-1", **self._insert_payload}
            self._rows.append(row)
            return _FakeResult([row])
        if self._update_payload is not None:
            matched = []
            for row in self._rows:
                if all(row.get(k) == v for k, v in self._filters.items()):
                    row.update(self._update_payload)
                    matched.append(row)
            return _FakeResult(matched)
        matched = [
            row for row in self._rows if all(row.get(k) == v for k, v in self._filters.items())
        ]
        return _FakeResult(matched)


class _FakeClient:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def table(self, name: str) -> _FakeTable:
        assert name == "escalation_attachments"
        return _FakeTable(self._rows)


@pytest.fixture
def rows() -> List[Dict[str, Any]]:
    return []


@pytest.fixture
def repo(rows: List[Dict[str, Any]]) -> AttachmentRepository:
    return AttachmentRepository(client=_FakeClient(rows))


class TestInsert:
    @pytest.mark.asyncio
    async def test_inserts_and_returns_the_new_row(self, repo: AttachmentRepository) -> None:
        attachment = await repo.insert(
            escalation_id="esc-1",
            storage_path="esc-1/attachment-1.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=1234,
        )
        assert attachment.escalation_id == "esc-1"
        assert attachment.storage_path == "esc-1/attachment-1.jpg"
        assert attachment.media_type == "image"
        assert attachment.mime_type == "image/jpeg"
        assert attachment.size_bytes == 1234
        assert attachment.ticket_id is None
        assert attachment.jira_attachment_id is None


class TestListByEscalation:
    @pytest.mark.asyncio
    async def test_returns_only_rows_for_the_given_escalation(
        self, repo: AttachmentRepository, rows: List[Dict[str, Any]]
    ) -> None:
        await repo.insert(
            escalation_id="esc-1",
            storage_path="esc-1/a.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=10,
        )
        await repo.insert(
            escalation_id="esc-2",
            storage_path="esc-2/b.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=20,
        )
        result = await repo.list_by_escalation("esc-1")
        assert len(result) == 1
        assert result[0].storage_path == "esc-1/a.jpg"

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_none_found(self, repo: AttachmentRepository) -> None:
        result = await repo.list_by_escalation("no-such-escalation")
        assert result == []


class TestLinkTicket:
    @pytest.mark.asyncio
    async def test_stamps_ticket_id_on_every_row_for_the_escalation(
        self, repo: AttachmentRepository
    ) -> None:
        await repo.insert(
            escalation_id="esc-1",
            storage_path="esc-1/a.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=10,
        )
        await repo.link_ticket("esc-1", "ticket-99")
        result = await repo.list_by_escalation("esc-1")
        assert result[0].ticket_id == "ticket-99"


class TestMarkSynced:
    @pytest.mark.asyncio
    async def test_stamps_jira_attachment_id(self, repo: AttachmentRepository) -> None:
        attachment = await repo.insert(
            escalation_id="esc-1",
            storage_path="esc-1/a.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=10,
        )
        await repo.mark_synced(attachment.id, "10001")
        result = await repo.list_by_escalation("esc-1")
        assert result[0].jira_attachment_id == "10001"
