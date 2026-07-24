"""Tests for db/migrations/0002_internal_ticket_ref_allocation.sql.

Lives under chat_orchestrator/tests/ (rather than next to the migration
file) so it's covered by CI's ``pytest tests/`` invocation for this
package -- see .github/scripts/check_test_wiring.py, which rejects any
tracked test_*.py file that isn't under a path a CI job actually runs.

Two kinds of coverage:

1. Static assertions on the migration file's SQL text (nextval() usage,
   the ref format, idempotent CREATE OR REPLACE) -- always run, no
   dependencies.
2. A live test against a real, throwaway local Postgres cluster that
   applies 0001 then 0002 and calls next_internal_ticket_ref() many times
   (sequentially, which is what a single Postgres backend can offer here --
   see note below) to assert refs are correctly formatted/prefixed and
   never collide. Skipped automatically if initdb/pg_ctl/psql/createdb
   aren't on PATH.

next_internal_ticket_ref() only wraps nextval('internal_ticket_seq') --
Postgres sequences are race-free under concurrency by construction (nextval()
atomically increments and returns a unique value per call), so there is
nothing left here for InternalTicketBackend to get wrong via a read-then-write
race; that's why this function no longer also performs the internal_tickets
insert (see db/migrations/0002_internal_ticket_ref_allocation.sql's header
comment for the full rationale).
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


def test_migration_declares_next_internal_ticket_ref_function():
    sql = MIGRATION_0002.read_text()

    assert "CREATE OR REPLACE FUNCTION next_internal_ticket_ref(" in sql, (
        "Expected an idempotent CREATE OR REPLACE FUNCTION next_internal_ticket_ref(...)"
    )
    assert "RETURNS text" in sql

    assert re.search(r"nextval\(\s*'internal_ticket_seq'\s*\)", sql), (
        "Expected nextval('internal_ticket_seq') inside the function body"
    )

    # Ref format: {prefix}-{nextval:06d}, e.g. 'TKT-000123'.
    assert "lpad(nextval('internal_ticket_seq')::text, 6, '0')" in sql

    # This function must NOT touch internal_tickets -- allocation only.
    assert "INSERT INTO internal_tickets" not in sql, (
        "next_internal_ticket_ref must not perform the internal_tickets insert -- "
        "that now happens in InternalTicketBackend.create_ticket() via a normal "
        ".table('internal_tickets').insert(...) call"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(
    not HAVE_POSTGRES,
    reason="initdb/pg_ctl/psql/createdb not found on PATH; cannot spin up a scratch Postgres",
)
def test_next_internal_ticket_ref_allocates_unique_sequential_refs():
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

                # 20 sequential calls (standing in for concurrent callers --
                # nextval() is atomic per-call regardless of caller count, so
                # sequential calls exercise the same guarantee a real
                # concurrent workload would rely on) must produce distinct,
                # correctly-formatted, monotonically increasing refs.
                refs = [
                    run_sql("SELECT next_internal_ticket_ref();") for _ in range(20)
                ]
                for ref in refs:
                    assert re.match(r"^TKT-\d{6}$", ref), f"unexpected ref format: {ref}"
                assert len(set(refs)) == len(refs), "refs must never collide"
                numbers = [int(ref.split("-")[1]) for ref in refs]
                assert numbers == sorted(numbers), "refs should be monotonically increasing"
                assert numbers == list(range(numbers[0], numbers[0] + 20))

                # Custom prefix is honored.
                custom_ref = run_sql("SELECT next_internal_ticket_ref('SUP');")
                assert custom_ref.startswith("SUP-")

                # The function must not touch internal_tickets -- confirm no
                # rows exist despite 21 allocations above.
                count = run_sql("SELECT count(*) FROM internal_tickets;")
                assert count == "0"
            finally:
                subprocess.run(
                    ["pg_ctl", "-D", data_dir, "-m", "fast", "stop"],
                    capture_output=True,
                )
