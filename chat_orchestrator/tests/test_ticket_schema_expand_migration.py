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


BACKFILL_SEED_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE chat_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id text UNIQUE NOT NULL
);
INSERT INTO chat_sessions (id, session_id) VALUES
    ('00000000-0000-0000-0000-000000000001', 'session-escalation');
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
CREATE TABLE escalation_mappings (
    id uuid PRIMARY KEY,
    session_id text NOT NULL,
    escalation_message_id bigint,
    customer_chat_id text,
    customer_topic_id text,
    customer_username text,
    customer_email text,
    org_hashtag text,
    reason text,
    action_type text,
    jira_ticket_key text,
    ticket_ref text,
    ticket_backend text,
    organization_id integer,
    escalation_topic_id integer,
    is_active boolean DEFAULT true,
    created_at timestamptz DEFAULT now(),
    resolved_at timestamptz,
    question_text text,
    thread_id text
);
CREATE TABLE internal_tickets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref text UNIQUE NOT NULL,
    escalation_mapping_id uuid,
    session_id text,
    organization_id integer,
    grid_name text,
    summary text NOT NULL,
    description text,
    status text NOT NULL DEFAULT 'open',
    assignee_email text,
    labels jsonb DEFAULT '[]'::jsonb,
    source text NOT NULL DEFAULT 'escalation',
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    resolved_at timestamptz
);
CREATE TABLE internal_ticket_comments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref text NOT NULL,
    author text,
    body text NOT NULL,
    is_public boolean DEFAULT false,
    source text DEFAULT 'staff',
    created_at timestamptz DEFAULT now()
);
CREATE TABLE ticket_correlations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref text UNIQUE NOT NULL,
    ticket_backend text,
    grid_name text NOT NULL,
    organization_id integer,
    root_cause_kind text,
    primary_signature text,
    signatures jsonb NOT NULL DEFAULT '[]'::jsonb,
    affected_keys jsonb NOT NULL DEFAULT '[]'::jsonb,
    summary_base text,
    summary_current text,
    description_base text,
    severity text,
    occurrence_count integer NOT NULL DEFAULT 1,
    escalated_at timestamptz,
    status text NOT NULL DEFAULT 'open',
    telegram_chat_id text,
    telegram_topic_id text,
    telegram_message_id bigint,
    last_alert_at timestamptz DEFAULT now(),
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);
CREATE TABLE ticket_correlation_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref text,
    grid_name text NOT NULL,
    source text,
    signature text,
    dedup_key text,
    decision text NOT NULL,
    decided_by text NOT NULL,
    confidence real,
    reason text,
    candidate_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    alert jsonb,
    llm_raw text,
    created_at timestamptz DEFAULT now()
);

INSERT INTO internal_tickets
    (ticket_ref, organization_id, grid_name, summary, description, status, labels, source)
VALUES
    ('TKT-000001', 10, 'Grid A', 'Escalation internal', 'internal escalation', 'open', '["a"]', 'escalation'),
    ('TKT-000002', 20, 'Grid B', 'Notification internal', 'internal notification', 'done', '[]', 'notify');
INSERT INTO internal_ticket_comments (ticket_ref, author, body, is_public, source)
VALUES ('TKT-000001', 'staff', 'Internal note', true, 'staff');
INSERT INTO escalation_mappings
    (id, session_id, escalation_message_id, customer_chat_id, jira_ticket_key, ticket_ref,
     ticket_backend, organization_id, question_text, created_at)
VALUES
    ('00000000-0000-0000-0000-000000000010', 'session-escalation', 91, '-100111', 'OPS-100',
     'OPS-100', 'jira', 10, 'Customer escalation', '2026-07-20T10:00:00Z');
INSERT INTO ticket_correlations
    (ticket_ref, ticket_backend, grid_name, organization_id, summary_base, summary_current,
     description_base, occurrence_count, telegram_chat_id, telegram_topic_id, telegram_message_id)
VALUES
    ('OPS-200', 'jira', 'Grid B', 20, 'Notification Jira', 'Notification Jira', 'notify', 2, '-100222', '7', 123),
    ('OPS-300', 'jira', 'Grid C', 30, 'Adopted Jira', 'Adopted Jira', 'adopted', 1, NULL, NULL, NULL),
    ('OPS-400', 'jira', 'Grid D', 40, 'Legacy Jira', 'Legacy Jira', 'legacy', 1, NULL, NULL, NULL);
INSERT INTO ticket_correlation_events (ticket_ref, grid_name, source, decision, decided_by, candidate_refs)
VALUES
    ('OPS-200', 'Grid B', 'notify', 'new', 'fallback', '[]'),
    ('OPS-300', 'Grid C', 'correlation', 'amend', 'signature', '["OPS-300"]');
INSERT INTO chat_messages (session_id, role, content, metadata, message_index)
VALUES ('00000000-0000-0000-0000-000000000001', 'user', 'Linked customer message',
        '{"ticket_ref":"TKT-000001","ticket_role":"comment"}', 1);
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
    assert "INSERT INTO escalations (" in sql
    assert "VALUES (" in sql

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
                    [*psql, "-d", "ticketexpand", "-c", BACKFILL_SEED_SQL],
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


@pytest.mark.skipif(not HAVE_POSTGRES, reason="local PostgreSQL binaries are unavailable")
def test_expand_migration_backfills_all_recoverable_ticket_origins():
    with tempfile.TemporaryDirectory(prefix="anansi_ticket_backfill_") as data_dir:
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
                    ["createdb", "-h", sock_dir, "-p", str(port), "ticketbackfill"],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    [*psql, "-d", "ticketbackfill", "-c", BACKFILL_SEED_SQL],
                    check=True,
                    capture_output=True,
                    text=True,
                )

                def scalar(sql: str) -> str:
                    result = subprocess.run(
                        [*psql, "-d", "ticketbackfill", "-t", "-A", "-c", sql],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    return result.stdout.strip()

                def apply() -> None:
                    subprocess.run(
                        [*psql, "-d", "ticketbackfill", "-f", str(MIGRATION)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )

                apply()
                apply()

                assert scalar("SELECT count(*) FROM tickets") == "6"
                assert scalar(
                    "SELECT string_agg(ticket_ref || ':' || created_via, ',' ORDER BY ticket_ref) FROM tickets"
                ) == (
                    "OPS-100:escalation,OPS-200:notification,OPS-300:adopted,OPS-400:legacy,"
                    "TKT-000001:escalation,TKT-000002:notification"
                )
                assert scalar(
                    "SELECT count(*) FROM tickets WHERE provisioning_state = 'active' "
                    "AND (ticket_ref IS NULL OR backend IS NULL OR activated_at IS NULL)"
                ) == "0"
                assert scalar("SELECT count(*) FROM escalations WHERE ticket_id IS NOT NULL") == "1"
                assert scalar("SELECT count(*) FROM ticket_comments") == "1"
                assert scalar("SELECT count(*) FROM chat_messages WHERE ticket_id IS NOT NULL") == "1"
                assert scalar("SELECT count(*) FROM ticket_correlations WHERE ticket_id IS NOT NULL") == "3"
                assert scalar("SELECT count(*) FROM ticket_correlation_events WHERE ticket_id IS NOT NULL") == "2"
                assert scalar("SELECT count(*) FROM message_deliveries") == "1"
            finally:
                subprocess.run(["pg_ctl", "-D", data_dir, "-m", "fast", "stop"], capture_output=True)
