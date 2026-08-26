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
