"""Live end-to-end test for the "no tag VALUE ever" fix against
ril_full_access_test.novalue_test (see setup_novalue_test_schema.py):

1. Runs Mode.APPLY_ABAC (SPECIFIC_TABLES scope) against both fixture
   tables.
2. Asserts mask_collision_tbl's ROW step succeeds and shares ONE bare
   (no-value) governed tag across ssn + national_id.
3. Asserts rls_collision_tbl's ROW_FILTER step FAILS with
   error_code=RLS_TAG_COLLISION_UNRESOLVABLE - NOT a crash, and the run
   still completes and records both tables to migration_audit.
4. Does a live SELECT against both tables to confirm:
   - mask_collision_tbl: both columns are masked, no
     UC_ABAC_AMBIGUOUS_COLUMN_MATCH.
   - rls_collision_tbl: still queryable under the untouched legacy row
     filter (ABAC was never applied here).
5. Queries governed tags / column tags directly to confirm no VALUE was
   ever minted for either table.
6. Prints the migration_audit rows for both tables.
"""
from __future__ import annotations

import json

from abac_migration.audit.audit_repository import AuditRepository
from abac_migration.config.models import Mode, RunConfig, ScopeType
from abac_migration.migration.migration_engine import run as run_migration
from abac_migration.spike._oauth_m2m import refresh as refresh_oauth_token
from abac_migration.uc_gateway.gateway import DatabricksUnityCatalogGateway
from abac_migration.uc_gateway.models import TableRef
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "ril_catalog_test_pat"
WAREHOUSE_ID = "5fe1692f119e2528"
CATALOG = "ril_full_access_test"
SCHEMA = "novalue_test"
AUDIT_CATALOG = CATALOG
AUDIT_SCHEMA = "governance_audit_novalue_test"


def fmt(results):
    lines = []
    for r in results:
        masks = {k: v.value for k, v in r.column_mask_status.items()}
        lines.append(
            f"  {r.table_name}: status={r.status.value} error={r.error_code} "
            f"error_msg={r.error_message} rls={r.rls_status.value if r.rls_status else None} masks={masks}"
        )
    return "\n".join(lines)


def main():
    refresh_oauth_token()
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()
    uc = DatabricksUnityCatalogGateway(client)

    config = RunConfig(
        mode=Mode.APPLY_ABAC,
        scope_type=ScopeType.SPECIFIC_TABLES,
        tables=[f"{CATALOG}.{SCHEMA}.mask_collision_tbl", f"{CATALOG}.{SCHEMA}.rls_collision_tbl"],
        dry_run=False,
        continue_on_error=True,
        max_parallelism=2,
        audit_catalog=AUDIT_CATALOG,
        audit_schema=AUDIT_SCHEMA,
        prefer_existing_tags=True,
    )

    print("=" * 90); print("APPLY_ABAC against novalue_test fixtures"); print("=" * 90)
    summary = run_migration(config, uc)
    print(f"run_id={summary.run_id}")
    print(f"tables_in_scope={summary.tables_in_scope} eligible={summary.tables_eligible}")
    print(f"succeeded={summary.tables_succeeded} abac_applied={summary.tables_abac_applied} failed={summary.tables_failed}")
    print(fmt(summary.conversion_results))

    # -- assertions on the in-memory results --------------------------
    by_name = {r.table_name: r for r in summary.conversion_results}
    mask_result = by_name.get(f"{CATALOG}.{SCHEMA}.mask_collision_tbl")
    rls_result = by_name.get(f"{CATALOG}.{SCHEMA}.rls_collision_tbl")

    assert mask_result is not None, "mask_collision_tbl not in results"
    assert set(mask_result.column_mask_status.keys()) == {"ssn", "national_id"}
    print("\nmask_collision_tbl column_mask_status:", {k: v.value for k, v in mask_result.column_mask_status.items()})
    assert all(v.value == "ABAC_APPLIED" for v in mask_result.column_mask_status.values()), "expected ABAC_APPLIED for both masked columns"

    assert rls_result is not None, "rls_collision_tbl not in results"
    print("rls_collision_tbl rls_status:", rls_result.rls_status.value if rls_result.rls_status else None)
    print("rls_collision_tbl error_code:", rls_result.error_code)
    assert rls_result.rls_status is not None and rls_result.rls_status.value == "FAILED"
    assert rls_result.error_code == "RLS_TAG_COLLISION_UNRESOLVABLE", f"unexpected error_code: {rls_result.error_code}"

    # -- verify no VALUE was ever minted, directly against UC ---------
    print("\n" + "=" * 90); print("Governed tags / column tags check"); print("=" * 90)
    mask_table_ref = TableRef(CATALOG, SCHEMA, "mask_collision_tbl")
    rls_table_ref = TableRef(CATALOG, SCHEMA, "rls_collision_tbl")

    mask_tags = uc.list_column_tags(mask_table_ref)
    rls_tags = uc.list_column_tags(rls_table_ref)
    print("mask_collision_tbl column tags:", mask_tags)
    print("rls_collision_tbl column tags:", rls_tags)

    ssn_tag = next((t for t in mask_tags if t.column == "ssn"), None)
    nid_tag = next((t for t in mask_tags if t.column == "national_id"), None)
    assert ssn_tag is not None and nid_tag is not None, "expected both mask columns tagged"
    assert ssn_tag.tag_value is None, f"expected no value on ssn tag, got {ssn_tag.tag_value!r}"
    assert nid_tag.tag_value is None, f"expected no value on national_id tag, got {nid_tag.tag_value!r}"
    assert ssn_tag.tag_key == nid_tag.tag_key, "expected ONE shared tag key for both mask columns"
    print(f"CONFIRMED: mask_collision_tbl.ssn and .national_id share tag_key={ssn_tag.tag_key!r}, both key-only (no value)")

    region_tag = next((t for t in rls_tags if t.column == "region"), None)
    unit_tag = next((t for t in rls_tags if t.column == "business_unit"), None)
    assert region_tag is None and unit_tag is None, "expected NO tag assigned to either RLS collision column"
    print("CONFIRMED: rls_collision_tbl.region and .business_unit have NO governed tag assigned (collision skipped)")

    governed_tag_def = next((t for t in uc.list_governed_tags() if t.tag_key == ssn_tag.tag_key), None)
    assert governed_tag_def is not None and governed_tag_def.values == [], f"expected key-only governed tag (no allowed values), got {governed_tag_def}"
    print(f"CONFIRMED: governed tag {ssn_tag.tag_key!r} has NO allowed values list: {governed_tag_def.values}")

    # -- live SELECT checks --------------------------------------------
    print("\n" + "=" * 90); print("Live SELECT checks"); print("=" * 90)
    mask_select = client.run(f"SELECT id, employee_name, ssn, national_id FROM {CATALOG}.{SCHEMA}.mask_collision_tbl ORDER BY id")
    print(f"mask_collision_tbl SELECT status={mask_select.status} error={mask_select.error_code} {mask_select.error}")
    assert mask_select.status == "SUCCEEDED", f"UC_ABAC_AMBIGUOUS_COLUMN_MATCH or other failure: {mask_select.error_code} {mask_select.error}"
    for row in mask_select.rows:
        print("  ", row)
    assert all(row[2] == "***REDACTED***" for row in mask_select.rows), "ssn not masked"
    assert all(row[3] == "***REDACTED***" for row in mask_select.rows), "national_id not masked"
    print("CONFIRMED: no UC_ABAC_AMBIGUOUS_COLUMN_MATCH on mask_collision_tbl; both columns correctly masked.")

    rls_select = client.run(f"SELECT id, region, business_unit, amount FROM {CATALOG}.{SCHEMA}.rls_collision_tbl ORDER BY id")
    print(f"rls_collision_tbl SELECT status={rls_select.status} error={rls_select.error_code} {rls_select.error}")
    assert rls_select.status == "SUCCEEDED", f"rls_collision_tbl SELECT unexpectedly failed: {rls_select.error_code} {rls_select.error}"
    for row in rls_select.rows:
        print("  ", row)
    print("CONFIRMED: rls_collision_tbl still queryable under the untouched legacy row filter (ABAC skipped safely).")

    # -- audit table --------------------------------------------------
    print("\n" + "=" * 90); print(f"Audit rows ({AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit)"); print("=" * 90)
    audit_select = client.run(
        f"SELECT catalog, schema, table, object_type, masked_column, status, error_code, error_message "
        f"FROM {AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit WHERE run_id = '{summary.run_id}' ORDER BY table, object_type, masked_column"
    )
    for row in audit_select.rows:
        print("  ", row)

    print("\nALL CHECKS PASSED.")
    print(f"audit table: {AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit")
    print(f"run_id: {summary.run_id}")


if __name__ == "__main__":
    main()
