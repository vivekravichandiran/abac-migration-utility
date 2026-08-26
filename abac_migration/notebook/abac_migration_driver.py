"""Thin notebook entry point (§2, §12). Contains NO business logic: loads
widgets -> RunConfig, builds the real gateway, calls migration_engine.run(),
prints a human-readable report. Every actual decision lives elsewhere.

`main()` is the local/CLI-driven path (reads a `~/.databrickscfg` profile).
`run_with_client()` is the shared core used both by `main()` and by the
Databricks Asset Bundle job notebooks (notebooks/abac_migration_run.py),
which build a `ResilientDatabricksSQL` from the notebook's own execution
context (host + token) instead of a local profile file.
"""
from __future__ import annotations

from ..config.config_loader import load_from_widgets
from ..migration.migration_engine import RunSummary, run
from ..uc_gateway.gateway import DatabricksUnityCatalogGateway
from ..uc_gateway.sql_statement_client import ResilientDatabricksSQL


def main(dbutils, profile: str, warehouse_id: str) -> RunSummary:
    client = ResilientDatabricksSQL(profile=profile, warehouse_id=warehouse_id)
    return run_with_client(dbutils, client)


def run_with_client(dbutils, client: ResilientDatabricksSQL) -> RunSummary:
    client.ensure_warehouse_running()
    config = load_from_widgets(dbutils)
    uc = DatabricksUnityCatalogGateway(client)

    summary = run(config, uc)
    print_report(summary)
    return summary


def print_report(summary: RunSummary) -> None:
    print(f"Run ID: {summary.run_id}")
    print(f"Mode: {summary.mode}   Dry run: {summary.dry_run}")

    if summary.pre_validation_errors:
        print("PRE-VALIDATION FAILED - no tables were touched:")
        for err in summary.pre_validation_errors:
            print(f"  - {err}")
        return

    if summary.mode in ("VERIFY", "RECONCILE", "ROLLBACK"):
        print(f"Results: {len(summary.other_results)}")
        for r in summary.other_results:
            print(f"  {r}")
        return

    print(f"Tables in scope:    {summary.tables_in_scope}")
    print(f"Tables eligible:    {summary.tables_eligible}")
    print(f"Tables not eligible:{summary.tables_not_eligible}")

    if summary.conversion_results:
        print(f"Succeeded:          {summary.tables_succeeded}")
        print(f"Would migrate:      {summary.tables_would_migrate}")
        print(f"Already migrated:   {summary.tables_already_migrated}")
        print(f"Failed:             {summary.tables_failed}")

        failures = [r for r in summary.conversion_results if r.status.value == "FAILED"]
        if failures:
            print("\nFailed tables:")
            for r in failures:
                print(f"  {r.table_name}: {r.error_code} - {r.error_message}")
