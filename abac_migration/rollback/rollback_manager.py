"""Given a table + rollback_metadata (§4.3), restores the original legacy
row filter/masks and removes only the ABAC policies this utility created
for that table - never touches any policy it didn't create itself (§9).
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
class RollbackResult:
    table_name: str
    status: StepStatus
    step_results: list = field(default_factory=list)
    error_message: Optional[str] = None


def rollback_table(
    table: TableRef, rollback_metadata: dict, uc: UnityCatalogGateway, dry_run: bool = True,
    policy_strategy: Optional[PolicyStrategy] = None,
) -> RollbackResult:
    if not rollback_metadata:
        return RollbackResult(table_name=table.full_name, status=StepStatus.SKIPPED,
                               error_message="No rollback_metadata available for this table.")

    strategy = policy_strategy or TableBasedPolicyStrategy()
    plugins = [RLSMigrationPlugin(strategy), ColumnMaskMigrationPlugin(strategy)]

    step_results = []
    for plugin in plugins:
        step_results.extend(plugin.rollback(table, rollback_metadata, uc, dry_run))

    if not step_results:
        return RollbackResult(table_name=table.full_name, status=StepStatus.SKIPPED,
                               error_message="Nothing in rollback_metadata applied to this table.")

    statuses = {r.status for r in step_results}
    overall = StepStatus.FAILED if StepStatus.FAILED in statuses else (
        StepStatus.WOULD_ROLLBACK if StepStatus.WOULD_ROLLBACK in statuses else StepStatus.ROLLED_BACK
    )
    failed = [r for r in step_results if r.status == StepStatus.FAILED]
    return RollbackResult(
        table_name=table.full_name, status=overall, step_results=step_results,
        error_message=failed[0].error_message if failed else None,
    )
