"""Live fixture for MATERIALIZED_VIEW support (Track B of the
streaming-tables ABAC plan, DESIGN.md §16 item 2) - reuses the same
ril_full_access_test.streaming_test schema as the STREAMING_TABLE fixture
(setup_streaming_table_test.py), idempotently (re)creating:

  source_events_tbl (already exists, shared with the streaming-table
    fixture) - plain MANAGED Delta source.
  events_mv - a real MATERIALIZED VIEW built via
    `CREATE OR REFRESH MATERIALIZED VIEW ... AS SELECT * FROM
    source_events_tbl` (requires a Serverless SQL warehouse, same as
    STREAMING TABLE - confirmed available on this profile), with a legacy
    row filter + column mask attached DIRECTLY via `ALTER MATERIALIZED VIEW
    ...` (NOT `ALTER TABLE` - confirmed live during the TC-03 negative
    control in STREAMING_TABLE_SUPPORT_TEST_CASES.md that plain
    `ALTER TABLE ...` fails outright against a materialized view with
    BAD_REQUEST [EXPECT_TABLE_NOT_VIEW.NO_ALTERNATIVE]).

This is the SAME `events_mv` object first created ad hoc during that
negative-control spike (functions rf_region_mv/mask_email_mv, kept
identical here for continuity) - this script just makes recreating it from
scratch reproducible/idempotent for future test runs.

Auth: same OAuth M2M bridge as the other ril_* spike scripts
(ABAC_OAUTH_HOST/ABAC_OAUTH_CLIENT_ID/ABAC_OAUTH_CLIENT_SECRET env vars).
"""
from __future__ import annotations

from abac_migration.spike._oauth_m2m import refresh as refresh_oauth_token
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "ril_catalog_test_pat"
WAREHOUSE_ID = "5fe1692f119e2528"
CATALOG = "ril_full_access_test"
SCHEMA = "streaming_test"

STATEMENTS: list[str] = [
    f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}",

    f"""CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.source_events_tbl (
  id BIGINT, region STRING, customer_email STRING, amount DOUBLE) USING DELTA""",
    f"""INSERT OVERWRITE TABLE {CATALOG}.{SCHEMA}.source_events_tbl VALUES
  (1, 'east', 'ananya.iyer@example.com', 150.0),
  (2, 'west', 'rahul.nair@example.com', 420.0),
  (3, 'north', 'divya.menon@example.com', 90.0)""",

    f"""CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.rf_region_mv(region STRING)
RETURNS BOOLEAN COMMENT 'RLS: only region=east visible - simple, no group-membership dependency'
RETURN region = 'east'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.mask_email_mv(v STRING)
RETURNS STRING COMMENT 'Column mask: fully redacts an email address'
RETURN '***MV-MASKED***'""",

    f"""CREATE OR REFRESH MATERIALIZED VIEW {CATALOG}.{SCHEMA}.events_mv
AS SELECT * FROM {CATALOG}.{SCHEMA}.source_events_tbl""",
    f"ALTER MATERIALIZED VIEW {CATALOG}.{SCHEMA}.events_mv SET ROW FILTER {CATALOG}.{SCHEMA}.rf_region_mv ON (region)",
    f"ALTER MATERIALIZED VIEW {CATALOG}.{SCHEMA}.events_mv ALTER COLUMN customer_email SET MASK {CATALOG}.{SCHEMA}.mask_email_mv",
]


def main():
    refresh_oauth_token()
    print(f"\nRunning {len(STATEMENTS)} DDL/DML statements against {PROFILE}...")
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()

    for stmt in STATEMENTS:
        r = client.run(stmt, timeout_s=180)
        status = "OK" if r.status == "SUCCEEDED" else f"FAILED: {r.error_code} {r.error}"
        first_line = stmt.strip().splitlines()[0][:100]
        print(f"[{status}] {first_line}")
        if r.status != "SUCCEEDED":
            raise SystemExit(f"Aborting on: {stmt}\n-> {r.error_code} {r.error}")

    print(f"\nDone. Schema ready: {CATALOG}.{SCHEMA}")
    print(f"  events_mv (MATERIALIZED_VIEW) -> rf_region_mv (RLS) + mask_email_mv (column mask), both via ALTER MATERIALIZED VIEW")

    r = client.run(f"DESCRIBE TABLE EXTENDED {CATALOG}.{SCHEMA}.events_mv")
    info = {row[0].strip(): row[1] for row in r.rows if row[0]}
    print(f"\nType/DDL check: Type={info.get('Type')} RowFilter={'Row Filter' in info} ColMasksHeader={'# Column Masks' in info}")


if __name__ == "__main__":
    main()
