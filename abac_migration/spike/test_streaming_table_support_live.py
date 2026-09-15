"""Live end-to-end test for STREAMING_TABLE eligibility (Track A of the
streaming-tables-abac plan) against ril_full_access_test.streaming_test
(see setup_streaming_table_test.py) - uses the REAL, un-patched code (no
in-process monkeypatch this time; SUPPORTED_TABLE_TYPES now genuinely
includes STREAMING_TABLE in inventory_manager.py).

1. Runs Mode.INVENTORY - asserts table_type='STREAMING_TABLE',
   migration_eligibility='ELIGIBLE'.
2. Runs Mode.APPLY_ABAC - asserts ABAC row filter + mask created,
   status=ABAC_APPLIED, legacy still present.
3. Live SELECT to confirm ABAC row filter + mask are actually enforced.
4. Runs Mode.FINALIZE - asserts legacy row filter + mask removed,
   status=SUCCESS.
5. Live SELECT again to confirm still-correct enforcement (now ABAC-only).
6. Prints migration_audit rows for both phases.
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
CATALOG = "ril_full_access_test"
SCHEMA = "streaming_test"
TABLE = "events_streaming_tbl"
AUDIT_CATALOG = CATALOG
AUDIT_SCHEMA = "governance_audit_streaming_test"


def main():
    refresh_oauth_token()
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()
    uc = DatabricksUnityCatalogGateway(client)
    full_name = f"{CATALOG}.{SCHEMA}.{TABLE}"

    base_config = dict(
        scope_type=ScopeType.SPECIFIC_TABLES,
        tables=[full_name],
        dry_run=False,
        audit_catalog=AUDIT_CATALOG,
        audit_schema=AUDIT_SCHEMA,
    )

    print("=" * 90); print("STEP 1: INVENTORY (real code, no monkeypatch)"); print("=" * 90)
    inv_summary = run_migration(RunConfig(mode=Mode.INVENTORY, **base_config), uc)
    print(f"run_id={inv_summary.run_id} tables_in_scope={inv_summary.tables_in_scope} eligible={inv_summary.tables_eligible}")
    assert inv_summary.tables_eligible == 1, "expected the streaming table to be ELIGIBLE now"

    r = client.run(
        f"SELECT table_type, migration_eligibility, eligibility_reason, has_row_filter, has_column_masks "
        f"FROM {AUDIT_CATALOG}.{AUDIT_SCHEMA}.inventory WHERE run_id = '{inv_summary.run_id}'"
    )
    print("inventory row:", r.rows)
    assert r.rows[0][0] == "STREAMING_TABLE"
    assert r.rows[0][1] == "ELIGIBLE"
    print("CONFIRMED: INVENTORY correctly marks the streaming table ELIGIBLE.\n")

    print("=" * 90); print("STEP 2: APPLY_ABAC"); print("=" * 90)
    apply_summary = run_migration(RunConfig(mode=Mode.APPLY_ABAC, **base_config), uc)
    print(f"run_id={apply_summary.run_id} succeeded={apply_summary.tables_succeeded} abac_applied={apply_summary.tables_abac_applied} failed={apply_summary.tables_failed}")
    result = apply_summary.conversion_results[0]
    masks = {k: v.value for k, v in result.column_mask_status.items()}
    print(f"  status={result.status.value} rls={result.rls_status.value if result.rls_status else None} masks={masks} error={result.error_code}")
    assert result.status.value == "ABAC_APPLIED"
    assert result.rls_status.value == "ABAC_APPLIED"
    assert masks.get("customer_email") == "ABAC_APPLIED"

    table_ref = TableRef(CATALOG, SCHEMA, TABLE)
    state = uc.describe_table_security(table_ref)
    print(f"  legacy still present: has_row_filter={state.has_row_filter} column_masks={[m.column for m in state.column_masks]}")
    assert state.has_row_filter and state.column_masks, "legacy should still be present after APPLY_ABAC (non-final state)"

    sel = client.run(f"SELECT id, region, customer_email, amount FROM {full_name} ORDER BY id")
    print(f"  live SELECT after APPLY_ABAC: status={sel.status}")
    for row in sel.rows:
        print("   ", row)
    assert sel.status == "SUCCEEDED"
    assert len(sel.rows) == 1 and sel.rows[0][1] == "east", "ABAC row filter should show only region=east"
    assert sel.rows[0][2] == "***EMAIL-MASKED***", "ABAC mask should redact customer_email"
    print("CONFIRMED: ABAC row filter + mask enforced correctly on the streaming table (both mechanisms active).\n")

    print("=" * 90); print("STEP 3: FINALIZE"); print("=" * 90)
    final_summary = run_migration(RunConfig(mode=Mode.FINALIZE, **base_config), uc)
    print(f"run_id={final_summary.run_id} succeeded={final_summary.tables_succeeded} failed={final_summary.tables_failed}")
    fresult = final_summary.conversion_results[0]
    fmasks = {k: v.value for k, v in fresult.column_mask_status.items()}
    print(f"  status={fresult.status.value} rls={fresult.rls_status.value if fresult.rls_status else None} masks={fmasks} error={fresult.error_code}")
    assert fresult.status.value == "SUCCESS"

    state2 = uc.describe_table_security(table_ref)
    print(f"  legacy after FINALIZE: has_row_filter={state2.has_row_filter} column_masks={state2.column_masks}")
    assert not state2.has_row_filter and not state2.column_masks, "legacy should be gone after FINALIZE"

    sel2 = client.run(f"SELECT id, region, customer_email, amount FROM {full_name} ORDER BY id")
    print(f"  live SELECT after FINALIZE (ABAC-only now): status={sel2.status}")
    for row in sel2.rows:
        print("   ", row)
    assert sel2.status == "SUCCEEDED"
    assert len(sel2.rows) == 1 and sel2.rows[0][1] == "east"
    assert sel2.rows[0][2] == "***EMAIL-MASKED***"
    print("CONFIRMED: legacy removed, ABAC-only enforcement still correct after FINALIZE.\n")

    print("=" * 90); print(f"Audit rows ({AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit)"); print("=" * 90)
    audit = client.run(
        f"SELECT run_id, migration_phase, object_type, masked_column, status, error_code "
        f"FROM {AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit "
        f"WHERE run_id IN ('{apply_summary.run_id}', '{final_summary.run_id}') ORDER BY migration_phase, object_type, masked_column"
    )
    for row in audit.rows:
        print("  ", row)

    print("\nALL CHECKS PASSED for STREAMING_TABLE support (Track A).")
    print(f"inventory run_id: {inv_summary.run_id}")
    print(f"apply_abac run_id: {apply_summary.run_id}")
    print(f"finalize run_id: {final_summary.run_id}")


if __name__ == "__main__":
    main()
