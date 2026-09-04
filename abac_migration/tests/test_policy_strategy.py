from __future__ import annotations

from ..migration.policy_strategy import TableBasedPolicyStrategy
from ..uc_gateway.models import MatchColumn, TableRef


def test_row_filter_policy_name_is_deterministic_across_calls():
    strategy = TableBasedPolicyStrategy()
    table = TableRef("cat", "sch", "t1")
    mc = MatchColumn(tag_key="k", tag_value="v", alias="mc_business_unit", source_column="business_unit")

    spec1 = strategy.plan_row_filter_policy(table, "cat.sch.fn", [mc])
    spec2 = strategy.plan_row_filter_policy(table, "cat.sch.fn", [mc])

    assert spec1.policy_name == spec2.policy_name == "abac_migrated_row_filter"


def test_mask_policy_name_is_deterministic_per_column():
    strategy = TableBasedPolicyStrategy()
    assert strategy.mask_policy_name("email") == "abac_migrated_mask_email"
    assert strategy.mask_policy_name("email") == strategy.mask_policy_name("email")
    assert strategy.mask_policy_name("email") != strategy.mask_policy_name("phone")


def test_single_arg_mask_omits_using_columns():
    strategy = TableBasedPolicyStrategy()
    table = TableRef("cat", "sch", "t1")
    mc = MatchColumn(tag_key="k", tag_value="v", alias="mc_email", source_column="email")

    spec = strategy.plan_column_mask_policy(table, "email", "cat.sch.mask_fn", mc)

    assert spec.using_columns == []
    assert spec.mask_target_alias == "mc_email"


def test_except_principals_defaults_to_empty_for_both_policy_types():
    strategy = TableBasedPolicyStrategy()
    table = TableRef("cat", "sch", "t1")
    mc = MatchColumn(tag_key="k", tag_value="v", alias="mc_email", source_column="email")

    rf_spec = strategy.plan_row_filter_policy(table, "cat.sch.fn", [mc])
    mask_spec = strategy.plan_column_mask_policy(table, "email", "cat.sch.mask_fn", mc)

    assert rf_spec.except_principals == []
    assert mask_spec.except_principals == []


def test_except_principals_propagates_to_both_policy_types():
    strategy = TableBasedPolicyStrategy(except_principals=["etl_service_principal", "break_glass_admins"])
    table = TableRef("cat", "sch", "t1")
    mc = MatchColumn(tag_key="k", tag_value="v", alias="mc_email", source_column="email")

    rf_spec = strategy.plan_row_filter_policy(table, "cat.sch.fn", [mc])
    mask_spec = strategy.plan_column_mask_policy(table, "email", "cat.sch.mask_fn", mc)

    assert rf_spec.except_principals == ["etl_service_principal", "break_glass_admins"]
    assert mask_spec.except_principals == ["etl_service_principal", "break_glass_admins"]


def test_except_principals_is_independent_per_strategy_instance():
    # Guards against the NamedTuple mutable-default-shared-across-instances
    # footgun this feature was added alongside a fix for (PolicySpec's
    # `to_principals` default was previously an unevaluated dataclasses.Field
    # object) - two strategies with different except_principals must never
    # see each other's list.
    strategy_a = TableBasedPolicyStrategy(except_principals=["service_a"])
    strategy_b = TableBasedPolicyStrategy(except_principals=["service_b"])
    table = TableRef("cat", "sch", "t1")
    mc = MatchColumn(tag_key="k", tag_value="v", alias="mc_email", source_column="email")

    spec_a = strategy_a.plan_row_filter_policy(table, "cat.sch.fn", [mc])
    spec_b = strategy_b.plan_row_filter_policy(table, "cat.sch.fn", [mc])

    assert spec_a.except_principals == ["service_a"]
    assert spec_b.except_principals == ["service_b"]
