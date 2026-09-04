"""ColumnMaskMigrationPlugin: discover/validate/convert/verify/rollback for
Column Masks -> ABAC COLUMN_MASK policies, one masked column at a time
(§2, §5). Each column is an independent unit - one column's failure never
blocks the others in the same table (§10). Deliberately scope-agnostic:
every `ON <securable>` clause and every deterministic policy name comes
from `self._policy_strategy` (policy_strategy.py) - this plugin works
identically whether the strategy is table-scoped ("table level
application", one policy per masked COLUMN) or catalog-scoped ("catalog
level application", one policy per masking FUNCTION, shared by every
column/table it happens to mask).
"""
from __future__ import annotations

from ...uc_gateway.gateway import UnityCatalogGateway
from ...uc_gateway.models import TableRef
from ..policy_strategy import PolicyStrategy
from ..tag_provisioner import TagRequest
from .base_plugin import ConversionStepResult, ConvertOptions, DiscoveryResult, PlannedObject, StepStatus, ValidationResult


class ColumnMaskMigrationPlugin:
    object_type = "COLUMN_MASK"

    def __init__(self, policy_strategy: PolicyStrategy):
        self._policy_strategy = policy_strategy

    def applies_to(self, table: TableRef, uc: UnityCatalogGateway) -> bool:
        if uc.describe_table_security(table).has_column_masks:
            return True
        # Legacy masks may already have been removed by a prior successful
        # run (§7 idempotency) - still "applicable" so a rerun correctly
        # reports ALREADY_MIGRATED instead of NO_LEGACY_SECURITY_FOUND.
        return len(self._policy_strategy.find_existing_mask_policies(table, uc)) > 0

    def discover(self, table: TableRef, uc: UnityCatalogGateway) -> DiscoveryResult:
        state = uc.describe_table_security(table)
        on_securable = self._policy_strategy.on_securable_for(table)
        policies = uc.show_policies(on_securable)
        already_migrated_exists = len(self._policy_strategy.find_existing_mask_policies(table, uc)) > 0
        applicable = state.has_column_masks or already_migrated_exists
        return DiscoveryResult(applicable=applicable, security_state=state, existing_policies=policies)

    def validate(self, table: TableRef, discovery: DiscoveryResult, uc: UnityCatalogGateway) -> ValidationResult:
        if not discovery.applicable:
            return ValidationResult(planned_objects=[PlannedObject(
                masked_column=None, source_function="", source_using_columns=[],
                tag_requests=[], decision="NOT_ELIGIBLE", reason_code="NO_LEGACY_SECURITY_FOUND",
            )])

        legacy_masks = discovery.security_state.column_masks if discovery.security_state else []
        legacy_columns = {m.column for m in legacy_masks}

        planned = [self._validate_one_mask(table, mask, discovery, uc) for mask in legacy_masks]

        # Columns whose legacy mask is already gone but a matching-function
        # ABAC policy still exists (§7): legacy discovery can no longer see
        # these directly, so recover them via the strategy instead.
        for existing in self._policy_strategy.find_existing_mask_policies(table, uc):
            if existing.column in legacy_columns:
                continue
            planned.append(PlannedObject(
                masked_column=existing.column, source_function=existing.policy_def.function_fqn,
                source_using_columns=[], tag_requests=[], decision="ALREADY_MIGRATED",
                existing_policy_name=existing.policy_def.name,
            ))

        return ValidationResult(planned_objects=planned)

    def _validate_one_mask(self, table, mask, discovery, uc) -> PlannedObject:
        if not uc.function_exists(mask.function_fqn):
            return PlannedObject(
                masked_column=mask.column, source_function=mask.function_fqn, source_using_columns=[],
                tag_requests=[], decision="FAILED", reason_code="SOURCE_FUNCTION_NOT_FOUND",
            )
        if not uc.can_execute_function(mask.function_fqn):
            return PlannedObject(
                masked_column=mask.column, source_function=mask.function_fqn, source_using_columns=[],
                tag_requests=[], decision="FAILED", reason_code="SOURCE_FUNCTION_NOT_ACCESSIBLE",
            )

        deterministic_name = self._policy_strategy.mask_policy_name(mask.column, mask.function_fqn)
        on_securable = self._policy_strategy.on_securable_for(table)

        # NOTE: `mask` only ever gets here via `legacy_masks` (§ discover), i.e.
        # the LEGACY mask is still live right now. So even if a matching ABAC
        # policy already exists, this is never "fully done" - it's either a
        # fresh migration or a previous run whose legacy-removal step failed
        # part-way (confirmed live: DROP MASK on one column can fail with
        # UC_DEPENDENCY_DOES_NOT_EXIST because Unity Catalog validates *every*
        # masked column on the table, not just the one being dropped, so one
        # column with an orphaned function can block another column's legacy
        # removal indefinitely). Treating "ABAC policy exists" as ALREADY_MIGRATED
        # here - without checking legacy is actually gone - meant a partially
        # migrated column (both mechanisms simultaneously active) could never
        # self-heal on rerun. Always PROCEED so the (idempotent) CREATE OR
        # REPLACE POLICY + legacy-removal retry actually runs again.
        #
        # Membership in `discovery.existing_policies` (SHOW POLICIES) is
        # checked BEFORE the more expensive/authoritative DESCRIBE POLICY
        # call - see the identical comment in rls_to_abac.py's validate().
        existing_ref = next((p for p in discovery.existing_policies if p.policy_name == deterministic_name), None)
        abac_already_applied = False
        if existing_ref is not None:
            existing_def = uc.describe_policy(on_securable, deterministic_name)
            if existing_def is not None and existing_def.function_fqn != mask.function_fqn:
                return PlannedObject(
                    masked_column=mask.column, source_function=mask.function_fqn, source_using_columns=[],
                    tag_requests=[], decision="NOT_ELIGIBLE", reason_code="EXISTING_ABAC_POLICY_CONFLICT",
                    existing_policy_name=deterministic_name,
                )
            abac_already_applied = existing_def is not None

        tag_reqs = [TagRequest(table=table, column=mask.column, role="mask", function_fqn=mask.function_fqn)]
        return PlannedObject(
            masked_column=mask.column, source_function=mask.function_fqn, source_using_columns=[],
            tag_requests=tag_reqs, decision="PROCEED", abac_already_applied=abac_already_applied,
        )

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
                    object_type=self.object_type, status=StepStatus.ALREADY_MIGRATED, masked_column=obj.masked_column,
                    source_function=obj.source_function, target_policy_name=obj.existing_policy_name,
                ))
            elif obj.decision in ("NOT_ELIGIBLE", "FAILED"):
                status = StepStatus.NOT_ELIGIBLE if obj.decision == "NOT_ELIGIBLE" else StepStatus.FAILED
                results.append(ConversionStepResult(
                    object_type=self.object_type, status=status, masked_column=obj.masked_column,
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

    def _apply_abac_one(
        self, table: TableRef, obj: PlannedObject, uc: UnityCatalogGateway, options: ConvertOptions,
    ) -> ConversionStepResult:
        """Mode.APPLY_ABAC: create + verify the ABAC column-mask policy only.
        Legacy mask is deliberately left in place - FINALIZE removes it later."""
        mc = options.resolved_match_columns.get((table, obj.masked_column, "mask"))
        if mc is None:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, masked_column=obj.masked_column,
                source_function=obj.source_function, error_code="TAG_RESOLUTION_MISSING",
                error_message=f"No resolved governed tag for masked column {obj.masked_column!r}",
            )

        spec = self._policy_strategy.plan_column_mask_policy(table, obj.masked_column, obj.source_function, mc)
        rollback_metadata = {
            "original_column_masks": [{"column": obj.masked_column, "function": obj.source_function}],
            "abac_policies_created_by_this_run": [
                {"policy_name": spec.policy_name, "on_securable": spec.on_securable, "policy_type": spec.policy_type},
            ],
        }

        if options.dry_run:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.WOULD_APPLY_ABAC, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=spec.policy_name,
                rollback_metadata=rollback_metadata,
            )

        apply_result = uc.create_or_replace_policy(spec, dry_run=False)
        if not apply_result.success:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=spec.policy_name,
                target_definition=apply_result.statement_text,
                error_code=apply_result.error_code or "POLICY_CREATE_FAILED", error_message=apply_result.error_message,
            )

        verify_def = uc.describe_policy(spec.on_securable, spec.policy_name)
        if verify_def is None or verify_def.function_fqn != spec.function_fqn:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=spec.policy_name,
                target_definition=apply_result.statement_text,
                error_code="POLICY_VERIFY_FAILED", rollback_metadata=rollback_metadata,
            )

        return ConversionStepResult(
            object_type=self.object_type, status=StepStatus.ABAC_APPLIED, masked_column=obj.masked_column,
            source_function=obj.source_function, target_policy_name=spec.policy_name,
            target_definition=apply_result.statement_text, rollback_metadata=rollback_metadata,
        )

    def _finalize_one(
        self, table: TableRef, obj: PlannedObject, uc: UnityCatalogGateway, options: ConvertOptions,
    ) -> ConversionStepResult:
        """Mode.FINALIZE: remove the legacy column mask for a column whose
        ABAC policy was already applied, then do the final state
        verification. Never creates a policy - refuses (NOT_ELIGIBLE) if
        APPLY_ABAC hasn't run for this column yet."""
        if not obj.abac_already_applied:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.NOT_ELIGIBLE, masked_column=obj.masked_column,
                source_function=obj.source_function, error_code="ABAC_NOT_APPLIED_YET",
                error_message=f"No ABAC mask policy found yet for column {obj.masked_column!r} - "
                               "run Mode.APPLY_ABAC (or MIGRATE) first.",
            )

        deterministic_name = self._policy_strategy.mask_policy_name(obj.masked_column, obj.source_function)
        on_securable = self._policy_strategy.on_securable_for(table)
        verify_def = uc.describe_policy(on_securable, deterministic_name)
        if verify_def is None or verify_def.function_fqn != obj.source_function:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=deterministic_name,
                error_code="POLICY_VERIFY_FAILED",
            )

        rollback_metadata = {
            "original_column_masks": [{"column": obj.masked_column, "function": obj.source_function}],
            "abac_policies_created_by_this_run": [
                {"policy_name": deterministic_name, "on_securable": on_securable, "policy_type": "COLUMN_MASK"},
            ],
        }

        if options.dry_run:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.WOULD_FINALIZE, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=deterministic_name,
                rollback_metadata=rollback_metadata,
            )

        try:
            uc.drop_column_mask(table, obj.masked_column, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=deterministic_name,
                error_code="LEGACY_REMOVAL_FAILED", error_message=str(exc), rollback_metadata=rollback_metadata,
            )

        final_state = uc.describe_table_security(table)
        if any(m.column == obj.masked_column for m in final_state.column_masks):
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=deterministic_name,
                error_code="FINAL_STATE_VERIFY_FAILED", rollback_metadata=rollback_metadata,
            )

        return ConversionStepResult(
            object_type=self.object_type, status=StepStatus.SUCCESS, masked_column=obj.masked_column,
            source_function=obj.source_function, target_policy_name=deterministic_name,
            rollback_metadata=rollback_metadata,
        )

    def _convert_one(
        self, table: TableRef, obj: PlannedObject, uc: UnityCatalogGateway, options: ConvertOptions,
    ) -> ConversionStepResult:
        mc = options.resolved_match_columns.get((table, obj.masked_column, "mask"))
        if mc is None:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, masked_column=obj.masked_column,
                source_function=obj.source_function, error_code="TAG_RESOLUTION_MISSING",
                error_message=f"No resolved governed tag for masked column {obj.masked_column!r}",
            )

        spec = self._policy_strategy.plan_column_mask_policy(table, obj.masked_column, obj.source_function, mc)
        rollback_metadata = {
            "original_column_masks": [{"column": obj.masked_column, "function": obj.source_function}],
            "abac_policies_created_by_this_run": [
                {"policy_name": spec.policy_name, "on_securable": spec.on_securable, "policy_type": spec.policy_type},
            ],
        }

        if options.dry_run:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.WOULD_MIGRATE, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=spec.policy_name,
                rollback_metadata=rollback_metadata,
            )

        apply_result = uc.create_or_replace_policy(spec, dry_run=False)
        if not apply_result.success:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=spec.policy_name,
                target_definition=apply_result.statement_text,
                error_code=apply_result.error_code or "POLICY_CREATE_FAILED", error_message=apply_result.error_message,
            )

        verify_def = uc.describe_policy(spec.on_securable, spec.policy_name)
        if verify_def is None or verify_def.function_fqn != spec.function_fqn:
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=spec.policy_name,
                target_definition=apply_result.statement_text,
                error_code="POLICY_VERIFY_FAILED", rollback_metadata=rollback_metadata,
            )

        try:
            uc.drop_column_mask(table, obj.masked_column, dry_run=False)
        except Exception as exc:  # noqa: BLE001 - converted into a taxonomy'd failure, never bubbled raw
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=spec.policy_name,
                target_definition=apply_result.statement_text,
                error_code="LEGACY_REMOVAL_FAILED", error_message=str(exc), rollback_metadata=rollback_metadata,
            )

        final_state = uc.describe_table_security(table)
        if any(m.column == obj.masked_column for m in final_state.column_masks):
            return ConversionStepResult(
                object_type=self.object_type, status=StepStatus.FAILED, masked_column=obj.masked_column,
                source_function=obj.source_function, target_policy_name=spec.policy_name,
                target_definition=apply_result.statement_text,
                error_code="FINAL_STATE_VERIFY_FAILED", rollback_metadata=rollback_metadata,
            )

        return ConversionStepResult(
            object_type=self.object_type, status=StepStatus.SUCCESS, masked_column=obj.masked_column,
            source_function=obj.source_function, target_policy_name=spec.policy_name,
            target_definition=apply_result.statement_text, rollback_metadata=rollback_metadata,
        )

    def verify(self, table: TableRef, uc: UnityCatalogGateway) -> list:
        """Mirrors the pre-existing TABLE_BASED semantics exactly (iterate
        already-*discoverable* ABAC mask policies via the strategy, not raw
        legacy state) - a masked column that was never migrated at all (no
        ABAC policy exists yet for it) reports nothing here, same as before
        this method delegated naming/discovery to PolicyStrategy. Consistent
        across scopes since `find_existing_mask_policies` already
        encapsulates exactly "what does this table have today" for either
        scope."""
        state = uc.describe_table_security(table)
        still_legacy_columns = {m.column for m in state.column_masks}
        results = []
        for existing in self._policy_strategy.find_existing_mask_policies(table, uc):
            if existing.column in still_legacy_columns:
                # Both mechanisms present - expected, non-final resting state
                # after an APPLY_ABAC-phase run awaiting FINALIZE. Not a
                # failure - see the identical case in rls_to_abac.py.
                results.append(ConversionStepResult(
                    object_type=self.object_type, status=StepStatus.ABAC_APPLIED, masked_column=existing.column,
                    source_function=existing.policy_def.function_fqn, target_policy_name=existing.policy_def.name,
                ))
            else:
                results.append(ConversionStepResult(
                    object_type=self.object_type, status=StepStatus.SUCCESS, masked_column=existing.column,
                    source_function=existing.policy_def.function_fqn, target_policy_name=existing.policy_def.name,
                ))
        return results

    def rollback(self, table: TableRef, rollback_metadata: dict, uc: UnityCatalogGateway, dry_run: bool) -> list:
        results = []
        for original in rollback_metadata.get("original_column_masks", []):
            column = original["column"]
            for policy_info in rollback_metadata.get("abac_policies_created_by_this_run", []):
                if policy_info.get("policy_type") != "COLUMN_MASK":
                    continue
                if policy_info["policy_name"] != self._policy_strategy.mask_policy_name(column, original["function"]):
                    continue
                # See rls_to_abac.py's identical comment: use the exact
                # on_securable this run created the policy on, not a
                # recomputation from the current strategy config.
                on_securable = policy_info.get("on_securable") or self._policy_strategy.on_securable_for(table)
                uc.drop_policy(on_securable, policy_info["policy_name"], dry_run=dry_run)

            if not uc.function_exists(original["function"]):
                results.append(ConversionStepResult(
                    object_type=self.object_type, status=StepStatus.FAILED, masked_column=column,
                    source_function=original["function"], error_code="SOURCE_FUNCTION_NOT_FOUND",
                    error_message="Cannot restore legacy column mask: original function no longer exists.",
                ))
                continue

            status = StepStatus.WOULD_ROLLBACK if dry_run else StepStatus.ROLLED_BACK
            uc.set_column_mask(table, column, original["function"], dry_run=dry_run)
            results.append(ConversionStepResult(
                object_type=self.object_type, status=status, masked_column=column, source_function=original["function"],
            ))
        return results
