"""Confirms an already-migrated table's live UC state still matches what
was applied: the new ABAC policy exists with the expected function, and
the corresponding legacy row filter/mask is gone (§2). Used standalone by
VERIFY mode - table_converter's own convert() already does an equivalent
check inline right after each mutation, this is the same check re-run
independently, without needing to have just performed the migration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..migration.plugins.base_plugin import StepStatus
from ..migration.plugins.mask_to_abac import ColumnMaskMigrationPlugin
from ..migration.plugins.rls_to_abac import RLSMigrationPlugin
from ..migration.policy_strategy import PolicyStrategy, TableBasedPolicyStrategy
from ..uc_gateway.gateway import UnityCatalogGateway
from ..uc_gateway.models import TableRef


@dataclass(frozen=True)
class PostValidationResult:
    table_name: str
    status: StepStatus
    step_results: list = field(default_factory=list)
    error_code: Optional[str] = None


def verify_table(table: TableRef, uc: UnityCatalogGateway, policy_strategy: Optional[PolicyStrategy] = None) -> PostValidationResult:
    strategy = policy_strategy or TableBasedPolicyStrategy()
    plugins = [RLSMigrationPlugin(strategy), ColumnMaskMigrationPlugin(strategy)]

    step_results = []
    for plugin in plugins:
        step_results.extend(plugin.verify(table, uc))

    if not step_results:
        return PostValidationResult(table_name=table.full_name, status=StepStatus.NOT_ELIGIBLE, step_results=[])

    statuses = {r.status for r in step_results}
    if StepStatus.FAILED in statuses:
        overall = StepStatus.FAILED
    elif StepStatus.ABAC_APPLIED in statuses:
        # Not a failure - a legitimate non-final resting state for any
        # object still awaiting a Mode.FINALIZE run.
        overall = StepStatus.ABAC_APPLIED
    else:
        overall = StepStatus.SUCCESS
    failed = [r for r in step_results if r.status == StepStatus.FAILED]
    return PostValidationResult(
        table_name=table.full_name, status=overall, step_results=step_results,
        error_code=failed[0].error_code if failed else None,
    )
