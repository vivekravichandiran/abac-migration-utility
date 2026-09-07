"""Given a table + rollback_metadata (§4.3), restores the original legacy
row filter/masks and removes only the ABAC policies this utility created
for that table - never touches any policy it didn't create itself (§9).

Resilience (§9/§14): rollback is inherently a best-effort cleanup operation,
never all-or-nothing - one table/object's rollback failing must never
prevent every OTHER table/object's rollback from being attempted. Each
plugin's `.rollback()` call below is individually try/except-wrapped so an
exception raised by the RLS plugin (e.g. a transient SQL error dropping its
ABAC policy) can never suppress the mask plugin's own rollback attempt for
the very same table, and vice versa - both always get a real, audit-visible
outcome (ROLLED_BACK/WOULD_ROLLBACK/FAILED) instead of one exception
silently aborting the other. The caller (migration_engine._run_rollback)
applies the identical per-row try/except + always-persist-then-continue
policy one level up, across different tables/audit rows.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from ..migration.plugins.base_plugin import ConversionStepResult, StepStatus
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
    started_at: Optional[dt.datetime] = None
    completed_at: Optional[dt.datetime] = None


def rollback_table(
    table: TableRef, rollback_metadata: dict, uc: UnityCatalogGateway, dry_run: bool = True,
    policy_strategy: Optional[PolicyStrategy] = None,
) -> RollbackResult:
    started_at = dt.datetime.utcnow()
    if not rollback_metadata:
        return RollbackResult(
            table_name=table.full_name, status=StepStatus.SKIPPED,
            error_message="No rollback_metadata available for this table.",
            started_at=started_at, completed_at=dt.datetime.utcnow(),
        )

    strategy = policy_strategy or TableBasedPolicyStrategy()
    plugins = [RLSMigrationPlugin(strategy), ColumnMaskMigrationPlugin(strategy)]

    step_results = []
    for plugin in plugins:
        try:
            step_results.extend(plugin.rollback(table, rollback_metadata, uc, dry_run))
        except Exception as exc:  # noqa: BLE001 - see module docstring: never let one
            # plugin's raised exception suppress the other plugin's rollback attempt
            # for this same table, or bubble up and abort every OTHER table's rollback
            # in migration_engine._run_rollback's loop. Converted into a normal,
            # audit-visible FAILED step instead - exactly how every other mutating
            # call site in this codebase (table_converter.py, rls_to_abac.py,
            # mask_to_abac.py) already treats an unexpected gateway exception.
            step_results.append(ConversionStepResult(
                object_type=getattr(plugin, "object_type", "UNKNOWN"),
                status=StepStatus.FAILED,
                error_code="ROLLBACK_FAILED",
                error_message=str(exc),
            ))

    completed_at = dt.datetime.utcnow()
    if not step_results:
        return RollbackResult(
            table_name=table.full_name, status=StepStatus.SKIPPED,
            error_message="Nothing in rollback_metadata applied to this table.",
            started_at=started_at, completed_at=completed_at,
        )

    statuses = {r.status for r in step_results}
    overall = StepStatus.FAILED if StepStatus.FAILED in statuses else (
        StepStatus.WOULD_ROLLBACK if StepStatus.WOULD_ROLLBACK in statuses else StepStatus.ROLLED_BACK
    )
    failed = [r for r in step_results if r.status == StepStatus.FAILED]
    return RollbackResult(
        table_name=table.full_name, status=overall, step_results=step_results,
        error_message=failed[0].error_message if failed else None,
        started_at=started_at, completed_at=completed_at,
    )
