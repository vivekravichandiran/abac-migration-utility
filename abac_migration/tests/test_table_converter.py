"""The 15 required scenarios from DESIGN.md §15, all against
FakeUnityCatalogGateway - no real Databricks connection needed.
"""
from __future__ import annotations

from ..migration.plugins.base_plugin import StepStatus
from ..migration.table_converter import convert_table
from ..uc_gateway.models import PolicyDefinition, RowFilterInfo, TableRef
from .fake_gateway import FakeUnityCatalogGateway

RF_FN = "cat.sch.rf_business_unit"
MASK_FN_1 = "cat.sch.mask_email"
MASK_FN_2 = "cat.sch.mask_phone"
MASK_FN_3 = "cat.sch.mask_ssn"


def _table(name: str = "orders") -> TableRef:
    return TableRef("cat", "sch", name)


# 1 - Table with RLS only
def test_scenario_1_rls_only():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])

    result = convert_table(table, fake, dry_run=False)

    assert result.rls_status == StepStatus.SUCCESS
    assert result.column_mask_status == {}
    assert result.status == StepStatus.SUCCESS
    assert fake.row_filters[table.full_name] is None
    assert "abac_migrated_row_filter" in fake.policies[table.full_name]


# 2 - Table with Column Mask only
def test_scenario_2_masks_only():
    fake = FakeUnityCatalogGateway()
    table = _table("customers")
    fake.set_column_mask_state(table, "email", MASK_FN_1)
    fake.set_column_mask_state(table, "phone", MASK_FN_2)

    result = convert_table(table, fake, dry_run=False)

    assert result.rls_status is None
    assert result.column_mask_status == {"email": StepStatus.SUCCESS, "phone": StepStatus.SUCCESS}
    assert result.status == StepStatus.SUCCESS
    assert fake.column_masks[table.full_name] == {}


# 3 - Table with both RLS and masks
def test_scenario_3_both():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    fake.set_column_mask_state(table, "email", MASK_FN_1)

    result = convert_table(table, fake, dry_run=False)

    assert result.rls_status == StepStatus.SUCCESS
    assert result.column_mask_status == {"email": StepStatus.SUCCESS}
    assert result.status == StepStatus.SUCCESS


# 4 - Table with no security functions at all
def test_scenario_4_not_eligible_no_security():
    fake = FakeUnityCatalogGateway()
    table = _table("plain")
    fake.register_table(table)

    result = convert_table(table, fake, dry_run=False)

    assert result.status == StepStatus.NOT_ELIGIBLE
    assert result.step_results[0].error_code == "NO_LEGACY_SECURITY_FOUND"


# 5 - Missing function
def test_scenario_5_missing_function():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.register_table(table)
    fake.row_filters[table.full_name] = RowFilterInfo(function_fqn="cat.sch.missing_fn", using_columns=["business_unit"])
    # deliberately not added to fake.functions -> function_exists() is False

    result = convert_table(table, fake, dry_run=False)

    assert result.status == StepStatus.FAILED
    assert result.step_results[0].error_code == "SOURCE_FUNCTION_NOT_FOUND"
    assert fake.mutation_calls == []


# 6 - Existing ABAC policy (conflicting)
def test_scenario_6_existing_conflicting_policy():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    fake.add_existing_policy(table, PolicyDefinition(
        name="abac_migrated_row_filter", policy_type="ROW_FILTER", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_business_unit"],
        function_fqn="cat.sch.some_other_fn", using_columns=["mc_business_unit"],
    ))

    result = convert_table(table, fake, dry_run=False)

    assert result.status == StepStatus.NOT_ELIGIBLE
    assert result.step_results[0].error_code == "EXISTING_ABAC_POLICY_CONFLICT"
    assert fake.mutation_calls == []


# 7 - Already migrated table
def test_scenario_7_already_migrated():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.register_table(table)
    fake.functions.add(RF_FN)
    fake.add_existing_policy(table, PolicyDefinition(
        name="abac_migrated_row_filter", policy_type="ROW_FILTER", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_business_unit"],
        function_fqn=RF_FN, using_columns=["mc_business_unit"],
    ))
    # legacy row filter already removed (row_filters[table.full_name] stays None)

    result = convert_table(table, fake, dry_run=False)

    assert result.status == StepStatus.ALREADY_MIGRATED
    assert fake.mutation_calls == []


def test_scenario_7b_abac_exists_but_legacy_removal_previously_failed_retries_not_already_migrated():
    """Regression: found via a live end-to-end run. If a prior run created
    the ABAC policy but the legacy-removal step failed (e.g. UC rejected
    dropping one column's mask because of an unrelated sibling column's
    dangling function reference), the legacy row filter/mask is still live.
    A rerun must retry finishing the job, not declare ALREADY_MIGRATED and
    leave both mechanisms permanently active."""
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])  # legacy STILL present
    fake.add_existing_policy(table, PolicyDefinition(  # ABAC policy already created
        name="abac_migrated_row_filter", policy_type="ROW_FILTER", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_business_unit"],
        function_fqn=RF_FN, using_columns=["mc_business_unit"],
    ))

    result = convert_table(table, fake, dry_run=False)

    assert result.rls_status == StepStatus.SUCCESS
    assert ("drop_row_filter", table.full_name) in fake.mutation_calls
    assert fake.row_filters[table.full_name] is None


# 8 - ABAC creation failure
def test_scenario_8_policy_create_failure():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    fake.fail_next_create_policy(error_code="POLICY_CREATE_FAILED", error_message="boom")

    result = convert_table(table, fake, dry_run=False)

    assert result.status == StepStatus.FAILED
    assert result.step_results[0].error_code == "POLICY_CREATE_FAILED"
    assert fake.row_filters[table.full_name] is not None  # legacy untouched


# 9 - ABAC validation failure (describe_policy returns a mismatched spec)
def test_scenario_9_policy_verify_failure():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    fake.override_describe_policy(table, "abac_migrated_row_filter", PolicyDefinition(
        name="abac_migrated_row_filter", policy_type="ROW_FILTER", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_business_unit"],
        function_fqn="cat.sch.totally_different_fn", using_columns=["mc_business_unit"],
    ))

    result = convert_table(table, fake, dry_run=False)

    assert result.status == StepStatus.FAILED
    assert result.step_results[0].error_code == "POLICY_VERIFY_FAILED"
    assert fake.row_filters[table.full_name] is not None  # legacy untouched
    assert "abac_migrated_row_filter" in fake.policies[table.full_name]  # new policy left for inspection


# 10 - Old RLS removal failure
def test_scenario_10_legacy_removal_failure():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    fake.set_fault("drop_row_filter", RuntimeError("cannot drop"))

    result = convert_table(table, fake, dry_run=False)

    assert result.status == StepStatus.FAILED
    assert result.step_results[0].error_code == "LEGACY_REMOVAL_FAILED"
    assert fake.row_filters[table.full_name] is not None  # legacy still present
    assert "abac_migrated_row_filter" in fake.policies[table.full_name]  # ABAC policy also still present


# 11 - Multiple masked columns, one fails
def test_scenario_11_partial_mask_failure_is_weakest_link():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_column_mask_state(table, "email", MASK_FN_1)
    fake.set_column_mask_state(table, "phone", MASK_FN_2)
    fake.set_column_mask_state(table, "ssn", MASK_FN_3)

    from ..uc_gateway.models import PolicyApplyResult

    original_create = fake.create_or_replace_policy

    def flaky_create(spec, dry_run):
        if spec.policy_name == "abac_migrated_mask_phone":
            return PolicyApplyResult(success=False, policy_name=spec.policy_name,
                                      error_code="POLICY_CREATE_FAILED", error_message="phone column failed")
        return original_create(spec, dry_run)

    fake.create_or_replace_policy = flaky_create

    result = convert_table(table, fake, dry_run=False)

    assert result.column_mask_status["email"] == StepStatus.SUCCESS
    assert result.column_mask_status["ssn"] == StepStatus.SUCCESS
    assert result.column_mask_status["phone"] == StepStatus.FAILED
    assert result.status == StepStatus.FAILED  # weakest link (§6)


def test_scenario_11b_mask_abac_exists_but_legacy_removal_previously_failed_retries():
    """Same regression as scenario 7b, for masks specifically - this is the
    exact shape a live run hit: one column's source function was dropped
    after being set, and Unity Catalog's DROP MASK validates every masked
    column on the table, so it failed removing the *other*, healthy
    columns' legacy masks too even after their ABAC policies were created."""
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_column_mask_state(table, "email", MASK_FN_1)  # legacy STILL present
    fake.add_existing_policy(table, PolicyDefinition(  # ABAC policy already created
        name="abac_migrated_mask_email", policy_type="COLUMN_MASK", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_email"],
        function_fqn=MASK_FN_1, using_columns=[], on_column_alias="mc_email",
    ))

    result = convert_table(table, fake, dry_run=False)

    assert result.column_mask_status["email"] == StepStatus.SUCCESS
    assert ("drop_column_mask", table.full_name, "email") in fake.mutation_calls
    assert "email" not in fake.column_masks[table.full_name]


# 12 - Dry run
def test_scenario_12_dry_run():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    fake.set_column_mask_state(table, "email", MASK_FN_1)

    result = convert_table(table, fake, dry_run=True)

    assert result.status == StepStatus.WOULD_MIGRATE
    assert result.rls_status == StepStatus.WOULD_MIGRATE
    assert result.column_mask_status == {"email": StepStatus.WOULD_MIGRATE}
    assert fake.mutation_calls == []
    # legacy state completely untouched
    assert fake.row_filters[table.full_name] is not None
    assert "email" in fake.column_masks[table.full_name]


# 13 - Retry / idempotent execution
def test_scenario_13_rerun_is_idempotent():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])

    first = convert_table(table, fake, dry_run=False)
    assert first.status == StepStatus.SUCCESS
    policy_count_after_first = len(fake.policies[table.full_name])

    second = convert_table(table, fake, dry_run=False)

    assert second.status == StepStatus.ALREADY_MIGRATED
    assert len(fake.policies[table.full_name]) == policy_count_after_first  # no duplicate objects


# 14 - Configuration drift
def test_scenario_14_drift_detection():
    from ..validation.drift_detection import detect_drift

    class _StubAuditRepo:
        """Stands in for AuditRepository.latest_status (§4.4) without
        needing full SQL emulation in the fake gateway - drift_detection
        only ever calls this one method."""
        def latest_status(self, catalog, schema, table):
            return {"status": "SUCCESS", "target_policy_name": "abac_migrated_row_filter",
                    "object_type": "ROW_FILTER", "masked_column": None, "rollback_metadata": {}}

    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    convert_table(table, fake, dry_run=False)
    assert detect_drift(table, _StubAuditRepo(), fake).drift_detected is False

    # externally remove the ABAC policy before RECONCILE runs
    del fake.policies[table.full_name]["abac_migrated_row_filter"]

    drift = detect_drift(table, _StubAuditRepo(), fake)

    assert drift.drift_detected is True


# 15 - Rollback
def test_scenario_15_rollback_restores_exactly_and_leaves_unrelated_policy_alone():
    from ..rollback.rollback_manager import rollback_table

    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    fake.set_column_mask_state(table, "email", MASK_FN_1)
    fake.add_existing_policy(table, PolicyDefinition(
        name="unrelated_policy", policy_type="COLUMN_MASK", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["x"],
        function_fqn="cat.sch.unrelated_fn", using_columns=[],
    ))

    result = convert_table(table, fake, dry_run=False)
    assert result.status == StepStatus.SUCCESS

    rollback_result = rollback_table(table, result.rollback_metadata, fake, dry_run=False)

    assert rollback_result.status == StepStatus.ROLLED_BACK
    assert fake.row_filters[table.full_name].function_fqn == RF_FN
    assert fake.column_masks[table.full_name]["email"].function_fqn == MASK_FN_1
    assert "abac_migrated_row_filter" not in fake.policies[table.full_name]
    assert "abac_migrated_mask_email" not in fake.policies[table.full_name]
    assert "unrelated_policy" in fake.policies[table.full_name]  # untouched


# ---------------------------------------------------------------------------
# Isolated-phase modes (Mode.APPLY_ABAC / Mode.FINALIZE): APPLY_ABAC creates
# the ABAC policy and stops - legacy stays live - then FINALIZE removes the
# legacy mechanism and never creates a policy itself.
# ---------------------------------------------------------------------------

def test_apply_abac_phase_creates_policy_and_leaves_legacy_untouched():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    fake.set_column_mask_state(table, "email", MASK_FN_1)

    result = convert_table(table, fake, dry_run=False, phase="APPLY_ABAC")

    assert result.rls_status == StepStatus.ABAC_APPLIED
    assert result.column_mask_status == {"email": StepStatus.ABAC_APPLIED}
    assert result.status == StepStatus.ABAC_APPLIED
    assert result.migration_phase == "ABAC_APPLIED"
    # both mechanisms present now - legacy deliberately untouched
    assert fake.row_filters[table.full_name] is not None
    assert "email" in fake.column_masks[table.full_name]
    assert "abac_migrated_row_filter" in fake.policies[table.full_name]
    assert "abac_migrated_mask_email" in fake.policies[table.full_name]
    assert ("drop_row_filter", table.full_name) not in fake.mutation_calls
    assert ("drop_column_mask", table.full_name, "email") not in fake.mutation_calls


def test_apply_abac_phase_dry_run_mutates_nothing():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])

    result = convert_table(table, fake, dry_run=True, phase="APPLY_ABAC")

    assert result.status == StepStatus.WOULD_APPLY_ABAC
    assert result.migration_phase == "DRY_RUN"
    assert fake.mutation_calls == []


def test_finalize_phase_refuses_when_abac_not_yet_applied():
    """FINALIZE never creates a policy - a table that only ever had legacy
    security (no prior APPLY_ABAC/MIGRATE run) is NOT_ELIGIBLE, not silently
    migrated in one step."""
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])

    result = convert_table(table, fake, dry_run=False, phase="FINALIZE")

    assert result.status == StepStatus.NOT_ELIGIBLE
    assert result.step_results[0].error_code == "ABAC_NOT_APPLIED_YET"
    assert fake.row_filters[table.full_name] is not None  # untouched
    assert fake.mutation_calls == []


def test_finalize_phase_removes_legacy_after_apply_abac_and_reaches_success():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    fake.set_column_mask_state(table, "email", MASK_FN_1)

    applied = convert_table(table, fake, dry_run=False, phase="APPLY_ABAC")
    assert applied.status == StepStatus.ABAC_APPLIED

    finalized = convert_table(table, fake, dry_run=False, phase="FINALIZE")

    assert finalized.status == StepStatus.SUCCESS
    assert finalized.migration_phase == "FINALIZED"
    assert finalized.rls_status == StepStatus.SUCCESS
    assert finalized.column_mask_status == {"email": StepStatus.SUCCESS}
    assert fake.row_filters[table.full_name] is None
    assert fake.column_masks[table.full_name] == {}
    assert "abac_migrated_row_filter" in fake.policies[table.full_name]
    assert "abac_migrated_mask_email" in fake.policies[table.full_name]


def test_finalize_phase_dry_run_mutates_nothing():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    convert_table(table, fake, dry_run=False, phase="APPLY_ABAC")

    result = convert_table(table, fake, dry_run=True, phase="FINALIZE")

    assert result.status == StepStatus.WOULD_FINALIZE
    assert fake.row_filters[table.full_name] is not None  # untouched
    assert ("drop_row_filter", table.full_name) not in fake.mutation_calls


def test_finalize_phase_never_needs_tag_provisioner_call():
    """FINALIZE skips tag resolution entirely - it never builds a
    CREATE POLICY statement, so it must never call list_governed_tags()."""
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    convert_table(table, fake, dry_run=False, phase="APPLY_ABAC")

    calls_before = len(fake.mutation_calls)
    result = convert_table(table, fake, dry_run=False, phase="FINALIZE")

    assert result.status == StepStatus.SUCCESS
    assert ("set_column_tags", table.full_name, "business_unit") not in fake.mutation_calls[calls_before:]


def test_apply_abac_then_finalize_matches_full_phase_end_state():
    """Running APPLY_ABAC then FINALIZE must reach the exact same live UC
    end-state as one atomic FULL-phase convert_table() call."""
    fake_full = FakeUnityCatalogGateway()
    table_full = _table("full_path")
    fake_full.set_row_filter_state(table_full, RF_FN, ["business_unit"])
    fake_full.set_column_mask_state(table_full, "email", MASK_FN_1)
    full_result = convert_table(table_full, fake_full, dry_run=False)
    assert full_result.status == StepStatus.SUCCESS

    fake_phased = FakeUnityCatalogGateway()
    table_phased = _table("phased_path")
    fake_phased.set_row_filter_state(table_phased, RF_FN, ["business_unit"])
    fake_phased.set_column_mask_state(table_phased, "email", MASK_FN_1)
    convert_table(table_phased, fake_phased, dry_run=False, phase="APPLY_ABAC")
    phased_result = convert_table(table_phased, fake_phased, dry_run=False, phase="FINALIZE")
    assert phased_result.status == StepStatus.SUCCESS

    assert fake_full.row_filters[table_full.full_name] == fake_phased.row_filters[table_phased.full_name]
    assert fake_full.column_masks[table_full.full_name] == fake_phased.column_masks[table_phased.full_name]
    assert set(fake_full.policies[table_full.full_name]) == set(fake_phased.policies[table_phased.full_name])


def test_apply_abac_rerun_is_idempotent():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])

    first = convert_table(table, fake, dry_run=False, phase="APPLY_ABAC")
    assert first.status == StepStatus.ABAC_APPLIED
    policy_count_after_first = len(fake.policies[table.full_name])

    second = convert_table(table, fake, dry_run=False, phase="APPLY_ABAC")

    assert second.status == StepStatus.ABAC_APPLIED
    assert len(fake.policies[table.full_name]) == policy_count_after_first
    assert fake.row_filters[table.full_name] is not None  # still untouched


def test_finalize_rerun_after_success_reports_already_migrated():
    fake = FakeUnityCatalogGateway()
    table = _table()
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    convert_table(table, fake, dry_run=False, phase="APPLY_ABAC")
    first_finalize = convert_table(table, fake, dry_run=False, phase="FINALIZE")
    assert first_finalize.status == StepStatus.SUCCESS

    second_finalize = convert_table(table, fake, dry_run=False, phase="FINALIZE")

    assert second_finalize.status == StepStatus.ALREADY_MIGRATED
