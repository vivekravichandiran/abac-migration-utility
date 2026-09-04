"""Dedicated, disposable test catalog on the NEW workspace
(adb-7405616318078204, OAuth M2M service principal `migration-sp-tgt`)
created specifically to exercise `policy_scope=CATALOG` ("catalog level
application") stage by stage: INVENTORY -> APPLY_ABAC -> FINALIZE.

Deliberately reuses the SAME legacy function across multiple tables within
a schema (rls-only table + both table share one row-filter function; the
mask-only table + both table share one mask function) so that after
APPLY_ABAC we can concretely observe ONE catalog-scoped ABAC policy
covering >1 table - the entire point of this policy_scope, vs.
policy_scope=TABLE which would mint a separate policy per table for the
exact same function.

Catalog: ril_catalog_scope_test
Schemas: hr, finance (+ governance)
Tables:  3 per domain schema (6 total) -> 2 RLS-only, 2 mask-only, 2 both.

Auth: this workspace's `~/.databrickscfg` OAuth M2M profile
(`ril_catalog_test`, client_id/client_secret) has no plain `token` field
that ResilientDatabricksSQL._load_profile()/Scim._load_profile() can read
directly - `abac_migration/spike/_oauth_m2m.py` mints a short-lived (~1h)
access token via the client_credentials grant (reading
ABAC_OAUTH_HOST/ABAC_OAUTH_CLIENT_ID/ABAC_OAUTH_CLIENT_SECRET from the
environment - never hardcoded, this repo is public) and stores it under a
separate `ril_catalog_test_pat` profile (host+token) instead. Re-run
`python -m abac_migration.spike._oauth_m2m` if a long-running script starts
hitting 401s.
"""
from __future__ import annotations

import os

from abac_migration.spike._oauth_m2m import refresh as refresh_oauth_token
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "ril_catalog_test_pat"
WAREHOUSE_ID = "5fe1692f119e2528"
CATALOG = "ril_catalog_scope_test"
GOV_SCHEMA = "governance"

# Same storage account/external location (`classic_stable_vk_target`) the
# service principal already has WRITE FILES/MANAGE on - confirmed live.
CATALOG_MANAGED_LOCATION = (
    "abfss://unity-catalog-storage@dbstorageziwqzkb2dgooo.dfs.core.windows.net"
    f"/7405616318078204/{CATALOG}"
)

# Simple control-table group-membership pattern (same as
# setup_source_abac_e2e_test_catalog.py) - avoids needing real account-level
# groups (this SP's SCIM access can only create workspace-local groups,
# which is_account_group_member() can't resolve). The service principal
# itself is seeded as a member of every group so a `SELECT` run as this SP
# always takes the "privileged" branch, and the "public" branch is
# exercised implicitly by every other row in each table.
DOMAIN_GROUPS = {
    "hr": "hr_admins",
    "finance": "finance_admins",
}
# This SP's application id/userName - read from env, not hardcoded (public repo).
SEEDED_MEMBER = os.environ["ABAC_OAUTH_CLIENT_ID"]

STATEMENTS: list[str] = [
    f"CREATE CATALOG IF NOT EXISTS {CATALOG} MANAGED LOCATION '{CATALOG_MANAGED_LOCATION}'",
    f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{GOV_SCHEMA}",

    f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.{GOV_SCHEMA}.group_membership (
  group_name STRING,
  member_email STRING
) COMMENT 'ABAC control table: logical group -> member identity. Used by
  every domain row filter below via is_group_member().'
""",
    "INSERT OVERWRITE TABLE {}.{}.group_membership (group_name, member_email) VALUES\n  {}".format(
        CATALOG, GOV_SCHEMA,
        ",\n  ".join(f"('{g}', '{SEEDED_MEMBER}')" for g in DOMAIN_GROUPS.values()),
    ),
    f"""
CREATE OR REPLACE FUNCTION {CATALOG}.{GOV_SCHEMA}.is_group_member(p_group_name STRING)
RETURNS BOOLEAN
COMMENT 'Checks membership in the logical group_membership control table for current_user()'
RETURN EXISTS (
  SELECT 1 FROM {CATALOG}.{GOV_SCHEMA}.group_membership m
  WHERE m.group_name = p_group_name AND m.member_email = current_user()
)
""",
]

# ---------------------------------------------------------------------------
# hr - rf_hr_department shared by employees_rls_tbl + employees_both_tbl;
#      mask_ssn_hr shared by employees_mask_tbl + employees_both_tbl.
# ---------------------------------------------------------------------------
STATEMENTS += [
    f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.hr",

    f"""CREATE OR REPLACE FUNCTION {CATALOG}.hr.rf_hr_department(department STRING)
RETURNS BOOLEAN
COMMENT 'RLS: hr_admins group see every department; everyone else only public ones'
RETURN {CATALOG}.{GOV_SCHEMA}.is_group_member('hr_admins')
  OR department IN ('Public Relations', 'Customer Support')""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.hr.mask_ssn_hr(v STRING)
RETURNS STRING COMMENT 'Column mask: fully redacts SSN' RETURN '***-**-****'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.hr.mask_salary_hr(v DOUBLE)
RETURNS DOUBLE COMMENT 'Column mask: nulls out salary' RETURN CAST(NULL AS DOUBLE)""",

    # RLS only (function #1)
    f"""CREATE OR REPLACE TABLE {CATALOG}.hr.employees_rls_tbl (
  id BIGINT, department STRING, region STRING, employee_name STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.hr.employees_rls_tbl VALUES
  (1, 'Engineering', 'east', 'Meera Krishnan'),
  (2, 'Finance Ops', 'west', 'Arjun Malhotra'),
  (3, 'Public Relations', 'east', 'Kavya Reddy'),
  (4, 'Legal', 'north', 'Vikram Chawla'),
  (5, 'Customer Support', 'south', 'Neha Kapoor'),
  (6, 'HR Admin', 'east', 'Rohan Bose')""",
    f"ALTER TABLE {CATALOG}.hr.employees_rls_tbl SET ROW FILTER {CATALOG}.hr.rf_hr_department ON (department)",

    # Mask only (functions #2, #3)
    f"""CREATE OR REPLACE TABLE {CATALOG}.hr.employees_mask_tbl (
  id BIGINT, employee_name STRING, ssn STRING, salary DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.hr.employees_mask_tbl VALUES
  (1, 'Meera Krishnan', '121-22-3333', 96000.0),
  (2, 'Arjun Malhotra', '222-32-4444', 88500.0),
  (3, 'Kavya Reddy', '323-44-5555', 71000.0),
  (4, 'Vikram Chawla', '424-55-6666', 115000.0)""",
    f"ALTER TABLE {CATALOG}.hr.employees_mask_tbl ALTER COLUMN ssn SET MASK {CATALOG}.hr.mask_ssn_hr",
    f"ALTER TABLE {CATALOG}.hr.employees_mask_tbl ALTER COLUMN salary SET MASK {CATALOG}.hr.mask_salary_hr",

    # Both (reuses rf_hr_department + mask_ssn_hr - function #1 and #2 each
    # now guard 2 tables in this schema)
    f"""CREATE OR REPLACE TABLE {CATALOG}.hr.employees_both_tbl (
  id BIGINT, department STRING, ssn STRING, salary DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.hr.employees_both_tbl VALUES
  (1, 'Engineering', '525-66-7777', 99500.0),
  (2, 'Public Relations', '626-77-8888', 64000.0),
  (3, 'Legal', '727-88-9999', 121500.0)""",
    f"ALTER TABLE {CATALOG}.hr.employees_both_tbl SET ROW FILTER {CATALOG}.hr.rf_hr_department ON (department)",
    f"ALTER TABLE {CATALOG}.hr.employees_both_tbl ALTER COLUMN ssn SET MASK {CATALOG}.hr.mask_ssn_hr",
]

# ---------------------------------------------------------------------------
# finance - rf_finance_business_unit shared by transactions_rls_tbl +
#           invoices_both_tbl; mask_acct_finance shared by
#           accounts_mask_tbl + invoices_both_tbl.
# ---------------------------------------------------------------------------
STATEMENTS += [
    f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.finance",

    f"""CREATE OR REPLACE FUNCTION {CATALOG}.finance.rf_finance_business_unit(business_unit STRING)
RETURNS BOOLEAN
COMMENT 'RLS: finance_admins group see every business unit; everyone else only Public Reporting'
RETURN {CATALOG}.{GOV_SCHEMA}.is_group_member('finance_admins')
  OR business_unit = 'Public Reporting'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.finance.mask_acct_finance(v STRING)
RETURNS STRING COMMENT 'Column mask: shows only last 4 digits of account number'
RETURN CONCAT('XXXX-XXXX-', RIGHT(v, 4))""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.finance.mask_balance_finance(v DOUBLE)
RETURNS DOUBLE COMMENT 'Column mask: nulls out balance' RETURN CAST(NULL AS DOUBLE)""",

    # RLS only
    f"""CREATE OR REPLACE TABLE {CATALOG}.finance.transactions_rls_tbl (
  id BIGINT, business_unit STRING, amount DOUBLE, description STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.finance.transactions_rls_tbl VALUES
  (1, 'Retail', 16200.0, 'Store fixture purchase'),
  (2, 'Wholesale', 79500.0, 'Bulk inventory order'),
  (3, 'Treasury', 512000.0, 'Interbank transfer'),
  (4, 'Public Reporting', 1350.0, 'Quarterly disclosure filing fee'),
  (5, 'Wholesale', 44500.0, 'Distributor rebate')""",
    f"ALTER TABLE {CATALOG}.finance.transactions_rls_tbl SET ROW FILTER {CATALOG}.finance.rf_finance_business_unit ON (business_unit)",

    # Mask only
    f"""CREATE OR REPLACE TABLE {CATALOG}.finance.accounts_mask_tbl (
  id BIGINT, account_holder STRING, account_number STRING, balance DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.finance.accounts_mask_tbl VALUES
  (1, 'Summit Retail Ltd', '1324567890123456', 260000.0),
  (2, 'Harbor Wholesale Co', '2435678901234567', 975000.0),
  (3, 'Treasury Ops Desk', '3546789012345678', 5120000.0)""",
    f"ALTER TABLE {CATALOG}.finance.accounts_mask_tbl ALTER COLUMN account_number SET MASK {CATALOG}.finance.mask_acct_finance",
    f"ALTER TABLE {CATALOG}.finance.accounts_mask_tbl ALTER COLUMN balance SET MASK {CATALOG}.finance.mask_balance_finance",

    # Both (reuses rf_finance_business_unit + mask_acct_finance)
    f"""CREATE OR REPLACE TABLE {CATALOG}.finance.invoices_both_tbl (
  id BIGINT, business_unit STRING, account_number STRING, balance DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.finance.invoices_both_tbl VALUES
  (1, 'Retail', '4657890123456789', 15900.0),
  (2, 'Public Reporting', '5768901234567890', 910.0),
  (3, 'Treasury', '6879012345678901', 735000.0)""",
    f"ALTER TABLE {CATALOG}.finance.invoices_both_tbl SET ROW FILTER {CATALOG}.finance.rf_finance_business_unit ON (business_unit)",
    f"ALTER TABLE {CATALOG}.finance.invoices_both_tbl ALTER COLUMN account_number SET MASK {CATALOG}.finance.mask_acct_finance",
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

    print("\nDone. Catalog ready:", CATALOG)
    print("Function -> table sharing map (this is what CATALOG scope should consolidate):")
    print(f"  {CATALOG}.hr.rf_hr_department        -> employees_rls_tbl, employees_both_tbl")
    print(f"  {CATALOG}.hr.mask_ssn_hr              -> employees_mask_tbl, employees_both_tbl")
    print(f"  {CATALOG}.finance.rf_finance_business_unit -> transactions_rls_tbl, invoices_both_tbl")
    print(f"  {CATALOG}.finance.mask_acct_finance       -> accounts_mask_tbl, invoices_both_tbl")


if __name__ == "__main__":
    main()
