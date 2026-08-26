"""MigrationPlugin Protocol + supporting result types (§5). The core engine
never branches on "is this RLS or masks" - it just calls whichever plugins
are applicable and aggregates their per-object ConversionStepResults (§6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol

from ...uc_gateway.gateway import UnityCatalogGateway
from ...uc_gateway.models import TableRef, TableSecurityState


class StepStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ALREADY_MIGRATED = "ALREADY_MIGRATED"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    WOULD_MIGRATE = "WOULD_MIGRATE"
    DRIFT = "DRIFT"
    ROLLED_BACK = "ROLLED_BACK"
    WOULD_ROLLBACK = "WOULD_ROLLBACK"
    # Isolated-phase (Mode.APPLY_ABAC / Mode.FINALIZE) statuses: ABAC_APPLIED
    # is a deliberate, non-final resting state - the new ABAC policy is live
    # and verified, but the legacy row filter/column mask was intentionally
    # left untouched (both mechanisms active simultaneously, never a security
    # gap). SUCCESS remains reserved for "fully finalized" (legacy removed +
    # verified gone), whether reached via one atomic MIGRATE run or via
    # APPLY_ABAC followed by a later FINALIZE run.
    ABAC_APPLIED = "ABAC_APPLIED"
    WOULD_APPLY_ABAC = "WOULD_APPLY_ABAC"
    WOULD_FINALIZE = "WOULD_FINALIZE"


@dataclass(frozen=True)
class DiscoveryResult:
    applicable: bool
    security_state: Optional[TableSecurityState] = None
    existing_policies: list = field(default_factory=list)


@dataclass(frozen=True)
class PlannedObject:
    """One migratable object within a plugin's scope - the RLS plugin
    always produces exactly one, the mask plugin produces one per masked
    column (§4.2: 'a table with 1 RLS + 3 masks yields 4 rows per attempt').
    `desired_spec` is intentionally NOT stored here: it can't be finalized
    until governed tags are resolved (§7.4), which happens between
    validate() and convert() in the serialized "Prepare Governed Tags"
    phase - convert() builds the final PolicySpec from these raw
    ingredients plus the resolved MatchColumns it receives via
    ConvertOptions."""
    masked_column: Optional[str]
    source_function: str
    source_using_columns: list
    tag_requests: list
    decision: str  # "PROCEED" | "ALREADY_MIGRATED" | "NOT_ELIGIBLE" | "FAILED"
    reason_code: Optional[str] = None
    existing_policy_name: Optional[str] = None
    # True when validate() found a deterministically-named ABAC policy
    # already live for this object (matching function) WHILE the legacy
    # mechanism is still present too - i.e. a prior APPLY_ABAC phase already
    # ran for this object. Mode.FINALIZE requires this to be True before it
    # will remove anything; it never creates a policy itself (that's
    # APPLY_ABAC's / full MIGRATE's job).
    abac_already_applied: bool = False


@dataclass(frozen=True)
class ValidationResult:
    planned_objects: list = field(default_factory=list)  # list[PlannedObject]

    @property
    def any_proceedable(self) -> bool:
        return any(o.decision == "PROCEED" for o in self.planned_objects)


@dataclass(frozen=True)
class ConversionStepResult:
    object_type: str  # "ROW_FILTER" | "COLUMN_MASK"
    status: StepStatus
    masked_column: Optional[str] = None
    source_function: Optional[str] = None
    target_policy_name: Optional[str] = None
    target_definition: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    rollback_metadata: Optional[dict] = None


@dataclass(frozen=True)
class ConvertOptions:
    dry_run: bool = True
    # {(table, column, role): MatchColumn} - produced by the serialized
    # "Prepare Governed Tags" phase (§3, §7.4), consumed here read-only.
    resolved_match_columns: dict = field(default_factory=dict)
    # "FULL" (default, atomic MIGRATE/INVENTORY_AND_MIGRATE - create+verify
    # ABAC then remove+verify legacy in one convert() call), "APPLY_ABAC"
    # (create+verify ABAC only, legacy left alone), or "FINALIZE" (remove
    # legacy + final verify only, requires abac_already_applied=True on the
    # PlannedObject - never creates a policy itself).
    phase: str = "FULL"


class MigrationPlugin(Protocol):
    """Implementations take a `PolicyStrategy` via their own __init__ (not
    per-call) - migration_engine builds `[RLSMigrationPlugin(strategy),
    ColumnMaskMigrationPlugin(strategy)]` once per run (§5 point 1)."""

    object_type: str

    def applies_to(self, table: TableRef, uc: UnityCatalogGateway) -> bool: ...

    def discover(self, table: TableRef, uc: UnityCatalogGateway) -> DiscoveryResult: ...

    def validate(
        self, table: TableRef, discovery: DiscoveryResult, uc: UnityCatalogGateway,
    ) -> ValidationResult: ...

    def tag_requests(self, table: TableRef, validation: ValidationResult) -> list:
        """TagRequests this plugin's PROCEED-decision objects need resolved
        before convert() can build final PolicySpecs. Called by the
        orchestrator during the serialized tag-preparation phase (§7.4),
        not during convert()."""
        ...

    def convert(
        self, table: TableRef, validation: ValidationResult, uc: UnityCatalogGateway, options: ConvertOptions,
    ) -> list: ...  # list[ConversionStepResult]

    def verify(self, table: TableRef, uc: UnityCatalogGateway) -> list: ...  # list[ConversionStepResult]

    def rollback(
        self, table: TableRef, rollback_metadata: dict, uc: UnityCatalogGateway, dry_run: bool,
    ) -> list: ...  # list[ConversionStepResult]
