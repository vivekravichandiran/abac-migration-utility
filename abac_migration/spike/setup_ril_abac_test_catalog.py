"""Builds a brand-new, richer test catalog on the TARGET workspace
(adb-7405609958717235.15) for exercising the ABAC migration utility against
a realistic multi-domain estate: several schemas, several tables per
schema, a deliberate mix of RLS-only / mask-only / both-flavored tables,
dummy PII data, and workspace groups referenced by the row filters.

Catalog: ril_abac_test
Schemas: hr, finance, sales, healthcare (+ governance, for the group
         membership control table shared by all four domains)
Tables:  3 per domain schema (12 total) -> 4 RLS-only, 4 mask-only, 4 both.

Group note (see abac_sales_demo/README.md for the full story): this
workspace's PAT/SCIM access can only create WORKSPACE-LOCAL groups, which
Unity Catalog's GRANT/is_account_group_member() cannot resolve. So this
script (a) still creates one real workspace SCIM group per domain via
`abac_sales_demo.identity.Scim`, for visibility/documentation, and (b) the
actual enforcement in every row filter goes through a `governance.
is_group_member()` control-table check (same proven pattern as
abac_sales_demo/ddl.py) rather than `is_account_group_member()`.
Production deployments should create real account-level groups via the
Account Console/Account API instead and drop the control-table workaround.

Deliberately reuses several functions across two tables within the same
domain (one row-filter function + two mask functions per domain each guard
a column on both that domain's "_rls"/"_mask" table AND its "_both" table)
so the resulting governed-tag corpus exercises BOTH shapes covered in
tag_provisioner.py: one tag key -> many (table, column) values (shared
functions) and one tag key -> one value (single-use functions).
"""
from __future__ import annotations

from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL
from abac_sales_demo.identity import Scim

PROFILE = "uc_target"
WAREHOUSE_ID = "03ac4037e7134a18"
CATALOG = "ril_abac_test"
GOV_SCHEMA = "governance"

# One workspace group per domain + who's seeded as a member of it (so
# querying as this user demonstrates the "admin" bypass branch of each row
# filter). Real account-level groups would let a wider audience be added;
# see the module docstring for why this uses the control-table workaround.
DOMAIN_GROUPS = {
    "hr": "hr_admins",
    "finance": "finance_admins",
    "sales": "sales_managers",
    "healthcare": "healthcare_providers",
}
SEEDED_MEMBER_EMAIL = "vivek.ravichandiran@databricks.com"

# This workspace's Unity Catalog metastore has no default storage root
# configured, so every catalog needs an explicit MANAGED LOCATION - reusing
# the same storage-account/container the pre-existing `ril_raw` catalog uses
# (`.../7405609958717235/ril_raw`), just under a catalog-specific subpath,
# since that path prefix is already confirmed writable from this workspace.
CATALOG_MANAGED_LOCATION = (
    "abfss://unity-catalog-storage@dbstorageisbf2ky3sgcdc.dfs.core.windows.net"
    f"/7405609958717235/{CATALOG}"
)

STATEMENTS: list[str] = [
    f"CREATE CATALOG IF NOT EXISTS {CATALOG} MANAGED LOCATION '{CATALOG_MANAGED_LOCATION}'",
    f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{GOV_SCHEMA}",

    # -- shared group-membership control table + checker function ---------
    f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.{GOV_SCHEMA}.group_membership (
  group_name STRING,
  member_email STRING
) COMMENT 'ABAC control table: logical group -> member emails. Used by every
  domain row filter below via is_group_member(), because this workspace''s
  PAT/SCIM access can only create workspace-local groups, which Unity
  Catalog GRANT/is_account_group_member() cannot resolve. Production
  deployments should create real account-level groups instead.'
""",
    "INSERT OVERWRITE TABLE {}.{}.group_membership (group_name, member_email) VALUES\n  {}".format(
        CATALOG, GOV_SCHEMA,
        ",\n  ".join(f"('{g}', '{SEEDED_MEMBER_EMAIL}')" for g in DOMAIN_GROUPS.values()),
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
# hr
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
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.hr.mask_email_hr(v STRING)
RETURNS STRING COMMENT 'Column mask: partial email mask'
RETURN CONCAT(LEFT(v, 1), '***@masked.com')""",

    # RLS only
    f"""CREATE OR REPLACE TABLE {CATALOG}.hr.employees_rls_tbl (
  id BIGINT, department STRING, region STRING, employee_name STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.hr.employees_rls_tbl VALUES
  (1, 'Engineering', 'east', 'Priya Nair'),
  (2, 'Finance Ops', 'west', 'Rahul Verma'),
  (3, 'Public Relations', 'east', 'Ananya Iyer'),
  (4, 'Legal', 'north', 'Karan Mehta'),
  (5, 'Customer Support', 'south', 'Divya Shah'),
  (6, 'HR Admin', 'east', 'Sanjay Rao')""",
    f"ALTER TABLE {CATALOG}.hr.employees_rls_tbl SET ROW FILTER {CATALOG}.hr.rf_hr_department ON (department)",

    # Mask only
    f"""CREATE OR REPLACE TABLE {CATALOG}.hr.employees_mask_tbl (
  id BIGINT, employee_name STRING, ssn STRING, salary DOUBLE, personal_email STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.hr.employees_mask_tbl VALUES
  (1, 'Priya Nair', '111-22-3333', 95000.0, 'priya.nair@example.com'),
  (2, 'Rahul Verma', '222-33-4444', 87000.0, 'rahul.verma@example.com'),
  (3, 'Ananya Iyer', '333-44-5555', 72000.0, 'ananya.iyer@example.com'),
  (4, 'Karan Mehta', '444-55-6666', 110000.0, 'karan.mehta@example.com')""",
    f"ALTER TABLE {CATALOG}.hr.employees_mask_tbl ALTER COLUMN ssn SET MASK {CATALOG}.hr.mask_ssn_hr",
    f"ALTER TABLE {CATALOG}.hr.employees_mask_tbl ALTER COLUMN salary SET MASK {CATALOG}.hr.mask_salary_hr",
    f"ALTER TABLE {CATALOG}.hr.employees_mask_tbl ALTER COLUMN personal_email SET MASK {CATALOG}.hr.mask_email_hr",

    # Both (reuses rf_hr_department + mask_ssn_hr + mask_salary_hr)
    f"""CREATE OR REPLACE TABLE {CATALOG}.hr.employees_both_tbl (
  id BIGINT, department STRING, ssn STRING, salary DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.hr.employees_both_tbl VALUES
  (1, 'Engineering', '555-66-7777', 98000.0),
  (2, 'Public Relations', '666-77-8888', 65000.0),
  (3, 'Legal', '777-88-9999', 120000.0)""",
    f"ALTER TABLE {CATALOG}.hr.employees_both_tbl SET ROW FILTER {CATALOG}.hr.rf_hr_department ON (department)",
    f"ALTER TABLE {CATALOG}.hr.employees_both_tbl ALTER COLUMN ssn SET MASK {CATALOG}.hr.mask_ssn_hr",
    f"ALTER TABLE {CATALOG}.hr.employees_both_tbl ALTER COLUMN salary SET MASK {CATALOG}.hr.mask_salary_hr",
]

# ---------------------------------------------------------------------------
# finance
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
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.finance.mask_routing_finance(v STRING)
RETURNS STRING COMMENT 'Column mask: fully redacts routing number' RETURN 'REDACTED'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.finance.mask_balance_finance(v DOUBLE)
RETURNS DOUBLE COMMENT 'Column mask: nulls out balance' RETURN CAST(NULL AS DOUBLE)""",

    # RLS only
    f"""CREATE OR REPLACE TABLE {CATALOG}.finance.transactions_rls_tbl (
  id BIGINT, business_unit STRING, amount DOUBLE, description STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.finance.transactions_rls_tbl VALUES
  (1, 'Retail', 15000.0, 'Store fixture purchase'),
  (2, 'Wholesale', 82000.0, 'Bulk inventory order'),
  (3, 'Treasury', 500000.0, 'Interbank transfer'),
  (4, 'Public Reporting', 1200.0, 'Quarterly disclosure filing fee'),
  (5, 'Wholesale', 43000.0, 'Distributor rebate')""",
    f"ALTER TABLE {CATALOG}.finance.transactions_rls_tbl SET ROW FILTER {CATALOG}.finance.rf_finance_business_unit ON (business_unit)",

    # Mask only
    f"""CREATE OR REPLACE TABLE {CATALOG}.finance.accounts_mask_tbl (
  id BIGINT, account_holder STRING, account_number STRING, routing_number STRING, balance DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.finance.accounts_mask_tbl VALUES
  (1, 'Acme Retail Ltd', '1234567890123456', '021000021', 250000.0),
  (2, 'Blue Wholesale Co', '2345678901234567', '021000089', 980000.0),
  (3, 'Treasury Ops Desk', '3456789012345678', '021000122', 5000000.0)""",
    f"ALTER TABLE {CATALOG}.finance.accounts_mask_tbl ALTER COLUMN account_number SET MASK {CATALOG}.finance.mask_acct_finance",
    f"ALTER TABLE {CATALOG}.finance.accounts_mask_tbl ALTER COLUMN routing_number SET MASK {CATALOG}.finance.mask_routing_finance",
    f"ALTER TABLE {CATALOG}.finance.accounts_mask_tbl ALTER COLUMN balance SET MASK {CATALOG}.finance.mask_balance_finance",

    # Both (reuses rf_finance_business_unit + mask_acct_finance + mask_balance_finance)
    f"""CREATE OR REPLACE TABLE {CATALOG}.finance.invoices_both_tbl (
  id BIGINT, business_unit STRING, account_number STRING, balance DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.finance.invoices_both_tbl VALUES
  (1, 'Retail', '4567890123456789', 15400.0),
  (2, 'Public Reporting', '5678901234567890', 890.0),
  (3, 'Treasury', '6789012345678901', 720000.0)""",
    f"ALTER TABLE {CATALOG}.finance.invoices_both_tbl SET ROW FILTER {CATALOG}.finance.rf_finance_business_unit ON (business_unit)",
    f"ALTER TABLE {CATALOG}.finance.invoices_both_tbl ALTER COLUMN account_number SET MASK {CATALOG}.finance.mask_acct_finance",
    f"ALTER TABLE {CATALOG}.finance.invoices_both_tbl ALTER COLUMN balance SET MASK {CATALOG}.finance.mask_balance_finance",
]

# ---------------------------------------------------------------------------
# sales
# ---------------------------------------------------------------------------
STATEMENTS += [
    f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.sales",

    f"""CREATE OR REPLACE FUNCTION {CATALOG}.sales.rf_sales_region(region STRING)
RETURNS BOOLEAN
COMMENT 'RLS: sales_managers group see every region; everyone else only Global'
RETURN {CATALOG}.{GOV_SCHEMA}.is_group_member('sales_managers')
  OR region = 'Global'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.sales.mask_email_sales(v STRING)
RETURNS STRING COMMENT 'Column mask: partial email mask'
RETURN CONCAT(LEFT(v, 1), '***@masked.com')""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.sales.mask_phone_sales(v STRING)
RETURNS STRING COMMENT 'Column mask: shows only last 4 digits of phone'
RETURN CONCAT('***-***-', RIGHT(v, 4))""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.sales.mask_cc_sales(v STRING)
RETURNS STRING COMMENT 'Column mask: prefixes a masked card pattern in front of the stored last-4'
RETURN CONCAT('****-****-****-', v)""",

    # RLS only
    f"""CREATE OR REPLACE TABLE {CATALOG}.sales.orders_rls_tbl (
  id BIGINT, region STRING, customer_name STRING, order_total DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.sales.orders_rls_tbl VALUES
  (1, 'APAC', 'Nova Traders', 4200.0),
  (2, 'EMEA', 'Halberd Retail', 8900.0),
  (3, 'AMER', 'Crescent Foods', 1500.0),
  (4, 'Global', 'Sample Demo Buyer', 300.0),
  (5, 'APAC', 'Orbit Mart', 6700.0)""",
    f"ALTER TABLE {CATALOG}.sales.orders_rls_tbl SET ROW FILTER {CATALOG}.sales.rf_sales_region ON (region)",

    # Mask only
    f"""CREATE OR REPLACE TABLE {CATALOG}.sales.customers_mask_tbl (
  id BIGINT, customer_name STRING, email STRING, phone STRING, credit_card_last4 STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.sales.customers_mask_tbl VALUES
  (1, 'Nova Traders', 'contact@novatraders.example.com', '555-010-1111', '4242'),
  (2, 'Halberd Retail', 'orders@halberdretail.example.com', '555-010-2222', '1881'),
  (3, 'Crescent Foods', 'billing@crescentfoods.example.com', '555-010-3333', '0005')""",
    f"ALTER TABLE {CATALOG}.sales.customers_mask_tbl ALTER COLUMN email SET MASK {CATALOG}.sales.mask_email_sales",
    f"ALTER TABLE {CATALOG}.sales.customers_mask_tbl ALTER COLUMN phone SET MASK {CATALOG}.sales.mask_phone_sales",
    f"ALTER TABLE {CATALOG}.sales.customers_mask_tbl ALTER COLUMN credit_card_last4 SET MASK {CATALOG}.sales.mask_cc_sales",

    # Both (reuses rf_sales_region + mask_email_sales)
    f"""CREATE OR REPLACE TABLE {CATALOG}.sales.deals_both_tbl (
  id BIGINT, region STRING, email STRING, deal_value DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.sales.deals_both_tbl VALUES
  (1, 'APAC', 'deal.desk@novatraders.example.com', 52000.0),
  (2, 'Global', 'deal.desk@sampledemo.example.com', 9000.0),
  (3, 'EMEA', 'deal.desk@halberdretail.example.com', 76000.0)""",
    f"ALTER TABLE {CATALOG}.sales.deals_both_tbl SET ROW FILTER {CATALOG}.sales.rf_sales_region ON (region)",
    f"ALTER TABLE {CATALOG}.sales.deals_both_tbl ALTER COLUMN email SET MASK {CATALOG}.sales.mask_email_sales",
]

# ---------------------------------------------------------------------------
# healthcare
# ---------------------------------------------------------------------------
STATEMENTS += [
    f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.healthcare",

    f"""CREATE OR REPLACE FUNCTION {CATALOG}.healthcare.rf_healthcare_facility(facility STRING)
RETURNS BOOLEAN
COMMENT 'RLS: healthcare_providers group see every facility; everyone else only the public clinic'
RETURN {CATALOG}.{GOV_SCHEMA}.is_group_member('healthcare_providers')
  OR facility = 'Public Health Clinic'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.healthcare.mask_ssn_healthcare(v STRING)
RETURNS STRING COMMENT 'Column mask: fully redacts SSN' RETURN '***-**-****'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.healthcare.mask_mrn_healthcare(v STRING)
RETURNS STRING COMMENT 'Column mask: fully redacts medical record number' RETURN 'REDACTED'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.healthcare.mask_diagnosis_healthcare(v STRING)
RETURNS STRING COMMENT 'Column mask: fully redacts diagnosis (PHI)' RETURN 'REDACTED-PHI'""",

    # RLS only
    f"""CREATE OR REPLACE TABLE {CATALOG}.healthcare.patients_rls_tbl (
  id BIGINT, facility STRING, patient_name STRING, diagnosis_code STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.healthcare.patients_rls_tbl VALUES
  (1, 'General Hospital', 'Jordan Lee', 'J45.909'),
  (2, 'Cardiology Wing', 'Sam Patel', 'I25.10'),
  (3, 'Pediatrics Wing', 'Robin Fox', 'J06.9'),
  (4, 'Public Health Clinic', 'Demo Patient', 'Z00.00')""",
    f"ALTER TABLE {CATALOG}.healthcare.patients_rls_tbl SET ROW FILTER {CATALOG}.healthcare.rf_healthcare_facility ON (facility)",

    # Mask only
    f"""CREATE OR REPLACE TABLE {CATALOG}.healthcare.records_mask_tbl (
  id BIGINT, patient_name STRING, ssn STRING, medical_record_number STRING, diagnosis STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.healthcare.records_mask_tbl VALUES
  (1, 'Jordan Lee', '777-11-2222', 'MRN-000123', 'Asthma, unspecified'),
  (2, 'Sam Patel', '888-22-3333', 'MRN-000456', 'Atherosclerotic heart disease'),
  (3, 'Robin Fox', '999-33-4444', 'MRN-000789', 'Acute upper respiratory infection')""",
    f"ALTER TABLE {CATALOG}.healthcare.records_mask_tbl ALTER COLUMN ssn SET MASK {CATALOG}.healthcare.mask_ssn_healthcare",
    f"ALTER TABLE {CATALOG}.healthcare.records_mask_tbl ALTER COLUMN medical_record_number SET MASK {CATALOG}.healthcare.mask_mrn_healthcare",
    f"ALTER TABLE {CATALOG}.healthcare.records_mask_tbl ALTER COLUMN diagnosis SET MASK {CATALOG}.healthcare.mask_diagnosis_healthcare",

    # Both (reuses rf_healthcare_facility + mask_ssn_healthcare)
    f"""CREATE OR REPLACE TABLE {CATALOG}.healthcare.visits_both_tbl (
  id BIGINT, facility STRING, ssn STRING, treatment_cost DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.healthcare.visits_both_tbl VALUES
  (1, 'General Hospital', '111-99-8888', 4200.0),
  (2, 'Public Health Clinic', '222-88-7777', 150.0),
  (3, 'Cardiology Wing', '333-77-6666', 9800.0)""",
    f"ALTER TABLE {CATALOG}.healthcare.visits_both_tbl SET ROW FILTER {CATALOG}.healthcare.rf_healthcare_facility ON (facility)",
    f"ALTER TABLE {CATALOG}.healthcare.visits_both_tbl ALTER COLUMN ssn SET MASK {CATALOG}.healthcare.mask_ssn_healthcare",
]


def main():
    print(f"Creating workspace groups (one per domain, {PROFILE})...")
    scim = Scim(PROFILE)
    for domain, group_name in DOMAIN_GROUPS.items():
        scim.create_group(group_name)

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


if __name__ == "__main__":
    main()
