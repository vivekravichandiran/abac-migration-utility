"""Builds fixtures in the TARGET workspace (adb-7405609958717235.15) to
exercise the isolated-phase (INVENTORY -> APPLY_ABAC -> FINALIZE) jobs
defined in resources/phased_jobs.yml, before they're deployed/run there.

Lives in ril_raw.abac_migration_phased_test - a schema dedicated to this
test so it never collides with anything else on the target metastore
(which is a completely separate metastore from `source`; confirmed via
`SHOW SCHEMAS IN ril_raw` returning no pre-existing abac_migration_* schema).

Covers, deliberately, the three legacy-security combinations the isolated
modes must all handle correctly:
  - rls_only_tbl    - legacy ROW FILTER, no column mask
  - mask_only_tbl   - legacy COLUMN MASK(s), no row filter
  - both_tbl        - legacy ROW FILTER *and* COLUMN MASK on the same table

Each legacy function is named distinctly (rf_region_rls_only,
mask_email_mask_only, ...) specifically so the new "one governed tag key
per function" scheme (abac_migration/migration/tag_provisioner.py) produces
three (or more) *different* tag keys across this fixture set, not one
shared key - the whole point of this run is to prove that per-function
tagging + LLM PII-suggestion behave correctly on a fresh workspace.
"""
from __future__ import annotations

from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "uc_target"
WAREHOUSE_ID = "03ac4037e7134a18"
SCHEMA = "ril_raw.abac_migration_phased_test"

STATEMENTS = [
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",

    # -- rls_only_tbl: legacy ROW FILTER only --------------------------
    f"CREATE OR REPLACE TABLE {SCHEMA}.rls_only_tbl "
    f"(id BIGINT, region STRING, customer_name STRING) USING DELTA",
    f"INSERT INTO {SCHEMA}.rls_only_tbl VALUES "
    f"(1,'east','Alice Smith'), (2,'west','Bob Jones'), (3,'east','Carol Lee')",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.rf_region_rls_only(region STRING) "
    f"RETURN is_account_group_member('account users') OR region = 'east'",
    f"ALTER TABLE {SCHEMA}.rls_only_tbl SET ROW FILTER {SCHEMA}.rf_region_rls_only ON (region)",

    # -- mask_only_tbl: legacy COLUMN MASK(s) only ---------------------
    f"CREATE OR REPLACE TABLE {SCHEMA}.mask_only_tbl "
    f"(id BIGINT, email STRING, phone STRING, ssn STRING) USING DELTA",
    f"INSERT INTO {SCHEMA}.mask_only_tbl VALUES "
    f"(1,'alice@example.com','555-1000','111-11-1111'), "
    f"(2,'bob@example.com','555-2000','222-22-2222')",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.mask_email_mask_only(v STRING) RETURN '***EMAIL-MASKED***'",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.mask_phone_mask_only(v STRING) RETURN '***PHONE-MASKED***'",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.mask_ssn_mask_only(v STRING) RETURN '***-**-****'",
    f"ALTER TABLE {SCHEMA}.mask_only_tbl ALTER COLUMN email SET MASK {SCHEMA}.mask_email_mask_only",
    f"ALTER TABLE {SCHEMA}.mask_only_tbl ALTER COLUMN phone SET MASK {SCHEMA}.mask_phone_mask_only",
    f"ALTER TABLE {SCHEMA}.mask_only_tbl ALTER COLUMN ssn SET MASK {SCHEMA}.mask_ssn_mask_only",

    # -- both_tbl: legacy ROW FILTER *and* COLUMN MASK -----------------
    f"CREATE OR REPLACE TABLE {SCHEMA}.both_tbl "
    f"(id BIGINT, region STRING, email STRING, salary DOUBLE) USING DELTA",
    f"INSERT INTO {SCHEMA}.both_tbl VALUES "
    f"(1,'east','dave@example.com',95000.0), (2,'west','erin@example.com',88000.0)",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.rf_region_both(region STRING) "
    f"RETURN is_account_group_member('account users') OR region = 'east'",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.mask_email_both(v STRING) RETURN '***EMAIL-MASKED***'",
    f"CREATE OR REPLACE FUNCTION {SCHEMA}.mask_salary_both(v DOUBLE) RETURN CAST(NULL AS DOUBLE)",
    f"ALTER TABLE {SCHEMA}.both_tbl SET ROW FILTER {SCHEMA}.rf_region_both ON (region)",
    f"ALTER TABLE {SCHEMA}.both_tbl ALTER COLUMN email SET MASK {SCHEMA}.mask_email_both",
    f"ALTER TABLE {SCHEMA}.both_tbl ALTER COLUMN salary SET MASK {SCHEMA}.mask_salary_both",
]


def main():
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()

    for stmt in STATEMENTS:
        r = client.run(stmt)
        status = "OK" if r.status == "SUCCEEDED" else f"FAILED: {r.error_code} {r.error}"
        print(f"[{status}] {stmt}")
        if r.status != "SUCCEEDED":
            raise SystemExit(f"Aborting: {stmt} -> {r.error_code} {r.error}")


if __name__ == "__main__":
    main()
