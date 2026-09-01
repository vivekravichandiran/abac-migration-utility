"""Dedicated, disposable test catalog on the SOURCE workspace
(adb-7405618912789045), created specifically to exercise the no-wheel,
workspace-file-sync deployment end to end via the three ISOLATED-PHASE jobs
(Inventory -> Apply ABAC -> Finalize), mirroring the same 4-domain,
RLS-only/mask-only/both structure used for `ril_abac_test` /
`ril_abac_manual_test` on the target workspace.

Catalog: ril_abac_e2e_test
Schemas: hr, finance, sales, healthcare (+ governance)
Tables:  3 per domain schema (12 total) -> 4 RLS-only, 4 mask-only, 4 both.

See `setup_ril_abac_test_catalog.py`'s module docstring for the full
rationale on the group-membership control-table workaround (this
workspace's PAT/SCIM access can only create workspace-local groups, which
UC GRANT/is_account_group_member() cannot resolve) and the deliberate
function-reuse-within-a-domain pattern (both apply identically here).
"""
from __future__ import annotations

from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL
from abac_sales_demo.identity import Scim

PROFILE = "uc_source"
WAREHOUSE_ID = "525de76b2ccdd7d5"
CATALOG = "ril_abac_e2e_test"
GOV_SCHEMA = "governance"

DOMAIN_GROUPS = {
    "hr": "hr_admins",
    "finance": "finance_admins",
    "sales": "sales_managers",
    "healthcare": "healthcare_providers",
}
SEEDED_MEMBER_EMAIL = "vivek.ravichandiran@databricks.com"

CATALOG_MANAGED_LOCATION = (
    "abfss://unity-catalog-storage@dbstoragem73nzis4ihj6a.dfs.core.windows.net"
    f"/7405618912789045/vkdemo/{CATALOG}"
)

STATEMENTS: list[str] = [
    f"CREATE CATALOG IF NOT EXISTS {CATALOG} MANAGED LOCATION '{CATALOG_MANAGED_LOCATION}'",
    f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{GOV_SCHEMA}",

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
  (1, 'Engineering', 'east', 'Meera Krishnan'),
  (2, 'Finance Ops', 'west', 'Arjun Malhotra'),
  (3, 'Public Relations', 'east', 'Kavya Reddy'),
  (4, 'Legal', 'north', 'Vikram Chawla'),
  (5, 'Customer Support', 'south', 'Neha Kapoor'),
  (6, 'HR Admin', 'east', 'Rohan Bose')""",
    f"ALTER TABLE {CATALOG}.hr.employees_rls_tbl SET ROW FILTER {CATALOG}.hr.rf_hr_department ON (department)",

    # Mask only
    f"""CREATE OR REPLACE TABLE {CATALOG}.hr.employees_mask_tbl (
  id BIGINT, employee_name STRING, ssn STRING, salary DOUBLE, personal_email STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.hr.employees_mask_tbl VALUES
  (1, 'Meera Krishnan', '121-22-3333', 96000.0, 'meera.krishnan@example.com'),
  (2, 'Arjun Malhotra', '222-32-4444', 88500.0, 'arjun.malhotra@example.com'),
  (3, 'Kavya Reddy', '323-44-5555', 71000.0, 'kavya.reddy@example.com'),
  (4, 'Vikram Chawla', '424-55-6666', 115000.0, 'vikram.chawla@example.com')""",
    f"ALTER TABLE {CATALOG}.hr.employees_mask_tbl ALTER COLUMN ssn SET MASK {CATALOG}.hr.mask_ssn_hr",
    f"ALTER TABLE {CATALOG}.hr.employees_mask_tbl ALTER COLUMN salary SET MASK {CATALOG}.hr.mask_salary_hr",
    f"ALTER TABLE {CATALOG}.hr.employees_mask_tbl ALTER COLUMN personal_email SET MASK {CATALOG}.hr.mask_email_hr",

    # Both (reuses rf_hr_department + mask_ssn_hr + mask_salary_hr)
    f"""CREATE OR REPLACE TABLE {CATALOG}.hr.employees_both_tbl (
  id BIGINT, department STRING, ssn STRING, salary DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.hr.employees_both_tbl VALUES
  (1, 'Engineering', '525-66-7777', 99500.0),
  (2, 'Public Relations', '626-77-8888', 64000.0),
  (3, 'Legal', '727-88-9999', 121500.0)""",
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
  (1, 'Retail', 16200.0, 'Store fixture purchase'),
  (2, 'Wholesale', 79500.0, 'Bulk inventory order'),
  (3, 'Treasury', 512000.0, 'Interbank transfer'),
  (4, 'Public Reporting', 1350.0, 'Quarterly disclosure filing fee'),
  (5, 'Wholesale', 44500.0, 'Distributor rebate')""",
    f"ALTER TABLE {CATALOG}.finance.transactions_rls_tbl SET ROW FILTER {CATALOG}.finance.rf_finance_business_unit ON (business_unit)",

    # Mask only
    f"""CREATE OR REPLACE TABLE {CATALOG}.finance.accounts_mask_tbl (
  id BIGINT, account_holder STRING, account_number STRING, routing_number STRING, balance DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.finance.accounts_mask_tbl VALUES
  (1, 'Summit Retail Ltd', '1324567890123456', '021000031', 260000.0),
  (2, 'Harbor Wholesale Co', '2435678901234567', '021000099', 975000.0),
  (3, 'Treasury Ops Desk', '3546789012345678', '021000133', 5120000.0)""",
    f"ALTER TABLE {CATALOG}.finance.accounts_mask_tbl ALTER COLUMN account_number SET MASK {CATALOG}.finance.mask_acct_finance",
    f"ALTER TABLE {CATALOG}.finance.accounts_mask_tbl ALTER COLUMN routing_number SET MASK {CATALOG}.finance.mask_routing_finance",
    f"ALTER TABLE {CATALOG}.finance.accounts_mask_tbl ALTER COLUMN balance SET MASK {CATALOG}.finance.mask_balance_finance",

    # Both (reuses rf_finance_business_unit + mask_acct_finance + mask_balance_finance)
    f"""CREATE OR REPLACE TABLE {CATALOG}.finance.invoices_both_tbl (
  id BIGINT, business_unit STRING, account_number STRING, balance DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.finance.invoices_both_tbl VALUES
  (1, 'Retail', '4657890123456789', 15900.0),
  (2, 'Public Reporting', '5768901234567890', 910.0),
  (3, 'Treasury', '6879012345678901', 735000.0)""",
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
  (1, 'APAC', 'Zenith Traders', 4350.0),
  (2, 'EMEA', 'Falcon Retail', 9100.0),
  (3, 'AMER', 'Meridian Foods', 1600.0),
  (4, 'Global', 'Sample Demo Buyer', 320.0),
  (5, 'APAC', 'Orbit Mart Two', 6850.0)""",
    f"ALTER TABLE {CATALOG}.sales.orders_rls_tbl SET ROW FILTER {CATALOG}.sales.rf_sales_region ON (region)",

    # Mask only
    f"""CREATE OR REPLACE TABLE {CATALOG}.sales.customers_mask_tbl (
  id BIGINT, customer_name STRING, email STRING, phone STRING, credit_card_last4 STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.sales.customers_mask_tbl VALUES
  (1, 'Zenith Traders', 'contact@zenithtraders.example.com', '555-020-1111', '5252'),
  (2, 'Falcon Retail', 'orders@falconretail.example.com', '555-020-2222', '1991'),
  (3, 'Meridian Foods', 'billing@meridianfoods.example.com', '555-020-3333', '0015')""",
    f"ALTER TABLE {CATALOG}.sales.customers_mask_tbl ALTER COLUMN email SET MASK {CATALOG}.sales.mask_email_sales",
    f"ALTER TABLE {CATALOG}.sales.customers_mask_tbl ALTER COLUMN phone SET MASK {CATALOG}.sales.mask_phone_sales",
    f"ALTER TABLE {CATALOG}.sales.customers_mask_tbl ALTER COLUMN credit_card_last4 SET MASK {CATALOG}.sales.mask_cc_sales",

    # Both (reuses rf_sales_region + mask_email_sales)
    f"""CREATE OR REPLACE TABLE {CATALOG}.sales.deals_both_tbl (
  id BIGINT, region STRING, email STRING, deal_value DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.sales.deals_both_tbl VALUES
  (1, 'APAC', 'deal.desk@zenithtraders.example.com', 53500.0),
  (2, 'Global', 'deal.desk@sampledemo.example.com', 9250.0),
  (3, 'EMEA', 'deal.desk@falconretail.example.com', 77500.0)""",
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
  (1, 'General Hospital', 'Aditi Rao', 'J45.909'),
  (2, 'Cardiology Wing', 'Sameer Khanna', 'I25.10'),
  (3, 'Pediatrics Wing', 'Ishaan Gupta', 'J06.9'),
  (4, 'Public Health Clinic', 'Demo Patient', 'Z00.00')""",
    f"ALTER TABLE {CATALOG}.healthcare.patients_rls_tbl SET ROW FILTER {CATALOG}.healthcare.rf_healthcare_facility ON (facility)",

    # Mask only
    f"""CREATE OR REPLACE TABLE {CATALOG}.healthcare.records_mask_tbl (
  id BIGINT, patient_name STRING, ssn STRING, medical_record_number STRING, diagnosis STRING) USING DELTA""",
    f"""INSERT INTO {CATALOG}.healthcare.records_mask_tbl VALUES
  (1, 'Aditi Rao', '787-11-2222', 'MRN-000133', 'Asthma, unspecified'),
  (2, 'Sameer Khanna', '888-23-3333', 'MRN-000466', 'Atherosclerotic heart disease'),
  (3, 'Ishaan Gupta', '999-34-4444', 'MRN-000799', 'Acute upper respiratory infection')""",
    f"ALTER TABLE {CATALOG}.healthcare.records_mask_tbl ALTER COLUMN ssn SET MASK {CATALOG}.healthcare.mask_ssn_healthcare",
    f"ALTER TABLE {CATALOG}.healthcare.records_mask_tbl ALTER COLUMN medical_record_number SET MASK {CATALOG}.healthcare.mask_mrn_healthcare",
    f"ALTER TABLE {CATALOG}.healthcare.records_mask_tbl ALTER COLUMN diagnosis SET MASK {CATALOG}.healthcare.mask_diagnosis_healthcare",

    # Both (reuses rf_healthcare_facility + mask_ssn_healthcare)
    f"""CREATE OR REPLACE TABLE {CATALOG}.healthcare.visits_both_tbl (
  id BIGINT, facility STRING, ssn STRING, treatment_cost DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.healthcare.visits_both_tbl VALUES
  (1, 'General Hospital', '121-99-8888', 4350.0),
  (2, 'Public Health Clinic', '232-88-7777', 160.0),
  (3, 'Cardiology Wing', '343-77-6666', 9950.0)""",
    f"ALTER TABLE {CATALOG}.healthcare.visits_both_tbl SET ROW FILTER {CATALOG}.healthcare.rf_healthcare_facility ON (facility)",
    f"ALTER TABLE {CATALOG}.healthcare.visits_both_tbl ALTER COLUMN ssn SET MASK {CATALOG}.healthcare.mask_ssn_healthcare",
]


def main():
    print(f"Ensuring workspace groups exist (one per domain, {PROFILE})...")
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
