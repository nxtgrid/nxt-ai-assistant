"""Tests for orchestrator.experts.entity_fanout (Phase 5 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md, item 5): entity
eligibility/metadata logic lifted out of agent_worker.py so skill
scheduling and persistent-agent reconciliation share one implementation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.experts.entity_fanout import (
    SUPPORTED_ANCHOR_ENTITY_TYPES,
    build_anchor_metadata,
    get_eligible_entities,
)


class TestGetEligibleEntities:
    @pytest.mark.asyncio
    async def test_grid_delegates_to_get_eligible_grids_for_agents(self):
        fake_auth = AsyncMock()
        fake_auth.get_eligible_grids_for_agents = AsyncMock(
            return_value=[{"id": 1, "name": "Example Grid"}]
        )

        with patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth):
            result = await get_eligible_entities("grid")

        assert result == [{"id": 1, "name": "Example Grid"}]
        fake_auth.get_eligible_grids_for_agents.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_organization_delegates_to_get_eligible_organizations_for_agents(self):
        fake_auth = AsyncMock()
        fake_auth.get_eligible_organizations_for_agents = AsyncMock(
            return_value=[{"id": 7, "name": "Acme"}]
        )

        with patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth):
            result = await get_eligible_entities("organization")

        assert result == [{"id": 7, "name": "Acme"}]
        fake_auth.get_eligible_organizations_for_agents.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unsupported_entity_type_returns_empty_list_not_raise(self):
        result = await get_eligible_entities("meter")

        assert result == []

    def test_supported_types_are_exactly_grid_and_organization(self):
        assert set(SUPPORTED_ANCHOR_ENTITY_TYPES) == {"grid", "organization"}


class TestBuildAnchorMetadata:
    def test_grid_shape_unchanged_from_pre_lift_agent_worker(self):
        # Byte-for-byte what agent_worker.py always produced -- persistent
        # agent instances (still reconciled through it) must see zero
        # change from this lift.
        entity = {
            "name": "Example Grid",
            "internal_telegram_group_chat_id": "-100123",
            "internal_telegram_group_thread_id": 5,
            "generation_external_site_id": "site-1",
            "organization_id": 7,
            "organization_name": "Acme",
        }

        metadata = build_anchor_metadata("grid", entity)

        assert metadata == {
            "grid_name": "Example Grid",
            "telegram_chat_id": "-100123",
            "telegram_topic_id": 5,
            "vrm_site_id": "site-1",
            "organization_id": 7,
            "organization_name": "Acme",
        }

    def test_organization_shape(self):
        entity = {
            "id": 7,
            "name": "Acme",
            "developer_group_telegram_chat_id": "-100999",
        }

        metadata = build_anchor_metadata("organization", entity)

        assert metadata["organization_id"] == 7
        assert metadata["organization_name"] == "Acme"
        assert metadata["telegram_chat_id"] == "-100999"
        assert metadata["telegram_topic_id"] is None

    def test_unsupported_entity_type_falls_back_to_name_and_org_id(self):
        metadata = build_anchor_metadata("meter", {"name": "M1", "organization_id": 3})

        assert metadata == {"name": "M1", "organization_id": 3}
