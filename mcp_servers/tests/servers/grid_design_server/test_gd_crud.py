"""Unit tests for the generic gd_* table registry and read tools (gd_crud.py).

Covers: registry completeness against schema.sql, gd_describe_tables, and the
access-control branching of gd_list_rows/gd_get_row for each scope (denied,
grid, catalog), including the multi-hop grid-anchor walk and the
not-found/access-denied error-shape parity that avoids an existence oracle.

Mocking follows test_backend_dispatch.py's convention: rather than importing
`gd_auth` a second time at module scope (which can resolve to a distinct
module object from the one `gd_crud.py` itself imported, since the codebase
is reachable under both the `servers.*` and `mcp_servers.servers.*` dotted
paths), we always go through `gd_crud.gd_auth` — the exact module object
`gd_crud`'s internals reference — for both patching and exception classes.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers.servers.grid_design_server import gd_crud
from shared.auth.auth_service import STAFF_ORG_ID

CUSTOMER_ORG_ID = 42

SCHEMA_PATH = Path(__file__).resolve().parents[4] / "anansi_app" / "db" / "schema.sql"

EXPECTED_SCOPES = {
    "organizations": "denied",
    "users": "denied",
    "grids": "grid",
    "grid_coords": "grid",
    "components": "catalog",
    "subassemblies": "catalog",
    "subassembly_components": "catalog",
    "design_rules": "catalog",
    "designs": "grid",
    "design_subassemblies": "grid",
    "bom_items": "grid",
    "procedures": "catalog",
    "procedure_steps": "catalog",
    "jobs": "grid",
    "job_procedures": "grid",
    "job_steps": "grid",
    "job_subassemblies": "grid",
    "purchases": "catalog",
    "unit_rental_prices": "catalog",
    "wp_per_conn_lookup": "catalog",
}


def _patch_repository(repos: dict[str, MagicMock]):
    """Patch gd_crud.Repository so Repository(table) returns repos[table]."""
    return patch.object(gd_crud, "Repository", side_effect=lambda t: repos[t])


# ── Registry completeness (the "lint") ───────────────────────────────────────


def test_registry_covers_every_table_in_schema_sql():
    content = SCHEMA_PATH.read_text()
    table_names = re.findall(r"CREATE TABLE IF NOT EXISTS gd_(\w+)", content)
    assert table_names, "no gd_* tables found in schema.sql — regex likely broken"
    missing = [t for t in table_names if t not in gd_crud.GD_TABLE_REGISTRY]
    assert not missing, f"schema.sql tables missing from GD_TABLE_REGISTRY: {missing}"

    extra = [t for t in gd_crud.GD_TABLE_REGISTRY if t not in table_names]
    assert not extra, f"GD_TABLE_REGISTRY has entries with no matching schema.sql table: {extra}"


def test_registry_has_exactly_twenty_tables():
    assert len(gd_crud.GD_TABLE_REGISTRY) == 20


# ── gd_describe_tables ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_describe_tables_returns_all_tables_with_correct_scopes():
    result = await gd_crud.gd_describe_tables()
    tables = {t["table"]: t for t in result["tables"]}

    assert set(tables) == set(EXPECTED_SCOPES)
    for table, expected_scope in EXPECTED_SCOPES.items():
        assert tables[table]["scope"] == expected_scope, table
        assert isinstance(tables[table]["description"], str) and tables[table]["description"]
        assert isinstance(tables[table]["writable_columns"], list)
        assert isinstance(tables[table]["grid_anchor"], list)

    # Denied tables carry no writable columns / anchors.
    assert tables["organizations"]["writable_columns"] == []
    assert tables["users"]["writable_columns"] == []

    # Multi-hop anchors are as specified.
    assert tables["design_subassemblies"]["grid_anchor"] == ["design", "grid"]
    assert tables["bom_items"]["grid_anchor"] == ["design_or_job", "grid"]
    assert tables["job_steps"]["grid_anchor"] == ["job", "grid"]
    assert tables["grids"]["grid_anchor"] == []


# ── gd_list_rows ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_rows_denied_table_errors_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_list_rows("users", organization_id=STAFF_ORG_ID)

    assert result["success"] is False
    assert "error" in result
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_list_rows_unknown_table_errors_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_list_rows("not_a_real_table", organization_id=STAFF_ORG_ID)

    assert result["success"] is False
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_list_rows_grid_scoped_without_grid_filter_errors_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_list_rows("designs", organization_id=CUSTOMER_ORG_ID)

    assert result["success"] is False
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_list_rows_grid_scoped_with_grid_id_filter_returns_allowed_rows():
    rows = [{"id": "des1", "grid": "g1", "name": "Design A"}]
    designs_repo = MagicMock()
    designs_repo.list.return_value = rows
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "ExampleGrid"}

    with (
        _patch_repository({"designs": designs_repo, "grids": grids_repo}),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        result = await gd_crud.gd_list_rows(
            "designs", organization_id=CUSTOMER_ORG_ID, filters={"grid": "g1"}
        )

    assert result["success"] is True
    assert result["rows"] == rows
    assert result["count"] == 1
    designs_repo.list.assert_called_once()


@pytest.mark.asyncio
async def test_list_rows_resolves_grid_name_filter_to_id():
    grids_repo = MagicMock()
    grids_repo.list.return_value = [{"id": "g1", "name": "ExampleGrid"}]
    grids_repo.get.return_value = {"id": "g1", "name": "ExampleGrid"}
    designs_repo = MagicMock()
    designs_repo.list.return_value = [{"id": "des1", "grid": "g1"}]

    with (
        _patch_repository({"designs": designs_repo, "grids": grids_repo}),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        result = await gd_crud.gd_list_rows(
            "designs", organization_id=CUSTOMER_ORG_ID, filters={"grid_name": "ExampleGrid"}
        )

    assert result["success"] is True
    assert result["count"] == 1
    _, kwargs = designs_repo.list.call_args
    assert kwargs["filters"] == {"grid": "g1"}
    # "grid_name" is never forwarded to Repository as a raw filter key.
    assert "grid_name" not in kwargs["filters"]


@pytest.mark.asyncio
async def test_list_rows_unresolvable_grid_name_errors():
    grids_repo = MagicMock()
    grids_repo.list.return_value = []

    with _patch_repository({"grids": grids_repo}):
        result = await gd_crud.gd_list_rows(
            "designs", organization_id=CUSTOMER_ORG_ID, filters={"grid_name": "NoSuchGrid"}
        )

    assert result["success"] is False


@pytest.mark.asyncio
async def test_list_rows_grid_scoped_access_denied_rows_are_filtered_out_not_raised():
    rows = [{"id": "des1", "grid": "g1", "name": "Design A"}]
    designs_repo = MagicMock()
    designs_repo.list.return_value = rows
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "OtherOrgGrid"}

    denied = gd_crud.gd_auth.GridAccessDenied("nope")
    with (
        _patch_repository({"designs": designs_repo, "grids": grids_repo}),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(side_effect=denied)),
    ):
        result = await gd_crud.gd_list_rows(
            "designs", organization_id=CUSTOMER_ORG_ID, filters={"grid": "g1"}
        )

    assert result["success"] is True
    assert result["rows"] == []
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_list_rows_catalog_table_non_staff_org_denied_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_list_rows("components", organization_id=CUSTOMER_ORG_ID)

    assert result["success"] is False
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_list_rows_catalog_table_staff_org_bypass_returns_rows():
    rows = [{"id": "c1", "name": "Comp"}]
    components_repo = MagicMock()
    components_repo.list.return_value = rows

    with _patch_repository({"components": components_repo}):
        result = await gd_crud.gd_list_rows("components", organization_id=STAFF_ORG_ID)

    assert result["success"] is True
    assert result["rows"] == rows
    assert result["count"] == 1


# ── gd_get_row ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_row_denied_table_errors_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_get_row("organizations", "org1", organization_id=STAFF_ORG_ID)

    assert result["success"] is False
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_get_row_catalog_table_non_staff_denied_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_get_row("components", "c1", organization_id=CUSTOMER_ORG_ID)

    assert result["success"] is False
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_get_row_catalog_table_staff_bypass_returns_row():
    components_repo = MagicMock()
    components_repo.get.return_value = {"id": "c1", "name": "Comp"}

    with _patch_repository({"components": components_repo}):
        result = await gd_crud.gd_get_row("components", "c1", organization_id=STAFF_ORG_ID)

    assert result["success"] is True
    assert result["row"]["id"] == "c1"


@pytest.mark.asyncio
async def test_get_row_not_found_and_access_denied_produce_identical_error_shape():
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "OtherOrgGrid"}

    # Case A: row_id doesn't exist at all.
    designs_repo_missing = MagicMock()
    designs_repo_missing.get.return_value = None
    with _patch_repository({"designs": designs_repo_missing, "grids": grids_repo}):
        result_missing = await gd_crud.gd_get_row(
            "designs", "d-shared-id", organization_id=CUSTOMER_ORG_ID
        )

    # Case B: row_id exists but belongs to a grid this org can't access.
    designs_repo_denied = MagicMock()
    designs_repo_denied.get.return_value = {"id": "d-shared-id", "grid": "g1"}
    denied = gd_crud.gd_auth.GridAccessDenied("nope")
    with (
        _patch_repository({"designs": designs_repo_denied, "grids": grids_repo}),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(side_effect=denied)),
    ):
        result_denied = await gd_crud.gd_get_row(
            "designs", "d-shared-id", organization_id=CUSTOMER_ORG_ID
        )

    assert result_missing["success"] is False
    assert result_denied["success"] is False
    assert result_missing["error"] == result_denied["error"]
    assert "OtherOrgGrid" not in result_denied["error"]


@pytest.mark.asyncio
async def test_get_row_grid_scoped_allowed_returns_row():
    row = {"id": "des1", "grid": "g1", "name": "Design A"}
    designs_repo = MagicMock()
    designs_repo.get.return_value = row
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "ExampleGrid"}

    with (
        _patch_repository({"designs": designs_repo, "grids": grids_repo}),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        result = await gd_crud.gd_get_row("designs", "des1", organization_id=CUSTOMER_ORG_ID)

    assert result["success"] is True
    assert result["row"] == row


# ── _resolve_grid_for_row: multi-hop anchor walking ─────────────────────────


def test_resolve_grid_for_row_grids_table_uses_own_name():
    policy = gd_crud.GD_TABLE_REGISTRY["grids"]
    row = {"id": "g1", "name": "ExampleGrid"}
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        name = gd_crud._resolve_grid_for_row(policy, row)
    assert name == "ExampleGrid"
    mock_repo_cls.assert_not_called()


def test_resolve_grid_for_row_single_hop_designs():
    policy = gd_crud.GD_TABLE_REGISTRY["designs"]
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "ExampleGrid"}
    row = {"id": "des1", "grid": "g1"}

    with _patch_repository({"grids": grids_repo}):
        name = gd_crud._resolve_grid_for_row(policy, row)

    assert name == "ExampleGrid"
    grids_repo.get.assert_called_once_with("g1")


def test_resolve_grid_for_row_design_subassemblies_walks_design_then_grid():
    policy = gd_crud.GD_TABLE_REGISTRY["design_subassemblies"]
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1", "grid": "g1"}
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "ExampleGrid"}
    row = {"id": "ds1", "design": "d1"}

    with _patch_repository({"designs": designs_repo, "grids": grids_repo}):
        name = gd_crud._resolve_grid_for_row(policy, row)

    assert name == "ExampleGrid"
    designs_repo.get.assert_called_once_with("d1")
    grids_repo.get.assert_called_once_with("g1")


def test_resolve_grid_for_row_bom_items_design_set_uses_design_hop():
    policy = gd_crud.GD_TABLE_REGISTRY["bom_items"]
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1", "grid": "g1"}
    jobs_repo = MagicMock()
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "ExampleGrid"}
    row = {"id": "bom1", "design": "d1", "job": None}

    with _patch_repository({"designs": designs_repo, "jobs": jobs_repo, "grids": grids_repo}):
        name = gd_crud._resolve_grid_for_row(policy, row)

    assert name == "ExampleGrid"
    designs_repo.get.assert_called_once_with("d1")
    jobs_repo.get.assert_not_called()


def test_resolve_grid_for_row_bom_items_job_set_uses_job_hop():
    policy = gd_crud.GD_TABLE_REGISTRY["bom_items"]
    jobs_repo = MagicMock()
    jobs_repo.get.return_value = {"id": "j1", "grid": "g1"}
    designs_repo = MagicMock()
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "ExampleGrid"}
    row = {"id": "bom1", "design": None, "job": "j1"}

    with _patch_repository({"designs": designs_repo, "jobs": jobs_repo, "grids": grids_repo}):
        name = gd_crud._resolve_grid_for_row(policy, row)

    assert name == "ExampleGrid"
    jobs_repo.get.assert_called_once_with("j1")
    designs_repo.get.assert_not_called()


def test_resolve_grid_for_row_bom_items_neither_set_returns_none():
    policy = gd_crud.GD_TABLE_REGISTRY["bom_items"]
    row = {"id": "bom1", "design": None, "job": None}
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        name = gd_crud._resolve_grid_for_row(policy, row)
    assert name is None
    mock_repo_cls.assert_not_called()


def test_resolve_grid_for_row_missing_intermediate_row_returns_none():
    policy = gd_crud.GD_TABLE_REGISTRY["design_subassemblies"]
    designs_repo = MagicMock()
    designs_repo.get.return_value = None  # design row vanished
    row = {"id": "ds1", "design": "d1"}

    with _patch_repository({"designs": designs_repo}):
        name = gd_crud._resolve_grid_for_row(policy, row)

    assert name is None


# ── gd_upsert_row: column whitelisting ───────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_row_unknown_table_errors_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_upsert_row(
            "not_a_real_table", organization_id=STAFF_ORG_ID, user_email="staff@x.com"
        )

    assert result["success"] is False
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_row_denied_table_errors_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_upsert_row(
            "users", organization_id=STAFF_ORG_ID, user_email="staff@x.com", values={"name": "x"}
        )

    assert result["success"] is False
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_row_unknown_column_errors_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_upsert_row(
            "designs",
            organization_id=CUSTOMER_ORG_ID,
            user_email="user@x.com",
            values={"grid": "g1", "bogus_column": 123},
        )

    assert result["success"] is False
    assert "bogus_column" in result["error"]
    assert "Allowed columns" in result["error"]
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_row_rejects_artifacts_column_on_designs():
    """Regression test: `artifacts` is Phase B's versioned-history column,
    exclusively managed by shared/grid_design/artifact_log.py. A generic
    upsert must never be able to overwrite it."""
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_upsert_row(
            "designs",
            organization_id=CUSTOMER_ORG_ID,
            user_email="user@x.com",
            values={"grid": "g1", "artifacts": [{"fake": "history"}]},
        )

    assert result["success"] is False
    assert "artifacts" in result["error"]
    mock_repo_cls.assert_not_called()
    assert "artifacts" not in gd_crud.GD_TABLE_REGISTRY["designs"].writable_columns


# ── gd_upsert_row: CREATE on a grid-scoped table ─────────────────────────────


@pytest.mark.asyncio
async def test_upsert_row_create_grid_scoped_success():
    design_subassemblies_repo = MagicMock()
    design_subassemblies_repo.insert.side_effect = lambda row: {**row}
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1", "grid": "g1"}
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "ExampleGrid"}

    with (
        _patch_repository(
            {
                "design_subassemblies": design_subassemblies_repo,
                "designs": designs_repo,
                "grids": grids_repo,
            }
        ),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        result = await gd_crud.gd_upsert_row(
            "design_subassemblies",
            organization_id=CUSTOMER_ORG_ID,
            user_email="user@x.com",
            values={"design": "d1", "subassembly": "sub1", "qty": 2},
        )

    assert result["success"] is True
    created = result["created"]
    assert created["design"] == "d1"
    assert created["subassembly"] == "sub1"
    assert isinstance(created["id"], str) and created["id"]
    # design_subassemblies has no audit_columns — nothing stamped.
    assert "created_by" not in created
    design_subassemblies_repo.insert.assert_called_once()


@pytest.mark.asyncio
async def test_upsert_row_create_grid_scoped_missing_anchor_errors_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_upsert_row(
            "design_subassemblies",
            organization_id=CUSTOMER_ORG_ID,
            user_email="user@x.com",
            values={"subassembly": "sub1", "qty": 2},  # no "design" key
        )

    assert result["success"] is False
    assert "design" in result["error"]
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_row_create_stamps_created_by_and_drops_caller_supplied_value():
    jobs_repo = MagicMock()
    jobs_repo.insert.side_effect = lambda row: {**row}
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "ExampleGrid"}

    with (
        _patch_repository({"jobs": jobs_repo, "grids": grids_repo}),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        result = await gd_crud.gd_upsert_row(
            "jobs",
            organization_id=CUSTOMER_ORG_ID,
            user_email="real@x.com",
            values={"grid": "g1", "type": "install", "created_by": "sneaky@x.com"},
        )

    assert result["success"] is True
    created = result["created"]
    assert created["created_by"] == "real@x.com"
    jobs_repo.insert.assert_called_once()


# ── gd_upsert_row: UPDATE on a grid-scoped table ─────────────────────────────


@pytest.mark.asyncio
async def test_upsert_row_update_grid_scoped_success_stamps_updated_by():
    existing = {"id": "js1", "job": "j1", "name": "Old Name"}
    job_steps_repo = MagicMock()
    job_steps_repo.get.return_value = existing
    job_steps_repo.update.side_effect = lambda row_id, changes: {**existing, **changes}
    jobs_repo = MagicMock()
    jobs_repo.get.return_value = {"id": "j1", "grid": "g1"}
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "ExampleGrid"}

    with (
        _patch_repository({"job_steps": job_steps_repo, "jobs": jobs_repo, "grids": grids_repo}),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        result = await gd_crud.gd_upsert_row(
            "job_steps",
            organization_id=CUSTOMER_ORG_ID,
            user_email="real@x.com",
            row_id="js1",
            values={"name": "New Name", "updated_by": "sneaky@x.com"},
        )

    assert result["success"] is True
    job_steps_repo.update.assert_called_once()
    _, changes = job_steps_repo.update.call_args[0]
    assert changes["name"] == "New Name"
    assert changes["updated_by"] == "real@x.com"


@pytest.mark.asyncio
async def test_upsert_row_update_moving_to_unowned_grid_is_denied():
    """Moving a design's `grid` column to a grid the caller can edit the
    CURRENT row for, but does not own, must be re-checked and denied — not
    just checked against the row's existing grid."""
    existing = {"id": "d1", "grid": "g1", "name": "Design A"}
    designs_repo = MagicMock()
    designs_repo.get.return_value = existing
    grids_repo = MagicMock()
    grids_repo.get.side_effect = lambda gid: {
        "g1": {"id": "g1", "name": "OwnedGrid"},
        "g2": {"id": "g2", "name": "OtherOrgGrid"},
    }[gid]

    denied = gd_crud.gd_auth.GridAccessDenied("nope")
    with (
        _patch_repository({"designs": designs_repo, "grids": grids_repo}),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(side_effect=[None, denied])),
    ):
        result = await gd_crud.gd_upsert_row(
            "designs",
            organization_id=CUSTOMER_ORG_ID,
            user_email="user@x.com",
            row_id="d1",
            values={"grid": "g2"},
        )

    assert result["success"] is False
    assert "OtherOrgGrid" not in result["error"]
    designs_repo.update.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_row_update_not_found_and_access_denied_share_error_shape():
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "OtherOrgGrid"}

    designs_repo_missing = MagicMock()
    designs_repo_missing.get.return_value = None
    with _patch_repository({"designs": designs_repo_missing, "grids": grids_repo}):
        result_missing = await gd_crud.gd_upsert_row(
            "designs",
            organization_id=CUSTOMER_ORG_ID,
            user_email="user@x.com",
            row_id="d-shared-id",
            values={"name": "New Name"},
        )

    designs_repo_denied = MagicMock()
    designs_repo_denied.get.return_value = {"id": "d-shared-id", "grid": "g1"}
    denied = gd_crud.gd_auth.GridAccessDenied("nope")
    with (
        _patch_repository({"designs": designs_repo_denied, "grids": grids_repo}),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(side_effect=denied)),
    ):
        result_denied = await gd_crud.gd_upsert_row(
            "designs",
            organization_id=CUSTOMER_ORG_ID,
            user_email="user@x.com",
            row_id="d-shared-id",
            values={"name": "New Name"},
        )

    assert result_missing["success"] is False
    assert result_denied["success"] is False
    assert result_missing["error"] == result_denied["error"]
    assert "OtherOrgGrid" not in result_denied["error"]


# ── gd_upsert_row: catalog scope ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_row_catalog_non_staff_denied_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_upsert_row(
            "components",
            organization_id=CUSTOMER_ORG_ID,
            user_email="user@x.com",
            values={"name": "Widget"},
        )

    assert result["success"] is False
    mock_repo_cls.assert_not_called()


# ── gd_upsert_row: subassembly_components one-of-child + cycle validation ──


@pytest.mark.asyncio
async def test_upsert_row_subassembly_components_rejects_both_set():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_upsert_row(
            "subassembly_components",
            organization_id=STAFF_ORG_ID,
            user_email="staff@x.com",
            values={"subassembly": "s1", "component": "c1", "component_subassembly": "cs1"},
        )

    assert result["success"] is False
    assert "exactly one" in result["error"]
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_row_subassembly_components_rejects_neither_set():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_upsert_row(
            "subassembly_components",
            organization_id=STAFF_ORG_ID,
            user_email="staff@x.com",
            values={"subassembly": "s1", "qty": 1},
        )

    assert result["success"] is False
    assert "exactly one" in result["error"]
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_row_subassembly_components_rejects_cycle():
    subassembly_components_repo = MagicMock()
    with (
        _patch_repository({"subassembly_components": subassembly_components_repo}),
        patch.object(gd_crud, "_would_create_cycle", return_value=True) as mock_cycle,
    ):
        result = await gd_crud.gd_upsert_row(
            "subassembly_components",
            organization_id=STAFF_ORG_ID,
            user_email="staff@x.com",
            values={"subassembly": "s1", "component_subassembly": "cs1"},
        )

    assert result["success"] is False
    assert "circular" in result["error"]
    mock_cycle.assert_called_once_with("s1", "cs1")
    subassembly_components_repo.insert.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_row_subassembly_components_accepts_valid_single_child():
    subassembly_components_repo = MagicMock()
    subassembly_components_repo.insert.side_effect = lambda row: {**row}

    with (
        _patch_repository({"subassembly_components": subassembly_components_repo}),
        patch.object(gd_crud, "_would_create_cycle", return_value=False) as mock_cycle,
    ):
        result = await gd_crud.gd_upsert_row(
            "subassembly_components",
            organization_id=STAFF_ORG_ID,
            user_email="staff@x.com",
            values={"subassembly": "s1", "component_subassembly": "cs1"},
        )

    assert result["success"] is True
    mock_cycle.assert_called_once_with("s1", "cs1")
    subassembly_components_repo.insert.assert_called_once()
    assert result["created"]["created_by"] == "staff@x.com"


# ── gd_upsert_row: bom_items design/job masking validation ──────────────────


@pytest.mark.asyncio
async def test_upsert_row_bom_items_rejects_cross_org_job_masking_bypass():
    """Regression test for the reviewed bypass: a caller who owns Grid1 via an
    existing bom_items row's `design` column submits an update that only sets
    `job` (leaving `design` untouched). Before the fix, `_resolve_grid_for_row`'s
    design_or_job sentinel always preferred the unchanged `design`, so the
    anchor-move re-auth saw the same grid on both sides and never inspected
    the new `job` value at all — silently planting an unauthorized
    cross-org job reference. The new one-of-design/job check on the EFFECTIVE
    (merged) row must reject this before any Repository write.
    """
    bom_items_repo = MagicMock()
    bom_items_repo.get.return_value = {"id": "b1", "design": "d1", "job": None}
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1", "grid": "g1"}
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "OwnedGrid"}

    with (
        _patch_repository(
            {"bom_items": bom_items_repo, "designs": designs_repo, "grids": grids_repo}
        ),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        result = await gd_crud.gd_upsert_row(
            "bom_items",
            organization_id=CUSTOMER_ORG_ID,
            user_email="user@x.com",
            row_id="b1",
            values={"job": "other-org-job-id"},
        )

    assert result["success"] is False
    assert "exactly one" in result["error"]
    bom_items_repo.update.assert_not_called()
    bom_items_repo.insert.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_row_bom_items_accepts_single_design_set():
    """Legitimate one-of-set case: creating a bom_items row with only `design`
    set (no `job`) must still succeed."""
    bom_items_repo = MagicMock()
    bom_items_repo.insert.side_effect = lambda row: {**row}
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1", "grid": "g1"}
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "OwnedGrid"}

    with (
        _patch_repository(
            {"bom_items": bom_items_repo, "designs": designs_repo, "grids": grids_repo}
        ),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        result = await gd_crud.gd_upsert_row(
            "bom_items",
            organization_id=CUSTOMER_ORG_ID,
            user_email="user@x.com",
            values={"item": "c1", "qty": 1, "design": "d1"},
        )

    assert result["success"] is True
    bom_items_repo.insert.assert_called_once()
    assert result["created"]["design"] == "d1"


def test_validate_and_upsert_sync_bom_items_rejects_neither_design_nor_job_set():
    """The both-unset case, tested directly against `_validate_and_upsert_sync`.

    A bare `gd_upsert_row` CREATE call for a grid-scoped table like bom_items
    with neither `design` nor `job` present would actually be rejected even
    earlier, by `gd_upsert_row`'s own "cannot determine which grid this new
    row belongs to" anchor-resolution guard (grid_anchor resolution fails
    before `_validate_and_upsert_sync` is ever called) — a different, but
    also correct, error message. This test isolates the new bom_items
    validation branch itself to prove it independently rejects the
    neither-set case with the one-of error, rather than relying on that
    earlier gate to happen to catch it.
    """
    policy = gd_crud.GD_TABLE_REGISTRY["bom_items"]

    with pytest.raises(ValueError, match="exactly one"):
        gd_crud._validate_and_upsert_sync(
            policy,
            "bom_items",
            None,
            {"item": "c1", "qty": 1},
            "user@x.com",
            None,
        )


@pytest.mark.asyncio
async def test_upsert_row_bom_items_design_to_job_reassignment_denied_via_anchor_move_reauth():
    """Legitimate design->job reassignment (clearing `design`, setting `job`)
    still passes the one-of-design/job check (exactly one truthy on the
    effective row) — but must still be denied when the job's grid isn't one
    the caller owns. Verifies denial happens via the EXISTING anchor-move
    re-auth path (a second `assert_grid_access` call against the NEW grid),
    not via the one-of validation.
    """
    bom_items_repo = MagicMock()
    bom_items_repo.get.return_value = {"id": "b1", "design": "d1", "job": None}
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1", "grid": "g1"}
    jobs_repo = MagicMock()
    jobs_repo.get.return_value = {"id": "job-other", "grid": "g2"}
    grids_repo = MagicMock()
    grids_repo.get.side_effect = lambda gid: {
        "g1": {"id": "g1", "name": "OwnedGrid"},
        "g2": {"id": "g2", "name": "OtherOrgGrid"},
    }[gid]

    denied = gd_crud.gd_auth.GridAccessDenied("nope")
    mock_assert = AsyncMock(side_effect=[None, denied])
    with (
        _patch_repository(
            {
                "bom_items": bom_items_repo,
                "designs": designs_repo,
                "jobs": jobs_repo,
                "grids": grids_repo,
            }
        ),
        patch.object(gd_crud.gd_auth, "assert_grid_access", mock_assert),
    ):
        result = await gd_crud.gd_upsert_row(
            "bom_items",
            organization_id=CUSTOMER_ORG_ID,
            user_email="user@x.com",
            row_id="b1",
            values={"design": None, "job": "job-other"},
        )

    assert result["success"] is False
    assert "OtherOrgGrid" not in result["error"]
    bom_items_repo.update.assert_not_called()
    assert mock_assert.call_count == 2
    assert mock_assert.call_args_list[0].args[0] == "OwnedGrid"
    assert mock_assert.call_args_list[1].args[0] == "OtherOrgGrid"


# ── gd_delete_row ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_row_unknown_table_errors_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_delete_row(
            "not_a_real_table", "row1", organization_id=STAFF_ORG_ID
        )

    assert result["success"] is False
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_delete_row_grid_scoped_calls_soft_delete_only():
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1", "grid": "g1"}
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "ExampleGrid"}

    with (
        _patch_repository({"designs": designs_repo, "grids": grids_repo}),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        result = await gd_crud.gd_delete_row("designs", "d1", organization_id=CUSTOMER_ORG_ID)

    assert result["success"] is True
    assert result["deleted_id"] == "d1"
    designs_repo.soft_delete.assert_called_once_with("d1")
    designs_repo.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_row_not_found_and_access_denied_share_error_shape():
    grids_repo = MagicMock()
    grids_repo.get.return_value = {"id": "g1", "name": "OtherOrgGrid"}

    designs_repo_missing = MagicMock()
    designs_repo_missing.get.return_value = None
    with _patch_repository({"designs": designs_repo_missing, "grids": grids_repo}):
        result_missing = await gd_crud.gd_delete_row(
            "designs", "d-shared-id", organization_id=CUSTOMER_ORG_ID
        )

    designs_repo_denied = MagicMock()
    designs_repo_denied.get.return_value = {"id": "d-shared-id", "grid": "g1"}
    denied = gd_crud.gd_auth.GridAccessDenied("nope")
    with (
        _patch_repository({"designs": designs_repo_denied, "grids": grids_repo}),
        patch.object(gd_crud.gd_auth, "assert_grid_access", AsyncMock(side_effect=denied)),
    ):
        result_denied = await gd_crud.gd_delete_row(
            "designs", "d-shared-id", organization_id=CUSTOMER_ORG_ID
        )

    assert result_missing["success"] is False
    assert result_denied["success"] is False
    assert result_missing["error"] == result_denied["error"]
    assert "OtherOrgGrid" not in result_denied["error"]
    designs_repo_missing.soft_delete.assert_not_called()
    designs_repo_denied.soft_delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_row_catalog_non_staff_denied_without_repository_call():
    with patch.object(gd_crud, "Repository") as mock_repo_cls:
        result = await gd_crud.gd_delete_row("components", "c1", organization_id=CUSTOMER_ORG_ID)

    assert result["success"] is False
    mock_repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_delete_row_catalog_non_staff_denial_is_identical_regardless_of_row_existence():
    """Existence-oracle regression test: a non-staff caller must get the exact
    same denial whether row_id exists or not, because the code short-circuits
    on the staff-org check before ever calling `Repository(table).get` — so
    the two cases are provably indistinguishable from the response alone.
    """
    existing_row_repo = MagicMock()
    existing_row_repo.get.return_value = {"id": "c1", "name": "Widget"}

    missing_row_repo = MagicMock()
    missing_row_repo.get.return_value = None

    with _patch_repository({"components": existing_row_repo}):
        result_existing = await gd_crud.gd_delete_row(
            "components", "c1", organization_id=CUSTOMER_ORG_ID
        )

    with _patch_repository({"components": missing_row_repo}):
        result_missing = await gd_crud.gd_delete_row(
            "components", "does-not-exist", organization_id=CUSTOMER_ORG_ID
        )

    assert result_existing["success"] is False
    assert result_missing["success"] is False
    assert result_existing["error"] == result_missing["error"]
    existing_row_repo.get.assert_not_called()
    missing_row_repo.get.assert_not_called()
    existing_row_repo.soft_delete.assert_not_called()
    missing_row_repo.soft_delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_row_catalog_staff_bypass_soft_deletes():
    components_repo = MagicMock()
    components_repo.get.return_value = {"id": "c1", "name": "Widget"}

    with _patch_repository({"components": components_repo}):
        result = await gd_crud.gd_delete_row("components", "c1", organization_id=STAFF_ORG_ID)

    assert result["success"] is True
    components_repo.soft_delete.assert_called_once_with("c1")
