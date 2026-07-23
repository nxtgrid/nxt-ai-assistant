"""Tests for db/migrations/0002_internal_ticket_ref_allocation.sql.

Lives under chat_orchestrator/tests/ (rather than next to the migration
file) so it's covered by CI's ``pytest tests/`` invocation for this
package -- see .github/scripts/check_test_wiring.py, which rejects any
tracked test_*.py file that isn't under a path a CI job actually runs.

Two kinds of coverage, mirroring test_ticket_backend_migration.py:

1. Static assertions on the migration file's SQL text (nextval() usage,
   atomic insert-in-the-same-statement, idempotent CREATE OR REPLACE) --
   always run, no dependencies.
2. A live test against a real, throwaway local Postgres cluster that
   applies 0001 then 0002 and calls create_internal_ticket() to assert refs
   are correctly formatted/prefixed, monotonically increasing, and that
   concurrent-looking sequential calls never collide -- i.e. there is no
   read-then-write window between allocating the ref and inserting the row.
   Skipped automatically if initdb/pg_ctl/psql/createdb aren't on PATH.
"""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

# This file lives at <repo_root>/chat_orchestrator/tests/, so the migrations
# it exercises are three parents up and back down into db/migrations/.
REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_0001 = REPO_ROOT / "db" / "migrations" / "0001_jira_optional_ticket_backend.sql"
MIGRATION_0002 = REPO_ROOT / "db" / "migrations" / "0002_internal_ticket_ref_allocation.sql"

HAVE_POSTGRES = all(
    shutil.which(binary) for binary in ("initdb", "pg_ctl", "psql", "createdb")
)


def test_migration_files_exist_at_resolved_paths():
    assert MIGRATION_0001.is_file(), f"Expected migration file at {MIGRATION_0001}"
    assert MIGRATION_0002.is_file(), f"Expected migration file at {MIGRATION_0002}"


def test_migration_declares_create_internal_ticket_function():
    sql = MIGRATION_0002.read_text()

    assert "CREATE OR REPLACE FUNCTION create_internal_ticket(" in sql, (
        "Expected an idempotent CREATE OR REPLACE FUNCTION create_internal_ticket(...)"
    )
    assert "RETURNS SETOF internal_tickets" in sql

    # The ref must be allocated (nextval) and the row inserted in the *same*
    # statement/transaction -- no separate read-then-write round trip that
    # could race between two concurrent callers.
    assert re.search(r"nextval\(\s*'internal_ticket_seq'\s*\)", sql), (
        "Expected nextval('internal_ticket_seq') inside the function body"
    )
    assert re.search(r"INSERT INTO internal_tickets\s*\(", sql, re.IGNORECASE), (
        "Expected an INSERT INTO internal_tickets inside the same function body as nextval()"
    )

    # Ref format: {prefix}-{nextval:06d}, e.g. 'TKT-000123'.
    assert "lpad(nextval('internal_ticket_seq')::text, 6, '0')" in sql


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(
    not HAVE_POSTGRES,
    reason="initdb/pg_ctl/psql/createdb not found on PATH; cannot spin up a scratch Postgres",
)
def test_create_internal_ticket_allocates_atomically_and_sequentially():
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

                # 0001 creates escalation_mappings-independent bits we need
                # (internal_tickets, internal_ticket_seq) -- but 0001 itself
                # ALTERs escalation_mappings, which doesn't exist in this
                # fresh scratch DB, so seed a minimal stand-in first (same
                # approach as test_ticket_backend_migration.py's SEED_SQL).
                run_sql(
                    "CREATE TABLE escalation_mappings (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
                    "jira_ticket_key text, ticket_ref text, ticket_backend text);"
                )
                run_file(MIGRATION_0001)
                run_file(MIGRATION_0002)

                # Re-applying 0002 must be a no-op (CREATE OR REPLACE FUNCTION).
                run_file(MIGRATION_0002)

                ref1 = run_sql(
                    "SELECT ticket_ref FROM create_internal_ticket(p_summary => 'first ticket');"
                )
                ref2 = run_sql(
                    "SELECT ticket_ref FROM create_internal_ticket(p_summary => 'second ticket');"
                )

                assert re.match(r"^TKT-\d{6}$", ref1), f"unexpected ref format: {ref1}"
                assert re.match(r"^TKT-\d{6}$", ref2), f"unexpected ref format: {ref2}"
                assert ref1 != ref2, "sequential calls must not collide on the same ref"
                assert int(ref2.split("-")[1]) == int(ref1.split("-")[1]) + 1, (
                    "refs should be monotonically increasing"
                )

                # Row was actually persisted with the returned ref and given summary.
                persisted_summary = run_sql(
                    f"SELECT summary FROM internal_tickets WHERE ticket_ref = '{ref1}';"
                )
                assert persisted_summary == "first ticket"

                # Custom prefix is honored.
                ref3 = run_sql(
                    "SELECT ticket_ref FROM create_internal_ticket("
                    "p_summary => 'third', p_prefix => 'SUP');"
                )
                assert ref3.startswith("SUP-")

                # Default status/source are applied by the table's own defaults.
                status_and_source = run_sql(
                    f"SELECT status || ',' || source FROM internal_tickets WHERE ticket_ref = '{ref1}';"
                )
                assert status_and_source == "open,escalation"
            finally:
                subprocess.run(
                    ["pg_ctl", "-D", data_dir, "-m", "fast", "stop"],
                    capture_output=True,
                )
