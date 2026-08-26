"""Persist/read InventoryRecords to the audit catalog (§2, §4.1). Contains
no business logic - eligibility decisions are inventory_manager's job.
"""
from __future__ import annotations

import json

from ..audit.sql_literals import sql_literal
from ..uc_gateway.gateway import UnityCatalogGateway
from .inventory_manager import InventoryRecord


class InventoryRepository:
    def __init__(self, uc: UnityCatalogGateway, inventory_table_fqn: str):
        self._uc = uc
        self._inventory_table_fqn = inventory_table_fqn

    def append(self, record: InventoryRecord, dry_run: bool = False) -> None:
        row = {
            "run_id": record.run_id, "inventoried_at": record.inventoried_at,
            "catalog": record.catalog, "schema": record.schema, "table": record.table,
            "full_name": record.full_name, "table_type": record.table_type,
            "has_row_filter": record.has_row_filter, "row_filter_function": record.row_filter_function,
            "row_filter_columns": record.row_filter_columns,
            "row_filter_expression_text": record.row_filter_expression_text,
            "has_column_masks": record.has_column_masks, "column_masks": record.column_masks,
            "has_existing_abac_policy": record.has_existing_abac_policy,
            "existing_abac_policy_names": record.existing_abac_policy_names,
            "migration_eligibility": record.migration_eligibility,
            "eligibility_reason": record.eligibility_reason,
            "current_migration_status": record.current_migration_status,
            "row_filter_suggested_pii_tag": record.row_filter_suggested_pii_tag,
            "column_mask_suggested_pii_tags": (
                json.dumps(record.column_mask_suggested_pii_tags) if record.column_mask_suggested_pii_tags else None
            ),
        }
        columns = ", ".join(row.keys())
        values = ", ".join(sql_literal(v) for v in row.values())
        statement = f"INSERT INTO {self._inventory_table_fqn} ({columns}) VALUES ({values})"
        self._uc.run_sql(statement, dry_run=dry_run)

    def for_run(self, run_id: str) -> list:
        return self._uc.run_sql(f"SELECT * FROM {self._inventory_table_fqn} WHERE run_id = '{run_id}'")
