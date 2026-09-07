"""Top-level orchestrator (§2, §3). Wires together config -> scope ->
inventory -> (tag preparation ->) per-table conversion -> audit, per the
mode requested. This is the only module that knows about ALL the other
packages at once - everything else is composable/testable in isolation.
"""
from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from ..audit.audit_repository import AuditRepository, MigrationAuditRecord
from ..config.models import Mode, PolicyScope, RunConfig
from ..inventory.inventory_manager import build_inventory_record
from ..inventory.inventory_repository import InventoryRepository
from ..rollback.rollback_manager import RollbackResult, rollback_table
from ..scope.scope_resolver import resolve_scope
from ..uc_gateway.gateway import UnityCatalogGateway
from ..uc_gateway.models import TableRef
from ..validation.drift_detection import detect_drift
from ..validation.pre_validation import run_pre_validation
from ..validation.post_validation import verify_table
from .plugins.base_plugin import StepStatus
from .plugins.mask_to_abac import ColumnMaskMigrationPlugin
from .plugins.rls_to_abac import RLSMigrationPlugin
from .policy_strategy import CatalogBasedPolicyStrategy, PolicyStrategy, TableBasedPolicyStrategy
from .table_converter import ConversionResult, convert_table
from .tag_provisioner import TagProvisioner

# §7.3: the only place `RunConfig.policy_scope` gets turned into an actual
# PolicyStrategy instance - every other component takes a PolicyStrategy
# object, never the enum, which is what keeps them oblivious to how many
# scope choices exist.
_STRATEGY_BY_SCOPE = {
    PolicyScope.TABLE: TableBasedPolicyStrategy,
    PolicyScope.CATALOG: CatalogBasedPolicyStrategy,
}


def build_policy_strategy(config: RunConfig) -> PolicyStrategy:
    strategy_cls = _STRATEGY_BY_SCOPE[config.policy_scope]
    return strategy_cls(
        to_principals=config.policy_to_principals, except_principals=config.policy_except_principals,
        team_prefix=config.tag_team_prefix,
    )


@dataclass
class RunSummary:
    run_id: str
    mode: str
    dry_run: bool
    tables_in_scope: int = 0
    tables_eligible: int = 0
    tables_succeeded: int = 0
    tables_would_migrate: int = 0
    tables_already_migrated: int = 0
    tables_not_eligible: int = 0
    tables_failed: int = 0
    # Mode.APPLY_ABAC's terminal per-table status: ABAC policy live+verified,
    # legacy row filter/column mask deliberately still in place - not final.
    tables_abac_applied: int = 0
    inventory_records: list = field(default_factory=list)
    conversion_results: list = field(default_factory=list)
    other_results: list = field(default_factory=list)
    pre_validation_errors: list = field(default_factory=list)


def run(config: RunConfig, uc: UnityCatalogGateway) -> RunSummary:
    summary = RunSummary(run_id=config.run_id, mode=config.mode.value, dry_run=config.dry_run)

    pre = run_pre_validation(config, uc)
    if not pre.passed:
        summary.pre_validation_errors = pre.errors
        return summary

    audit_repo = AuditRepository(uc, config.audit_full_schema, config.audit_table_fqn, config.inventory_table_fqn)
    audit_repo.ensure_tables_exist(dry_run=config.dry_run)
    inventory_repo = InventoryRepository(uc, config.inventory_table_fqn)

    strategy: PolicyStrategy = build_policy_strategy(config)

    if config.mode == Mode.ROLLBACK:
        summary.other_results = _run_rollback(config, uc, audit_repo, strategy)
        return summary

    tables = resolve_scope(config, uc)
    summary.tables_in_scope = len(tables)

    if config.mode in (Mode.VERIFY, Mode.RECONCILE):
        summary.other_results = _run_verify_or_reconcile(config, tables, uc, audit_repo, strategy)
        return summary

    inventory_records = [
        build_inventory_record(
            t, uc, config.run_id, strategy,
            enable_llm_pii_tagging=config.enable_llm_pii_tagging, pii_llm_endpoint=config.pii_llm_endpoint,
        )
        for t in tables
    ]
    for record in inventory_records:
        inventory_repo.append(record, dry_run=config.dry_run)
    summary.inventory_records = inventory_records
    summary.tables_eligible = sum(1 for r in inventory_records if r.migration_eligibility == "ELIGIBLE")
    summary.tables_not_eligible = summary.tables_in_scope - summary.tables_eligible

    if config.mode == Mode.INVENTORY:
        return summary

    eligible_tables = [
        TableRef(r.catalog, r.schema, r.table) for r in inventory_records if r.migration_eligibility == "ELIGIBLE"
    ]
    phase = _PHASE_BY_MODE.get(config.mode, "FULL")
    results = _run_migration(config, eligible_tables, uc, strategy, phase=phase)
    summary.conversion_results = results

    # NOTE: conversion itself already ran fully in parallel above - with
    # max_parallelism > 1 there is no cheap way to cancel in-flight work, so
    # continue_on_error=False here governs "stop persisting/counting after
    # the first failure", not "abort other tables' in-flight mutations".
    for table, result in zip(eligible_tables, results):
        _persist_conversion_result(audit_repo, config, table, result)
        if result.status == StepStatus.SUCCESS:
            summary.tables_succeeded += 1
        elif result.status == StepStatus.ABAC_APPLIED:
            summary.tables_abac_applied += 1
        elif result.status in (StepStatus.WOULD_MIGRATE, StepStatus.WOULD_APPLY_ABAC, StepStatus.WOULD_FINALIZE):
            summary.tables_would_migrate += 1
        elif result.status == StepStatus.ALREADY_MIGRATED:
            summary.tables_already_migrated += 1
        elif result.status == StepStatus.FAILED:
            summary.tables_failed += 1
            if not config.continue_on_error:
                break

    return summary


# Isolated-phase mode -> table_converter.convert_table()/ConvertOptions phase
# (base_plugin.py). MIGRATE/INVENTORY_AND_MIGRATE keep doing both steps
# atomically ("FULL"), unchanged from before APPLY_ABAC/FINALIZE existed.
_PHASE_BY_MODE = {
    Mode.APPLY_ABAC: "APPLY_ABAC",
    Mode.FINALIZE: "FINALIZE",
}


def _run_migration(
    config: RunConfig, eligible_tables: list, uc: UnityCatalogGateway, strategy: PolicyStrategy, phase: str = "FULL",
) -> list:
    """Serialized 'Prepare Governed Tags' phase (§3, §7.4) BEFORE the
    parallel per-table dispatch - this is what avoids the read-modify-write
    race on ALTER GOVERNED TAG ... SET VALUES (§7.4 point 3). Skipped
    entirely for phase="FINALIZE": by construction every object FINALIZE
    touches already has its governed tag(s) assigned from a prior
    APPLY_ABAC/FULL run, and FINALIZE never creates a policy so it never
    needs a MatchColumn."""
    resolved = {}
    if phase != "FINALIZE":
        plugins = [RLSMigrationPlugin(strategy), ColumnMaskMigrationPlugin(strategy)]
        all_tag_requests = []
        for table in eligible_tables:
            for plugin in plugins:
                if not plugin.applies_to(table, uc):
                    continue
                discovery = plugin.discover(table, uc)
                validation = plugin.validate(table, discovery, uc)
                all_tag_requests.extend(plugin.tag_requests(table, validation))

        if all_tag_requests:
            provisioner = TagProvisioner(
                uc, prefer_existing_tags=config.prefer_existing_tags, team_prefix=config.tag_team_prefix,
            )
            resolved = provisioner.prepare(all_tag_requests, dry_run=config.dry_run)

    results = [None] * len(eligible_tables)
    with ThreadPoolExecutor(max_workers=max(1, config.max_parallelism)) as pool:
        future_to_idx = {
            pool.submit(
                convert_table, table, uc, config.dry_run, strategy, resolved, config.prefer_existing_tags, phase,
                config.tag_team_prefix,
            ): i
            for i, table in enumerate(eligible_tables)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            results[idx] = future.result()
    return results


def _persist_conversion_result(audit_repo: AuditRepository, config: RunConfig, table: TableRef, result: ConversionResult) -> None:
    for step in result.step_results:
        record = MigrationAuditRecord(
            run_id=config.run_id, attempt_id=str(uuid.uuid4()),
            catalog=table.catalog, schema=table.schema, table=table.table,
            object_type=step.object_type, masked_column=step.masked_column,
            source_function=step.source_function, target_policy_name=step.target_policy_name,
            target_policy_type=step.object_type if step.target_policy_name else None,
            target_definition=step.target_definition, status=step.status.value,
            error_code=step.error_code, error_message=step.error_message,
            validation_status=result.validation_status, rollback_metadata=step.rollback_metadata or {},
            migration_phase=result.migration_phase,
            started_at=result.started_at, completed_at=result.completed_at, dry_run=config.dry_run,
        )
        audit_repo.append(record, dry_run=config.dry_run)


def _run_verify_or_reconcile(config, tables, uc, audit_repo, strategy) -> list:
    results = []
    for table in tables:
        if config.mode == Mode.VERIFY:
            results.append(verify_table(table, uc, strategy))
        else:
            results.append(detect_drift(table, audit_repo, uc, strategy))
    return results


def _run_rollback(config: RunConfig, uc: UnityCatalogGateway, audit_repo: AuditRepository, strategy: PolicyStrategy) -> list:
    """§9/§14 resilience (explicit requirement): every audit row for this
    run_id is rolled back independently. One row's failure - whether a
    normal FAILED outcome from rollback_table() itself, or a totally
    unexpected exception (malformed rollback_metadata JSON, a bad
    catalog/schema/table value, anything) - is recorded to migration_audit
    and the loop moves on to the next row. ROLLBACK never aborts the whole
    job over one bad row; previously it did (an uncaught exception here
    propagated all the way out of run() and failed the entire job task),
    AND it never wrote anything to migration_audit at all, successful or
    not - both fixed here."""
    rows = audit_repo.rows_for_run(config.run_id)
    results = []
    for row in rows:
        row_dict = dict(zip(_MIGRATION_AUDIT_COLUMNS, row))
        rollback_metadata_raw = row_dict.get("rollback_metadata")
        if not rollback_metadata_raw:
            continue  # nothing to roll back for this row - not a failure, nothing to record

        table = TableRef(row_dict.get("catalog") or "", row_dict.get("schema") or "", row_dict.get("table") or "")
        try:
            rollback_metadata = (
                json.loads(rollback_metadata_raw) if isinstance(rollback_metadata_raw, str) else rollback_metadata_raw
            )
            result = rollback_table(table, rollback_metadata, uc, config.dry_run, strategy)
        except Exception as exc:  # noqa: BLE001 - converted into a normal, audit-visible
            # FAILED result instead of propagating and aborting every remaining row's
            # rollback (and the whole job) - see function docstring.
            result = RollbackResult(
                table_name=table.full_name, status=StepStatus.FAILED,
                error_message=f"Unexpected error while rolling back this row: {exc}",
            )

        results.append(result)
        _persist_rollback_result(audit_repo, config, table, result)

    return results


# Coarse "how far along" signal for migration_audit.migration_phase,
# ROLLBACK's own equivalent of table_converter._migration_phase_for() for
# the forward migration path.
_ROLLBACK_PHASE_BY_STATUS = {
    StepStatus.ROLLED_BACK: "ROLLED_BACK",
    StepStatus.WOULD_ROLLBACK: "DRY_RUN",
    StepStatus.FAILED: "ROLLBACK_FAILED",
    StepStatus.SKIPPED: "NOT_APPLICABLE",
}


def _persist_rollback_result(
    audit_repo: AuditRepository, config: RunConfig, table: TableRef, result: RollbackResult,
) -> None:
    """Always writes at least one migration_audit row per rollback attempt,
    including a SKIPPED/no-op or FAILED one - previously ROLLBACK wrote
    NOTHING to the audit table at all, regardless of outcome. One row per
    underlying step_result when rollback_table() produced any (mirrors
    _persist_conversion_result's per-object granularity, §4.2); exactly one
    summary row when it short-circuited before producing any (SKIPPED - no
    rollback_metadata, or an unexpected top-level exception caught in
    _run_rollback above)."""
    steps = result.step_results or [None]
    for step in steps:
        if step is None:
            record = MigrationAuditRecord(
                run_id=config.run_id, attempt_id=str(uuid.uuid4()),
                catalog=table.catalog, schema=table.schema, table=table.table,
                object_type="NONE", status=result.status.value,
                error_message=result.error_message,
                migration_phase=_ROLLBACK_PHASE_BY_STATUS.get(result.status, "NOT_APPLICABLE"),
                started_at=result.started_at, completed_at=result.completed_at, dry_run=config.dry_run,
            )
        else:
            record = MigrationAuditRecord(
                run_id=config.run_id, attempt_id=str(uuid.uuid4()),
                catalog=table.catalog, schema=table.schema, table=table.table,
                object_type=step.object_type, masked_column=step.masked_column,
                source_function=step.source_function, target_policy_name=step.target_policy_name,
                status=step.status.value, error_code=step.error_code, error_message=step.error_message,
                migration_phase=_ROLLBACK_PHASE_BY_STATUS.get(step.status, "NOT_APPLICABLE"),
                started_at=result.started_at, completed_at=result.completed_at, dry_run=config.dry_run,
            )
        audit_repo.append(record, dry_run=config.dry_run)


_MIGRATION_AUDIT_COLUMNS = [
    "run_id", "attempt_id", "catalog", "schema", "table", "object_type", "masked_column",
    "source_security_type", "source_function", "source_definition", "target_policy_name",
    "target_policy_type", "target_definition", "status", "error_code", "error_message",
    "validation_status", "rollback_metadata", "migration_phase", "started_at", "completed_at", "dry_run",
]
