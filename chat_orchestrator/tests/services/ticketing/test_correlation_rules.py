"""Tests for correlation_rules.py: the operator-editable rules doc, RAG
context, and grid operational-facts wrappers the AlertCorrelator prompt
assembly (correlator.py, Task 7) draws on.

Every public function here must degrade gracefully (never raise) when its
upstream is absent/unreachable -- a Google Doc fetch failure, a missing
bundled file, RAG disabled, or an AuthService error must never break a
correlation decision; they just mean less context in the prompt.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing import correlation_rules


class TestGetCorrelationInstructions:
    def test_prefers_google_doc_when_configured(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_DOC_ID", "doc-123")

        def fake_fetch(self, doc_id, start_section="system instructions"):
            assert doc_id == "doc-123"
            return {"system_instructions": "from the doc"}

        monkeypatch.setattr(
            "orchestrator.services.artifacts_provider.ArtifactsProvider._fetch_google_doc_sections",
            fake_fetch,
        )

        sections = correlation_rules.get_correlation_instructions()

        assert sections == {"system_instructions": "from the doc"}

    def test_falls_back_to_bundled_file_when_doc_unset(self, monkeypatch):
        monkeypatch.delenv("ALERT_CORRELATION_DOC_ID", raising=False)
        monkeypatch.setattr(
            "orchestrator.services.instructions_provider._load_fallback_instructions",
            lambda filename: {"system_instructions": "from bundled file"},
        )

        sections = correlation_rules.get_correlation_instructions()

        assert sections == {"system_instructions": "from bundled file"}

    def test_falls_back_to_bundled_file_when_doc_fetch_fails(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_DOC_ID", "doc-123")
        monkeypatch.setattr(
            "orchestrator.services.artifacts_provider.ArtifactsProvider._fetch_google_doc_sections",
            lambda self, doc_id, start_section="system instructions": None,
        )
        monkeypatch.setattr(
            "orchestrator.services.instructions_provider._load_fallback_instructions",
            lambda filename: {"system_instructions": "from bundled file"},
        )

        sections = correlation_rules.get_correlation_instructions()

        assert sections == {"system_instructions": "from bundled file"}

    def test_falls_back_to_bundled_file_when_doc_fetch_raises(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_DOC_ID", "doc-123")

        def raising_fetch(self, doc_id, start_section="system instructions"):
            raise RuntimeError("network blip")

        monkeypatch.setattr(
            "orchestrator.services.artifacts_provider.ArtifactsProvider._fetch_google_doc_sections",
            raising_fetch,
        )
        monkeypatch.setattr(
            "orchestrator.services.instructions_provider._load_fallback_instructions",
            lambda filename: {"system_instructions": "from bundled file"},
        )

        sections = correlation_rules.get_correlation_instructions()

        assert sections == {"system_instructions": "from bundled file"}

    def test_minimal_builtin_fallback_when_bundled_file_missing_too(self, monkeypatch):
        monkeypatch.delenv("ALERT_CORRELATION_DOC_ID", raising=False)
        monkeypatch.setattr(
            "orchestrator.services.instructions_provider._load_fallback_instructions",
            lambda filename: None,
        )

        sections = correlation_rules.get_correlation_instructions()

        assert "system_instructions" in sections
        assert sections["system_instructions"]  # non-empty

    def test_uses_real_bundled_file_by_default(self):
        """Sanity check against the actual shipped file (no monkeypatching) --
        confirms alert_correlation_instructions.md exists and parses."""
        sections = correlation_rules.get_correlation_instructions(doc_id="")

        assert "system_instructions" in sections
        assert len(sections["system_instructions"]) > 0


class _FakeRagProvider:
    def __init__(self, result: List[str]):
        self.result = result
        self.calls: List[Dict[str, Any]] = []

    async def retrieve_as_text(self, query: str, user_email: str, limit: int) -> List[str]:
        self.calls.append({"query": query, "user_email": user_email, "limit": limit})
        return self.result


class TestGetRagContext:
    @pytest.mark.asyncio
    async def test_returns_snippets_when_enabled_and_identity_set(self, monkeypatch):
        monkeypatch.setenv("rag__enabled", "true")
        monkeypatch.setenv("ALERT_CORRELATION_RAG_IDENTITY", "staff@example.com")
        fake = _FakeRagProvider(["snippet one"])

        result = await correlation_rules.get_rag_context("MPPT low", rag_provider=fake)

        assert result == ["snippet one"]
        assert fake.calls[0]["user_email"] == "staff@example.com"

    @pytest.mark.asyncio
    async def test_empty_when_rag_disabled(self, monkeypatch):
        monkeypatch.setenv("rag__enabled", "false")
        monkeypatch.setenv("ALERT_CORRELATION_RAG_IDENTITY", "staff@example.com")
        fake = _FakeRagProvider(["snippet one"])

        result = await correlation_rules.get_rag_context("MPPT low", rag_provider=fake)

        assert result == []
        assert fake.calls == []

    @pytest.mark.asyncio
    async def test_empty_when_identity_blank(self, monkeypatch):
        monkeypatch.setenv("rag__enabled", "true")
        monkeypatch.setenv("ALERT_CORRELATION_RAG_IDENTITY", "")
        fake = _FakeRagProvider(["snippet one"])

        result = await correlation_rules.get_rag_context("MPPT low", rag_provider=fake)

        assert result == []
        assert fake.calls == []

    @pytest.mark.asyncio
    async def test_empty_when_query_blank(self, monkeypatch):
        monkeypatch.setenv("rag__enabled", "true")
        monkeypatch.setenv("ALERT_CORRELATION_RAG_IDENTITY", "staff@example.com")
        fake = _FakeRagProvider(["snippet one"])

        result = await correlation_rules.get_rag_context("   ", rag_provider=fake)

        assert result == []
        assert fake.calls == []

    @pytest.mark.asyncio
    async def test_empty_when_provider_raises(self, monkeypatch):
        monkeypatch.setenv("rag__enabled", "true")
        monkeypatch.setenv("ALERT_CORRELATION_RAG_IDENTITY", "staff@example.com")

        class _RaisingProvider:
            async def retrieve_as_text(self, **_kwargs):
                raise RuntimeError("boom")

        result = await correlation_rules.get_rag_context("MPPT low", rag_provider=_RaisingProvider())

        assert result == []


class _FakeAuthService:
    def __init__(self, facts: Optional[Dict[str, Any]] = None, error: Optional[Exception] = None):
        self.facts = facts or {}
        self.error = error
        self.calls: List[str] = []

    async def get_grid_operational_facts(self, grid_name: str) -> Dict[str, Any]:
        self.calls.append(grid_name)
        if self.error:
            raise self.error
        return self.facts


class TestGetGridOperationalContext:
    @pytest.mark.asyncio
    async def test_returns_facts_from_auth_service(self):
        fake = _FakeAuthService(facts={"is_hps_on": False, "dcu_offline_count": 3})

        result = await correlation_rules.get_grid_operational_context("Kudi", auth_service=fake)

        assert result == {"is_hps_on": False, "dcu_offline_count": 3}
        assert fake.calls == ["Kudi"]

    @pytest.mark.asyncio
    async def test_empty_dict_on_error(self):
        fake = _FakeAuthService(error=RuntimeError("db down"))

        result = await correlation_rules.get_grid_operational_context("Kudi", auth_service=fake)

        assert result == {}
