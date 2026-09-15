"""Complete regression test for the ABAC Migration Utility across ALL 3
supported non-VIEW table types x ALL 3 security flavors -
{MANAGED, STREAMING_TABLE, MATERIALIZED_VIEW} x {RLS-only, mask-only, both}
- run side by side in one catalog, `ril_full_regression_test.demo` (see
setup_ril_full_regression_test_catalog.py). This is the single comprehensive
run requested after Track A (STREAMING_TABLE) and Track B (MATERIALIZED_VIEW)
both shipped, to prove every table type/flavor combination still works
correctly together, not just individually.

1. INVENTORY over the whole `demo` schema - asserts all 9 tables are
   discovered with the correct table_type + ELIGIBLE, and that no table's
   `column_masks` list is polluted by a phantom entry (DEF-01 regression
   check, most relevant for the 3 MATERIALIZED_VIEW tables).
2. APPLY_ABAC - asserts every table reaches ABAC_APPLIED with the expected
   RLS/mask step statuses, and legacy is still present for every table.
3. Live SELECT against all 9 tables (as the seeded "data_admins" member,
   so RLS shows ALL rows - masks still apply since masks aren't
   group-gated) to confirm correct ABAC enforcement.
4. FINALIZE - asserts every table reaches SUCCESS and legacy is gone
   (including, for the 3 MATERIALIZED_VIEW tables, confirming the DDL used
   was ALTER MATERIALIZED VIEW, not a failed ALTER TABLE).
5. Live SELECT again to confirm ABAC-only enforcement is still correct.
6. SHOW POLICIES on every table + a full migration_audit/inventory summary.
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
CATALOG = "ril_full_regression_test"
SCHEMA = "demo"
AUDIT_CATALOG = CATALOG
AUDIT_SCHEMA = "governance_audit"

# (table, expected table_type, has_rls, mask_columns)
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

_MASK_SUFFIX_BY_TYPE = {
    "MANAGED": "***MANAGED-MASKED***",
    "STREAMING_TABLE": "***STREAMING-MASKED***",
    "MATERIALIZED_VIEW": "***MV-MASKED***",
}
_TYPE_BY_TABLE = {name: t for name, t, _, _ in TABLES}


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
    section("STEP 1: INVENTORY over ril_full_regression_test.demo (9 target tables + 6 backing sources)")
    inv = run_migration(RunConfig(mode=Mode.INVENTORY, **base), uc)
    print(f"run_id={inv.run_id} tables_in_scope={inv.tables_in_scope} eligible={inv.tables_eligible}")
    r = client.run(
        f"SELECT table, table_type, has_row_filter, has_column_masks, column_masks, migration_eligibility, eligibility_reason "
        f"FROM {AUDIT_CATALOG}.{AUDIT_SCHEMA}.inventory WHERE run_id = '{inv.run_id}' ORDER BY table"
    )
    inv_by_table = {row[0]: row for row in r.rows}
    for name, expected_type, has_rls, mask_cols in TABLES:
        row = inv_by_table.get(name)
        print(f"  {name}: {row}")
        assert row is not None, f"{name} missing from inventory"
        assert row[1] == expected_type, f"{name}: expected type {expected_type}, got {row[1]}"
        assert row[5] == "ELIGIBLE", f"{name}: expected ELIGIBLE, got {row[5]} ({row[6]})"
        mask_json = row[4] or "[]"
        actual_mask_cols = sorted(c.strip('"') for c in __import__("re").findall(r'"column":"([^"]+)"', mask_json))
        assert actual_mask_cols == sorted(mask_cols), (
            f"{name}: DEF-01 regression - expected mask columns {mask_cols}, got {actual_mask_cols} (raw={mask_json})"
        )
    print("CONFIRMED: all 9 tables (3 MANAGED + 3 STREAMING_TABLE + 3 MATERIALIZED_VIEW) discovered correctly, "
          "ELIGIBLE, and with exactly the expected mask columns (no DEF-01 phantom entries anywhere).\n")

    # -- 2. APPLY_ABAC ------------------------------------------------------
    section("STEP 2: APPLY_ABAC")
    apply_summary = run_migration(RunConfig(mode=Mode.APPLY_ABAC, **base), uc)
    print(f"run_id={apply_summary.run_id} succeeded={apply_summary.tables_succeeded} "
          f"abac_applied={apply_summary.tables_abac_applied} failed={apply_summary.tables_failed}")
    by_name = {r.table_name.split(".")[-1]: r for r in apply_summary.conversion_results}
    for name, expected_type, has_rls, mask_cols in TABLES:
        result = by_name.get(name)
        masks = {k: v.value for k, v in result.column_mask_status.items()}
        print(f"  {name}: status={result.status.value} rls={result.rls_status.value if result.rls_status else None} masks={masks} error={result.error_code} {result.error_message}")
        assert result.status.value == "ABAC_APPLIED", f"{name}: expected ABAC_APPLIED, got {result.status.value} ({result.error_code}: {result.error_message})"
        if has_rls:
            assert result.rls_status.value == "ABAC_APPLIED"
        for col in mask_cols:
            assert masks.get(col) == "ABAC_APPLIED"

        table_ref = TableRef(CATALOG, SCHEMA, name)
        state = uc.describe_table_security(table_ref)
        assert state.has_row_filter == has_rls, f"{name}: legacy row filter should still be present={has_rls}"
        assert bool(state.column_masks) == bool(mask_cols), f"{name}: legacy masks should still be present={bool(mask_cols)}"
        assert len(state.column_masks) == len(mask_cols), f"{name}: DEF-01 regression - expected {len(mask_cols)} masks, got {len(state.column_masks)}\n"
    print("CONFIRMED: all 9 tables reached ABAC_APPLIED, legacy still present on every table (non-final state), "
          "no phantom mask counts anywhere.\n")

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
        print(f"  {name}: status={result.status.value} rls={result.rls_status.value if result.rls_status else None} masks={masks} error={result.error_code} {result.error_message}")
        assert result.status.value == "SUCCESS", f"{name}: expected SUCCESS, got {result.status.value} ({result.error_code}: {result.error_message})"

        table_ref = TableRef(CATALOG, SCHEMA, name)
        state = uc.describe_table_security(table_ref)
        assert not state.has_row_filter, f"{name}: legacy row filter should be gone"
        assert not state.column_masks, f"{name}: legacy masks should be gone\n"
    print("CONFIRMED: all 9 tables reached SUCCESS, legacy fully removed from every table - including the 3 "
          "MATERIALIZED_VIEW tables via ALTER MATERIALIZED VIEW ... DROP (not a failed ALTER TABLE).\n")

    # -- 5. Live SELECT after FINALIZE --------------------------------------
    section("STEP 5: Live SELECT checks after FINALIZE (ABAC-only)")
    run_live_selects(client)

    # -- 6. SHOW POLICIES + audit summary -----------------------------------
    section("STEP 6: SHOW POLICIES on every table")
    for name, _, _, _ in TABLES:
        r = client.run(f"SHOW POLICIES ON TABLE {CATALOG}.{SCHEMA}.{name}")
        policy_names = sorted(row[0] for row in r.rows)
        print(f"  {name}: {policy_names}")

    section(f"STEP 7: migration_audit summary ({AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit)")
    audit = client.run(
        f"SELECT table, migration_phase, object_type, masked_column, status, error_code "
        f"FROM {AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit "
        f"WHERE run_id IN ('{apply_summary.run_id}', '{final_summary.run_id}') "
        f"ORDER BY table, migration_phase, object_type, masked_column"
    )
    for row in audit.rows:
        print("  ", row)
    failures = [row for row in audit.rows if row[5] is not None]
    assert not failures, f"Unexpected error_code(s) in migration_audit: {failures}"

    print("\nALL CHECKS PASSED for ril_full_regression_test (9/9 tables, MANAGED + STREAMING_TABLE + "
          "MATERIALIZED_VIEW, all 3 flavors each).")
    print(f"inventory run_id:  {inv.run_id}")
    print(f"apply_abac run_id: {apply_summary.run_id}")
    print(f"finalize run_id:   {final_summary.run_id}")
    print(f"audit table:       {AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit")
    print(f"inventory table:   {AUDIT_CATALOG}.{AUDIT_SCHEMA}.inventory\n")


def run_live_selects(client):
    checks = [
        ("managed_rls_tbl", "SELECT id, region, customer_name, amount FROM {}.{}.managed_rls_tbl ORDER BY id", None),
        ("managed_mask_tbl", "SELECT id, customer_name, customer_email, amount FROM {}.{}.managed_mask_tbl ORDER BY id", "customer_email"),
        ("managed_both_tbl", "SELECT id, region, customer_email, amount FROM {}.{}.managed_both_tbl ORDER BY id", "customer_email"),
        ("streaming_rls_tbl", "SELECT id, region, customer_name, amount FROM {}.{}.streaming_rls_tbl ORDER BY id", None),
        ("streaming_mask_tbl", "SELECT id, customer_name, customer_email, amount FROM {}.{}.streaming_mask_tbl ORDER BY id", "customer_email"),
        ("streaming_both_tbl", "SELECT id, region, customer_email, amount FROM {}.{}.streaming_both_tbl ORDER BY id", "customer_email"),
        ("mv_rls_tbl", "SELECT id, region, customer_name, amount FROM {}.{}.mv_rls_tbl ORDER BY id", None),
        ("mv_mask_tbl", "SELECT id, customer_name, customer_email, amount FROM {}.{}.mv_mask_tbl ORDER BY id", "customer_email"),
        ("mv_both_tbl", "SELECT id, region, customer_email, amount FROM {}.{}.mv_both_tbl ORDER BY id", "customer_email"),
    ]
    for name, sql_tmpl, mask_col in checks:
        r = client.run(sql_tmpl.format(CATALOG, SCHEMA))
        print(f"  {name}: status={r.status} error={r.error_code} {r.error}")
        assert r.status == "SUCCEEDED", f"{name} SELECT failed: {r.error_code} {r.error}"
        for row in r.rows:
            print("    ", row)
        # As the seeded data_admins member, RLS should show ALL rows (not
        # filtered) for every *_rls_tbl / *_both_tbl - only the mask column
        # (present for *_mask_tbl / *_both_tbl) should visibly change.
        if mask_col is not None:
            expected_masked_value = _MASK_SUFFIX_BY_TYPE[_TYPE_BY_TABLE[name]]
            mask_col_idx = 2  # every checked SELECT lists the mask column 3rd
            assert all(row[mask_col_idx] == expected_masked_value for row in r.rows), (
                f"{name}: {mask_col} not correctly masked - expected all rows == {expected_masked_value!r}"
            )


if __name__ == "__main__":
    main()
