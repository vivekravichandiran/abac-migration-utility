"""RLSMigrationPlugin: discover/validate/convert/verify/rollback for the
table-level Row Filter -> ABAC ROW_FILTER policy conversion of one table
(§2, §5). Never touches column masks.
"""
from __future__ import annotations

from ...uc_gateway.gateway import UnityCatalogGateway
from ...uc_gateway.models import TableRef
from ..policy_strategy import PolicyStrategy
from ..tag_provisioner import TagRequest
from .base_plugin import ConversionStepResult, ConvertOptions, DiscoveryResult, PlannedObject, StepStatus, ValidationResult


class RLSMigrationPlugin:
    object_type = "ROW_FILTER"

    def __init__(self, policy_strategy: PolicyStrategy):
        self._policy_strategy = policy_strategy

    def applies_to(self, table: TableRef, uc: UnityCatalogGateway) -> bool:
        if uc.describe_table_security(table).has_row_filter:
            return True
        # Legacy row filter may already have been removed by a prior
        # successful run (§7 idempotency) - still "applicable" so a rerun
        # correctly reports ALREADY_MIGRATED instead of NO_LEGACY_SECURITY_FOUND.
        return uc.describe_policy(table, self._policy_strategy.ROW_FILTER_POLICY_NAME) is not None

    def discover(self, table: TableRef, uc: UnityCatalogGateway) -> DiscoveryResult:
        state = uc.describe_table_security(table)
        policies = uc.show_policies(table)
        applicable = state.has_row_filter or any(
            p.policy_name == self._policy_strategy.ROW_FILTER_POLICY_NAME for p in policies
        )
        return DiscoveryResult(applicable=applicable, security_state=state, existing_policies=policies)

    def validate(self, table: TableRef, discovery: DiscoveryResult, uc: UnityCatalogGateway) -> ValidationResult:
        if not discovery.applicable:
            return ValidationResult(planned_objects=[PlannedObject(
                masked_column=None, source_function="", source_using_columns=[],
                tag_requests=[], decision="NOT_ELIGIBLE", reason_code="NO_LEGACY_SECURITY_FOUND",
            )])

        deterministic_name = self._policy_strategy.ROW_FILTER_POLICY_NAME
        rf = discovery.security_state.row_filter if discovery.security_state else None
        existing_ref = next((p for p in discovery.existing_policies if p.policy_name == deterministic_name), None)

        if rf is None:
            # applicable purely because a matching-named ABAC policy already
            # exists and the legacy row filter is already gone (§7).
            if existing_ref is not None:
                existing_def = uc.describe_policy(table, deterministic_name)
                return ValidationResult(planned_objects=[PlannedObject(
                    masked_column=None, source_function=existing_def.function_fqn if existing_def else "",
                    source_using_columns=[], tag_requests=[], decision="ALREADY_MIGRATED",
                    existing_policy_name=deterministic_name,
                )])
            return ValidationResult(planned_objects=[PlannedObject(
                masked_column=None, source_function="", source_using_columns=[],
                tag_requests=[], decision="NOT_ELIGIBLE", reason_code="NO_LEGACY_SECURITY_FOUND",
            )])

        if not uc.function_exists(rf.function_fqn):
            return ValidationResult(planned_objects=[PlannedObject(
                masked_column=None, source_function=rf.function_fqn, source_using_columns=rf.using_columns,
                tag_requests=[], decision="FAILED", reason_code="SOURCE_FUNCTION_NOT_FOUND",
            )])
        if not uc.can_execute_function(rf.function_fqn):
            return ValidationResult(planned_objects=[PlannedObject(
                masked_column=None, source_function=rf.function_fqn, source_using_columns=rf.using_columns,
                tag_requests=[], decision="FAILED", reason_code="SOURCE_FUNCTION_NOT_ACCESSIBLE",
            )])

        abac_already_applied = False
        if existing_ref is not None:
            existing_def = uc.describe_policy(table, deterministic_name)
            # `rf is not None` here (legacy row filter is still live right
            # now, checked above) - so this is never "fully done" even if a
            # matching-function ABAC policy already exists: it's either a
            # fresh migration, a previous APPLY_ABAC-phase run (Mode.APPLY_ABAC)
            # awaiting a FINALIZE run, or a previous full-MIGRATE run whose
            # legacy-removal step failed part-way (confirmed live: `DROP MASK`/
            # legacy-removal can fail due to an unrelated sibling object on the
            # same table, see mask_to_abac.py). Reporting ALREADY_MIGRATED
            # without checking legacy was actually removed would leave the
            # table permanently stuck with both mechanisms active - PROCEED
            # instead so the (idempotent) CREATE OR REPLACE POLICY + legacy-
            # removal retry actually runs again. Only a genuinely *different*
            # function is a real conflict.
            if existing_def is not None and existing_def.function_fqn != rf.function_fqn:
                return ValidationResult(planned_objects=[PlannedObject(
                    masked_column=None, source_function=rf.function_fqn, source_using_columns=rf.using_columns,
                    tag_requests=[], decision="NOT_ELIGIBLE", reason_code="EXISTING_ABAC_POLICY_CONFLICT",
                    existing_policy_name=deterministic_name,
                )])
            abac_already_applied = existing_def is not None

        tag_reqs = [
            TagRequest(table=table, column=c, role="row_filter", function_fqn=rf.function_fqn)
            for c in rf.using_columns
        ]
        return ValidationResult(planned_objects=[PlannedObject(
            masked_column=None, source_function=rf.function_fqn, source_using_columns=rf.using_columns,
            tag_requests=tag_reqs, decision="PROCEED", abac_already_applied=abac_already_applied,
        )])

    def tag_requests(self, table: TableRef, validation: ValidationResult) -> list:
        reqs = []
        for obj in validation.planned_objects:
            if obj.decision == "PROCEED":
                reqs.extend(obj.tag_requests)
        return reqs

    def convert(
        self, table: TableRef, validation: ValidationResult, uc: UnityCatalogGateway, options: ConvertOptions,
    ) -> list:
        results = []
        for obj in validation.planned_objects:
            if obj.decision == "ALREADY_MIGRATED":
                results.append(ConversionStepResult(
                    object_type=self.object_type, status=StepStatus.ALREADY_MIGRATED,
                    source_function=obj.source_function, target_policy_name=obj.existing_policy_name,
                ))
            elif obj.decision in ("NOT_ELIGIBLE", "FAILED"):
                status = StepStatus.NOT_ELIGIBLE if obj.decision == "NOT_ELIGIBLE" else StepStatus.FAILED
                results.append(ConversionStepResult(
                    object_type=self.object_type, status=status,
                    source_function=obj.source_function, error_code=obj.reason_code,
                    target_policy_name=obj.existing_policy_name,
                ))
            elif options.phase == "APPLY_ABAC":
                results.append(self._apply_abac_one(table, obj, uc, options))
            elif options.phase == "FINALIZE":
                results.append(self._finalize_one(table, obj, uc, options))
            else:  # PROCEED, phase == "FULL"
                results.append(self._convert_one(table, obj, uc, options))
        return results

    def _build_match_columns_or_fail(self, table: TableRef, obj: PlannedObject, options: ConvertOptions):
        match_columns = []
        for col in obj.source_using_columns:
            mc = options.resolved_match_columns.get((table, col, "row_filter"))
            if mc is None:
                return None, ConversionStepResult(
                    object_type=self.object_type, status=StepStatus.FAILED, source_function=obj.source_function,
                    error_code="TAG_RESOLUTION_MISSING",
                    error_message=f"No resolved governed tag for row-filter column {col!r}",
                )
            match_columns.append(mc)
        return match_columns, None

    def _apply_abac_one(
        self, table: TableRef, obj: PlannedObject, uc: UnityCatalogGateway, options: ConvertOptions,
    ) -> ConversionStepResult:
        """Mode.APPLY_ABAC: create + verify the ABAC policy only. Legacy row
        filter is deliberately left in place - FINALIZE removes it later."""
        match_columns, failure = self._build_match_columns_or_fail(table, obj, options)
        if failure is not None:
            return failure

        spec = self._policy_strategy.plan_row_filter_policy(table, obj.source_function, match_columns)
        rollback_metadata = {
            "original_row_filter": {"function": obj.source_function, "using_columns": obj.source_using_columns},
            "abac_policies_created_by_this_run": [
                {"policy_name": spec.policy_name, "on_securable": spec.on_securable, "policy_type": spec.policy_type},
            ],
        }

        if options.dry_run:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.WOULD_APPLY_ABAC, source_function=obj.source_function,
                target_policy_name=spec.policy_name, rollback_metadata=rollback_metadata,
            )

        apply_result = uc.create_or_replace_policy(spec, dry_run=False)
        if not apply_result.success:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, source_function=obj.source_function,
                target_policy_name=spec.policy_name, target_definition=apply_result.statement_text,
                error_code=apply_result.error_code or "POLICY_CREATE_FAILED", error_message=apply_result.error_message,
            )

        verify_def = uc.describe_policy(table, spec.policy_name)
        if verify_def is None or verify_def.function_fqn != spec.function_fqn:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, source_function=obj.source_function,
                target_policy_name=spec.policy_name, target_definition=apply_result.statement_text,
                error_code="POLICY_VERIFY_FAILED", rollback_metadata=rollback_metadata,
            )

        return ConversionStepResult(
            object_type=self.object_type, status=StepStatus.ABAC_APPLIED, source_function=obj.source_function,
            target_policy_name=spec.policy_name, target_definition=apply_result.statement_text,
            rollback_metadata=rollback_metadata,
        )

    def _finalize_one(
        self, table: TableRef, obj: PlannedObject, uc: UnityCatalogGateway, options: ConvertOptions,
    ) -> ConversionStepResult:
        """Mode.FINALIZE: remove the legacy row filter for an object whose
        ABAC policy was already applied (by a prior APPLY_ABAC or MIGRATE
        run) and do the final state verification. Never creates a policy -
        refuses (NOT_ELIGIBLE) if APPLY_ABAC hasn't run for this object yet."""
        deterministic_name = self._policy_strategy.ROW_FILTER_POLICY_NAME
        if not obj.abac_already_applied:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.NOT_ELIGIBLE, source_function=obj.source_function,
                error_code="ABAC_NOT_APPLIED_YET",
                error_message="No ABAC row-filter policy found yet for this table - run Mode.APPLY_ABAC (or MIGRATE) first.",
            )

        # Re-confirm right before mutating (§8 state machine: never remove
        # legacy without a fresh VALIDATE ABAC immediately before it).
        verify_def = uc.describe_policy(table, deterministic_name)
        if verify_def is None or verify_def.function_fqn != obj.source_function:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, source_function=obj.source_function,
                target_policy_name=deterministic_name, error_code="POLICY_VERIFY_FAILED",
            )

        rollback_metadata = {
            "original_row_filter": {"function": obj.source_function, "using_columns": obj.source_using_columns},
            "abac_policies_created_by_this_run": [
                {"policy_name": deterministic_name, "on_securable": f"TABLE {table.quoted_full_name}",
                 "policy_type": "ROW_FILTER"},
            ],
        }

        if options.dry_run:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.WOULD_FINALIZE, source_function=obj.source_function,
                target_policy_name=deterministic_name, rollback_metadata=rollback_metadata,
            )

        try:
            uc.drop_row_filter(table, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, source_function=obj.source_function,
                target_policy_name=deterministic_name, error_code="LEGACY_REMOVAL_FAILED",
                error_message=str(exc), rollback_metadata=rollback_metadata,
            )

        final_state = uc.describe_table_security(table)
        if final_state.has_row_filter:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, source_function=obj.source_function,
                target_policy_name=deterministic_name, error_code="FINAL_STATE_VERIFY_FAILED",
                rollback_metadata=rollback_metadata,
            )

        return ConversionStepResult(
            object_type=self.object_type, status=StepStatus.SUCCESS, source_function=obj.source_function,
            target_policy_name=deterministic_name, rollback_metadata=rollback_metadata,
        )

    def _convert_one(
        self, table: TableRef, obj: PlannedObject, uc: UnityCatalogGateway, options: ConvertOptions,
    ) -> ConversionStepResult:
        match_columns, failure = self._build_match_columns_or_fail(table, obj, options)
        if failure is not None:
            return failure

        spec = self._policy_strategy.plan_row_filter_policy(table, obj.source_function, match_columns)
        rollback_metadata = {
            "original_row_filter": {"function": obj.source_function, "using_columns": obj.source_using_columns},
            "abac_policies_created_by_this_run": [
                {"policy_name": spec.policy_name, "on_securable": spec.on_securable, "policy_type": spec.policy_type},
            ],
        }

        if options.dry_run:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.WOULD_MIGRATE, source_function=obj.source_function,
                target_policy_name=spec.policy_name, rollback_metadata=rollback_metadata,
            )

        apply_result = uc.create_or_replace_policy(spec, dry_run=False)
        if not apply_result.success:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, source_function=obj.source_function,
                target_policy_name=spec.policy_name, target_definition=apply_result.statement_text,
                error_code=apply_result.error_code or "POLICY_CREATE_FAILED", error_message=apply_result.error_message,
            )

        verify_def = uc.describe_policy(table, spec.policy_name)
        if verify_def is None or verify_def.function_fqn != spec.function_fqn:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, source_function=obj.source_function,
                target_policy_name=spec.policy_name, target_definition=apply_result.statement_text,
                error_code="POLICY_VERIFY_FAILED", rollback_metadata=rollback_metadata,
            )

        try:
            uc.drop_row_filter(table, dry_run=False)
        except Exception as exc:  # noqa: BLE001 - converted into a taxonomy'd failure, never bubbled raw
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, source_function=obj.source_function,
                target_policy_name=spec.policy_name, target_definition=apply_result.statement_text,
                error_code="LEGACY_REMOVAL_FAILED", error_message=str(exc), rollback_metadata=rollback_metadata,
            )

        final_state = uc.describe_table_security(table)
        if final_state.has_row_filter:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, source_function=obj.source_function,
                target_policy_name=spec.policy_name, target_definition=apply_result.statement_text,
                error_code="FINAL_STATE_VERIFY_FAILED", rollback_metadata=rollback_metadata,
            )

        return ConversionStepResult(
            object_type=self.object_type, status=StepStatus.SUCCESS, source_function=obj.source_function,
            target_policy_name=spec.policy_name, target_definition=apply_result.statement_text,
            rollback_metadata=rollback_metadata,
        )

    def verify(self, table: TableRef, uc: UnityCatalogGateway) -> list:
        deterministic_name = self._policy_strategy.ROW_FILTER_POLICY_NAME
        policy_def = uc.describe_policy(table, deterministic_name)
        state = uc.describe_table_security(table)
        if policy_def is None and not state.has_row_filter:
            # RLS was never applicable to this table (mirrors applies_to()) -
            # nothing to verify here, not a failure. Without this check every
            # RLS-free table (masks-only, or never eligible at all) would be
            # unconditionally reported as a verify FAILED, which is what an
            # earlier live end-to-end run against real UC surfaced.
            return []
        if policy_def is not None and not state.has_row_filter:
            return [ConversionStepResult(
                object_type=self.object_type, status=StepStatus.SUCCESS,
                source_function=policy_def.function_fqn, target_policy_name=deterministic_name,
            )]
        if policy_def is not None and state.has_row_filter:
            # Both mechanisms present - the expected, non-final resting state
            # after an APPLY_ABAC-phase run that hasn't been FINALIZEd yet.
            # Not a failure: report it as its own status so VERIFY/RECONCILE
            # don't misrepresent a normal mid-pipeline state as broken.
            return [ConversionStepResult(
                object_type=self.object_type, status=StepStatus.ABAC_APPLIED,
                source_function=policy_def.function_fqn, target_policy_name=deterministic_name,
            )]
        return [ConversionStepResult(
            object_type=self.object_type, status=StepStatus.FAILED, target_policy_name=deterministic_name,
            error_code="POLICY_VERIFY_FAILED",
        )]

    def rollback(self, table: TableRef, rollback_metadata: dict, uc: UnityCatalogGateway, dry_run: bool) -> list:
        original = rollback_metadata.get("original_row_filter")
        if not original:
            return []

        for policy_info in rollback_metadata.get("abac_policies_created_by_this_run", []):
            if policy_info.get("policy_type") != "ROW_FILTER":
                continue
            uc.drop_policy(table, policy_info["policy_name"], dry_run=dry_run)

        if not uc.function_exists(original["function"]):
            return [ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, source_function=original["function"],
                error_code="SOURCE_FUNCTION_NOT_FOUND",
                error_message="Cannot restore legacy row filter: original function no longer exists.",
            )]

        status = StepStatus.WOULD_ROLLBACK if dry_run else StepStatus.ROLLED_BACK
        uc.set_row_filter(table, original["function"], original["using_columns"], dry_run=dry_run)
        return [ConversionStepResult(object_type=self.object_type, status=status, source_function=original["function"])]
