# Databricks notebook source
# MAGIC %md
# MAGIC # ABAC Migration Utility - Full End-to-End Scenario Test
# MAGIC Runs the real `abac_migration` package (synced to the workspace as plain
# MAGIC files by `databricks bundle deploy`, unmodified - no wheel/library
# MAGIC install) against real Unity Catalog tables in this workspace: the
# MAGIC `sales_abac_demo` fixtures plus dedicated edge-case fixtures in
# MAGIC `<audit_catalog>.abac_migration_scenario_tests`. Exercises dry-run, real
# MAGIC migration, idempotent rerun, VERIFY (post-validation), drift
# MAGIC detection/RECONCILE, and ROLLBACK end to end against live UC, via the SQL
# MAGIC Statement Execution API.
# MAGIC
# MAGIC This is the bundle-deployable, parameterized version of the ad hoc
# MAGIC script used to validate the utility (`abac_migration/spike/job_notebook_source.py`);
# MAGIC behavior is unchanged, only the catalogs/schemas/warehouse and auth are
# MAGIC now job parameters/notebook-context-derived instead of hardcoded.

# COMMAND ----------
import os
import sys

# See notebooks/abac_migration_run.py for why this is needed: no wheel is
# built/installed - `abac_migration/` is just a synced sibling directory of
# this notebook's own folder once deployed.
sys.path.append(os.path.abspath(".."))

import json

from abac_migration.audit.audit_repository import AuditRepository
from abac_migration.config.models import Mode, RunConfig, ScopeType
from abac_migration.migration.migration_engine import run as run_migration
from abac_migration.migration.policy_strategy import TableBasedPolicyStrategy
from abac_migration.rollback.rollback_manager import rollback_table
from abac_migration.uc_gateway.gateway import DatabricksUnityCatalogGateway
from abac_migration.uc_gateway.models import TableRef
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL
from abac_migration.validation.drift_detection import detect_drift

# COMMAND ----------
dbutils.widgets.text("warehouse_id", "", "SQL warehouse ID")
dbutils.widgets.text("audit_catalog", "ril_raw", "Audit catalog")
dbutils.widgets.text("audit_schema", "abac_migration_audit", "Audit schema")
dbutils.widgets.text("demo_schema", "sales_abac_demo", "Demo/fixture schema name")
dbutils.widgets.text("demo_catalogs", '["ril_raw","ril_curated","ril_bulk","ril_migration","ril_sandbox"]',
                      "JSON list of catalogs holding the demo_schema fixtures")
dbutils.widgets.text("scenario_tests_schema", "abac_migration_scenario_tests",
                      "Schema (inside audit_catalog) holding edge-case fixtures")
dbutils.widgets.text("rollback_demo_table", "rollback_demo_tbl", "Rollback-demo fixture table name")

warehouse_id = dbutils.widgets.get("warehouse_id")
if not warehouse_id:
    raise ValueError("The 'warehouse_id' job parameter is required (a running/startable SQL warehouse).")
AUDIT_CATALOG = dbutils.widgets.get("audit_catalog")
AUDIT_SCHEMA = dbutils.widgets.get("audit_schema")
DEMO_SCHEMA = dbutils.widgets.get("demo_schema")
DEMO_CATALOGS = json.loads(dbutils.widgets.get("demo_catalogs"))
SCENARIO_TESTS_SCHEMA = dbutils.widgets.get("scenario_tests_schema")
ROLLBACK_DEMO_TABLE = dbutils.widgets.get("rollback_demo_table")

SCHEMAS = {cat: [DEMO_SCHEMA] for cat in DEMO_CATALOGS}
SCHEMAS.setdefault(AUDIT_CATALOG, [])
if SCENARIO_TESTS_SCHEMA not in SCHEMAS[AUDIT_CATALOG]:
    SCHEMAS[AUDIT_CATALOG].append(SCENARIO_TESTS_SCHEMA)

# COMMAND ----------
# MAGIC %md ### Resolve host + token from this notebook's own execution context

# COMMAND ----------
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
host = ctx.apiUrl().get()
token = ctx.apiToken().get()

client = ResilientDatabricksSQL.from_host_and_token(host=host, token=token, warehouse_id=warehouse_id)
client.ensure_warehouse_running()

uc = DatabricksUnityCatalogGateway(client)
strategy = TableBasedPolicyStrategy()
print("Gateway ready. Warehouse running.")

# COMMAND ----------
def make_config(mode, dry_run, run_id=None, scope_type=ScopeType.SELECTED_SCHEMAS, schemas=None, tables=None):
    kwargs = dict(
        mode=mode, scope_type=scope_type, dry_run=dry_run, continue_on_error=True, max_parallelism=4,
        audit_catalog=AUDIT_CATALOG, audit_schema=AUDIT_SCHEMA, prefer_existing_tags=True,
    )
    if scope_type == ScopeType.SELECTED_SCHEMAS:
        kwargs["schemas"] = schemas or SCHEMAS
    if scope_type == ScopeType.SPECIFIC_TABLES:
        kwargs["tables"] = tables or []
    if run_id:
        kwargs["run_id"] = run_id
    return RunConfig(**kwargs)


def fmt(results):
    out = []
    for r in results:
        masks = {k: v.value for k, v in r.column_mask_status.items()}
        out.append(f"  {r.table_name}: status={r.status.value} error={r.error_code} rls={r.rls_status.value if r.rls_status else None} masks={masks}")
    return "\n".join(out)


REPORT = {}

# COMMAND ----------
print("=" * 90); print("PHASE 1: DRY RUN across full real scope"); print("=" * 90)
dry_config = make_config(Mode.INVENTORY_AND_MIGRATE, dry_run=True)
dry_summary = run_migration(dry_config, uc)
print(f"run_id={dry_summary.run_id}")
print(f"tables_in_scope={dry_summary.tables_in_scope} eligible={dry_summary.tables_eligible} not_eligible={dry_summary.tables_not_eligible}")
print(f"would_migrate={dry_summary.tables_would_migrate} already_migrated={dry_summary.tables_already_migrated} failed={dry_summary.tables_failed}")
print(fmt(dry_summary.conversion_results))
REPORT["phase1_dry_run"] = {
    "run_id": dry_summary.run_id, "tables_in_scope": dry_summary.tables_in_scope,
    "tables_eligible": dry_summary.tables_eligible, "would_migrate": dry_summary.tables_would_migrate,
    "failed": dry_summary.tables_failed,
}

# COMMAND ----------
print("=" * 90); print("PHASE 2: REAL RUN (dry_run=False) across full scope"); print("=" * 90)
real_config = make_config(Mode.INVENTORY_AND_MIGRATE, dry_run=False)
real_summary = run_migration(real_config, uc)
print(f"run_id={real_summary.run_id}")
print(f"tables_in_scope={real_summary.tables_in_scope} eligible={real_summary.tables_eligible} not_eligible={real_summary.tables_not_eligible}")
print(f"succeeded={real_summary.tables_succeeded} already_migrated={real_summary.tables_already_migrated} failed={real_summary.tables_failed}")
print(fmt(real_summary.conversion_results))
REPORT["phase2_real_run"] = {
    "run_id": real_summary.run_id, "tables_in_scope": real_summary.tables_in_scope,
    "tables_eligible": real_summary.tables_eligible, "succeeded": real_summary.tables_succeeded,
    "already_migrated": real_summary.tables_already_migrated, "failed": real_summary.tables_failed,
    "details": [
        {"table": r.table_name, "status": r.status.value, "error_code": r.error_code,
         "rls_status": r.rls_status.value if r.rls_status else None,
         "column_mask_status": {k: v.value for k, v in r.column_mask_status.items()}}
        for r in real_summary.conversion_results
    ],
}

# COMMAND ----------
print("=" * 90); print("PHASE 3: IDEMPOTENT RERUN"); print("=" * 90)
rerun_config = make_config(Mode.MIGRATE, dry_run=False)
rerun_summary = run_migration(rerun_config, uc)
print(f"already_migrated={rerun_summary.tables_already_migrated} succeeded={rerun_summary.tables_succeeded} failed={rerun_summary.tables_failed}")
print(fmt(rerun_summary.conversion_results))
REPORT["phase3_idempotent_rerun"] = {
    "already_migrated": rerun_summary.tables_already_migrated, "succeeded": rerun_summary.tables_succeeded,
    "failed": rerun_summary.tables_failed,
}

# COMMAND ----------
print("=" * 90); print("PHASE 4: VERIFY MODE (post-validation)"); print("=" * 90)
verify_config = make_config(Mode.VERIFY, dry_run=True)
verify_summary = run_migration(verify_config, uc)
verify_pass = sum(1 for r in verify_summary.other_results if r.status.value == "SUCCESS")
verify_fail = sum(1 for r in verify_summary.other_results if r.status.value == "FAILED")
verify_not_eligible = sum(1 for r in verify_summary.other_results if r.status.value == "NOT_ELIGIBLE")
print(f"verify results: pass={verify_pass} fail={verify_fail} not_eligible(never migrated)={verify_not_eligible} total={len(verify_summary.other_results)}")
for r in verify_summary.other_results:
    if r.status.value == "FAILED":
        print("  VERIFY FAILED:", r.table_name, r.error_code)
REPORT["phase4_verify"] = {"pass": verify_pass, "fail": verify_fail, "not_eligible": verify_not_eligible}

# COMMAND ----------
print("=" * 90); print("PHASE 5: DRIFT DETECTION + RECONCILE"); print("=" * 90)
drift_table = TableRef(AUDIT_CATALOG, SCENARIO_TESTS_SCHEMA, ROLLBACK_DEMO_TABLE)
audit_repo = AuditRepository(uc, f"{AUDIT_CATALOG}.{AUDIT_SCHEMA}", f"{AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit", f"{AUDIT_CATALOG}.{AUDIT_SCHEMA}.inventory")

before = detect_drift(drift_table, audit_repo, uc, strategy)
print("drift check BEFORE external tamper:", before)

uc.drop_policy(drift_table, "abac_migrated_row_filter", dry_run=False)
print(f"Externally dropped the ABAC row-filter policy on {ROLLBACK_DEMO_TABLE} (simulating out-of-band change).")

after = detect_drift(drift_table, audit_repo, uc, strategy)
print("drift check AFTER external tamper:", after)
REPORT["phase5_drift"] = {"before_drift_detected": before.drift_detected, "after_drift_detected": after.drift_detected, "after_reason": after.reason}

# COMMAND ----------
print("=" * 90); print("PHASE 6: ROLLBACK"); print("=" * 90)
# Once BOTH the legacy filter and the ABAC policy are gone (the normal
# post-migration end-state, now drifted), a plain re-run of MIGRATE is a
# no-op for RLS - applies_to() only looks at *current* live state, and
# neither condition holds any more. Genuinely restoring requires ROLLBACK
# (using the real rollback_metadata captured during the original successful
# migration in Phase 2) to bring the legacy filter back, and only THEN can
# MIGRATE re-convert it.
original_result = next((r for r in real_summary.conversion_results if r.table_name.endswith(f".{ROLLBACK_DEMO_TABLE}")), None)
if original_result and original_result.rollback_metadata:
    rb = rollback_table(drift_table, original_result.rollback_metadata, uc, dry_run=False, policy_strategy=strategy)
    print(f"rollback status={rb.status.value}")
    for s in rb.step_results:
        print(f"  {s.object_type} masked_column={s.masked_column} status={s.status.value}")
    REPORT["phase6_rollback"] = {"status": rb.status.value, "steps": [(s.object_type, s.masked_column, s.status.value) for s in rb.step_results]}

    state_after_rollback = uc.describe_table_security(drift_table)
    print(f"post-rollback state: has_row_filter={state_after_rollback.has_row_filter} has_column_masks={state_after_rollback.has_column_masks}")

    # Leave the fixture table protected again for cleanliness - re-migrate
    # now succeeds for real since the legacy filter/masks are back.
    reprotect = run_migration(make_config(Mode.MIGRATE, dry_run=False, scope_type=ScopeType.SPECIFIC_TABLES,
                                           tables=[f"{AUDIT_CATALOG}.{SCENARIO_TESTS_SCHEMA}.{ROLLBACK_DEMO_TABLE}"]), uc)
    print("re-protected fixture after rollback demo:", fmt(reprotect.conversion_results))
    REPORT["phase6b_reprotect_after_rollback"] = [
        {"table": r.table_name, "status": r.status.value} for r in reprotect.conversion_results
    ]
else:
    print("No rollback metadata available from the original migration - skipping rollback demo.")
    REPORT["phase6_rollback"] = {"status": "SKIPPED"}

# COMMAND ----------
print("=" * 90); print("FINAL REPORT (JSON)"); print("=" * 90)
REPORT["audit_table"] = f"{AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit"
REPORT["inventory_table"] = f"{AUDIT_CATALOG}.{AUDIT_SCHEMA}.inventory"
REPORT["main_run_id"] = real_summary.run_id
print(json.dumps(REPORT, indent=2, default=str))
dbutils.notebook.exit(json.dumps(REPORT, default=str))
