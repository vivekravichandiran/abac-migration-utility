"""Regression coverage for verify_table()/plugin.verify(), added after a
live end-to-end run against a real workspace surfaced a false-positive: a
masks-only (or entirely unmigrated) table was unconditionally reported as
FAILED by RLSMigrationPlugin.verify() even though row-level security was
never applicable to it in the first place.
"""
from __future__ import annotations

from ..migration.plugins.base_plugin import StepStatus
from ..migration.policy_strategy import TableBasedPolicyStrategy
from ..uc_gateway.models import PolicyDefinition, TableRef
from ..validation.post_validation import verify_table
from .fake_gateway import FakeUnityCatalogGateway


def _strategy():
    return TableBasedPolicyStrategy()


def test_verify_table_never_had_any_security_is_not_eligible_not_failed():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "plain_table")
    uc.register_table(table)

    result = verify_table(table, uc, _strategy())

    assert result.status == StepStatus.NOT_ELIGIBLE
    assert result.step_results == []


def test_verify_table_masks_only_migrated_table_passes():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "masks_only")
    uc.register_table(table)
    mask_policy_name = _strategy().mask_policy_name("email")
    uc.add_existing_policy(table, PolicyDefinition(
        name=mask_policy_name, policy_type="COLUMN_MASK", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_email"],
        function_fqn="cat.schema.mask_email", using_columns=[], on_column_alias="mc_email",
    ))

    result = verify_table(table, uc, _strategy())

    assert result.status == StepStatus.SUCCESS
    assert len(result.step_results) == 1
    assert result.step_results[0].object_type == "COLUMN_MASK"


def test_verify_table_legacy_rls_never_migrated_is_failed():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "half_migrated")
    uc.set_row_filter_state(table, "cat.schema.rf_fn", ["region"])

    result = verify_table(table, uc, _strategy())

    assert result.status == StepStatus.FAILED
    assert any(r.object_type == "ROW_FILTER" for r in result.step_results)


def test_verify_table_both_mechanisms_present_reports_abac_applied_not_failed():
    """A table mid-way through the isolated-phase pipeline (APPLY_ABAC ran,
    FINALIZE hasn't yet) has both the ABAC policy AND the legacy row filter
    live simultaneously - this is expected, not a failure, and VERIFY/
    RECONCILE must not misreport it as FAILED."""
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "abac_applied_not_final")
    strategy = _strategy()
    uc.set_row_filter_state(table, "cat.schema.rf_fn", ["region"])
    uc.add_existing_policy(table, PolicyDefinition(
        name=strategy.ROW_FILTER_POLICY_NAME, policy_type="ROW_FILTER", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_region"],
        function_fqn="cat.schema.rf_fn", using_columns=["region"],
    ))

    result = verify_table(table, uc, strategy)

    assert result.status == StepStatus.ABAC_APPLIED
    assert len(result.step_results) == 1
    assert result.step_results[0].status == StepStatus.ABAC_APPLIED
    assert result.step_results[0].error_code is None


def test_verify_table_fully_migrated_rls_and_mask_passes():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "fully_migrated")
    strategy = _strategy()
    uc.register_table(table)
    uc.add_existing_policy(table, PolicyDefinition(
        name=strategy.ROW_FILTER_POLICY_NAME, policy_type="ROW_FILTER", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_region"],
        function_fqn="cat.schema.rf_fn", using_columns=["region"],
    ))
    mask_policy_name = strategy.mask_policy_name("email")
    uc.add_existing_policy(table, PolicyDefinition(
        name=mask_policy_name, policy_type="COLUMN_MASK", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_email"],
        function_fqn="cat.schema.mask_email", using_columns=[], on_column_alias="mc_email",
    ))

    result = verify_table(table, uc, strategy)

    assert result.status == StepStatus.SUCCESS
    assert len(result.step_results) == 2
