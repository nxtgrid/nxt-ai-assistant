"""Tests for scripts/migrate_docs_to_db.py -- one-time seeding of the DB
override layer with today's live Google Doc content, for every Doc-bound
overridable prompt.

`scripts` has no `__init__.py` but is importable as a namespace package given
the repo root is on PYTHONPATH (see test_backfill_design_artifacts.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts.migrate_docs_to_db import DOC_BOUND_PROMPT_IDS, migrate


def test_doc_bound_prompt_ids_matches_the_legacy_env_var_mapping():
    from shared.prompts.gdoc import LEGACY_DOC_ENV_VARS

    assert set(DOC_BOUND_PROMPT_IDS) == set(LEGACY_DOC_ENV_VARS)


def test_migrate_skips_prompts_with_no_doc_id_configured(monkeypatch):
    for env_var in ["CUSTOMER_SUPPORT_DOC_ID", "STAFF_SUPPORT_DOC_ID",
                     "EXPERT_INSTRUCTIONS_DOC_ID", "TROUBLESHOOTING_PROCEDURES_DOC_ID",
                     "VERIFICATION_DOC_ID"]:
        monkeypatch.delenv(env_var, raising=False)

    fake_store = MagicMock()
    with patch("scripts.migrate_docs_to_db.OverrideStore.from_env", return_value=fake_store):
        results = migrate(actor="ops@example.com")

    assert all(r["status"] == "skipped_no_doc" for r in results)
    fake_store.propose.assert_not_called()
    fake_store.publish.assert_not_called()


def test_migrate_proposes_and_publishes_each_configured_doc(monkeypatch):
    monkeypatch.setenv("CUSTOMER_SUPPORT_DOC_ID", "DOC1")
    for env_var in ["STAFF_SUPPORT_DOC_ID", "EXPERT_INSTRUCTIONS_DOC_ID",
                     "TROUBLESHOOTING_PROCEDURES_DOC_ID", "VERIFICATION_DOC_ID"]:
        monkeypatch.delenv(env_var, raising=False)

    fake_store = MagicMock()
    fake_store.propose.return_value = 1

    with patch("scripts.migrate_docs_to_db.OverrideStore.from_env", return_value=fake_store), \
         patch("scripts.migrate_docs_to_db.fetch_google_doc_markdown", return_value="live content"):
        results = migrate(actor="ops@example.com")

    customer_result = next(r for r in results if r["prompt_id"] == "customer.system")
    assert customer_result["status"] == "published"
    assert customer_result["version"] == 1
    fake_store.propose.assert_called_once_with(
        "customer.system", "live content",
        note="Migrated from live Google Doc DOC1",
        actor="ops@example.com", via="api",
    )
    fake_store.publish.assert_called_once_with("customer.system", 1, actor="ops@example.com")


def test_migrate_reports_empty_doc_without_writing(monkeypatch):
    monkeypatch.setenv("CUSTOMER_SUPPORT_DOC_ID", "DOC1")
    for env_var in ["STAFF_SUPPORT_DOC_ID", "EXPERT_INSTRUCTIONS_DOC_ID",
                     "TROUBLESHOOTING_PROCEDURES_DOC_ID", "VERIFICATION_DOC_ID"]:
        monkeypatch.delenv(env_var, raising=False)

    fake_store = MagicMock()
    with patch("scripts.migrate_docs_to_db.OverrideStore.from_env", return_value=fake_store), \
         patch("scripts.migrate_docs_to_db.fetch_google_doc_markdown", return_value=""):
        results = migrate(actor="ops@example.com")

    customer_result = next(r for r in results if r["prompt_id"] == "customer.system")
    assert customer_result["status"] == "empty_doc"
    fake_store.propose.assert_not_called()


def test_migrate_reports_fetch_failure_without_writing(monkeypatch):
    monkeypatch.setenv("CUSTOMER_SUPPORT_DOC_ID", "DOC1")
    for env_var in ["STAFF_SUPPORT_DOC_ID", "EXPERT_INSTRUCTIONS_DOC_ID",
                     "TROUBLESHOOTING_PROCEDURES_DOC_ID", "VERIFICATION_DOC_ID"]:
        monkeypatch.delenv(env_var, raising=False)

    fake_store = MagicMock()

    def boom(doc_id):
        raise RuntimeError("network error")

    with patch("scripts.migrate_docs_to_db.OverrideStore.from_env", return_value=fake_store), \
         patch("scripts.migrate_docs_to_db.fetch_google_doc_markdown", side_effect=boom):
        results = migrate(actor="ops@example.com")

    customer_result = next(r for r in results if r["prompt_id"] == "customer.system")
    assert customer_result["status"] == "fetch_failed"
    fake_store.propose.assert_not_called()


def test_migrate_exits_when_storage_not_configured(monkeypatch):
    import pytest

    monkeypatch.setenv("CUSTOMER_SUPPORT_DOC_ID", "DOC1")
    fake_store = MagicMock()
    fake_store.is_configured.return_value = False
    with patch("scripts.migrate_docs_to_db.OverrideStore.from_env", return_value=fake_store):
        with pytest.raises(SystemExit):
            migrate(actor="ops@example.com")
