"""Creates a throwaway table (with a real legacy row filter + column mask,
same pattern as the sales ABAC demo) purely for verifying the Databricks
APIs the ABAC migration utility design depends on. Isolated in its own
schema so it never touches the sales_abac_demo tables.
"""
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "uc_source"
WAREHOUSE_ID = "525de76b2ccdd7d5"
CATALOG = "ril_raw"
SCHEMA = "abac_api_spike"
TABLE = "spike_orders"


def run(stmt: str, client: ResilientDatabricksSQL, label: str):
    res = client.run(stmt)
    status = "OK" if res.status == "SUCCEEDED" else f"FAIL ({res.error_code}: {res.error})"
    print(f"[{status}] {label}")
    if res.status != "SUCCEEDED":
        print(f"    stmt: {stmt.strip()[:200]}")
    return res


def main():
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()

    run(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}", client, "create spike schema")
    run(f"DROP TABLE IF EXISTS {CATALOG}.{SCHEMA}.{TABLE}", client, "drop pre-existing spike table")
    run(f"""
        CREATE TABLE {CATALOG}.{SCHEMA}.{TABLE} (
          order_id BIGINT,
          business_unit STRING,
          customer_email STRING
        )
    """, client, "create spike table")
    run(f"""
        INSERT INTO {CATALOG}.{SCHEMA}.{TABLE} VALUES
          (1, 'Retail', 'a@example.com'),
          (2, 'O2C', 'b@example.com')
    """, client, "insert spike rows")

    run(f"""
        CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.spike_rf(business_unit STRING)
        RETURNS BOOLEAN
        RETURN is_account_group_member('metastore_admins') OR business_unit = 'Retail'
    """, client, "create legacy row-filter function")
    run(f"""
        CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.spike_mask(v STRING)
        RETURNS STRING
        RETURN CASE WHEN is_account_group_member('metastore_admins') THEN v ELSE 'MASKED' END
    """, client, "create legacy mask function")

    run(f"ALTER TABLE {CATALOG}.{SCHEMA}.{TABLE} SET ROW FILTER {CATALOG}.{SCHEMA}.spike_rf ON (business_unit)",
        client, "apply legacy row filter")
    run(f"ALTER TABLE {CATALOG}.{SCHEMA}.{TABLE} ALTER COLUMN customer_email SET MASK {CATALOG}.{SCHEMA}.spike_mask",
        client, "apply legacy column mask")

    print("\nSpike table ready:", f"{CATALOG}.{SCHEMA}.{TABLE}")


if __name__ == "__main__":
    main()
