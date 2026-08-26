"""Compares the audit table's last-known state for a table against live UC
state (§2) - used by RECONCILE mode to find tables whose ABAC policy or
legacy config was changed/removed by something else after this utility
migrated them. Never mutates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..audit.audit_repository import AuditRepository
from ..migration.policy_strategy import PolicyStrategy
from ..uc_gateway.gateway import UnityCatalogGateway
from ..uc_gateway.models import TableRef
from .post_validation import verify_table


@dataclass(frozen=True)
class DriftResult:
    table_name: str
    drift_detected: bool
    reason: str


def detect_drift(
    table: TableRef, audit_repository: AuditRepository, uc: UnityCatalogGateway,
    policy_strategy: Optional[PolicyStrategy] = None,
) -> DriftResult:
    last = audit_repository.latest_status(table.catalog, table.schema, table.table)
    if last is None:
        return DriftResult(table_name=table.full_name, drift_detected=False, reason="NEVER_MIGRATED_BY_THIS_UTILITY")
    if last["status"] not in ("SUCCESS", "ALREADY_MIGRATED"):
        return DriftResult(table_name=table.full_name, drift_detected=False, reason="LAST_RUN_WAS_NOT_SUCCESSFUL")

    result = verify_table(table, uc, policy_strategy)
    if result.status.value == "SUCCESS":
        return DriftResult(table_name=table.full_name, drift_detected=False, reason="LIVE_STATE_MATCHES_AUDIT")
    return DriftResult(
        table_name=table.full_name, drift_detected=True,
        reason=result.error_code or "LIVE_STATE_DOES_NOT_MATCH_AUDIT",
    )
