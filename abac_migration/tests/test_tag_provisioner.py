from __future__ import annotations

import pytest

from ..migration.tag_provisioner import (
    SYNTHETIC_TAG_DESCRIPTION_TEMPLATE,
    TAG_GRANT_ROLE_BY_NAME,
    TagKeyCollisionError,
    TagProvisioner,
    TagRequest,
    _short_function_name,
    normalize_principal,
    tag_key_for_function,
)
from ..uc_gateway.models import TableRef
from .fake_gateway import FakeUnityCatalogGateway

RF_FN = "cat.sch.rf_business_unit_fn"
MASK_FN = "cat.sch.mask_email_fn"
RF_TAG_KEY = tag_key_for_function(RF_FN, "row_filter")
MASK_TAG_KEY = tag_key_for_function(MASK_FN, "mask")


def test_tag_key_is_derived_per_function_not_shared():
    other_rf_fn = "cat.sch.rf_region_fn"
    key_a = tag_key_for_function(RF_FN, "row_filter")
    key_b = tag_key_for_function(other_rf_fn, "row_filter")
    assert key_a != key_b  # one governed tag KEY per function, not one shared key
    assert key_a == RF_TAG_KEY  # deterministic/stable across calls


def test_tag_key_includes_catalog_and_schema_with_no_hash():
    # cat.sch.rf_region_both -> abac_rls_cat_sch_rf_region_both - fully
    # qualified, deterministic, and with NO hash/digest suffix anywhere.
    key = tag_key_for_function("cat.sch.rf_region_both", "row_filter")
    assert key == "abac_rls_cat_sch_rf_region_both"

    mask_key = tag_key_for_function("`some_catalog`.`some_schema`.`mask_email_fn`", "mask")
    assert mask_key == "abac_colmask_some_catalog_some_schema_mask_email_fn"


def test_tag_key_replaces_hyphens_with_underscores_in_catalog_and_schema():
    # Confirmed-live real case: catalog/schema names may contain hyphens
    # (e.g. `jh-demo`), which must become `_`, not be dropped or left as-is
    # (governed tag keys don't allow hyphens).
    key = tag_key_for_function("jh-demo.some-schema.rf_region_both", "row_filter")
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


def test_same_function_guarding_two_columns_of_the_same_table_skips_both_for_row_filter():
    # A row filter function taking 2 USING COLUMNS from the SAME table is a
    # real same-table collision - has_tag(key) alone would be ambiguous
    # (confirmed live: UC_ABAC_AMBIGUOUS_COLUMN_MATCH at query time). No
    # value is minted to disambiguate anymore (removed by design) - both
    # columns are simply left unresolved so the caller (rls_to_abac.py)
    # fails only this table's ROW_FILTER step instead of crashing the run.
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake)
    req_a = TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)
    req_b = TagRequest(table=table, column="region", role="row_filter", function_fqn=RF_FN)
    resolved = provisioner.prepare([req_a, req_b], dry_run=False)

    assert (table, "business_unit", "row_filter") not in resolved
    assert (table, "region", "row_filter") not in resolved
    # No column ever got the tag assigned, and the tag itself is never
    # minted at all when every request for it collides.
    assert RF_TAG_KEY not in fake.governed_tags
    assert fake.column_tags.get(table.full_name, []) == []
    assert {req_a, req_b} == set(provisioner.last_row_filter_collisions)


def test_same_function_guarding_two_columns_of_the_same_table_stays_key_only_for_masks():
    # The exact same shape of collision as above, but role="mask" - masks
    # are confirmed safe to share one bare key-only tag across multiple
    # columns of one table, so both should resolve normally with NO value.
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare([
        TagRequest(table=table, column="ssn", role="mask", function_fqn=MASK_FN),
        TagRequest(table=table, column="email", role="mask", function_fqn=MASK_FN),
    ], dry_run=False)

    mc_a = resolved[(table, "ssn", "mask")]
    mc_b = resolved[(table, "email", "mask")]
    assert mc_a.tag_key == MASK_TAG_KEY and mc_b.tag_key == MASK_TAG_KEY
    assert mc_a.tag_value is None and mc_b.tag_value is None
    assert fake.governed_tags[MASK_TAG_KEY].values == []  # never gets any allowed values
    assert not provisioner.last_row_filter_collisions


def test_row_filter_collision_with_other_tables_does_not_affect_them():
    # table1 has the real collision (2 columns); table2's single column,
    # same function, is completely unaffected and still resolves key-only.
    fake = FakeUnityCatalogGateway()
    table1 = TableRef("cat", "sch", "t1")
    table2 = TableRef("cat", "sch", "t2")
    fake.register_table(table1)
    fake.register_table(table2)

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare([
        TagRequest(table=table1, column="business_unit", role="row_filter", function_fqn=RF_FN),
        TagRequest(table=table1, column="region", role="row_filter", function_fqn=RF_FN),
        TagRequest(table=table2, column="dept", role="row_filter", function_fqn=RF_FN),
    ], dry_run=False)

    assert (table1, "business_unit", "row_filter") not in resolved
    assert (table1, "region", "row_filter") not in resolved
    mc = resolved[(table2, "dept", "row_filter")]
    assert mc.tag_key == RF_TAG_KEY
    assert mc.tag_value is None
    assert fake.governed_tags[RF_TAG_KEY].values == []  # still no values, ever


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


def test_new_column_colliding_with_a_pre_existing_key_only_assignment_is_skipped():
    # table already has ONE column key-only-tagged with RF_TAG_KEY from a
    # prior run (not reusable for a DIFFERENT column - _find_reusable_tag
    # only reuses a tag already on the SAME column). A second, different
    # column in the SAME table now also needs RF_FN's tag - this must NOT
    # become key-only too (that would recreate the exact ambiguity this
    # whole mechanism exists to avoid), and no value is minted to
    # disambiguate anymore, so it must simply be skipped/left unresolved.
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)
    fake.register_governed_tag(
        RF_TAG_KEY, values=[], description=SYNTHETIC_TAG_DESCRIPTION_TEMPLATE.format(function_fqn=RF_FN),
    )
    fake.add_column_tag(table, "business_unit", RF_TAG_KEY, None)  # pre-existing key-only assignment

    provisioner = TagProvisioner(fake)
    req = TagRequest(table=table, column="region", role="row_filter", function_fqn=RF_FN)
    resolved = provisioner.prepare([req], dry_run=False)

    assert (table, "region", "row_filter") not in resolved
    assert fake.governed_tags[RF_TAG_KEY].values == []  # still no values, ever
    assert provisioner.last_row_filter_collisions == [req]


# ---------------------------------------------------------------------------
# team_prefix (RunConfig.tag_team_prefix): optional namespace segment right
# after the abac_rls_/abac_colmask_ role prefix, for multi-team migrations
# against the same metastore. Omitted entirely when empty (default,
# unchanged behavior - already covered by every test above using the
# 2-arg tag_key_for_function(fn, role) call form).
# ---------------------------------------------------------------------------

def test_tag_key_for_function_with_team_prefix_inserts_segment_after_role():
    key = tag_key_for_function("cat.sch.rf_region_both", "row_filter", "mobility")
    assert key == "abac_rls_mobility_cat_sch_rf_region_both"


def test_tag_key_for_function_empty_team_prefix_matches_no_prefix_default():
    assert tag_key_for_function(RF_FN, "row_filter", "") == RF_TAG_KEY
    assert tag_key_for_function(RF_FN, "row_filter") == RF_TAG_KEY


def test_tag_key_for_function_team_prefix_is_sanitized():
    # Same sanitization rule as catalog/schema/function-name (hyphens etc.
    # replaced with `_`, no exceptions for team_prefix).
    key = tag_key_for_function("cat.sch.rf_region_both", "row_filter", "team-mobility")
    assert key == "abac_rls_team_mobility_cat_sch_rf_region_both"
    assert "-" not in key


def test_tag_key_for_function_different_team_prefixes_are_distinct_keys():
    key_a = tag_key_for_function(RF_FN, "row_filter", "mobility")
    key_b = tag_key_for_function(RF_FN, "row_filter", "payments")
    assert key_a != key_b
    assert key_a != RF_TAG_KEY
    assert key_b != RF_TAG_KEY


def test_provisioner_team_prefix_mints_namespaced_tag_key():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake, team_prefix="mobility")
    resolved = provisioner.prepare(
        [TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)], dry_run=False,
    )

    mc = resolved[(table, "business_unit", "row_filter")]
    expected_key = tag_key_for_function(RF_FN, "row_filter", "mobility")
    assert mc.tag_key == expected_key
    assert expected_key in fake.governed_tags
    assert RF_TAG_KEY not in fake.governed_tags  # no-prefix key never minted


def test_provisioner_default_team_prefix_is_empty_and_unchanged():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare(
        [TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)], dry_run=False,
    )

    assert resolved[(table, "business_unit", "row_filter")].tag_key == RF_TAG_KEY


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


# ---------------------------------------------------------------------------
# tag_grantee_principals / tag_grantee_role (§7.4 point 6): grants a
# configured account role to a list of principals on every governed tag a
# `prepare()` call touches - newly minted OR reused. Not SQL (see
# uc_gateway/access_control_client.py) - the fake gateway's
# `grant_tag_principals()` is the seam every test below exercises.
# ---------------------------------------------------------------------------

SPN_1 = "b2dbcc98-7d9f-467d-a7b1-e8a026f94b73"
SPN_2 = "91f9bc02-aab4-4f58-b253-b98b3c676428"


def test_normalize_principal_prefixes_bare_spn_uuid():
    assert normalize_principal(SPN_1) == f"servicePrincipals/{SPN_1}"


def test_normalize_principal_passes_through_already_qualified_strings():
    assert normalize_principal(f"servicePrincipals/{SPN_1}") == f"servicePrincipals/{SPN_1}"
    assert normalize_principal("groups/data-platform") == "groups/data-platform"
    assert normalize_principal("users/someone@example.com") == "users/someone@example.com"


def test_normalize_principal_passes_through_non_uuid_non_slash_strings_unchanged():
    # Doesn't look like a UUID and has no "/" - passed through as-is rather
    # than guessed at (e.g. a malformed config value surfaces downstream,
    # at the real API, as a clean not-found error - not silently mangled here).
    assert normalize_principal("not-a-uuid") == "not-a-uuid"


def test_no_grantee_principals_configured_means_zero_grant_calls():
    # Default/empty tag_grantee_principals: feature fully off, no behavior
    # change whatsoever vs. every test above this section.
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake)
    resolved = provisioner.prepare(
        [TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)], dry_run=False,
    )

    assert resolved  # sanity: the tag was still minted
    assert fake.grant_tag_principals_calls == []
    assert provisioner.last_tag_grants == []


def test_configured_grantee_principals_get_granted_on_newly_minted_tag():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake, tag_grantee_principals=[SPN_1], tag_grantee_role="ASSIGN")
    provisioner.prepare(
        [TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)], dry_run=False,
    )

    assert fake.grant_tag_principals_calls == [
        (RF_TAG_KEY, (f"servicePrincipals/{SPN_1}",), "roles/tagPolicy.assigner", False),
    ]
    assert len(provisioner.last_tag_grants) == 1
    assert provisioner.last_tag_grants[0].tag_key == RF_TAG_KEY
    assert provisioner.last_tag_grants[0].status == "GRANTED"


def test_manage_role_resolves_to_tag_policy_manager():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake, tag_grantee_principals=[SPN_1], tag_grantee_role="manage")  # lower-case input
    provisioner.prepare(
        [TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)], dry_run=False,
    )

    assert fake.grant_tag_principals_calls[0][2] == "roles/tagPolicy.manager"
    assert TAG_GRANT_ROLE_BY_NAME["MANAGE"] == "roles/tagPolicy.manager"


def test_reused_tag_also_gets_re_granted_every_call_self_healing():
    # A tag minted by an earlier prepare() call (before tag_grantee_principals
    # was configured, or with a different grantee) must still get granted
    # once it's configured - reuse is not exempt.
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)
    fake.register_governed_tag(RF_TAG_KEY)
    fake.add_column_tag(table, "business_unit", RF_TAG_KEY, None)

    provisioner = TagProvisioner(fake, tag_grantee_principals=[SPN_1])
    resolved = provisioner.prepare(
        [TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)], dry_run=False,
    )

    assert resolved[(table, "business_unit", "row_filter")].tag_key == RF_TAG_KEY
    assert fake.grant_tag_principals_calls == [
        (RF_TAG_KEY, (f"servicePrincipals/{SPN_1}",), "roles/tagPolicy.assigner", False),
    ]


def test_multiple_columns_sharing_one_tag_key_grant_once_not_per_column():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake, tag_grantee_principals=[SPN_1])
    provisioner.prepare(
        [
            TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN),
            TagRequest(table=TableRef("cat", "sch", "t2"), column="business_unit", role="row_filter", function_fqn=RF_FN),
        ],
        dry_run=False,
    )

    # Both columns resolve to the SAME tag_key (same function) - exactly one
    # grant call, not two.
    assert len(fake.grant_tag_principals_calls) == 1
    assert len(provisioner.last_tag_grants) == 1


def test_distinct_tag_keys_each_get_their_own_grant_call():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake, tag_grantee_principals=[SPN_1])
    provisioner.prepare(
        [
            TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN),
            TagRequest(table=table, column="email", role="mask", function_fqn=MASK_FN),
        ],
        dry_run=False,
    )

    granted_keys = {call[0] for call in fake.grant_tag_principals_calls}
    assert granted_keys == {RF_TAG_KEY, MASK_TAG_KEY}
    assert len(provisioner.last_tag_grants) == 2


def test_multiple_principals_all_passed_in_one_grant_call():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake, tag_grantee_principals=[SPN_1, SPN_2, "groups/data-platform"])
    provisioner.prepare(
        [TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)], dry_run=False,
    )

    call = fake.grant_tag_principals_calls[0]
    assert call[1] == (f"servicePrincipals/{SPN_1}", f"servicePrincipals/{SPN_2}", "groups/data-platform")


def test_dry_run_still_reports_would_grant_without_mutating_fake_state():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake, tag_grantee_principals=[SPN_1])
    provisioner.prepare(
        [TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)], dry_run=True,
    )

    assert provisioner.last_tag_grants[0].status == "WOULD_GRANT"
    assert fake.tag_grants == {}  # dry_run: fake's grant call short-circuits before mutating state


def test_grant_failure_is_non_fatal_and_recorded_as_failed():
    # A grant failure (e.g. real API 400 "ServicePrincipal not found") must
    # never abort resolution of the tag itself - resolved still contains the
    # entry, just with a FAILED TagGrantResult alongside it.
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)
    fake.fail_next_grant_tag_principals("SPN_NOT_FOUND", "ServicePrincipal not found")

    provisioner = TagProvisioner(fake, tag_grantee_principals=[SPN_1])
    resolved = provisioner.prepare(
        [TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)], dry_run=False,
    )

    assert resolved[(table, "business_unit", "row_filter")].tag_key == RF_TAG_KEY  # tag resolution unaffected
    assert provisioner.last_tag_grants[0].status == "FAILED"
    assert provisioner.last_tag_grants[0].error_code == "SPN_NOT_FOUND"


def test_last_tag_grants_reset_on_every_prepare_call_not_accumulated():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "t1")
    fake.register_table(table)

    provisioner = TagProvisioner(fake, tag_grantee_principals=[SPN_1])
    provisioner.prepare(
        [TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=RF_FN)], dry_run=False,
    )
    assert len(provisioner.last_tag_grants) == 1

    provisioner.prepare([], dry_run=False)  # empty request list - short-circuits before the reset even matters
    assert provisioner.last_tag_grants == []
