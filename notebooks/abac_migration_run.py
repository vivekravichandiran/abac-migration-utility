# Databricks notebook source
# MAGIC %md
# MAGIC # ABAC Migration Utility - Job Entry Point
# MAGIC
# MAGIC Thin driver deployed by the Databricks Asset Bundle (`databricks.yml`).
# MAGIC Contains no business logic - it only:
# MAGIC 1. Builds a `RunConfig` from the job's widgets/parameters
# MAGIC    (`abac_migration.config.config_loader.load_from_widgets`).
# MAGIC 2. Builds a resilient SQL client authenticated with this notebook's own
# MAGIC    execution context (host + short-lived token) - no secret scope or
# MAGIC    `~/.databrickscfg` profile required.
# MAGIC 3. Delegates to `abac_migration.migration.migration_engine.run()` and
# MAGIC    prints/returns the report.
# MAGIC
# MAGIC The `abac_migration` package is NOT a wheel/library dependency - the
# MAGIC bundle (`databricks.yml`) syncs the whole source tree to the workspace
# MAGIC as plain files on every `databricks bundle deploy`, and the two lines
# MAGIC below just add that synced directory to `sys.path` so `import
# MAGIC abac_migration...` resolves to it directly, no build/install step
# MAGIC involved.

# COMMAND ----------
import os
import sys

# This notebook lives at <root>/notebooks/abac_migration_run.py once synced;
# `abac_migration/` is a sibling directory one level up. Databricks sets a
# notebook's default working directory to its own containing folder, so
# `os.path.abspath("..")` resolves to `<root>` for both interactive runs and
# job runs.
sys.path.append(os.path.abspath(".."))

import dataclasses
import enum
import json

from abac_migration.notebook.abac_migration_driver import run_with_client
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL


def _to_jsonable(obj):
    """Recursively converts dataclasses/enums in VERIFY/RECONCILE/ROLLBACK's
    `summary.other_results` (PostValidationResult/DriftResult/RollbackResult,
    each possibly nesting ConversionStepResult) into plain dict/str so they
    show up as structured JSON in the job's notebook output/run page instead
    of being silently dropped or stringified by `json.dumps(default=str)`."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj

# COMMAND ----------
# MAGIC %md ### Resolve host + token from this notebook's own execution context
# MAGIC No secrets or CLI profiles needed - the job runs with the identity/host
# MAGIC of whichever workspace it was deployed to.

# COMMAND ----------
dbutils.widgets.text("warehouse_id", "", "SQL warehouse ID")
warehouse_id = dbutils.widgets.get("warehouse_id")
if not warehouse_id:
    raise ValueError("The 'warehouse_id' job parameter is required (a running/startable SQL warehouse).")

ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
host = ctx.apiUrl().get()
token = ctx.apiToken().get()

client = ResilientDatabricksSQL.from_host_and_token(host=host, token=token, warehouse_id=warehouse_id)

# COMMAND ----------
# MAGIC %md ### Run the engine
# MAGIC All other job parameters (`mode`, `scope_type`, `catalogs`, `schemas`,
# MAGIC `tables`, `audit_catalog`, `audit_schema`, `dry_run`, ...) are read
# MAGIC directly from widgets by `load_from_widgets` inside `run_with_client`.

# COMMAND ----------
summary = run_with_client(dbutils, client)

# COMMAND ----------
report = {
    "run_id": summary.run_id,
    "mode": summary.mode,
    "dry_run": summary.dry_run,
    "tables_in_scope": summary.tables_in_scope,
    "tables_eligible": summary.tables_eligible,
    "tables_not_eligible": summary.tables_not_eligible,
    "tables_succeeded": summary.tables_succeeded,
    "tables_abac_applied": summary.tables_abac_applied,
    "tables_would_migrate": summary.tables_would_migrate,
    "tables_already_migrated": summary.tables_already_migrated,
    "tables_failed": summary.tables_failed,
    "pre_validation_errors": summary.pre_validation_errors,
    # Populated only for VERIFY (PostValidationResult per table), RECONCILE
    # (DriftResult per table), and ROLLBACK (RollbackResult per object) -
    # empty list for every other mode, which report through the counters
    # above + the audit tables instead.
    "other_results": _to_jsonable(summary.other_results),
}
print(json.dumps(report, indent=2, default=str))
dbutils.notebook.exit(json.dumps(report, default=str))
