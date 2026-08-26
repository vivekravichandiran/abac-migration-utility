"""The pluggable `convert_table(table_ref, options) -> ConversionResult`
entry point (§2, §5, §6). Independent of catalog-wide orchestration -
knows nothing about scope or other tables, which is what makes it directly
unit-testable with just a FakeUnityCatalogGateway (§12, §15).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from ..uc_gateway.gateway import UnityCatalogGateway
from ..uc_gateway.models import TableRef
from .plugins.base_plugin import ConversionStepResult, ConvertOptions, StepStatus
from .plugins.mask_to_abac import ColumnMaskMigrationPlugin
from .plugins.rls_to_abac import RLSMigrationPlugin
from .policy_strategy import PolicyStrategy, TableBasedPolicyStrategy
from .tag_provisioner import TagProvisioner


@dataclass(frozen=True)
class ConversionResult:
    """Return type of convert_table (§6)."""
    status: StepStatus
    table_name: str
    rls_status: Optional[StepStatus] = None
    column_mask_status: dict = field(default_factory=dict)
    source_functions: dict = field(default_factory=dict)
    target_policies: dict = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[dt.datetime] = None
    completed_at: Optional[dt.datetime] = None
    validation_status: str = "NOT_RUN"
    rollback_metadata: dict = field(default_factory=dict)
    # Table-level "is this final yet" summary - FINALIZED | ABAC_APPLIED |
    # DRY_RUN | FAILED | NOT_APPLICABLE, derived from `status` (see
    # _migration_phase_for). Kept alongside `status` rather than replacing
    # it since `status` is the granular pass/fail signal and this is the
    # coarser, audit-facing "how far through the pipeline is this" signal.
    migration_phase: str = "NOT_APPLICABLE"
    # Raw per-object results, kept alongside the aggregated view above so
    # audit_repository can persist §4.2's finer per-object granularity
    # without table_converter needing to know about the audit schema.
    step_results: list = field(default_factory=list)


def _aggregate_status(step_results: list) -> StepStatus:
    """Weakest-link rule (§6): any FAILED wins; otherwise dry-run/already-
    migrated/success are reported in that priority order. ABAC_APPLIED
    (Mode.APPLY_ABAC's terminal per-object status) ranks below SUCCESS but
    above ALREADY_MIGRATED/NOT_ELIGIBLE - it means real progress was made
    this run, just not yet final."""
    statuses = {r.status for r in step_results}
    if not statuses:
        return StepStatus.NOT_ELIGIBLE
    if StepStatus.FAILED in statuses:
        return StepStatus.FAILED
    if statuses <= {StepStatus.NOT_ELIGIBLE}:
        return StepStatus.NOT_ELIGIBLE
    if StepStatus.WOULD_MIGRATE in statuses:
        return StepStatus.WOULD_MIGRATE
    if StepStatus.WOULD_APPLY_ABAC in statuses:
        return StepStatus.WOULD_APPLY_ABAC
    if StepStatus.WOULD_FINALIZE in statuses:
        return StepStatus.WOULD_FINALIZE
    if statuses <= {StepStatus.ALREADY_MIGRATED, StepStatus.NOT_ELIGIBLE}:
        return StepStatus.ALREADY_MIGRATED
    if StepStatus.ABAC_APPLIED in statuses:
        return StepStatus.ABAC_APPLIED
    if StepStatus.SUCCESS in statuses:
        return StepStatus.SUCCESS
    return next(iter(statuses))


def _migration_phase_for(status: StepStatus) -> str:
    """Derives the human-facing, persisted `migration_phase` from a
    ConversionStepResult's per-object status - this is the "is this final
    yet" flag requested for the audit table, orthogonal to `status` (which
    already exists for the more granular pass/fail outcome)."""
    if status == StepStatus.SUCCESS:
        return "FINALIZED"
    if status == StepStatus.ALREADY_MIGRATED:
        return "FINALIZED"
    if status == StepStatus.ABAC_APPLIED:
        return "ABAC_APPLIED"
    if status in (StepStatus.WOULD_MIGRATE, StepStatus.WOULD_APPLY_ABAC, StepStatus.WOULD_FINALIZE):
        return "DRY_RUN"
    if status == StepStatus.FAILED:
        return "FAILED"
    return "NOT_APPLICABLE"


def convert_table(
    table: TableRef,
    uc: UnityCatalogGateway,
    dry_run: bool = True,
    policy_strategy: Optional[PolicyStrategy] = None,
    resolved_match_columns: Optional[dict] = None,
    prefer_existing_tags: bool = True,
    phase: str = "FULL",
) -> ConversionResult:
    """Runs discover->validate->convert for both plugin types against one
    table (§5 point 2). `resolved_match_columns` lets a caller (typically
    migration_engine's serialized "Prepare Governed Tags" phase, §7.4) hand
    in tags it already resolved for a whole run; anything still missing is
    resolved here as a fallback, which is what keeps this function usable
    standalone (§12/§15 unit-testability requirement) without requiring the
    full orchestrator - the fallback path is only ever exercised
    concurrently if a caller bypasses migration_engine's serialized phase,
    which is a documented v1 limitation (§14), not the default path.

    `phase` selects which part of the state machine actually runs this call
    (Mode.APPLY_ABAC / Mode.FINALIZE isolated-phase support, see base_plugin.py
    ConvertOptions): "FULL" (default) does create-ABAC + remove-legacy in one
    call exactly as before; "APPLY_ABAC" creates+verifies the ABAC policy and
    stops there (legacy left in place); "FINALIZE" removes the legacy
    mechanism for objects that already have a matching ABAC policy applied
    and never creates one itself. Tag resolution is skipped entirely for
    "FINALIZE" - by construction it never needs a MatchColumn it didn't
    already have from a prior APPLY_ABAC/FULL run.
    """
    started_at = dt.datetime.utcnow()
    strategy = policy_strategy or TableBasedPolicyStrategy()
    plugins = [RLSMigrationPlugin(strategy), ColumnMaskMigrationPlugin(strategy)]

    validations = []
    all_tag_requests = []
    for plugin in plugins:
        if not plugin.applies_to(table, uc):
            continue
        discovery = plugin.discover(table, uc)
        validation = plugin.validate(table, discovery, uc)
        validations.append((plugin, validation))
        if phase != "FINALIZE":
            all_tag_requests.extend(plugin.tag_requests(table, validation))

    resolved = dict(resolved_match_columns or {})
    if phase != "FINALIZE":
        missing = [r for r in all_tag_requests if (r.table, r.column, r.role) not in resolved]
        if missing:
            provisioner = TagProvisioner(uc, prefer_existing_tags=prefer_existing_tags)
            resolved.update(provisioner.prepare(missing, dry_run=dry_run))

    options = ConvertOptions(dry_run=dry_run, resolved_match_columns=resolved, phase=phase)

    step_results = []
    for plugin, validation in validations:
        step_results.extend(plugin.convert(table, validation, uc, options))

    if not step_results:
        step_results = [ConversionStepResult(
            object_type="NONE", status=StepStatus.NOT_ELIGIBLE, error_code="NO_LEGACY_SECURITY_FOUND",
        )]

    completed_at = dt.datetime.utcnow()
    return _build_conversion_result(table, step_results, started_at, completed_at)


def _build_conversion_result(table: TableRef, step_results: list, started_at, completed_at) -> ConversionResult:
    rls_steps = [r for r in step_results if r.object_type == "ROW_FILTER"]
    mask_steps = [r for r in step_results if r.object_type == "COLUMN_MASK"]

    rls_status = rls_steps[0].status if rls_steps else None
    column_mask_status = {r.masked_column: r.status for r in mask_steps if r.masked_column}

    source_functions = {}
    if rls_steps and rls_steps[0].source_function:
        source_functions["row_filter"] = rls_steps[0].source_function
    if mask_steps:
        source_functions["column_masks"] = {
            r.masked_column: r.source_function for r in mask_steps if r.masked_column
        }

    target_policies = {}
    if rls_steps and rls_steps[0].target_policy_name:
        target_policies["row_filter"] = rls_steps[0].target_policy_name
    if mask_steps:
        target_policies["column_masks"] = {
            r.masked_column: r.target_policy_name for r in mask_steps if r.masked_column and r.target_policy_name
        }

    overall_status = _aggregate_status(step_results)
    failed = [r for r in step_results if r.status == StepStatus.FAILED]
    error_code = failed[0].error_code if failed else None
    error_message = failed[0].error_message if failed else None

    rollback_metadata: dict = {}
    rf_rollback = next((r.rollback_metadata for r in rls_steps if r.rollback_metadata), None)
    if rf_rollback:
        rollback_metadata.update(rf_rollback)
    mask_rollbacks = [r.rollback_metadata for r in mask_steps if r.rollback_metadata]
    if mask_rollbacks:
        rollback_metadata.setdefault("original_column_masks", [])
        rollback_metadata.setdefault("abac_policies_created_by_this_run", [])
        for rb in mask_rollbacks:
            rollback_metadata["original_column_masks"].extend(rb.get("original_column_masks", []))
            rollback_metadata["abac_policies_created_by_this_run"].extend(rb.get("abac_policies_created_by_this_run", []))

    if overall_status == StepStatus.SUCCESS:
        validation_status = "PASSED"
    elif overall_status == StepStatus.FAILED:
        validation_status = "FAILED"
    elif overall_status == StepStatus.ABAC_APPLIED:
        validation_status = "PARTIAL"
    else:
        validation_status = "NOT_RUN"

    return ConversionResult(
        status=overall_status,
        table_name=table.full_name,
        rls_status=rls_status,
        column_mask_status=column_mask_status,
        source_functions=source_functions,
        target_policies=target_policies,
        error_code=error_code,
        error_message=error_message,
        started_at=started_at,
        completed_at=completed_at,
        validation_status=validation_status,
        rollback_metadata=rollback_metadata,
        migration_phase=_migration_phase_for(overall_status),
        step_results=step_results,
    )
