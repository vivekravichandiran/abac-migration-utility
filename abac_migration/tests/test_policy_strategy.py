from __future__ import annotations

from ..migration.policy_strategy import CatalogBasedPolicyStrategy, TableBasedPolicyStrategy
from ..migration.tag_provisioner import tag_key_for_function
from ..uc_gateway.models import MatchColumn, PolicyDefinition, TableRef
from .fake_gateway import FakeUnityCatalogGateway


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


# -- "Table level application" vs "Catalog level application" scope (§7.3) --

def test_table_based_on_securable_is_table_scoped():
    strategy = TableBasedPolicyStrategy()
    table = TableRef("cat", "sch", "t1")
    assert strategy.on_securable_for(table) == "TABLE `cat`.`sch`.`t1`"


def test_catalog_based_on_securable_is_catalog_scoped():
    strategy = CatalogBasedPolicyStrategy()
    table = TableRef("cat", "sch", "t1")
    assert strategy.on_securable_for(table) == "CATALOG `cat`"


def test_catalog_based_row_filter_policy_name_matches_governed_tag_key():
    # §7.3: CatalogBasedPolicyStrategy reuses tag_provisioner's deterministic
    # tag key verbatim as the policy name - no second naming scheme.
    strategy = CatalogBasedPolicyStrategy()
    fn = "cat.sch.rf_region_both"
    assert strategy.row_filter_policy_name(fn) == tag_key_for_function(fn, "row_filter")
    assert strategy.row_filter_policy_name(fn) == "abac_rls_cat_sch_rf_region_both"


def test_catalog_based_mask_policy_name_ignores_column_keys_by_function():
    strategy = CatalogBasedPolicyStrategy()
    fn = "cat.sch.mask_ssn"
    # Two different masked columns, same function -> identical policy name
    # (one CATALOG-scoped policy shared by every column that function masks).
    assert strategy.mask_policy_name("ssn", fn) == strategy.mask_policy_name("national_id", fn)
    assert strategy.mask_policy_name("ssn", fn) == tag_key_for_function(fn, "mask")


def test_catalog_based_plan_row_filter_policy_targets_catalog():
    strategy = CatalogBasedPolicyStrategy()
    table = TableRef("cat", "sch", "t1")
    mc = MatchColumn(tag_key="abac_rls_cat_sch_fn", tag_value=None, alias="mc_region", source_column="region")

    spec = strategy.plan_row_filter_policy(table, "cat.sch.fn", [mc])

    assert spec.on_securable == "CATALOG `cat`"
    assert spec.policy_name == "abac_rls_cat_sch_fn"
    assert spec.policy_type == "ROW_FILTER"


def test_catalog_based_plan_column_mask_policy_targets_catalog():
    strategy = CatalogBasedPolicyStrategy()
    table = TableRef("cat", "sch", "t1")
    mc = MatchColumn(tag_key="abac_colmask_cat_sch_mask_fn", tag_value=None, alias="mc_ssn", source_column="ssn")

    spec = strategy.plan_column_mask_policy(table, "ssn", "cat.sch.mask_fn", mc)

    assert spec.on_securable == "CATALOG `cat`"
    assert spec.policy_name == "abac_colmask_cat_sch_mask_fn"
    assert spec.policy_type == "COLUMN_MASK"
    assert spec.mask_target_alias == "mc_ssn"


def test_catalog_based_find_existing_row_filter_policy_none_when_no_tags():
    strategy = CatalogBasedPolicyStrategy()
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    uc.register_table(table)

    assert strategy.find_existing_row_filter_policy(table, uc) is None


def test_catalog_based_find_existing_row_filter_policy_recovers_via_column_tag():
    # Simulates the state AFTER Mode.FINALIZE removed the legacy row filter:
    # nothing left in describe_table_security(), only the governed column
    # tag + the CATALOG-scoped policy remain to recover the function from.
    strategy = CatalogBasedPolicyStrategy()
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    uc.register_table(table)
    tag_key = tag_key_for_function("cat.sch.rf_fn", "row_filter")
    uc.add_column_tag(table, "region", tag_key, None)
    uc.add_existing_catalog_policy("cat", PolicyDefinition(
        name=tag_key, policy_type="ROW_FILTER", on_securable_type="CATALOG", on_securable="cat",
        to_principals=["account users"], match_columns=["mc_region"],
        function_fqn="cat.sch.rf_fn", using_columns=["region"],
    ))

    found = strategy.find_existing_row_filter_policy(table, uc)

    assert found is not None
    assert found.function_fqn == "cat.sch.rf_fn"


def test_catalog_based_find_existing_mask_policies_recovers_per_column():
    strategy = CatalogBasedPolicyStrategy()
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    uc.register_table(table)
    tag_key = tag_key_for_function("cat.sch.mask_fn", "mask")
    # Same masking function tags two different columns of this table.
    uc.add_column_tag(table, "ssn", tag_key, "val_a")
    uc.add_column_tag(table, "national_id", tag_key, "val_b")
    uc.add_existing_catalog_policy("cat", PolicyDefinition(
        name=tag_key, policy_type="COLUMN_MASK", on_securable_type="CATALOG", on_securable="cat",
        to_principals=["account users"], match_columns=["mc_ssn", "mc_national_id"],
        function_fqn="cat.sch.mask_fn", using_columns=[],
    ))

    found = strategy.find_existing_mask_policies(table, uc)

    assert {f.column for f in found} == {"ssn", "national_id"}
    assert all(f.policy_def.function_fqn == "cat.sch.mask_fn" for f in found)


def test_catalog_based_find_existing_ignores_unrelated_tags():
    strategy = CatalogBasedPolicyStrategy()
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    uc.register_table(table)
    uc.add_column_tag(table, "region", "some_unrelated_tag", "x")

    assert strategy.find_existing_row_filter_policy(table, uc) is None
    assert strategy.find_existing_mask_policies(table, uc) == []


# ---------------------------------------------------------------------------
# tag_team_prefix (RunConfig.tag_team_prefix): namespaces BOTH the governed
# tag keys TagProvisioner mints AND every policy name this strategy plans -
# for TABLE scope, the previously-constant abac_migrated_row_filter/
# abac_migrated_mask_<column> now become abac_<team_prefix>_migrated_*; for
# CATALOG scope this falls straight out of reusing tag_key_for_function
# (already team_prefix-aware) verbatim as the policy name.
# ---------------------------------------------------------------------------

def test_table_based_team_prefix_defaults_to_empty_and_is_unchanged():
    strategy = TableBasedPolicyStrategy()
    assert strategy.team_prefix == ""
    assert strategy.row_filter_policy_name("cat.sch.fn") == "abac_migrated_row_filter"
    assert strategy.mask_policy_name("email") == "abac_migrated_mask_email"


def test_table_based_team_prefix_namespaces_row_filter_policy_name():
    strategy = TableBasedPolicyStrategy(team_prefix="mobility")
    assert strategy.row_filter_policy_name("cat.sch.fn") == "abac_mobility_migrated_row_filter"


def test_table_based_team_prefix_namespaces_mask_policy_name():
    strategy = TableBasedPolicyStrategy(team_prefix="mobility")
    assert strategy.mask_policy_name("email") == "abac_mobility_migrated_mask_email"


def test_table_based_team_prefix_is_sanitized_like_tag_keys():
    # Hyphens (a confirmed-live real case for catalog names, e.g. jh-demo)
    # must sanitize identically to tag_key_for_function's own sanitization -
    # both go through the same shared sanitize_name_part().
    strategy = TableBasedPolicyStrategy(team_prefix="team-mobility")
    assert strategy.row_filter_policy_name("cat.sch.fn") == "abac_team_mobility_migrated_row_filter"


def test_table_based_team_prefix_plan_methods_use_namespaced_names():
    strategy = TableBasedPolicyStrategy(team_prefix="mobility")
    table = TableRef("cat", "sch", "t1")
    mc = MatchColumn(tag_key="k", tag_value="v", alias="mc_email", source_column="email")

    rf_spec = strategy.plan_row_filter_policy(table, "cat.sch.fn", [mc])
    mask_spec = strategy.plan_column_mask_policy(table, "email", "cat.sch.mask_fn", mc)

    assert rf_spec.policy_name == "abac_mobility_migrated_row_filter"
    assert mask_spec.policy_name == "abac_mobility_migrated_mask_email"


def test_table_based_find_existing_row_filter_policy_respects_team_prefix():
    strategy = TableBasedPolicyStrategy(team_prefix="mobility")
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    uc.add_existing_policy(table, PolicyDefinition(
        name="abac_mobility_migrated_row_filter", policy_type="ROW_FILTER", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_region"],
        function_fqn="cat.sch.rf_fn", using_columns=["region"],
    ))
    # A no-prefix (or different-prefix) policy of the same generic kind must
    # NOT be found by a differently-prefixed strategy - prefixes are meant
    # to be mutually exclusive namespaces, not one shared fallback pool.
    other_strategy = TableBasedPolicyStrategy()

    found = strategy.find_existing_row_filter_policy(table, uc)
    not_found = other_strategy.find_existing_row_filter_policy(table, uc)

    assert found is not None and found.function_fqn == "cat.sch.rf_fn"
    assert not_found is None


def test_table_based_find_existing_mask_policies_respects_team_prefix():
    strategy = TableBasedPolicyStrategy(team_prefix="mobility")
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    uc.add_existing_policy(table, PolicyDefinition(
        name="abac_mobility_migrated_mask_ssn", policy_type="COLUMN_MASK", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_ssn"],
        function_fqn="cat.sch.mask_fn", using_columns=[],
    ))

    found = strategy.find_existing_mask_policies(table, uc)

    assert len(found) == 1
    assert found[0].column == "ssn"
    assert found[0].policy_def.function_fqn == "cat.sch.mask_fn"


def test_catalog_based_team_prefix_namespaces_tag_key_and_policy_name_identically():
    strategy = CatalogBasedPolicyStrategy(team_prefix="mobility")
    fn = "cat.sch.rf_region_both"
    assert strategy.row_filter_policy_name(fn) == tag_key_for_function(fn, "row_filter", "mobility")
    assert strategy.row_filter_policy_name(fn) == "abac_rls_mobility_cat_sch_rf_region_both"


def test_catalog_based_team_prefix_empty_matches_no_prefix_default():
    strategy = CatalogBasedPolicyStrategy(team_prefix="")
    fn = "cat.sch.rf_region_both"
    assert strategy.row_filter_policy_name(fn) == "abac_rls_cat_sch_rf_region_both"


def test_catalog_based_find_existing_row_filter_policy_ignores_a_different_teams_tag():
    # Confirmed-live bug this guards against: re-running APPLY_ABAC with a
    # NEW team_prefix against a catalog a prior no-prefix (or
    # differently-prefixed) run already migrated must NOT treat that
    # leftover tag/policy as "already migrated" just because it also
    # starts with the bare abac_rls_ role prefix.
    other_team_strategy = CatalogBasedPolicyStrategy()  # no prefix - created the tag/policy below
    my_strategy = CatalogBasedPolicyStrategy(team_prefix="mobility")
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    uc.register_table(table)
    other_team_tag_key = tag_key_for_function("cat.sch.rf_fn", "row_filter")  # abac_rls_cat_sch_rf_fn, no prefix
    uc.add_column_tag(table, "region", other_team_tag_key, None)
    uc.add_existing_catalog_policy("cat", PolicyDefinition(
        name=other_team_tag_key, policy_type="ROW_FILTER", on_securable_type="CATALOG", on_securable="cat",
        to_principals=["account users"], match_columns=["mc_region"],
        function_fqn="cat.sch.rf_fn", using_columns=["region"],
    ))

    # The tag/policy IS visible to a same-namespace (no-prefix) strategy...
    assert other_team_strategy.find_existing_row_filter_policy(table, uc) is not None
    # ...but must be INVISIBLE to a differently-prefixed ("mobility") one -
    # from mobility's point of view this table has never been touched.
    assert my_strategy.find_existing_row_filter_policy(table, uc) is None


def test_catalog_based_find_existing_mask_policies_ignores_a_different_teams_tag():
    other_team_strategy = CatalogBasedPolicyStrategy()
    my_strategy = CatalogBasedPolicyStrategy(team_prefix="mobility")
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    uc.register_table(table)
    other_team_tag_key = tag_key_for_function("cat.sch.mask_fn", "mask")
    uc.add_column_tag(table, "ssn", other_team_tag_key, "val_a")
    uc.add_existing_catalog_policy("cat", PolicyDefinition(
        name=other_team_tag_key, policy_type="COLUMN_MASK", on_securable_type="CATALOG", on_securable="cat",
        to_principals=["account users"], match_columns=["mc_ssn"],
        function_fqn="cat.sch.mask_fn", using_columns=[],
    ))

    assert len(other_team_strategy.find_existing_mask_policies(table, uc)) == 1
    assert my_strategy.find_existing_mask_policies(table, uc) == []


def test_catalog_based_find_existing_row_filter_policy_finds_own_teams_tag_only():
    strategy = CatalogBasedPolicyStrategy(team_prefix="mobility")
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    uc.register_table(table)
    my_tag_key = tag_key_for_function("cat.sch.rf_fn", "row_filter", "mobility")
    uc.add_column_tag(table, "region", my_tag_key, None)
    uc.add_existing_catalog_policy("cat", PolicyDefinition(
        name=my_tag_key, policy_type="ROW_FILTER", on_securable_type="CATALOG", on_securable="cat",
        to_principals=["account users"], match_columns=["mc_region"],
        function_fqn="cat.sch.rf_fn", using_columns=["region"],
    ))

    found = strategy.find_existing_row_filter_policy(table, uc)

    assert found is not None
    assert found.function_fqn == "cat.sch.rf_fn"
