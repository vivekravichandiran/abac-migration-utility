"""Regression tests for AuditRepository's schema-backfill logic
(_add_missing_columns / _existing_columns).

Live bug this guards against: `ensure_tables_exist()`'s `CREATE TABLE IF
NOT EXISTS` is a no-op against a table that already exists with an
older/narrower schema (e.g. a shared audit_catalog/audit_schema reused
across many earlier test runs, predating the `migration_phase` or
PII-suggestion columns). Without a backfill step, the subsequent `CREATE
OR REPLACE VIEW migration_audit_latest` blows up with UNRESOLVED_COLUMN
the moment it references a column the live table doesn't actually have -
confirmed live against ril_raw.abac_migration_audit.migration_audit.
"""
from __future__ import annotations

from abac_migration.audit.audit_repository import (
    _MIGRATION_AUDIT_EXPECTED_COLUMNS,
    _add_missing_columns,
    _existing_columns,
)
from abac_migration.uc_gateway.gateway import UCGatewayError


class _RecordingGateway:
    """Minimal run_sql-only stub - not the full UnityCatalogGateway Protocol,
    just enough surface for _add_missing_columns/_existing_columns."""

    def __init__(self, describe_rows=None, describe_raises=False):
        self.describe_rows = describe_rows or []
        self.describe_raises = describe_raises
        self.statements: list[str] = []

    def run_sql(self, statement: str, dry_run: bool = False) -> list:
        self.statements.append(statement)
        if statement.startswith("DESCRIBE TABLE"):
            if self.describe_raises:
                raise UCGatewayError("TABLE_OR_VIEW_NOT_FOUND", "no such table", statement)
            return self.describe_rows
        return []


def test_add_missing_columns_backfills_only_the_missing_ones():
    # Simulates a `migration_audit` table created before `migration_phase`
    # existed - every other column already present.
    existing_rows = [[name] for name, _ in _MIGRATION_AUDIT_EXPECTED_COLUMNS if name != "migration_phase"]
    gw = _RecordingGateway(describe_rows=existing_rows)

    _add_missing_columns(gw, "cat.schema.migration_audit", _MIGRATION_AUDIT_EXPECTED_COLUMNS, dry_run=False)

    alter_statements = [s for s in gw.statements if s.startswith("ALTER TABLE")]
    assert len(alter_statements) == 1
    assert "migration_phase STRING" in alter_statements[0]
    # Nothing else got re-added.
    assert "run_id" not in alter_statements[0]


def test_add_missing_columns_noop_when_nothing_missing():
    existing_rows = [[name] for name, _ in _MIGRATION_AUDIT_EXPECTED_COLUMNS]
    gw = _RecordingGateway(describe_rows=existing_rows)

    _add_missing_columns(gw, "cat.schema.migration_audit", _MIGRATION_AUDIT_EXPECTED_COLUMNS, dry_run=False)

    assert not any(s.startswith("ALTER TABLE") for s in gw.statements)


def test_add_missing_columns_noop_when_table_does_not_exist_yet():
    # Fresh deploy (or CREATE TABLE IF NOT EXISTS itself skipped under
    # dry_run) - DESCRIBE TABLE fails, treated as "nothing to backfill".
    gw = _RecordingGateway(describe_raises=True)

    _add_missing_columns(gw, "cat.schema.migration_audit", _MIGRATION_AUDIT_EXPECTED_COLUMNS, dry_run=False)

    assert not any(s.startswith("ALTER TABLE") for s in gw.statements)


def test_add_missing_columns_noop_under_dry_run():
    # Even if columns are genuinely missing, dry_run must skip DESCRIBE/ALTER
    # entirely - same "dry_run gates all persistence" rule as everywhere
    # else in this module.
    gw = _RecordingGateway(describe_rows=[["run_id"]])

    _add_missing_columns(gw, "cat.schema.migration_audit", _MIGRATION_AUDIT_EXPECTED_COLUMNS, dry_run=True)

    assert gw.statements == []


def test_existing_columns_is_case_insensitive_and_skips_comment_rows():
    gw = _RecordingGateway(describe_rows=[["Run_ID"], ["# Partitioning"], [""], ["Status"]])

    cols = _existing_columns(gw, "cat.schema.migration_audit")

    assert cols == {"run_id", "status"}
