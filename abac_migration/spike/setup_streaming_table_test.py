"""Live fixture: a real STREAMING_TABLE in ril_full_access_test, created to
verify the streaming_tables_abac_findings.md analysis end-to-end (current
skip behavior today, and to have a ready fixture for the future
implementation once STREAMING_TABLE support is added).

Schema: ril_full_access_test.streaming_test
  - source_events_tbl  - plain MANAGED Delta table (the "append-only
    source" a streaming table reads from).
  - events_streaming_tbl - a real STREAMING TABLE built via
    `CREATE STREAMING TABLE ... AS SELECT * FROM STREAM source_events_tbl`
    (requires a Serverless SQL warehouse - confirmed available on this
    profile: "Serverless Starter Warehouse").
  - A legacy row filter + column mask are then attached directly to
    events_streaming_tbl via `ALTER STREAMING TABLE ...` (NOT
    `ALTER TABLE` - confirmed live requirement from the findings doc) so
    INVENTORY correctly discovers it as STREAMING_TABLE + has legacy
    security, letting us confirm today's NOT_ELIGIBLE/UNSUPPORTED_TABLE_TYPE
    behavior end-to-end, and re-run APPLY_ABAC against it once support is
    implemented.

Auth: same OAuth M2M bridge as the other ril_* spike scripts.
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

    # -- plain append-only source table ---------------------------------
    f"""CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.source_events_tbl (
  id BIGINT, region STRING, customer_email STRING, amount DOUBLE) USING DELTA""",
    f"""INSERT INTO {CATALOG}.{SCHEMA}.source_events_tbl VALUES
  (1, 'east', 'ananya.iyer@example.com', 150.0),
  (2, 'west', 'rahul.nair@example.com', 420.0),
  (3, 'north', 'divya.menon@example.com', 90.0)""",

    # -- the streaming table itself ---------------------------------------
    f"""CREATE OR REFRESH STREAMING TABLE {CATALOG}.{SCHEMA}.events_streaming_tbl
AS SELECT * FROM STREAM {CATALOG}.{SCHEMA}.source_events_tbl""",

    # -- legacy row filter + mask function, applied via ALTER STREAMING TABLE --
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.rf_region_streaming(region STRING)
RETURNS BOOLEAN COMMENT 'RLS fixture for streaming-table support testing' RETURN region = 'east'""",
    f"""CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.mask_email_streaming(v STRING)
RETURNS STRING COMMENT 'Mask fixture for streaming-table support testing' RETURN '***EMAIL-MASKED***'""",
    f"ALTER STREAMING TABLE {CATALOG}.{SCHEMA}.events_streaming_tbl SET ROW FILTER {CATALOG}.{SCHEMA}.rf_region_streaming ON (region)",
    f"ALTER STREAMING TABLE {CATALOG}.{SCHEMA}.events_streaming_tbl ALTER COLUMN customer_email SET MASK {CATALOG}.{SCHEMA}.mask_email_streaming",
]


def main():
    refresh_oauth_token()
    print(f"\nRunning {len(STATEMENTS)} DDL/DML statements against {PROFILE}...")
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()

    for stmt in STATEMENTS:
        r = client.run(stmt, timeout_s=180)
        status = "OK" if r.status == "SUCCEEDED" else f"FAILED: {r.error_code} {r.error}"
        first_line = stmt.strip().splitlines()[0][:110]
        print(f"[{status}] {first_line}")
        if r.status != "SUCCEEDED":
            print(f"  -> continuing despite failure, to see how far we get: {r.error_code} {r.error}")

    print(f"\nDone (see above for any failures). Schema: {CATALOG}.{SCHEMA}")

    print("\nDESCRIBE TABLE EXTENDED events_streaming_tbl (to confirm the 'Type' value UC reports):")
    r = client.run(f"DESCRIBE TABLE EXTENDED {CATALOG}.{SCHEMA}.events_streaming_tbl")
    for row in r.rows:
        if row[0] and (row[0].strip() in ("Type", "Row Filter") or "Mask" in (row[0] or "")):
            print("  ", row)


if __name__ == "__main__":
    main()
