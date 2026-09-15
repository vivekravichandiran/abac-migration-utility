"""Dedicated test catalog covering ALL 6 combinations of
{MANAGED, STREAMING_TABLE} x {RLS-only, mask-only, both} - created to give
the STREAMING_TABLE eligibility fix (Track A, 2026-09-15) a full,
side-by-side regression fixture against the well-established managed-table
flavors, on the same workspace/warehouse as ril_full_access_test.

Catalog: ril_streaming_flavors_test
Schemas: governance (group_membership + is_group_member control table,
          same pattern as ril_full_access_test), demo (all 6 tables + the
          3 streaming-table backing sources)

Tables (schema `demo`):
  managed_rls_tbl     - MANAGED,        RLS only   (rf_region_managed)
  managed_mask_tbl    - MANAGED,        mask only  (mask_email_managed)
  managed_both_tbl    - MANAGED,        both       (reuses both functions above)
  src_streaming_rls_tbl / src_streaming_mask_tbl / src_streaming_both_tbl
                      - plain backing Delta source tables for the 3
                        streaming tables below (each streaming table needs
                        its own dedicated source - reusing one source
                        across multiple streaming tables, or recreating a
                        source in place, breaks Delta's streaming
                        checkpoint lineage - learned live on 2026-09-15)
  streaming_rls_tbl   - STREAMING_TABLE, RLS only  (rf_region_streaming)
  streaming_mask_tbl  - STREAMING_TABLE, mask only (mask_email_streaming)
  streaming_both_tbl  - STREAMING_TABLE, both      (reuses both functions above)

Access: GRANT ALL PRIVILEGES ON CATALOG ... TO vivek.ravichandiran@databricks.com
(no OWNER transfer - see ril_full_access_test's script for why: transferring
ownership away from the creating SP revokes its own MANAGE access with no
way back).

Auth: same OAuth M2M bridge as the other ril_* spike scripts
(ABAC_OAUTH_HOST/ABAC_OAUTH_CLIENT_ID/ABAC_OAUTH_CLIENT_SECRET env vars).
"""
from __future__ import annotations

import os

from abac_migration.spike._oauth_m2m import refresh as refresh_oauth_token
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "ril_catalog_test_pat"
WAREHOUSE_ID = "5fe1692f119e2528"
CATALOG = "ril_streaming_flavors_test"
GOV_SCHEMA = "governance"
SCHEMA = "demo"
GRANTEE = "vivek.ravichandiran@databricks.com"

CATALOG_MANAGED_LOCATION = (
    "abfss://unity-catalog-storage@dbstorageziwqzkb2dgooo.dfs.core.windows.net"
    f"/7405616318078204/{CATALOG}"
)

# This SP's application id - read from env, not hardcoded (public repo).
SEEDED_MEMBER = os.environ["ABAC_OAUTH_CLIENT_ID"]

STATEMENTS: list[str] = [
    f"CREATE CATALOG IF NOT EXISTS {CATALOG} MANAGED LOCATION '{CATALOG_MANAGED_LOCATION}'",
    f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{GOV_SCHEMA}",
    f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}",

    f"""CREATE TABLE IF NOT EXISTS {CATALOG}.{GOV_SCHEMA}.group_membership (
  group_name STRING, member_email STRING
) COMMENT 'ABAC control table: logical group -> member identity, used by every row filter below via is_group_member().'""",
    "INSERT OVERWRITE TABLE {}.{}.group_membership (group_name, member_email) VALUES\n  ('{}', '{}')".format(
        CATALOG, GOV_SCHEMA, "data_admins", SEEDED_MEMBER,
    ),
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.{GOV_SCHEMA}.is_group_member(p_group_name STRING)
RETURNS BOOLEAN
COMMENT 'Checks membership in the logical group_membership control table for current_user()'
RETURN EXISTS (
  SELECT 1 FROM {CATALOG}.{GOV_SCHEMA}.group_membership m
  WHERE m.group_name = p_group_name AND m.member_email = current_user()
)""",

    # -- shared legacy functions -----------------------------------------
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.rf_region_managed(region STRING)
RETURNS BOOLEAN COMMENT 'RLS: data_admins see all regions; everyone else only east'
RETURN {CATALOG}.{GOV_SCHEMA}.is_group_member('data_admins') OR region = 'east'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.mask_email_managed(v STRING)
RETURNS STRING COMMENT 'Column mask: fully redacts an email address' RETURN '***MANAGED-MASKED***'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.rf_region_streaming(region STRING)
RETURNS BOOLEAN COMMENT 'RLS: data_admins see all regions; everyone else only east'
RETURN {CATALOG}.{GOV_SCHEMA}.is_group_member('data_admins') OR region = 'east'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.mask_email_streaming(v STRING)
RETURNS STRING COMMENT 'Column mask: fully redacts an email address' RETURN '***STREAMING-MASKED***'""",

    # ===================== MANAGED flavors ==============================
    # 1. MANAGED - RLS only
    f"""CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.managed_rls_tbl (
  id BIGINT, region STRING, customer_name STRING, amount DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.{SCHEMA}.managed_rls_tbl VALUES
  (1, 'east', 'Ananya Iyer', 1500.0),
  (2, 'west', 'Rahul Nair', 4200.0),
  (3, 'north', 'Divya Menon', 900.0),
  (4, 'south', 'Karthik Subramanian', 7800.0)""",
    f"ALTER TABLE {CATALOG}.{SCHEMA}.managed_rls_tbl SET ROW FILTER {CATALOG}.{SCHEMA}.rf_region_managed ON (region)",

    # 2. MANAGED - mask only
    f"""CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.managed_mask_tbl (
  id BIGINT, customer_name STRING, customer_email STRING, amount DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.{SCHEMA}.managed_mask_tbl VALUES
  (1, 'Ananya Iyer', 'ananya.iyer@example.com', 1500.0),
  (2, 'Rahul Nair', 'rahul.nair@example.com', 4200.0),
  (3, 'Divya Menon', 'divya.menon@example.com', 900.0)""",
    f"ALTER TABLE {CATALOG}.{SCHEMA}.managed_mask_tbl ALTER COLUMN customer_email SET MASK {CATALOG}.{SCHEMA}.mask_email_managed",

    # 3. MANAGED - both (reuses rf_region_managed + mask_email_managed)
    f"""CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.managed_both_tbl (
  id BIGINT, region STRING, customer_email STRING, amount DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.{SCHEMA}.managed_both_tbl VALUES
  (1, 'east', 'ananya.iyer@example.com', 1500.0),
  (2, 'west', 'rahul.nair@example.com', 4200.0),
  (3, 'north', 'divya.menon@example.com', 900.0)""",
    f"ALTER TABLE {CATALOG}.{SCHEMA}.managed_both_tbl SET ROW FILTER {CATALOG}.{SCHEMA}.rf_region_managed ON (region)",
    f"ALTER TABLE {CATALOG}.{SCHEMA}.managed_both_tbl ALTER COLUMN customer_email SET MASK {CATALOG}.{SCHEMA}.mask_email_managed",

    # ===================== STREAMING_TABLE flavors ======================
    # backing sources (one dedicated source per streaming table)
    f"""CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.src_streaming_rls_tbl (
  id BIGINT, region STRING, customer_name STRING, amount DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.{SCHEMA}.src_streaming_rls_tbl VALUES
  (1, 'east', 'Meera Krishnan', 1650.0),
  (2, 'west', 'Arjun Malhotra', 3900.0),
  (3, 'north', 'Kavya Reddy', 1100.0),
  (4, 'south', 'Vikram Chawla', 8600.0)""",

    f"""CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.src_streaming_mask_tbl (
  id BIGINT, customer_name STRING, customer_email STRING, amount DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.{SCHEMA}.src_streaming_mask_tbl VALUES
  (1, 'Meera Krishnan', 'meera.krishnan@example.com', 1650.0),
  (2, 'Arjun Malhotra', 'arjun.malhotra@example.com', 3900.0),
  (3, 'Kavya Reddy', 'kavya.reddy@example.com', 1100.0)""",

    f"""CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.src_streaming_both_tbl (
  id BIGINT, region STRING, customer_email STRING, amount DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.{SCHEMA}.src_streaming_both_tbl VALUES
  (1, 'east', 'meera.krishnan@example.com', 1650.0),
  (2, 'west', 'arjun.malhotra@example.com', 3900.0),
  (3, 'north', 'kavya.reddy@example.com', 1100.0)""",

    # 4. STREAMING_TABLE - RLS only
    f"CREATE OR REFRESH STREAMING TABLE {CATALOG}.{SCHEMA}.streaming_rls_tbl AS SELECT * FROM STREAM {CATALOG}.{SCHEMA}.src_streaming_rls_tbl",
    f"ALTER STREAMING TABLE {CATALOG}.{SCHEMA}.streaming_rls_tbl SET ROW FILTER {CATALOG}.{SCHEMA}.rf_region_streaming ON (region)",

    # 5. STREAMING_TABLE - mask only
    f"CREATE OR REFRESH STREAMING TABLE {CATALOG}.{SCHEMA}.streaming_mask_tbl AS SELECT * FROM STREAM {CATALOG}.{SCHEMA}.src_streaming_mask_tbl",
    f"ALTER STREAMING TABLE {CATALOG}.{SCHEMA}.streaming_mask_tbl ALTER COLUMN customer_email SET MASK {CATALOG}.{SCHEMA}.mask_email_streaming",

    # 6. STREAMING_TABLE - both (reuses rf_region_streaming + mask_email_streaming)
    f"CREATE OR REFRESH STREAMING TABLE {CATALOG}.{SCHEMA}.streaming_both_tbl AS SELECT * FROM STREAM {CATALOG}.{SCHEMA}.src_streaming_both_tbl",
    f"ALTER STREAMING TABLE {CATALOG}.{SCHEMA}.streaming_both_tbl SET ROW FILTER {CATALOG}.{SCHEMA}.rf_region_streaming ON (region)",
    f"ALTER STREAMING TABLE {CATALOG}.{SCHEMA}.streaming_both_tbl ALTER COLUMN customer_email SET MASK {CATALOG}.{SCHEMA}.mask_email_streaming",

    # -- full access grant -------------------------------------------------
    f"GRANT ALL PRIVILEGES ON CATALOG {CATALOG} TO `{GRANTEE}`",
]

ALL_TABLES = [
    "managed_rls_tbl", "managed_mask_tbl", "managed_both_tbl",
    "streaming_rls_tbl", "streaming_mask_tbl", "streaming_both_tbl",
]


def main():
    refresh_oauth_token()
    print(f"\nRunning {len(STATEMENTS)} DDL/DML/GRANT statements against {PROFILE}...")
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()

    for stmt in STATEMENTS:
        r = client.run(stmt, timeout_s=180)
        status = "OK" if r.status == "SUCCEEDED" else f"FAILED: {r.error_code} {r.error}"
        first_line = stmt.strip().splitlines()[0][:100]
        print(f"[{status}] {first_line}")
        if r.status != "SUCCEEDED":
            raise SystemExit(f"Aborting on: {stmt}\n-> {r.error_code} {r.error}")

    print("\nDone. Catalog ready:", CATALOG)
    print(f"Full access (ALL PRIVILEGES) granted to: {GRANTEE}")
    print("\nType/DDL check for all 6 tables:")
    for t in ALL_TABLES:
        r = client.run(f"DESCRIBE TABLE EXTENDED {CATALOG}.{SCHEMA}.{t}")
        info = {row[0].strip(): row[1] for row in r.rows if row[0]}
        print(f"  {t}: Type={info.get('Type')} RowFilter={'Row Filter' in info} ColMasksHeader={'# Column Masks' in info}")


if __name__ == "__main__":
    main()
