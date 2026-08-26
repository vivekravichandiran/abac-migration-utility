"""Ad hoc exploration of the pre-existing sales_abac_demo fixtures across
catalogs, used to plan the real-workspace test run (not part of the
shipped utility)."""
from __future__ import annotations

from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "uc_source"
WAREHOUSE_ID = "525de76b2ccdd7d5"


def describe(client, fqn):
    r = client.run(f"DESCRIBE TABLE EXTENDED {fqn}")
    rows = [list(row) for row in r.rows]
    row_filter = None
    masks = []
    for i, row in enumerate(rows):
        if row[0] == "Row Filter":
            row_filter = row[1]
        elif row[0] == "# Column Masks":
            j = i + 1
            while j < len(rows) and rows[j][0] and not rows[j][0].startswith("#"):
                masks.append((rows[j][0], rows[j][1]))
                j += 1
    return row_filter, masks


def main():
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()

    catalogs = ["ril_raw", "ril_curated", "ril_bulk", "ril_migration", "ril_sandbox"]
    for catalog in catalogs:
        r = client.run(f"SHOW TABLES IN {catalog}.sales_abac_demo")
        tables = [row[1] for row in r.rows]
        print(f"\n=== {catalog}.sales_abac_demo === tables: {tables}")
        for t in tables:
            rf, masks = describe(client, f"{catalog}.sales_abac_demo.{t}")
            print(f"  {t}: row_filter={rf!r} masks={masks}")

    print("\n=== Existing ABAC policies on ril_raw.sales_abac_demo.customers ===")
    r = client.run("SHOW POLICIES ON TABLE ril_raw.sales_abac_demo.customers")
    print(r.columns, r.rows)

    print("\n=== Existing governed tags ===")
    r = client.run("SHOW GOVERNED TAGS")
    print(r.columns, r.rows[:20])


if __name__ == "__main__":
    main()
