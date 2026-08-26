"""Deploys the sales ABAC demo (schema, 5 tables + dummy PII data, column
masks, RLS row filters) into one or more ril_* catalogs on uc_source.

Usage:
    python3 -m abac_sales_demo.deploy ril_raw
    python3 -m abac_sales_demo.deploy ril_raw ril_bulk ril_curated ril_sandbox ril_migration
"""
from __future__ import annotations

import sys

from . import ddl
from .config import PROFILE, SCHEMA_NAME, TABLES, WAREHOUSE_ID
from .data_gen import generate_dataset
from .sql_client import DatabricksSQL


def deploy_catalog(client: DatabricksSQL, catalog: str) -> None:
    print(f"\n=== Deploying to {catalog}.{SCHEMA_NAME} ===")

    client.exec_or_raise(ddl.create_schema_sql(catalog, SCHEMA_NAME))
    print("  schema ready")

    dataset = generate_dataset(catalog)

    for table in TABLES:
        client.exec_or_raise(f"DROP TABLE IF EXISTS {catalog}.{SCHEMA_NAME}.{table}")
        client.exec_or_raise(ddl.create_table_sql(catalog, SCHEMA_NAME, table))
        client.exec_or_raise(ddl.insert_sql(catalog, SCHEMA_NAME, table, dataset[table]))
        print(f"  table {table}: created + {len(dataset[table])} rows inserted")

    for fn_sql in ddl.masking_functions_sql(catalog, SCHEMA_NAME):
        client.exec_or_raise(fn_sql)
    print("  masking functions created")

    for table in TABLES:
        for alter in ddl.alter_masks_sql(catalog, SCHEMA_NAME, table):
            client.exec_or_raise(alter)
    print("  column masks applied")

    client.exec_or_raise(ddl.create_group_membership_table_sql(catalog, SCHEMA_NAME))
    client.exec_or_raise(ddl.seed_group_membership_sql(catalog, SCHEMA_NAME))
    client.exec_or_raise(ddl.is_group_member_function_sql(catalog, SCHEMA_NAME))
    print("  group_membership control table + is_group_member() created/seeded")

    client.exec_or_raise(ddl.row_filter_function_sql(catalog, SCHEMA_NAME))
    print("  row-filter function created")

    for table in TABLES:
        client.exec_or_raise(ddl.alter_row_filter_sql(catalog, SCHEMA_NAME, table))
    print("  row filters applied")

    for grant in ddl.grant_sql(catalog, SCHEMA_NAME):
        client.exec_or_raise(grant)
    print("  grants applied to bu_retail_group / bu_o2c_group")

    print(f"=== {catalog}.{SCHEMA_NAME} deployment complete ===")


def main():
    catalogs = sys.argv[1:]
    if not catalogs:
        print("usage: python3 -m abac_sales_demo.deploy <catalog> [<catalog> ...]")
        sys.exit(1)

    client = DatabricksSQL(PROFILE, WAREHOUSE_ID)
    print("Starting SQL warehouse (if stopped)...")
    client.ensure_warehouse_running()
    print("Warehouse is RUNNING")

    for catalog in catalogs:
        deploy_catalog(client, catalog)


if __name__ == "__main__":
    main()
