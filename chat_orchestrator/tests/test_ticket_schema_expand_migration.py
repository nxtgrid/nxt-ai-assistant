"""Contract tests for the canonical ticket schema expansion migration."""

from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "db" / "migrations" / "0005a_ticket_schema_expand_and_backfill.sql"
HAVE_POSTGRES = all(shutil.which(binary) for binary in ("initdb", "pg_ctl", "psql", "createdb"))


SEED_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE chat_sessions (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), session_id text UNIQUE NOT NULL);
CREATE TABLE chat_threads (thread_id text PRIMARY KEY);
CREATE TABLE chat_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid REFERENCES chat_sessions(id),
    role text NOT NULL DEFAULT 'user',
    content text,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT now(),
    message_index integer NOT NULL DEFAULT 0
);
CREATE TABLE escalation_mappings (id uuid PRIMARY KEY DEFAULT gen_random_uuid());
CREATE TABLE internal_tickets (ticket_ref text PRIMARY KEY);
CREATE TABLE internal_ticket_comments (id uuid PRIMARY KEY DEFAULT gen_random_uuid());
CREATE TABLE ticket_correlations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    affected_keys jsonb NOT NULL DEFAULT '[]'::jsonb,
    occurrence_count integer NOT NULL DEFAULT 1,
    last_alert_at timestamptz DEFAULT now()
);
CREATE TABLE ticket_correlation_events (id uuid PRIMARY KEY DEFAULT gen_random_uuid());
"""


def test_expand_migration_exists_at_the_canonical_path():
    assert MIGRATION.is_file(), f"Expected canonical-ticket migration at {MIGRATION}"


def test_expand_migration_is_transactional_and_non_destructive():
    sql = MIGRATION.read_text()

    assert sql.lstrip().startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")

    for relation in ("tickets", "escalations", "ticket_comments", "message_deliveries"):
        assert f"CREATE TABLE IF NOT EXISTS {relation}" in sql

    assert "CREATE OR REPLACE VIEW ticket_list_view" in sql
    assert "tickets_ticket_ref_unique" in sql
    assert "tickets_active_requires_backend_ref" in sql
    assert "message_deliveries_owner_required" in sql
    assert "message_deliveries_external_identity_unique" in sql
    assert "sync_legacy_internal_ticket" in sql
    assert "sync_legacy_escalation" in sql
    assert "trg_legacy_internal_ticket_to_ticket" in sql
    assert "trg_legacy_escalation_to_escalation" in sql

    assert "DROP TABLE internal_tickets" not in sql
    assert "DROP TABLE escalation_mappings" not in sql
    assert "DROP COLUMN ticket_ref" not in sql


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(not HAVE_POSTGRES, reason="local PostgreSQL binaries are unavailable")
def test_expand_migration_creates_the_canonical_relations_idempotently():
    with tempfile.TemporaryDirectory(prefix="anansi_ticket_expand_") as data_dir:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="anansi_ticket_sock_") as sock_dir:
            port = _free_port()
            subprocess.run(
                ["initdb", "-D", data_dir, "--no-locale", "--encoding=UTF8"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "pg_ctl",
                    "-D",
                    data_dir,
                    "-o",
                    f"-p {port} -k {sock_dir} -c listen_addresses=''",
                    "-l",
                    str(Path(data_dir) / "postgres.log"),
                    "-w",
                    "start",
                ],
                check=True,
                capture_output=True,
            )
            try:
                psql = ["psql", "-h", sock_dir, "-p", str(port), "-v", "ON_ERROR_STOP=1"]
                subprocess.run(
                    ["createdb", "-h", sock_dir, "-p", str(port), "ticketexpand"],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    [*psql, "-d", "ticketexpand", "-c", SEED_SQL],
                    check=True,
                    capture_output=True,
                    text=True,
                )

                def scalar(sql: str) -> str:
                    result = subprocess.run(
                        [*psql, "-d", "ticketexpand", "-t", "-A", "-c", sql],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    return result.stdout.strip()

                def apply() -> None:
                    subprocess.run(
                        [*psql, "-d", "ticketexpand", "-f", str(MIGRATION)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )

                apply()
                apply()

                assert scalar("SELECT to_regclass('public.tickets')") == "tickets"
                assert scalar("SELECT to_regclass('public.escalations')") == "escalations"
                assert scalar("SELECT to_regclass('public.ticket_comments')") == "ticket_comments"
                assert scalar("SELECT to_regclass('public.message_deliveries')") == "message_deliveries"
                assert scalar("SELECT to_regclass('public.ticket_list_view')") == "ticket_list_view"
                assert scalar("SELECT to_regclass('public.internal_tickets')") == "internal_tickets"
                assert scalar("SELECT to_regclass('public.escalation_mappings')") == "escalation_mappings"
                assert scalar(
                    "SELECT count(*) FROM pg_indexes WHERE indexname = 'tickets_ticket_ref_unique'"
                ) == "1"
                assert scalar(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'tickets_active_requires_backend_ref'"
                ) == "1"
                assert scalar(
                    "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_tickets_updated_at'"
                ) == "1"
            finally:
                subprocess.run(["pg_ctl", "-D", data_dir, "-m", "fast", "stop"], capture_output=True)
