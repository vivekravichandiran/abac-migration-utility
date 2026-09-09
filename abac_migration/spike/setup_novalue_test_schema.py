"""One-off live test fixture for the "no tag VALUE ever" fix
(tag_provisioner.py's new role-aware `_split_by_collision`):

Catalog: ril_full_access_test (already exists, SP-owned, full access
already granted to vivek.ravichandiran@databricks.com)
Schema:  novalue_test (new, dedicated to this test - dropped/recreated
         idempotently by this script)

Tables:
  mask_collision_tbl - ONE mask function (mask_pii_generic) applied
    DIRECTLY (legacy `ALTER TABLE ... SET MASK`) to TWO columns (ssn,
    national_id) of the SAME table. Expected after APPLY_ABAC: both
    columns share ONE bare, key-only governed tag (no value, ever) and
    both mask policies apply cleanly - no UC_ABAC_AMBIGUOUS_COLUMN_MATCH
    at SELECT time (confirmed safe for COLUMN_MASK in an earlier live
    spike).

  rls_collision_tbl - ONE row-filter function (rf_region_and_unit) that
    takes TWO columns (region, business_unit) as USING COLUMNS of the SAME
    table - a genuine same-table RLS collision (both columns would need
    the identical bare tag key). Expected after APPLY_ABAC: the ROW_FILTER
    step FAILS with error_code RLS_TAG_COLLISION_UNRESOLVABLE, recorded to
    migration_audit - NOT a crash, NOT a disambiguating value, and the
    legacy row filter is left completely untouched/still enforcing.

Auth: same OAuth M2M bridge as the other ril_* spike scripts
(ABAC_OAUTH_HOST/ABAC_OAUTH_CLIENT_ID/ABAC_OAUTH_CLIENT_SECRET env vars,
via abac_migration/spike/_oauth_m2m.py) - re-run that module first if this
script starts failing with 401s (~1h token lifetime).
"""
from __future__ import annotations

from abac_migration.spike._oauth_m2m import refresh as refresh_oauth_token
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "ril_catalog_test_pat"
WAREHOUSE_ID = "5fe1692f119e2528"
CATALOG = "ril_full_access_test"
SCHEMA = "novalue_test"

STATEMENTS: list[str] = [
    f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}",

    # -- mask_collision_tbl: 1 mask function, 2 columns of the same table --
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.mask_pii_generic(v STRING)
RETURNS STRING COMMENT 'Column mask: fully redacts any generic PII string value'
RETURN '***REDACTED***'""",

    f"""CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.mask_collision_tbl (
  id BIGINT, employee_name STRING, ssn STRING, national_id STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.{SCHEMA}.mask_collision_tbl VALUES
  (1, 'Ananya Iyer', '111-22-3333', 'NID-90011122'),
  (2, 'Rahul Nair', '222-33-4444', 'NID-90022233'),
  (3, 'Divya Menon', '333-44-5555', 'NID-90033344')""",
    f"ALTER TABLE {CATALOG}.{SCHEMA}.mask_collision_tbl ALTER COLUMN ssn SET MASK {CATALOG}.{SCHEMA}.mask_pii_generic",
    f"ALTER TABLE {CATALOG}.{SCHEMA}.mask_collision_tbl ALTER COLUMN national_id SET MASK {CATALOG}.{SCHEMA}.mask_pii_generic",

    # -- rls_collision_tbl: 1 row-filter function, 2 USING COLUMNS of the same table --
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.rf_region_and_unit(region STRING, business_unit STRING)
RETURNS BOOLEAN
COMMENT 'RLS: multi-arg row filter needing 2 columns of the SAME table - deliberate same-table tag collision fixture'
RETURN region = 'east' OR business_unit = 'Public Reporting'""",

    f"""CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.rls_collision_tbl (
  id BIGINT, region STRING, business_unit STRING, amount DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.{SCHEMA}.rls_collision_tbl VALUES
  (1, 'east', 'Retail', 15000.0),
  (2, 'west', 'Wholesale', 42000.0),
  (3, 'north', 'Public Reporting', 900.0),
  (4, 'south', 'Treasury', 780000.0)""",
    f"ALTER TABLE {CATALOG}.{SCHEMA}.rls_collision_tbl SET ROW FILTER {CATALOG}.{SCHEMA}.rf_region_and_unit ON (region, business_unit)",
]


def main():
    refresh_oauth_token()
    print(f"\nRunning {len(STATEMENTS)} DDL/DML statements against {PROFILE}...")
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()

    for stmt in STATEMENTS:
        r = client.run(stmt)
        status = "OK" if r.status == "SUCCEEDED" else f"FAILED: {r.error_code} {r.error}"
        first_line = stmt.strip().splitlines()[0][:100]
        print(f"[{status}] {first_line}")
        if r.status != "SUCCEEDED":
            raise SystemExit(f"Aborting on: {stmt}\n-> {r.error_code} {r.error}")

    print(f"\nDone. Schema ready: {CATALOG}.{SCHEMA}")
    print(f"  mask_collision_tbl -> mask_pii_generic shared by ssn + national_id")
    print(f"  rls_collision_tbl  -> rf_region_and_unit(region, business_unit) - same-table RLS collision")


if __name__ == "__main__":
    main()
