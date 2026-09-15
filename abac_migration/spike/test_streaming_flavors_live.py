"""Full live regression test for STREAMING_TABLE support (Track A) run
side-by-side against MANAGED tables, covering all 6 combinations of
{MANAGED, STREAMING_TABLE} x {RLS-only, mask-only, both} in
ril_streaming_flavors_test.demo (see setup_ril_streaming_flavors_test_catalog.py).

1. INVENTORY over the whole `demo` schema - asserts all 6 tables are
   discovered with the correct table_type + ELIGIBLE.
2. APPLY_ABAC - asserts every table reaches ABAC_APPLIED with the expected
   RLS/mask step statuses, and legacy is still present for every table.
3. Live SELECT against all 6 tables (as the seeded "data_admins" member,
   so RLS shows ALL rows - masks still apply since masks aren't
   group-gated) to confirm correct ABAC enforcement.
4. FINALIZE - asserts every table reaches SUCCESS and legacy is gone.
5. Live SELECT again to confirm ABAC-only enforcement is still correct.
6. Prints a full summary table + migration_audit rows.
"""
from __future__ import annotations

from abac_migration.config.models import Mode, RunConfig, ScopeType
from abac_migration.migration.migration_engine import run as run_migration
from abac_migration.spike._oauth_m2m import refresh as refresh_oauth_token
from abac_migration.uc_gateway.gateway import DatabricksUnityCatalogGateway
from abac_migration.uc_gateway.models import TableRef
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "ril_catalog_test_pat"
WAREHOUSE_ID = "5fe1692f119e2528"
CATALOG = "ril_streaming_flavors_test"
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
]


def section(title):
    print("\n" + "=" * 100); print(title); print("=" * 100)


def main():
    refresh_oauth_token()
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()
    uc = DatabricksUnityCatalogGateway(client)

    base = dict(
        scope_type=ScopeType.SELECTED_SCHEMAS,
        schemas={CATALOG: [SCHEMA]},
        dry_run=False,
        audit_catalog=AUDIT_CATALOG,
        audit_schema=AUDIT_SCHEMA,
    )

    # -- 1. INVENTORY -----------------------------------------------------
    section("STEP 1: INVENTORY over ril_streaming_flavors_test.demo")
    inv = run_migration(RunConfig(mode=Mode.INVENTORY, **base), uc)
    print(f"run_id={inv.run_id} tables_in_scope={inv.tables_in_scope} eligible={inv.tables_eligible}")
    r = client.run(
        f"SELECT table, table_type, has_row_filter, has_column_masks, migration_eligibility, eligibility_reason "
        f"FROM {AUDIT_CATALOG}.{AUDIT_SCHEMA}.inventory WHERE run_id = '{inv.run_id}' ORDER BY table"
    )
    inv_by_table = {row[0]: row for row in r.rows}
    for name, expected_type, has_rls, mask_cols in TABLES:
        row = inv_by_table.get(name)
        print(f"  {name}: {row}")
        assert row is not None, f"{name} missing from inventory"
        assert row[1] == expected_type, f"{name}: expected type {expected_type}, got {row[1]}"
        assert row[4] == "ELIGIBLE", f"{name}: expected ELIGIBLE, got {row[4]} ({row[5]})"
    print("CONFIRMED: all 6 tables (3 MANAGED + 3 STREAMING_TABLE) discovered correctly and ELIGIBLE.")

    # -- 2. APPLY_ABAC ------------------------------------------------------
    section("STEP 2: APPLY_ABAC")
    apply_summary = run_migration(RunConfig(mode=Mode.APPLY_ABAC, **base), uc)
    print(f"run_id={apply_summary.run_id} succeeded={apply_summary.tables_succeeded} "
          f"abac_applied={apply_summary.tables_abac_applied} failed={apply_summary.tables_failed}")
    by_name = {r.table_name.split(".")[-1]: r for r in apply_summary.conversion_results}
    for name, expected_type, has_rls, mask_cols in TABLES:
        result = by_name.get(name)
        masks = {k: v.value for k, v in result.column_mask_status.items()}
        print(f"  {name}: status={result.status.value} rls={result.rls_status.value if result.rls_status else None} masks={masks} error={result.error_code}")
        assert result.status.value == "ABAC_APPLIED", f"{name}: expected ABAC_APPLIED, got {result.status.value} ({result.error_code}: {result.error_message})"
        if has_rls:
            assert result.rls_status.value == "ABAC_APPLIED"
        for col in mask_cols:
            assert masks.get(col) == "ABAC_APPLIED"

        table_ref = TableRef(CATALOG, SCHEMA, name)
        state = uc.describe_table_security(table_ref)
        assert state.has_row_filter == has_rls, f"{name}: legacy row filter should still be present={has_rls}"
        assert bool(state.column_masks) == bool(mask_cols), f"{name}: legacy masks should still be present={bool(mask_cols)}"
    print("CONFIRMED: all 6 tables reached ABAC_APPLIED, legacy still present on every table (non-final state).")

    # -- 3. Live SELECT after APPLY_ABAC ------------------------------------
    section("STEP 3: Live SELECT checks after APPLY_ABAC (both mechanisms active)")
    run_live_selects(client)

    # -- 4. FINALIZE ---------------------------------------------------------
    section("STEP 4: FINALIZE")
    final_summary = run_migration(RunConfig(mode=Mode.FINALIZE, **base), uc)
    print(f"run_id={final_summary.run_id} succeeded={final_summary.tables_succeeded} failed={final_summary.tables_failed}")
    fby_name = {r.table_name.split(".")[-1]: r for r in final_summary.conversion_results}
    for name, expected_type, has_rls, mask_cols in TABLES:
        result = fby_name.get(name)
        masks = {k: v.value for k, v in result.column_mask_status.items()}
        print(f"  {name}: status={result.status.value} rls={result.rls_status.value if result.rls_status else None} masks={masks} error={result.error_code}")
        assert result.status.value == "SUCCESS", f"{name}: expected SUCCESS, got {result.status.value} ({result.error_code}: {result.error_message})"

        table_ref = TableRef(CATALOG, SCHEMA, name)
        state = uc.describe_table_security(table_ref)
        assert not state.has_row_filter, f"{name}: legacy row filter should be gone"
        assert not state.column_masks, f"{name}: legacy masks should be gone"
    print("CONFIRMED: all 6 tables reached SUCCESS, legacy fully removed from every table.")

    # -- 5. Live SELECT after FINALIZE --------------------------------------
    section("STEP 5: Live SELECT checks after FINALIZE (ABAC-only)")
    run_live_selects(client)

    # -- 6. Audit summary -----------------------------------------------------
    section(f"STEP 6: migration_audit summary ({AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit)")
    audit = client.run(
        f"SELECT table, migration_phase, object_type, masked_column, status, error_code "
        f"FROM {AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit "
        f"WHERE run_id IN ('{apply_summary.run_id}', '{final_summary.run_id}') "
        f"ORDER BY table, migration_phase, object_type, masked_column"
    )
    for row in audit.rows:
        print("  ", row)

    print("\nALL CHECKS PASSED for ril_streaming_flavors_test (6/6 tables, MANAGED + STREAMING_TABLE, all 3 flavors).")
    print(f"inventory run_id:  {inv.run_id}")
    print(f"apply_abac run_id: {apply_summary.run_id}")
    print(f"finalize run_id:   {final_summary.run_id}")
    print(f"audit table:       {AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit")
    print(f"inventory table:   {AUDIT_CATALOG}.{AUDIT_SCHEMA}.inventory")


def run_live_selects(client):
    checks = [
        ("managed_rls_tbl", "SELECT id, region, customer_name, amount FROM {}.{}.managed_rls_tbl ORDER BY id", "region", "east"),
        ("managed_mask_tbl", "SELECT id, customer_name, customer_email, amount FROM {}.{}.managed_mask_tbl ORDER BY id", "customer_email", "***MANAGED-MASKED***"),
        ("managed_both_tbl", "SELECT id, region, customer_email, amount FROM {}.{}.managed_both_tbl ORDER BY id", None, None),
        ("streaming_rls_tbl", "SELECT id, region, customer_name, amount FROM {}.{}.streaming_rls_tbl ORDER BY id", "region", "east"),
        ("streaming_mask_tbl", "SELECT id, customer_name, customer_email, amount FROM {}.{}.streaming_mask_tbl ORDER BY id", "customer_email", "***STREAMING-MASKED***"),
        ("streaming_both_tbl", "SELECT id, region, customer_email, amount FROM {}.{}.streaming_both_tbl ORDER BY id", None, None),
    ]
    for name, sql_tmpl, col_hint, val_hint in checks:
        r = client.run(sql_tmpl.format(CATALOG, SCHEMA))
        print(f"  {name}: status={r.status} error={r.error_code} {r.error}")
        assert r.status == "SUCCEEDED", f"{name} SELECT failed: {r.error_code} {r.error}"
        for row in r.rows:
            print("    ", row)
        # as the seeded data_admins member, RLS should show ALL rows (not
        # filtered) - only the *email* mask should visibly change the data.
        if name in ("managed_mask_tbl", "streaming_mask_tbl"):
            assert all(row[2] == val_hint for row in r.rows), f"{name}: email not masked"
        if name in ("managed_both_tbl", "streaming_both_tbl"):
            expected_mask = "***MANAGED-MASKED***" if name.startswith("managed") else "***STREAMING-MASKED***"
            assert all(row[2] == expected_mask for row in r.rows), f"{name}: email not masked"


if __name__ == "__main__":
    main()
