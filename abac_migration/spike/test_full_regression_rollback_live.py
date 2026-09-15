"""ROLLBACK-mode regression check, run AFTER test_full_regression_live.py's
FINALIZE step, against the same ril_full_regression_test.demo catalog - the
one mode not yet exercised live for MATERIALIZED_VIEW (or side-by-side with
STREAMING_TABLE/MANAGED). Rolls back the FINALIZE run_id printed by
test_full_regression_live.py, which touches all 9 tables at once, so this
is itself a full 3-table-type x 3-flavor regression of ROLLBACK specifically.

Confirms, for every one of the 9 tables:
- The ABAC policy/policies this utility created are dropped.
- The original legacy row filter/column mask is restored via the correct
  DDL keyword for that table_type (`ALTER TABLE` for MANAGED/STREAMING_TABLE,
  `ALTER MATERIALIZED VIEW` for MATERIALIZED_VIEW) - rollback() has no
  ConvertOptions to read table_type from, so it re-discovers it live via a
  fresh describe_table_security() call (see rls_to_abac.py/mask_to_abac.py).
- A live SELECT still returns correct data (same function, now enforced via
  the legacy mechanism instead of ABAC).
- Every rollback attempt is recorded in migration_audit with
  migration_phase='ROLLED_BACK' and no error_code.

Usage: run test_full_regression_live.py first, note its printed
`finalize run_id`, then pass it as FINALIZE_RUN_ID below (or edit inline).
"""
from __future__ import annotations

import sys

from abac_migration.config.models import Mode, RunConfig, ScopeType
from abac_migration.migration.migration_engine import run as run_migration
from abac_migration.spike._oauth_m2m import refresh as refresh_oauth_token
from abac_migration.uc_gateway.gateway import DatabricksUnityCatalogGateway
from abac_migration.uc_gateway.models import TableRef
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "ril_catalog_test_pat"
WAREHOUSE_ID = "5fe1692f119e2528"
CATALOG = "ril_full_regression_test"
SCHEMA = "demo"
AUDIT_CATALOG = CATALOG
AUDIT_SCHEMA = "governance_audit"

TABLES = [
    ("managed_rls_tbl", "MANAGED", True, []),
    ("managed_mask_tbl", "MANAGED", False, ["customer_email"]),
    ("managed_both_tbl", "MANAGED", True, ["customer_email"]),
    ("streaming_rls_tbl", "STREAMING_TABLE", True, []),
    ("streaming_mask_tbl", "STREAMING_TABLE", False, ["customer_email"]),
    ("streaming_both_tbl", "STREAMING_TABLE", True, ["customer_email"]),
    ("mv_rls_tbl", "MATERIALIZED_VIEW", True, []),
    ("mv_mask_tbl", "MATERIALIZED_VIEW", False, ["customer_email"]),
    ("mv_both_tbl", "MATERIALIZED_VIEW", True, ["customer_email"]),
]


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python -m abac_migration.spike.test_full_regression_rollback_live <finalize_run_id>")
    finalize_run_id = sys.argv[1]

    refresh_oauth_token()
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()
    uc = DatabricksUnityCatalogGateway(client)

    print("=" * 100); print(f"ROLLBACK of FINALIZE run_id={finalize_run_id}"); print("=" * 100)
    rollback_summary = run_migration(
        RunConfig(
            mode=Mode.ROLLBACK, run_id=finalize_run_id, scope_type=ScopeType.SELECTED_SCHEMAS,
            schemas={CATALOG: [SCHEMA]}, dry_run=False,
            audit_catalog=AUDIT_CATALOG, audit_schema=AUDIT_SCHEMA,
        ),
        uc,
    )
    for r in rollback_summary.other_results:
        print(f"  {r.table_name}: status={r.status.value} error={r.error_message}")
        assert r.status.value in ("ROLLED_BACK", "SKIPPED"), f"{r.table_name}: unexpected rollback status {r.status.value}: {r.error_message}"

    print("\nVerifying legacy security restored + ABAC policies removed, per table:")
    for name, table_type, has_rls, mask_cols in TABLES:
        table_ref = TableRef(CATALOG, SCHEMA, name)
        state = uc.describe_table_security(table_ref)
        print(f"  {name} ({table_type}): has_row_filter={state.has_row_filter} column_masks={[m.column for m in state.column_masks]}")
        assert state.has_row_filter == has_rls, f"{name}: expected legacy row filter restored={has_rls}, got {state.has_row_filter}"
        assert sorted(m.column for m in state.column_masks) == sorted(mask_cols), (
            f"{name}: expected legacy masks restored={mask_cols}, got {[m.column for m in state.column_masks]}"
        )

        r = client.run(f"SHOW POLICIES ON TABLE {CATALOG}.{SCHEMA}.{name}")
        policy_names = [row[0] for row in r.rows]
        assert not policy_names, f"{name}: expected 0 ABAC policies after rollback, found {policy_names}"

    print("\nLive SELECT re-check (legacy-enforced now, should be identical data to before):")
    checks = [
        ("managed_rls_tbl", "SELECT id, region, customer_name, amount FROM {}.{}.managed_rls_tbl ORDER BY id"),
        ("managed_mask_tbl", "SELECT id, customer_name, customer_email, amount FROM {}.{}.managed_mask_tbl ORDER BY id"),
        ("managed_both_tbl", "SELECT id, region, customer_email, amount FROM {}.{}.managed_both_tbl ORDER BY id"),
        ("streaming_rls_tbl", "SELECT id, region, customer_name, amount FROM {}.{}.streaming_rls_tbl ORDER BY id"),
        ("streaming_mask_tbl", "SELECT id, customer_name, customer_email, amount FROM {}.{}.streaming_mask_tbl ORDER BY id"),
        ("streaming_both_tbl", "SELECT id, region, customer_email, amount FROM {}.{}.streaming_both_tbl ORDER BY id"),
        ("mv_rls_tbl", "SELECT id, region, customer_name, amount FROM {}.{}.mv_rls_tbl ORDER BY id"),
        ("mv_mask_tbl", "SELECT id, customer_name, customer_email, amount FROM {}.{}.mv_mask_tbl ORDER BY id"),
        ("mv_both_tbl", "SELECT id, region, customer_email, amount FROM {}.{}.mv_both_tbl ORDER BY id"),
    ]
    for name, sql_tmpl in checks:
        r = client.run(sql_tmpl.format(CATALOG, SCHEMA))
        print(f"  {name}: status={r.status} error={r.error_code} {r.error}")
        assert r.status == "SUCCEEDED", f"{name} SELECT failed after rollback: {r.error_code} {r.error}"
        for row in r.rows:
            print("    ", row)

    print("\nmigration_audit ROLLED_BACK rows:")
    audit = client.run(
        f"SELECT table, migration_phase, object_type, masked_column, status, error_code "
        f"FROM {AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit "
        f"WHERE migration_phase = 'ROLLED_BACK' ORDER BY table, object_type, masked_column"
    )
    for row in audit.rows:
        print("  ", row)
    failures = [row for row in audit.rows if row[5] is not None]
    assert not failures, f"Unexpected error_code(s) in ROLLED_BACK audit rows: {failures}"

    print("\nALL CHECKS PASSED for ROLLBACK across all 9 tables (MANAGED + STREAMING_TABLE + MATERIALIZED_VIEW).")
    print(f"rollback (ROLLBACK mode) run_id: {rollback_summary.run_id}")


if __name__ == "__main__":
    main()
