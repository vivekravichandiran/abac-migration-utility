"""Verifies every Databricks API the ABAC migration design (DESIGN.md §13)
depends on, against the throwaway table created by setup_spike.py. Prints a
PASS/FAIL ledger; does not assume any result in advance.
"""
from __future__ import annotations

from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "uc_source"
WAREHOUSE_ID = "525de76b2ccdd7d5"
CATALOG = "ril_raw"
SCHEMA = "abac_api_spike"
TABLE = "spike_orders"
FQN = f"{CATALOG}.{SCHEMA}.{TABLE}"

RESULTS = []


def check(label: str, client: ResilientDatabricksSQL, stmt: str, expect_success: bool | None = None):
    res = client.run(stmt)
    ok = res.status == "SUCCEEDED"
    verdict = "PASS" if (expect_success is None or ok == expect_success) else "UNEXPECTED"
    RESULTS.append((label, ok, res.error_code, res.error))
    print(f"[{verdict}] {label} -> status={res.status} error_code={res.error_code}")
    if res.error:
        print(f"    {res.error[:300]}")
    if ok and res.rows:
        for row in res.rows[:10]:
            print(f"    row: {row}")
    return res


def main():
    client = ResilientDatabricksSQL(PROFILE, WAREHOUSE_ID)
    client.ensure_warehouse_running()

    print("\n=== 1. DESCRIBE TABLE EXTENDED (baseline legacy state) ===")
    r = client.run(f"DESCRIBE TABLE EXTENDED {FQN}")
    for row in r.rows:
        if "filter" in str(row).lower() or "mask" in str(row).lower():
            print("   ", row)

    print("\n=== 2. CREATE POLICY (ROW FILTER) while legacy row filter still active ===")
    check(
        "CREATE POLICY row filter (table-scoped, no MATCH COLUMNS) coexisting with legacy RLS",
        client,
        f"""
        CREATE OR REPLACE POLICY abac_migrated_row_filter
        ON TABLE {FQN}
        COMMENT 'spike test'
        ROW FILTER {CATALOG}.{SCHEMA}.spike_rf
        TO `account users`
        FOR TABLES
        USING COLUMNS (business_unit)
        """,
    )

    print("\n=== 3. SHOW POLICIES ON TABLE ===")
    check("SHOW POLICIES ON TABLE", client, f"SHOW POLICIES ON TABLE {FQN}")

    print("\n=== 4. SHOW EFFECTIVE POLICIES ON TABLE ===")
    check("SHOW EFFECTIVE POLICIES ON TABLE", client, f"SHOW EFFECTIVE POLICIES ON TABLE {FQN}")

    print("\n=== 5. DESCRIBE POLICY ON TABLE ===")
    check("DESCRIBE POLICY ON TABLE", client, f"DESCRIBE POLICY abac_migrated_row_filter ON TABLE {FQN}")

    print("\n=== 6. CREATE POLICY (COLUMN MASK) without MATCH COLUMNS, coexisting with legacy mask ===")
    check(
        "CREATE POLICY column mask (table-scoped, ON COLUMN literal, no MATCH COLUMNS)",
        client,
        f"""
        CREATE OR REPLACE POLICY abac_migrated_mask_customer_email
        ON TABLE {FQN}
        COMMENT 'spike test'
        COLUMN MASK {CATALOG}.{SCHEMA}.spike_mask
        TO `account users`
        FOR TABLES
        ON COLUMN customer_email
        """,
    )

    print("\n=== 6b. If 6 failed: retry using MATCH COLUMNS + alias form ===")
    if not RESULTS[-1][1]:
        check(
            "CREATE POLICY column mask via MATCH COLUMNS alias fallback",
            client,
            f"""
            CREATE OR REPLACE POLICY abac_migrated_mask_customer_email
            ON TABLE {FQN}
            COMMENT 'spike test fallback'
            COLUMN MASK {CATALOG}.{SCHEMA}.spike_mask
            TO `account users`
            FOR TABLES
            MATCH COLUMNS regexp_like(column_name, '^customer_email$') AS pii_col
            ON COLUMN pii_col
            USING COLUMNS (pii_col)
            """,
        )

    print("\n=== 7. information_schema.abac_policy_definitions ===")
    check(
        "query abac_policy_definitions",
        client,
        f"""
        SELECT policy_name, policy_type, on_securable_type, securable_name
        FROM {CATALOG}.information_schema.abac_policy_definitions
        WHERE schema_name = '{SCHEMA}'
        """,
    )

    print("\n=== 8. Query table now: does it error with BOTH legacy + ABAC active? ===")
    check("SELECT * FROM spike table with legacy+ABAC both active", client, f"SELECT * FROM {FQN} ORDER BY order_id")

    print("\n=== 9. ALTER TABLE DROP ROW FILTER (removing legacy while ABAC policy remains) ===")
    check("ALTER TABLE ... DROP ROW FILTER", client, f"ALTER TABLE {FQN} DROP ROW FILTER")

    print("\n=== 10. ALTER TABLE ALTER COLUMN DROP MASK ===")
    check("ALTER TABLE ... ALTER COLUMN ... DROP MASK", client,
          f"ALTER TABLE {FQN} ALTER COLUMN customer_email DROP MASK")

    print("\n=== 11. DESCRIBE TABLE EXTENDED after removing legacy (ABAC policy should still show via SHOW POLICIES) ===")
    r = client.run(f"DESCRIBE TABLE EXTENDED {FQN}")
    for row in r.rows:
        if "filter" in str(row).lower() or "mask" in str(row).lower():
            print("   ", row)
    check("SHOW POLICIES ON TABLE (post legacy-removal)", client, f"SHOW POLICIES ON TABLE {FQN}")

    print("\n=== 12. Query table again: does ABAC policy alone still enforce? ===")
    check("SELECT * FROM spike table with ABAC only", client, f"SELECT * FROM {FQN} ORDER BY order_id")

    print("\n=== 13. DROP POLICY ON TABLE (rollback path) ===")
    check("DROP POLICY IF EXISTS ... ON TABLE", client,
          f"DROP POLICY IF EXISTS abac_migrated_row_filter ON TABLE {FQN}")
    check("DROP POLICY IF EXISTS (mask) ON TABLE", client,
          f"DROP POLICY IF EXISTS abac_migrated_mask_customer_email ON TABLE {FQN}")

    print("\n=== 14. Confirm policies gone ===")
    check("SHOW POLICIES ON TABLE (post drop)", client, f"SHOW POLICIES ON TABLE {FQN}")

    print("\n\n================ SUMMARY ================")
    for label, ok, code, err in RESULTS:
        print(f"{'PASS' if ok else 'FAIL':5} | {label}" + (f"  [{code}]" if code else ""))

    print(f"\nTotal API calls: {client.total_calls}, retried calls: {client.total_retried_calls}")


if __name__ == "__main__":
    main()
