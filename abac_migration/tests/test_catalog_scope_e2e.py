"""End-to-end coverage for "catalog level application" (§7.3,
`CatalogBasedPolicyStrategy`) through `table_converter.convert_table()` -
mirrors the phased-mode scenarios already covered for "table level
application" in test_table_converter.py, plus the scope-specific behavior
that only shows up once more than one table shares a function's policy:
a single CATALOG-scoped policy object covering N tables, and idempotent
rerun/VERIFY once the legacy artifact (which used to reveal the function
name) is gone after FINALIZE.
"""
from __future__ import annotations

from ..migration.plugins.base_plugin import StepStatus
from ..migration.policy_strategy import CatalogBasedPolicyStrategy
from ..migration.table_converter import convert_table
from ..migration.tag_provisioner import tag_key_for_function
from ..uc_gateway.models import TableRef
from ..validation.post_validation import verify_table
from .fake_gateway import FakeUnityCatalogGateway

RF_FN = "cat.sch.rf_business_unit"
MASK_FN = "cat.sch.mask_email"


def _strategy() -> CatalogBasedPolicyStrategy:
    return CatalogBasedPolicyStrategy()


def test_apply_abac_creates_catalog_scoped_policy_not_table_scoped():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "orders")
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    strategy = _strategy()

    result = convert_table(table, fake, dry_run=False, policy_strategy=strategy, phase="APPLY_ABAC")

    assert result.status == StepStatus.ABAC_APPLIED
    tag_key = tag_key_for_function(RF_FN, "row_filter")
    # policy lives under the CATALOG key ("cat"), never under the table's
    # own dict key ("cat.sch.orders") - the opposite of table level
    # application's storage.
    assert tag_key in fake.policies.get("cat", {})
    assert tag_key not in fake.policies.get(table.full_name, {})
    assert fake.policies["cat"][tag_key].on_securable_type == "CATALOG"


def test_one_catalog_scoped_policy_shared_by_two_tables_same_function():
    """The defining behavioral difference vs table level application: two
    DIFFERENT tables governed by the identical legacy function converge on
    the exact same one policy object, not two."""
    fake = FakeUnityCatalogGateway()
    table_a = TableRef("cat", "sch", "orders_a")
    table_b = TableRef("cat", "sch", "orders_b")
    fake.set_row_filter_state(table_a, RF_FN, ["business_unit"])
    fake.set_row_filter_state(table_b, RF_FN, ["business_unit"])
    strategy = _strategy()

    convert_table(table_a, fake, dry_run=False, policy_strategy=strategy, phase="APPLY_ABAC")
    convert_table(table_b, fake, dry_run=False, policy_strategy=strategy, phase="APPLY_ABAC")

    tag_key = tag_key_for_function(RF_FN, "row_filter")
    assert len(fake.policies.get("cat", {})) == 1
    assert tag_key in fake.policies["cat"]


def test_finalize_removes_legacy_but_keeps_shared_catalog_policy_for_sibling_table():
    fake = FakeUnityCatalogGateway()
    table_a = TableRef("cat", "sch", "orders_a")
    table_b = TableRef("cat", "sch", "orders_b")
    fake.set_row_filter_state(table_a, RF_FN, ["business_unit"])
    fake.set_row_filter_state(table_b, RF_FN, ["business_unit"])
    strategy = _strategy()
    convert_table(table_a, fake, dry_run=False, policy_strategy=strategy, phase="APPLY_ABAC")
    convert_table(table_b, fake, dry_run=False, policy_strategy=strategy, phase="APPLY_ABAC")

    finalized_a = convert_table(table_a, fake, dry_run=False, policy_strategy=strategy, phase="FINALIZE")

    assert finalized_a.status == StepStatus.SUCCESS
    assert fake.row_filters[table_a.full_name] is None  # table A's own legacy removed
    assert fake.row_filters[table_b.full_name] is not None  # table B untouched, still mid-pipeline
    tag_key = tag_key_for_function(RF_FN, "row_filter")
    assert tag_key in fake.policies["cat"]  # shared policy survives - table B still needs it


def test_apply_abac_then_finalize_full_row_filter_and_mask_lifecycle():
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "customers")
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    fake.set_column_mask_state(table, "email", MASK_FN)
    strategy = _strategy()

    applied = convert_table(table, fake, dry_run=False, policy_strategy=strategy, phase="APPLY_ABAC")
    assert applied.status == StepStatus.ABAC_APPLIED
    # both mechanisms present - legacy deliberately untouched mid-pipeline
    assert fake.row_filters[table.full_name] is not None
    assert "email" in fake.column_masks[table.full_name]

    finalized = convert_table(table, fake, dry_run=False, policy_strategy=strategy, phase="FINALIZE")

    assert finalized.status == StepStatus.SUCCESS
    assert finalized.migration_phase == "FINALIZED"
    assert fake.row_filters[table.full_name] is None
    assert fake.column_masks[table.full_name] == {}


def test_verify_after_finalize_recovers_via_governed_tag_not_legacy_state():
    """Once FINALIZE has removed the legacy row filter, the table's own
    `DESCRIBE TABLE EXTENDED` no longer names the function - VERIFY must
    still pass by recovering it from the governed column tag instead
    (CatalogBasedPolicyStrategy.find_existing_row_filter_policy)."""
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "orders")
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    strategy = _strategy()
    convert_table(table, fake, dry_run=False, policy_strategy=strategy, phase="APPLY_ABAC")
    convert_table(table, fake, dry_run=False, policy_strategy=strategy, phase="FINALIZE")

    result = verify_table(table, fake, strategy)

    assert result.status == StepStatus.SUCCESS


def test_rerun_inventory_style_applies_to_after_finalize_is_already_migrated():
    """A second APPLY_ABAC (or INVENTORY) pass over an already-FINALIZEd
    table must resolve to ALREADY_MIGRATED, not NOT_ELIGIBLE/re-attempt -
    exactly mirrors table level application's equivalent idempotency
    guarantee, just recovered via tags instead of a constant policy name."""
    fake = FakeUnityCatalogGateway()
    table = TableRef("cat", "sch", "orders")
    fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    strategy = _strategy()
    convert_table(table, fake, dry_run=False, policy_strategy=strategy, phase="APPLY_ABAC")
    convert_table(table, fake, dry_run=False, policy_strategy=strategy, phase="FINALIZE")

    rerun = convert_table(table, fake, dry_run=False, policy_strategy=strategy, phase="APPLY_ABAC")

    assert rerun.status == StepStatus.ALREADY_MIGRATED


def test_catalog_scope_matches_table_scope_end_state_for_a_single_table():
    """Different policy *objects*/names, but the same functional end
    state (legacy gone, exactly one live ABAC policy of each type)."""
    from ..migration.policy_strategy import TableBasedPolicyStrategy

    fake_table_scope = FakeUnityCatalogGateway()
    t1 = TableRef("cat", "sch", "t1")
    fake_table_scope.set_row_filter_state(t1, RF_FN, ["business_unit"])
    fake_table_scope.set_column_mask_state(t1, "email", MASK_FN)
    table_strategy = TableBasedPolicyStrategy()
    r1 = convert_table(t1, fake_table_scope, dry_run=False, policy_strategy=table_strategy)

    fake_catalog_scope = FakeUnityCatalogGateway()
    t2 = TableRef("cat", "sch", "t2")
    fake_catalog_scope.set_row_filter_state(t2, RF_FN, ["business_unit"])
    fake_catalog_scope.set_column_mask_state(t2, "email", MASK_FN)
    catalog_strategy = _strategy()
    r2 = convert_table(t2, fake_catalog_scope, dry_run=False, policy_strategy=catalog_strategy)

    assert r1.status == r2.status == StepStatus.SUCCESS
    assert fake_table_scope.row_filters[t1.full_name] is None
    assert fake_catalog_scope.row_filters[t2.full_name] is None
