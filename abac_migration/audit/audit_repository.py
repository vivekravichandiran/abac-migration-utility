"""Append-only writes/reads of MigrationAuditRecord rows (§2, §4.2). Owns
the inline DDL for BOTH audit tables (§12) - inventory/inventory_repository.py
reuses INVENTORY_TABLE_DDL from here rather than duplicating it, but writes/
reads its own rows independently.

Never treated as source of truth for current UC state - UC always is
(§4.4, §7). "Current status of table X" is always the latest row here,
ordered by completed_at, but only ever used as a *hint*.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Optional

from ..uc_gateway.gateway import UnityCatalogGateway
from .sql_literals import sql_literal

INVENTORY_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {fqn} (
  run_id STRING,
  inventoried_at TIMESTAMP,
  catalog STRING,
  schema STRING,
  table STRING,
  full_name STRING,
  table_type STRING,
  has_row_filter BOOLEAN,
  row_filter_function STRING,
  row_filter_columns ARRAY<STRING>,
  row_filter_expression_text STRING,
  has_column_masks BOOLEAN,
  column_masks ARRAY<STRUCT<column: STRING, function: STRING>>,
  has_existing_abac_policy BOOLEAN,
  existing_abac_policy_names ARRAY<STRING>,
  migration_eligibility STRING,
  eligibility_reason STRING,
  current_migration_status STRING,
  row_filter_suggested_pii_tag STRING,
  column_mask_suggested_pii_tags STRING
) USING DELTA
""".strip()

MIGRATION_AUDIT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {fqn} (
  run_id STRING,
  attempt_id STRING,
  catalog STRING,
  schema STRING,
  table STRING,
  object_type STRING,
  masked_column STRING,
  source_security_type STRING,
  source_function STRING,
  source_definition STRING,
  target_policy_name STRING,
  target_policy_type STRING,
  target_definition STRING,
  status STRING,
  error_code STRING,
  error_message STRING,
  validation_status STRING,
  rollback_metadata STRING,
  -- Coarse "how far through the isolated-phase pipeline is this object"
  -- signal, orthogonal to `status`: FINALIZED | ABAC_APPLIED (not final -
  -- legacy still present alongside the new ABAC policy) | DRY_RUN | FAILED |
  -- NOT_APPLICABLE. See table_converter._migration_phase_for().
  migration_phase STRING,
  started_at TIMESTAMP,
  completed_at TIMESTAMP,
  dry_run BOOLEAN
) USING DELTA
""".strip()

# `migration_audit` is append-only (one row-set per object per run - see
# module docstring), so "current state" always means "latest row per
# object", never "any row". This view does that dedup once, server-side,
# so operators/dashboards don't have to remember the QUALIFY ROW_NUMBER()
# pattern every time they want to answer "what's the current status of
# everything?" - ties out with `latest_status()` below, which answers the
# same question for a single table from Python.
MIGRATION_AUDIT_LATEST_VIEW_DDL = """
CREATE OR REPLACE VIEW {view_fqn} AS
SELECT run_id, attempt_id, catalog, schema, table, object_type, masked_column,
       source_security_type, source_function, source_definition,
       target_policy_name, target_policy_type, target_definition,
       status, error_code, error_message, validation_status, rollback_metadata,
       migration_phase, started_at, completed_at, dry_run
FROM {audit_fqn}
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY catalog, schema, table, object_type, masked_column
  ORDER BY completed_at DESC
) = 1
""".strip()


@dataclass(frozen=True)
class MigrationAuditRecord:
    run_id: str
    attempt_id: str
    catalog: str
    schema: str
    table: str
    object_type: str  # "ROW_FILTER" | "COLUMN_MASK"
    status: str
    masked_column: Optional[str] = None
    source_security_type: Optional[str] = None
    source_function: Optional[str] = None
    source_definition: Optional[str] = None
    target_policy_name: Optional[str] = None
    target_policy_type: Optional[str] = None
    target_definition: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    validation_status: str = "NOT_RUN"
    rollback_metadata: dict = field(default_factory=dict)
    migration_phase: Optional[str] = None
    started_at: Optional[dt.datetime] = None
    completed_at: Optional[dt.datetime] = None
    dry_run: bool = True

    def as_row_dict(self) -> dict:
        return {
            "run_id": self.run_id, "attempt_id": self.attempt_id,
            "catalog": self.catalog, "schema": self.schema, "table": self.table,
            "object_type": self.object_type, "masked_column": self.masked_column,
            "source_security_type": self.source_security_type, "source_function": self.source_function,
            "source_definition": self.source_definition, "target_policy_name": self.target_policy_name,
            "target_policy_type": self.target_policy_type, "target_definition": self.target_definition,
            "status": self.status, "error_code": self.error_code, "error_message": self.error_message,
            "validation_status": self.validation_status,
            "rollback_metadata": json.dumps(self.rollback_metadata) if self.rollback_metadata else None,
            "migration_phase": self.migration_phase,
            "started_at": self.started_at, "completed_at": self.completed_at, "dry_run": self.dry_run,
        }


class AuditRepository:
    def __init__(
        self,
        uc: UnityCatalogGateway,
        audit_full_schema: str,
        audit_table_fqn: str,
        inventory_table_fqn: str,
        latest_status_view_fqn: Optional[str] = None,
    ):
        self._uc = uc
        self._audit_full_schema = audit_full_schema
        self._audit_table_fqn = audit_table_fqn
        self._inventory_table_fqn = inventory_table_fqn
        # Defaults to `<audit_schema>.migration_audit_latest` alongside the
        # base table, unless a caller overrides it.
        self._latest_status_view_fqn = latest_status_view_fqn or f"{audit_full_schema}.migration_audit_latest"

    def ensure_tables_exist(self, dry_run: bool = False) -> None:
        self._uc.run_sql(f"CREATE SCHEMA IF NOT EXISTS {self._audit_full_schema}", dry_run=dry_run)
        self._uc.run_sql(INVENTORY_TABLE_DDL.format(fqn=self._inventory_table_fqn), dry_run=dry_run)
        self._uc.run_sql(MIGRATION_AUDIT_TABLE_DDL.format(fqn=self._audit_table_fqn), dry_run=dry_run)
        # CREATE OR REPLACE so the view's definition self-heals if this
        # module's DDL changes later - re-running is always safe/idempotent.
        # Skipped under dry_run like everything else here (§ dry_run gates
        # all persistence, including this purely-derived, zero-footprint-
        # in-spirit-but-still-a-DDL-statement view).
        self._uc.run_sql(
            MIGRATION_AUDIT_LATEST_VIEW_DDL.format(
                view_fqn=self._latest_status_view_fqn, audit_fqn=self._audit_table_fqn,
            ),
            dry_run=dry_run,
        )

    def append(self, record: MigrationAuditRecord, dry_run: bool = False) -> None:
        row = record.as_row_dict()
        columns = ", ".join(row.keys())
        values = ", ".join(sql_literal(v) for v in row.values())
        statement = f"INSERT INTO {self._audit_table_fqn} ({columns}) VALUES ({values})"
        self._uc.run_sql(statement, dry_run=dry_run)

    def latest_status(self, catalog: str, schema: str, table: str) -> Optional[dict]:
        """The append-only-log-derived *hint* (§4.4) - never trusted without
        a fresh live re-check by the caller."""
        rows = self._uc.run_sql(
            f"SELECT status, target_policy_name, object_type, masked_column, rollback_metadata "
            f"FROM {self._audit_table_fqn} "
            f"WHERE catalog = '{catalog}' AND schema = '{schema}' AND table = '{table}' "
            f"ORDER BY completed_at DESC"
        )
        if not rows:
            return None
        return {"status": rows[0][0], "target_policy_name": rows[0][1],
                "object_type": rows[0][2], "masked_column": rows[0][3],
                "rollback_metadata": json.loads(rows[0][4]) if rows[0][4] else {}}

    def rows_for_run(self, run_id: str) -> list:
        return self._uc.run_sql(f"SELECT * FROM {self._audit_table_fqn} WHERE run_id = '{run_id}'")
