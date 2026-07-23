"""Tests for db/migrations/0001_jira_optional_ticket_backend.sql.

Not wired into any existing pytest `testpaths` config (the repo-root
pyproject.toml points at a `tests/` directory that doesn't exist yet, and
chat_orchestrator's suite doesn't cover db/), so run explicitly:

    pytest db/migrations/test_0001_jira_optional_ticket_backend.py -v

Two kinds of coverage:

1. A static assertion on the migration file's backfill UPDATE statement —
   always runs, no dependencies.
2. A live test against a real, throwaway local Postgres cluster (spun up
   with initdb/pg_ctl, torn down after) that applies the migration to a
   stand-in for the pre-migration `escalation_mappings` table and asserts
   the backfill invariant holds, then re-applies the migration to prove
   it's idempotent. Skipped automatically if initdb/pg_ctl/psql aren't on
   PATH (e.g. in a CI image without Postgres installed).
"""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

MIGRATION_PATH = Path(__file__).parent / "0001_jira_optional_ticket_backend.sql"

HAVE_POSTGRES = all(
    shutil.which(binary) for binary in ("initdb", "pg_ctl", "psql", "createdb")
)

SEED_SQL = """
CREATE TABLE escalation_mappings (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              text NOT NULL,
    escalation_message_id   bigint NOT NULL,
    customer_chat_id        text NOT NULL,
    customer_topic_id       text,
    customer_username       text,
    customer_email          text,
    org_hashtag             text,
    reason                  text,
    action_type             text,
    jira_ticket_key         text,
    organization_id         integer,
    escalation_topic_id     integer,
    is_active               boolean DEFAULT true,
    created_at              timestamptz DEFAULT now(),
    resolved_at             timestamptz,
    question_text           text,
    thread_id               text
);

INSERT INTO escalation_mappings
    (session_id, escalation_message_id, customer_chat_id, jira_ticket_key, organization_id)
VALUES
    ('sess-1', 1001, 'chat-1', 'SUP-101', 1),
    ('sess-2', 1002, 'chat-2', 'SUP-202', 2),
    ('sess-3', 1003, 'chat-3', NULL, 3);
"""


def test_migration_file_backfills_ticket_ref_from_jira_ticket_key():
    """Static check: the migration's UPDATE statement mirrors jira_ticket_key
    into ticket_ref/ticket_backend, only for rows that have a Jira key and
    haven't been backfilled yet (idempotent)."""
    sql = MIGRATION_PATH.read_text()

    update_match = re.search(
        r"UPDATE\s+escalation_mappings\s+"
        r"SET\s+ticket_ref\s*=\s*jira_ticket_key\s*,\s*ticket_backend\s*=\s*'jira'\s+"
        r"WHERE\s+jira_ticket_key\s+IS\s+NOT\s+NULL\s+AND\s+ticket_ref\s+IS\s+NULL",
        sql,
        re.IGNORECASE,
    )
    assert update_match, (
        "Expected the backfill UPDATE (ticket_ref = jira_ticket_key, "
        "ticket_backend = 'jira' WHERE jira_ticket_key IS NOT NULL AND "
        "ticket_ref IS NULL) in db/migrations/0001_jira_optional_ticket_backend.sql"
    )

    # jira_ticket_key itself must not be dropped or renamed — the Jira inbound
    # webhook lookup (get_escalation_mapping_by_jira_key) still depends on it.
    assert "jira_ticket_key" in sql
    assert "DROP COLUMN" not in sql.upper()

    # New columns must be added defensively (safe to re-run against a
    # database that already has them, e.g. one bootstrapped from the full
    # chat_db.sql schema file).
    assert "ADD COLUMN IF NOT EXISTS ticket_ref" in sql
    assert "ADD COLUMN IF NOT EXISTS ticket_backend" in sql

    for table in ("internal_tickets", "internal_ticket_comments"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "CREATE SEQUENCE IF NOT EXISTS internal_ticket_seq" in sql


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(
    not HAVE_POSTGRES,
    reason="initdb/pg_ctl/psql/createdb not found on PATH; cannot spin up a scratch Postgres",
)
def test_migration_applies_cleanly_and_backfill_invariant_holds():
    with tempfile.TemporaryDirectory(prefix="anansi_pg_test_") as data_dir:
        # Unix socket paths have a ~103 byte limit; use a short /tmp dir
        # instead of the (long) pytest tmp path for the socket directory.
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="anansi_pg_sock_") as sock_dir:
            port = _free_port()
            subprocess.run(
                ["initdb", "-D", data_dir, "--no-locale", "--encoding=UTF8"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "pg_ctl", "-D", data_dir,
                    "-o", f"-p {port} -k {sock_dir} -c listen_addresses=''",
                    "-l", str(Path(data_dir) / "pg.log"),
                    "-w", "start",
                ],
                check=True,
                capture_output=True,
            )
            try:
                psql_base = ["psql", "-h", sock_dir, "-p", str(port), "-v", "ON_ERROR_STOP=1"]
                subprocess.run(
                    ["createdb", "-h", sock_dir, "-p", str(port), "migtest"],
                    check=True,
                    capture_output=True,
                )

                def run_sql(sql: str) -> str:
                    result = subprocess.run(
                        [*psql_base, "-d", "migtest", "-t", "-A", "-c", sql],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    return result.stdout.strip()

                def run_file(path: Path):
                    subprocess.run(
                        [*psql_base, "-d", "migtest", "-f", str(path)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )

                # Seed pre-migration state, then apply the migration.
                subprocess.run(
                    [*psql_base, "-d", "migtest", "-c", SEED_SQL],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                run_file(MIGRATION_PATH)

                # Invariant: every row with a jira_ticket_key got ticket_ref
                # mirrored and ticket_backend = 'jira'.
                mismatches = run_sql(
                    "SELECT count(*) FROM escalation_mappings "
                    "WHERE jira_ticket_key IS NOT NULL "
                    "AND (ticket_ref IS DISTINCT FROM jira_ticket_key "
                    "OR ticket_backend IS DISTINCT FROM 'jira');"
                )
                assert mismatches == "0", "backfill invariant violated for jira-backed rows"

                # Rows without a jira_ticket_key must be left untouched.
                untouched = run_sql(
                    "SELECT count(*) FROM escalation_mappings "
                    "WHERE jira_ticket_key IS NULL "
                    "AND (ticket_ref IS NOT NULL OR ticket_backend IS NOT NULL);"
                )
                assert untouched == "0", "rows without a jira key should not be backfilled"

                # Sanity: the two rows with a key really did get backfilled.
                backfilled = run_sql(
                    "SELECT count(*) FROM escalation_mappings WHERE ticket_backend = 'jira';"
                )
                assert backfilled == "2"

                # Re-applying the migration must be a no-op for the backfill
                # (idempotent) and must not error on the already-created
                # tables/columns/indexes.
                run_file(MIGRATION_PATH)
                second_run_updates = run_sql(
                    "SELECT count(*) FROM escalation_mappings "
                    "WHERE ticket_backend = 'jira' AND ticket_ref = jira_ticket_key;"
                )
                assert second_run_updates == "2"

                # New tables exist and enforce their constraints.
                run_sql(
                    "INSERT INTO internal_tickets (ticket_ref, summary) "
                    "VALUES ('TKT-000001', 'test ticket');"
                )
                with pytest.raises(subprocess.CalledProcessError):
                    run_sql(
                        "INSERT INTO internal_tickets (ticket_ref, summary, status) "
                        "VALUES ('TKT-BAD', 'bad status', 'closed');"
                    )
            finally:
                subprocess.run(
                    ["pg_ctl", "-D", data_dir, "-m", "fast", "stop"],
                    capture_output=True,
                )
