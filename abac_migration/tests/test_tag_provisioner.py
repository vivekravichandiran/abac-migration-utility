from __future__ import annotations

import pytest

from ..migration.tag_provisioner import (
    SYNTHETIC_TAG_DESCRIPTION_TEMPLATE,
    TagKeyCollisionError,
    TagProvisioner,
    TagRequest,
    _short_function_name,
    _tag_key_for_function,
)
from ..uc_gateway.models import TableRef
from .fake_gateway import FakeUnityCatalogGateway

RF_FN = "cat.sch.rf_business_unit_fn"
MASK_FN = "cat.sch.mask_email_fn"
RF_TAG_KEY = _tag_key_for_function(RF_FN, "row_filter")
MASK_TAG_KEY = _tag_key_for_function(MASK_FN, "mask")


def test_tag_key_is_derived_per_function_not_shared():
    other_rf_fn = "cat.sch.rf_region_fn"
    key_a = _tag_key_for_function(RF_FN, "row_filter")
    key_b = _tag_key_for_function(other_rf_fn, "row_filter")
    assert key_a != key_b  # one governed tag KEY per function, not one shared key
    assert key_a == RF_TAG_KEY  # deterministic/stable across calls


def test_tag_key_includes_catalog_and_schema_with_no_hash():
    # cat.sch.rf_region_both -> abac_rls_cat_sch_rf_region_both - fully
    # qualified, deterministic, and with NO hash/digest suffix anywhere.
    key = _tag_key_for_function("cat.sch.rf_region_both", "row_filter")
    assert key == "abac_rls_cat_sch_rf_region_both"

    mask_key = _tag_key_for_function("`some_catalog`.`some_schema`.`mask_email_fn`", "mask")
    assert mask_key == "abac_colmask_some_catalog_some_schema_mask_email_fn"


def test_tag_key_replaces_hyphens_with_underscores_in_catalog_and_schema():
    # Confirmed-live real case: catalog/schema names may contain hyphens
    # (e.g. `jh-demo`), which must become `_`, not be dropped or left as-is
    # (governed tag keys don't allow hyphens).
    key = _tag_key_for_function("jh-demo.some-schema.rf_region_both", "row_filter")
    assert key == "abac_rls_jh_demo_some_schema_rf_region_both"
    assert "-" not in key


def test_short_function_name_strips_qualification_and_backticks():
    assert _short_function_name("cat.sch.rf_region_both") == "rf_region_both"
    assert _short_function_name("`cat`.`sch`.`rf_region_both`") == "rf_region_both"
    assert _short_function_name("rf_region_both") == "rf_region_both"  # already unqualified


def test_mints_synthetic_tag_when_none_exists():
    # Single column, single table, no collision risk - a plain KEY-ONLY tag
    # (no allowed values) is minted and has_tag(key) is sufficient, so no
    # value should be assigned at all.
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare(
        [TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)], dry_run=False,
    )

    mc = resolved[(table, "business_unit", "row_filter")]
    assert mc.tag_key == RF_TAG_KEY
    assert mc.tag_value is None
    assert fake.governed_tags[RF_TAG_KEY].values == []
    assert any(
        t.column == "business_unit" and t.tag_key == mc.tag_key and t.tag_value is None
        for t in fake.column_tags[table.full_name]
    )


def test_two_distinct_functions_mint_two_distinct_tag_keys():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)
    other_fn = "cat.sch.rf_region_fn"

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare([
        TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN),
        TagRequest(table=table, column="region", role="row_filter", function_fqn=other_fn),
    ], dry_run=False)

    key_a = resolved[(table, "business_unit", "row_filter")].tag_key
    key_b = resolved[(table, "region", "row_filter")].tag_key
    assert key_a != key_b
    assert key_a in fake.governed_tags
    assert key_b in fake.governed_tags


def test_reuses_existing_unique_governed_tag_when_preferred():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)
    fake.register_governed_tag("pii_category", values=["email"])
    fake.add_column_tag(table, "email_col", "pii_category", "email")

    provisioner = TagProvisioner(fake, prefer_existing_tags=True)
    resolved = provisioner.prepare(
        [TagRequest(table=table, column="email_col", role="mask", function_fqn=MASK_FN)], dry_run=False,
    )

    mc = resolved[(table, "email_col", "mask")]
    assert mc.tag_key == "pii_category"
    assert mc.tag_value == "email"
    assert MASK_TAG_KEY not in fake.governed_tags  # no synthetic tag minted


def test_does_not_reuse_tag_that_is_not_unique_within_table():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)
    fake.register_governed_tag("pii_category", values=["email"])
    fake.add_column_tag(table, "email_col", "pii_category", "email")
    fake.add_column_tag(table, "backup_email_col", "pii_category", "email")  # same value, different column -> ambiguous

    provisioner = TagProvisioner(fake, prefer_existing_tags=True)
    resolved = provisioner.prepare(
        [TagRequest(table=table, column="email_col", role="mask", function_fqn=MASK_FN)], dry_run=False,
    )

    mc = resolved[(table, "email_col", "mask")]
    assert mc.tag_key == MASK_TAG_KEY  # fell back to minting


def test_single_column_per_table_stays_key_only_even_when_shared_across_tables():
    # RF_FN guards ONE column each in table1 and table2 - no single table
    # ever has 2 columns sharing the key, so has_tag(key) alone is
    # unambiguous in both - both should stay key-only, no values minted at
    # all despite the function being shared across 2 tables.
    fake = FakeUnityCatalogGateway()
    table1 = TableRef("cat", "sch", "t1")
    table2 = TableRef("cat", "sch", "t2")
    fake.register_table(table1)
    fake.register_table(table2)

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare([
        TagRequest(table=table1, column="business_unit", role="row_filter", function_fqn=RF_FN),
        TagRequest(table=table2, column="region", role="row_filter", function_fqn=RF_FN),
    ], dry_run=False)

    assert resolved[(table1, "business_unit", "row_filter")].tag_value is None
    assert resolved[(table2, "region", "row_filter")].tag_value is None
    assert fake.governed_tags[RF_TAG_KEY].values == []


def test_same_function_guarding_two_columns_of_the_same_table_gets_disambiguating_values():
    # A row filter function taking 2 USING COLUMNS from the SAME table is a
    # real same-table collision - has_tag(key) alone would be ambiguous
    # (confirmed live: UC_ABAC_AMBIGUOUS_COLUMN_MATCH at query time), so
    # BOTH columns must get their own unique value under the shared key.
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare([
        TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN),
        TagRequest(table=table, column="region", role="row_filter", function_fqn=RF_FN),
    ], dry_run=False)

    mc_a = resolved[(table, "business_unit", "row_filter")]
    mc_b = resolved[(table, "region", "row_filter")]
    assert mc_a.tag_value is not None and mc_b.tag_value is not None
    assert mc_a.tag_value != mc_b.tag_value  # each column uniquely identified
    assert {mc_a.tag_value, mc_b.tag_value} == set(fake.governed_tags[RF_TAG_KEY].values)


def test_grows_existing_governed_tag_values_instead_of_overwriting():
    fake = FakeUnityCatalogGateway()
    table1 = TableRef("cat", "sch", "t1")  # 2 columns -> real collision -> needs values
    table2 = TableRef("cat", "sch", "t2")  # 1 column -> no collision -> stays key-only
    fake.register_table(table1)
    fake.register_table(table2)
    # Simulates a prior run having already minted this exact key for RF_FN -
    # description matches what this tool itself would have written, so the
    # collision-safety check in _mint_and_assign recognizes it as "same
    # function, safe to extend" rather than raising TagKeyCollisionError.
    fake.register_governed_tag(
        RF_TAG_KEY, values=["preexisting_value"],
        description=SYNTHETIC_TAG_DESCRIPTION_TEMPLATE.format(function_fqn=RF_FN),
    )

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare([
        TagRequest(table=table1, column="business_unit", role="row_filter", function_fqn=RF_FN),
        TagRequest(table=table1, column="region", role="row_filter", function_fqn=RF_FN),
        TagRequest(table=table2, column="dept", role="row_filter", function_fqn=RF_FN),
    ], dry_run=False)

    final_values = set(fake.governed_tags[RF_TAG_KEY].values)
    assert "preexisting_value" in final_values  # old value preserved, not clobbered
    assert resolved[(table1, "business_unit", "row_filter")].tag_value in final_values
    assert resolved[(table1, "region", "row_filter")].tag_value in final_values
    assert len(final_values) == 3  # preexisting + the 2 colliding columns' new values
    assert resolved[(table2, "dept", "row_filter")].tag_value is None  # no collision here


def test_prefer_existing_tags_false_always_mints():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)
    fake.register_governed_tag("pii_category", values=["email"])
    fake.add_column_tag(table, "email_col", "pii_category", "email")

    provisioner = TagProvisioner(fake, prefer_existing_tags=False)
    resolved = provisioner.prepare(
        [TagRequest(table=table, column="email_col", role="mask", function_fqn=MASK_FN)], dry_run=False,
    )

    assert resolved[(table, "email_col", "mask")].tag_key == MASK_TAG_KEY


def test_two_functions_in_different_schemas_get_distinct_keys_without_collision_error():
    # cat1.sch1.rf_region and cat2.sch2.rf_region share a short name but
    # differ in catalog/schema, so the fully-qualified key keeps them
    # naturally distinct - no TagKeyCollisionError, no hash needed.
    fn_a = "cat1.sch1.rf_region"
    fn_b = "cat2.sch2.rf_region"
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare([
        TagRequest(table=table, column="region_a", role="row_filter", function_fqn=fn_a),
        TagRequest(table=table, column="region_b", role="row_filter", function_fqn=fn_b),
    ], dry_run=False)

    key_a = resolved[(table, "region_a", "row_filter")].tag_key
    key_b = resolved[(table, "region_b", "row_filter")].tag_key
    assert key_a == "abac_rls_cat1_sch1_rf_region"
    assert key_b == "abac_rls_cat2_sch2_rf_region"
    assert fake.governed_tags[key_a].description == SYNTHETIC_TAG_DESCRIPTION_TEMPLATE.format(function_fqn=fn_a)
    assert fake.governed_tags[key_b].description == SYNTHETIC_TAG_DESCRIPTION_TEMPLATE.format(function_fqn=fn_b)


def test_same_function_across_two_runs_reuses_same_key():
    fn = "cat.sch.rf_region"
    fake = FakeUnityCatalogGateway()
    table1 = TableRef("cat", "sch", "t1")
    table2 = TableRef("cat", "sch", "t2")
    fake.register_table(table1)
    fake.register_table(table2)

    provisioner = TagProvisioner(fake)
    provisioner.prepare(
        [TagRequest(table=table1, column="region", role="row_filter", function_fqn=fn)], dry_run=False,
    )
    # A second, later "run" against a different table, same function - must
    # land on the exact same deterministic key.
    resolved2 = provisioner.prepare(
        [TagRequest(table=table2, column="region", role="row_filter", function_fqn=fn)], dry_run=False,
    )

    assert resolved2[(table2, "region", "row_filter")].tag_key == "abac_rls_cat_sch_rf_region"
    assert len(fake.governed_tags) == 1  # no spurious duplicate was minted


def test_pre_existing_non_migration_tag_at_the_exact_deterministic_key_raises():
    # A governed tag with the exact deterministic key already exists but
    # was NOT created by this tool for this function (no matching
    # description, e.g. hand-created) - must not be silently
    # hijacked/reused, and since there's no hash-suffixed fallback anymore,
    # this must fail loudly instead.
    fn = "cat.sch.rf_region"
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)
    fake.register_governed_tag(
        "abac_rls_cat_sch_rf_region", values=["hand_made_value"], description="unrelated, hand-created",
    )

    provisioner = TagProvisioner(fake)
    with pytest.raises(TagKeyCollisionError):
        provisioner.prepare(
            [TagRequest(table=table, column="region", role="row_filter", function_fqn=fn)], dry_run=False,
        )


def test_new_column_colliding_with_a_pre_existing_key_only_assignment_gets_a_value():
    # table already has ONE column key-only-tagged with RF_TAG_KEY from a
    # prior run (not reusable for a DIFFERENT column - _find_reusable_tag
    # only reuses a tag already on the SAME column). A second, different
    # column in the SAME table now also needs RF_FN's tag - this must NOT
    # become key-only too (that would silently recreate the exact ambiguity
    # this whole mechanism exists to avoid), so it must get a real value.
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)
    fake.register_governed_tag(
        RF_TAG_KEY, values=[], description=SYNTHETIC_TAG_DESCRIPTION_TEMPLATE.format(function_fqn=RF_FN),
    )
    fake.add_column_tag(table, "business_unit", RF_TAG_KEY, None)  # pre-existing key-only assignment

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare(
        [TagRequest(table=table, column="region", role="row_filter", function_fqn=RF_FN)], dry_run=False,
    )

    mc = resolved[(table, "region", "row_filter")]
    assert mc.tag_key == RF_TAG_KEY
    assert mc.tag_value is not None
    assert mc.tag_value in fake.governed_tags[RF_TAG_KEY].values


def test_long_or_unusual_function_name_produces_valid_truncated_key_with_no_hash():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)
    weird_fn = "cat.sch." + ("very_long_function_name_" * 10)

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare(
        [TagRequest(table=table, column="col1", role="mask", function_fqn=weird_fn)], dry_run=False,
    )

    tag_key = resolved[(table, "col1", "mask")].tag_key
    assert len(tag_key) < 220
    assert tag_key in fake.governed_tags
    assert tag_key.startswith("abac_colmask_cat_sch_very_long_function_name_")
