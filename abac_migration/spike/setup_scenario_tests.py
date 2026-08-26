"""Builds real, isolated fixtures for scenarios that can't safely/
deterministically arise from the organic sales_abac_demo data: masks-only,
missing function, existing conflicting policy, partial mask failure, and a
dedicated table for dry-run/idempotent-rerun/drift/rollback demonstration.

Lives in ril_raw.abac_migration_scenario_tests - fully isolated from the
real demo data in ril_raw.sales_abac_demo (and its sibling catalogs).
"""
from __future__ import annotations

from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "uc_source"
WAREHOUSE_ID = "525de76b2ccdd7d5"
SCHEMA = "ril_raw.abac_migration_scenario_tests"

STATEMENTS = [
    # -- masks_only_tbl (scenario 2) -----------------------------------
    f"CREATE OR REPLACE TABLE {SCHEMA}.masks_only_tbl (id BIGINT, email STRING, phone STRING) USING DELTA",
    f"INSERT INTO {SCHEMA}.masks_only_tbl VALUES (1,'a@x.com','555-1111'), (2,'b@x.com','555-2222')",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.mask_email_st(v STRING) RETURN '***MASKED***'",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.mask_phone_st(v STRING) RETURN '***MASKED***'",
    f"ALTER TABLE {SCHEMA}.masks_only_tbl ALTER COLUMN email SET MASK {SCHEMA}.mask_email_st",
    f"ALTER TABLE {SCHEMA}.masks_only_tbl ALTER COLUMN phone SET MASK {SCHEMA}.mask_phone_st",

    # -- missing_function_tbl (scenario 5) -----------------------------
    f"CREATE OR REPLACE TABLE {SCHEMA}.missing_function_tbl (id BIGINT, region STRING) USING DELTA",
    f"INSERT INTO {SCHEMA}.missing_function_tbl VALUES (1,'east'), (2,'west')",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.rf_temp_will_drop(region STRING) RETURN TRUE",
    f"ALTER TABLE {SCHEMA}.missing_function_tbl SET ROW FILTER {SCHEMA}.rf_temp_will_drop ON (region)",

    # -- conflicting_policy_tbl (scenario 6) ---------------------------
    f"CREATE OR REPLACE TABLE {SCHEMA}.conflicting_policy_tbl (id BIGINT, region STRING) USING DELTA",
    f"INSERT INTO {SCHEMA}.conflicting_policy_tbl VALUES (1,'east')",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.rf_conflict_real(region STRING) RETURN TRUE",
    f"ALTER TABLE {SCHEMA}.conflicting_policy_tbl SET ROW FILTER {SCHEMA}.rf_conflict_real ON (region)",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.rf_conflict_other(region STRING) RETURN TRUE",
    "CREATE GOVERNED TAG IF NOT EXISTS test_scenario_conflict_tag VALUES ('region_marker')",
    f"ALTER TABLE {SCHEMA}.conflicting_policy_tbl ALTER COLUMN region SET TAGS ('test_scenario_conflict_tag' = 'region_marker')",

    # -- partial_mask_failure_tbl (scenario 11) ------------------------
    f"CREATE OR REPLACE TABLE {SCHEMA}.partial_mask_failure_tbl (id BIGINT, c1 STRING, c2 STRING, c3 STRING) USING DELTA",
    f"INSERT INTO {SCHEMA}.partial_mask_failure_tbl VALUES (1,'a','b','c')",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.mask_c1(v STRING) RETURN '***'",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.mask_c2_will_drop(v STRING) RETURN '***'",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.mask_c3(v STRING) RETURN '***'",
    f"ALTER TABLE {SCHEMA}.partial_mask_failure_tbl ALTER COLUMN c1 SET MASK {SCHEMA}.mask_c1",
    f"ALTER TABLE {SCHEMA}.partial_mask_failure_tbl ALTER COLUMN c2 SET MASK {SCHEMA}.mask_c2_will_drop",
    f"ALTER TABLE {SCHEMA}.partial_mask_failure_tbl ALTER COLUMN c3 SET MASK {SCHEMA}.mask_c3",

    # -- rollback_demo_tbl (scenarios 12/13/14/15) ---------------------
    f"CREATE OR REPLACE TABLE {SCHEMA}.rollback_demo_tbl (id BIGINT, region STRING, ssn STRING) USING DELTA",
    f"INSERT INTO {SCHEMA}.rollback_demo_tbl VALUES (1,'east','123-45-6789')",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.rf_region_demo(region STRING) RETURN TRUE",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.mask_ssn_demo(v STRING) RETURN '***-**-****'",
    f"ALTER TABLE {SCHEMA}.rollback_demo_tbl SET ROW FILTER {SCHEMA}.rf_region_demo ON (region)",
    f"ALTER TABLE {SCHEMA}.rollback_demo_tbl ALTER COLUMN ssn SET MASK {SCHEMA}.mask_ssn_demo",
]

# Run AFTER the above so the row filter/mask is already active and
# referencing a now-dangling function - this is what makes SOURCE_FUNCTION_NOT_FOUND
# genuinely reproducible instead of gateway fault-injected.
DROP_STATEMENTS = [
    f"DROP FUNCTION {SCHEMA}.rf_temp_will_drop",
    f"DROP FUNCTION {SCHEMA}.mask_c2_will_drop",
]


def main():
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()

    for stmt in STATEMENTS:
        r = client.run(stmt)
        status = "OK" if r.status == "SUCCEEDED" else f"FAILED: {r.error_code} {r.error}"
        print(f"[{status}] {stmt}")

    print("\n--- now dropping functions while still referenced (testing dependency enforcement) ---")
    for stmt in DROP_STATEMENTS:
        r = client.run(stmt)
        status = "OK" if r.status == "SUCCEEDED" else f"FAILED: {r.error_code} {r.error}"
        print(f"[{status}] {stmt}")


if __name__ == "__main__":
    main()
