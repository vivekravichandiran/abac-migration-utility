# Databricks notebook source
# COMMAND ----------
# MAGIC %md
# MAGIC # ABAC Migration Utility - Full Real-Workspace Test Run
# MAGIC Runs the real `abac_migration` package (uploaded as workspace files, unmodified)
# MAGIC against real Unity Catalog tables in this workspace: the organic
# MAGIC `sales_abac_demo` fixtures (5 catalogs) plus dedicated edge-case fixtures in
# MAGIC `ril_raw.abac_migration_scenario_tests`. Covers dry-run, real migration,
# MAGIC idempotent rerun, VERIFY (post-validation), drift detection/RECONCILE, and
# MAGIC rollback - end to end, against live UC, via the SQL Statement Execution API
# MAGIC (same resilient client validated in the earlier API spike).

# COMMAND ----------
import sys
sys.path.append("/Workspace/Users/vivek.ravichandiran@databricks.com/abac_migration_pkg")

import json
import uuid

from abac_migration.audit.audit_repository import AuditRepository
from abac_migration.config.models import Mode, RunConfig, ScopeType
from abac_migration.migration.migration_engine import run as run_migration
from abac_migration.migration.policy_strategy import TableBasedPolicyStrategy
from abac_migration.rollback.rollback_manager import rollback_table
from abac_migration.uc_gateway.gateway import DatabricksUnityCatalogGateway
from abac_migration.uc_gateway.models import TableRef
from abac_migration.uc_gateway.retry import RetryPolicy
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL
from abac_migration.validation.drift_detection import detect_drift

TOKEN = dbutils.secrets.get(scope="abac_migration_secrets", key="uc_source_token")
HOST = "https://adb-7405618912789045.5.azuredatabricks.net"
WAREHOUSE_ID = "525de76b2ccdd7d5"

client = ResilientDatabricksSQL.__new__(ResilientDatabricksSQL)
client.host = HOST
client.token = TOKEN
client.warehouse_id = WAREHOUSE_ID
client.retry_policy = RetryPolicy()
import requests
client.session = requests.Session()
client.session.headers.update({"Authorization": f"Bearer {TOKEN}"})
client.total_calls = 0
client.total_retried_calls = 0
client.ensure_warehouse_running()

uc = DatabricksUnityCatalogGateway(client)
strategy = TableBasedPolicyStrategy()
print("Gateway ready. Warehouse running.")

# COMMAND ----------
AUDIT_CATALOG = "ril_raw"
AUDIT_SCHEMA = "abac_migration_audit"
SCHEMAS = {
    "ril_raw": ["sales_abac_demo", "abac_migration_scenario_tests"],
    "ril_curated": ["sales_abac_demo"],
    "ril_bulk": ["sales_abac_demo"],
    "ril_migration": ["sales_abac_demo"],
    "ril_sandbox": ["sales_abac_demo"],
}

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
print("=" * 90); print("PHASE 1: DRY RUN across full real scope (scenario 12)"); print("=" * 90)
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
print("=" * 90); print("PHASE 3: IDEMPOTENT RERUN (scenario 13)"); print("=" * 90)
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
print("=" * 90); print("PHASE 5: DRIFT DETECTION + RECONCILE (scenario 14)"); print("=" * 90)
drift_table = TableRef("ril_raw", "abac_migration_scenario_tests", "rollback_demo_tbl")
audit_repo = AuditRepository(uc, f"{AUDIT_CATALOG}.{AUDIT_SCHEMA}", f"{AUDIT_CATALOG}.{AUDIT_SCHEMA}.migration_audit", f"{AUDIT_CATALOG}.{AUDIT_SCHEMA}.inventory")

before = detect_drift(drift_table, audit_repo, uc, strategy)
print("drift check BEFORE external tamper:", before)

uc.drop_policy(drift_table, "abac_migrated_row_filter", dry_run=False)
print("Externally dropped the ABAC row-filter policy on rollback_demo_tbl (simulating out-of-band change).")

after = detect_drift(drift_table, audit_repo, uc, strategy)
print("drift check AFTER external tamper:", after)
REPORT["phase5_drift"] = {"before_drift_detected": before.drift_detected, "after_drift_detected": after.drift_detected, "after_reason": after.reason}

# COMMAND ----------
print("=" * 90); print("PHASE 6: ROLLBACK (scenario 15)"); print("=" * 90)
# Important finding from the first live run: once BOTH the legacy filter and
# the ABAC policy are gone (the normal post-migration end-state, now drifted),
# a plain re-run of MIGRATE is a no-op for RLS - applies_to() only looks at
# *current* live state, and neither condition holds any more, so there is
# nothing for the plugin to notice. Genuinely restoring requires ROLLBACK
# (using the real rollback_metadata captured during the original successful
# migration in Phase 2, which still names the original legacy function) to
# bring the legacy filter back, and only THEN can MIGRATE re-convert it.
original_result = next((r for r in real_summary.conversion_results if "rollback_demo_tbl" in r.table_name), None)
if original_result and original_result.rollback_metadata:
    rb = rollback_table(drift_table, original_result.rollback_metadata, uc, dry_run=False, policy_strategy=strategy)
    print(f"rollback status={rb.status.value}")
    for s in rb.step_results:
        print(f"  {s.object_type} masked_column={s.masked_column} status={s.status.value}")
    REPORT["phase6_rollback"] = {"status": rb.status.value, "steps": [(s.object_type, s.masked_column, s.status.value) for s in rb.step_results]}

    state_after_rollback = uc.describe_table_security(drift_table)
    print(f"post-rollback state: has_row_filter={state_after_rollback.has_row_filter} has_column_masks={state_after_rollback.has_column_masks}")

    # leave the fixture table protected again for cleanliness - re-migrate
    # now succeeds for real since the legacy filter/masks are back.
    reprotect = run_migration(make_config(Mode.MIGRATE, dry_run=False, scope_type=ScopeType.SPECIFIC_TABLES,
                                           tables=["ril_raw.abac_migration_scenario_tests.rollback_demo_tbl"]), uc)
    print("re-protected rollback_demo_tbl after rollback demo:", fmt(reprotect.conversion_results))
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
