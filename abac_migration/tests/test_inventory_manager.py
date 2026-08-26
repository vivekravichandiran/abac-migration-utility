"""Regression coverage added after a live end-to-end run against a real
workspace surfaced that the inventory-level eligibility gate was too
coarse: a single masked column referencing a now-missing function marked
the ENTIRE table NOT_ELIGIBLE, silently skipping otherwise-migratable
sibling columns/row-filter and producing no audit trail for the bad one -
contradicting the documented "weakest link" per-object independence
(mask_to_abac.py, DESIGN.md scenario 11). Table-level eligibility should
only gate on whole-table conditions; per-object failures are the plugins'
job to report (already covered by test_table_converter.py scenarios 5/6).
"""
from __future__ import annotations

from ..inventory.inventory_manager import build_inventory_record
from ..uc_gateway.gateway import UCGatewayError
from ..uc_gateway.models import PolicyDefinition, TableRef
from .fake_gateway import FakeUnityCatalogGateway


def test_table_with_one_missing_mask_function_is_still_eligible():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "partial_mask_failure")
    uc.set_column_mask_state(table, "c1", "cat.schema.mask_c1")
    uc.set_column_mask_state(table, "c3", "cat.schema.mask_c3")
    # c2's mask is set, but its source function was later dropped -
    # register the mask without registering the function as existing.
    from ..uc_gateway.models import ColumnMaskInfo
    uc.column_masks[table.full_name]["c2"] = ColumnMaskInfo(column="c2", function_fqn="cat.schema.mask_c2_gone")

    record = build_inventory_record(table, uc, run_id="run-1")

    assert record.migration_eligibility == "ELIGIBLE"
    assert record.eligibility_reason is None


def test_table_with_conflicting_existing_policy_is_still_eligible_at_inventory_level():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "conflicting_policy")
    uc.set_row_filter_state(table, "cat.schema.rf_real", ["region"])
    uc.add_existing_policy(table, PolicyDefinition(
        name="abac_migrated_row_filter", policy_type="ROW_FILTER", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_region"],
        function_fqn="cat.schema.rf_other", using_columns=["region"],
    ))

    record = build_inventory_record(table, uc, run_id="run-1")

    # Whole-table eligibility no longer pre-empts this - table_converter's
    # RLSMigrationPlugin is the one that reports EXISTING_ABAC_POLICY_CONFLICT
    # per-object (see test_table_converter.py scenario 6).
    assert record.migration_eligibility == "ELIGIBLE"


def test_already_migrated_table_with_no_legacy_left_is_still_eligible():
    """Second regression from the same live run: a fully, successfully
    migrated table has NO legacy row filter/masks left at all by definition
    - without checking existing_policies too, a rerun (idempotent MIGRATE,
    or INVENTORY_AND_MIGRATE covering the same scope twice) would mark it
    NOT_ELIGIBLE/NO_LEGACY_SECURITY_FOUND and silently drop it from the run,
    instead of attempting it and correctly resolving to ALREADY_MIGRATED."""
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "fully_migrated")
    uc.add_existing_policy(table, PolicyDefinition(
        name="abac_migrated_row_filter", policy_type="ROW_FILTER", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_region"],
        function_fqn="cat.schema.rf_region", using_columns=["mc_region"],
    ))
    # no legacy row filter or masks registered at all - migration already
    # removed them in a prior run.

    record = build_inventory_record(table, uc, run_id="run-1")

    assert record.migration_eligibility == "ELIGIBLE"
    assert record.eligibility_reason is None


def test_table_with_no_legacy_security_is_not_eligible():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "plain")
    uc.register_table(table)

    record = build_inventory_record(table, uc, run_id="run-1")

    assert record.migration_eligibility == "NOT_ELIGIBLE"
    assert record.eligibility_reason == "NO_LEGACY_SECURITY_FOUND"


def test_unsupported_table_type_is_not_eligible():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "a_view")
    uc.set_row_filter_state(table, "cat.schema.rf_fn", ["region"])
    uc.tables[table.full_name] = "VIEW"

    record = build_inventory_record(table, uc, run_id="run-1")

    assert record.migration_eligibility == "NOT_ELIGIBLE"
    assert record.eligibility_reason == "UNSUPPORTED_TABLE_TYPE"


def test_permission_denied_table_is_not_eligible_and_does_not_raise():
    """Live ALL_CATALOGS run finding #3: a table can be listable (SHOW
    TABLES only needs USE SCHEMA) while DESCRIBE TABLE EXTENDED still denies
    SELECT to this identity - inventory must record it and move on, not
    abort the entire INVENTORY pass for every other table in scope."""
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "no_select_grant")
    uc.register_table(table)
    uc.set_fault(
        "describe_table_security",
        UCGatewayError("BAD_REQUEST", "PERMISSION_DENIED: User does not have SELECT on Table 'cat.schema.no_select_grant'."),
    )

    record = build_inventory_record(table, uc, run_id="run-1")

    assert record.migration_eligibility == "NOT_ELIGIBLE"
    assert record.eligibility_reason == "PERMISSION_DENIED"


def test_sample_catalog_table_is_not_eligible_and_does_not_raise():
    """Live ALL_CATALOGS run finding #4: Databricks-managed `samples`
    catalog tables reject policy operations outright with a distinct
    SAMPLE_TABLE_PERMISSIONS error_code (not PERMISSION_DENIED) - handled
    the same way, since it means the same thing for this tool's purposes."""
    uc = FakeUnityCatalogGateway()
    table = TableRef("samples", "nyctaxi", "trips")
    uc.register_table(table)
    uc.set_fault(
        "describe_table_security",
        UCGatewayError("BAD_REQUEST", "[SAMPLE_TABLE_PERMISSIONS] Permissions not supported on sample databases/tables."),
    )

    record = build_inventory_record(table, uc, run_id="run-1")

    assert record.migration_eligibility == "NOT_ELIGIBLE"
    assert record.eligibility_reason == "PERMISSION_DENIED"


def test_non_permission_error_still_propagates_from_inventory():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "flaky")
    uc.register_table(table)
    uc.set_fault("describe_table_security", UCGatewayError("INTERNAL_ERROR", "transient backend failure"))

    try:
        build_inventory_record(table, uc, run_id="run-1")
        assert False, "expected UCGatewayError to propagate"
    except UCGatewayError:
        pass


# ---------------------------------------------------------------------------
# LLM-assisted PII tagging (INVENTORY-only, advisory) - see
# uc_gateway/gateway.py PiiSuggestion/suggest_pii_tag and fake_gateway.py's
# deterministic keyword-based stand-in for the real ai_query() call.
# ---------------------------------------------------------------------------

def test_llm_pii_tagging_disabled_by_default_leaves_fields_empty():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "customers")
    uc.set_row_filter_state(table, "cat.schema.rf_region_fn", ["region"])
    uc.set_column_mask_state(table, "email", "cat.schema.mask_email_fn")

    record = build_inventory_record(table, uc, run_id="run-1")

    assert record.row_filter_suggested_pii_tag is None
    assert record.column_mask_suggested_pii_tags == {}
    assert uc.pii_suggestion_calls == []


def test_llm_pii_tagging_when_enabled_classifies_row_filter_and_masks():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "customers")
    uc.set_row_filter_state(table, "cat.schema.rf_region_fn", ["region"])
    uc.set_column_mask_state(table, "email_address", "cat.schema.mask_email_fn")
    uc.set_column_mask_state(table, "ssn", "cat.schema.mask_ssn_fn")

    record = build_inventory_record(table, uc, run_id="run-1", enable_llm_pii_tagging=True)

    assert record.row_filter_suggested_pii_tag == "region"
    assert record.column_mask_suggested_pii_tags == {"email_address": "email", "ssn": "ssn"}
    assert len(uc.pii_suggestion_calls) == 3  # 1 row filter + 2 masks
    # each call is scoped to its own function/columns - never all at once
    fns_called = {call[0] for call in uc.pii_suggestion_calls}
    assert fns_called == {"cat.schema.rf_region_fn", "cat.schema.mask_email_fn", "cat.schema.mask_ssn_fn"}


def test_llm_pii_tagging_uses_configured_endpoint():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "customers")
    uc.set_row_filter_state(table, "cat.schema.rf_region_fn", ["region"])

    build_inventory_record(
        table, uc, run_id="run-1", enable_llm_pii_tagging=True, pii_llm_endpoint="custom-endpoint",
    )

    assert uc.pii_suggestion_calls[0][2] == "custom-endpoint"


def test_real_gateway_suggest_pii_tag_degrades_gracefully_on_executor_failure():
    """A classification failure (endpoint unavailable, network error, etc.)
    must never raise out of suggest_pii_tag - callers (inventory_manager)
    rely on this to never let a best-effort LLM call fail/block INVENTORY."""
    from ..uc_gateway.gateway import DatabricksUnityCatalogGateway

    class _BoomExecutor:
        def run(self, statement, timeout_s=60):
            raise RuntimeError("model serving endpoint not found")

    gateway = DatabricksUnityCatalogGateway(_BoomExecutor())
    suggestion = gateway.suggest_pii_tag("cat.schema.rf_fn", ["region"], "some-endpoint")

    assert suggestion.tag is None
    assert "model serving endpoint not found" in suggestion.error


def test_real_gateway_suggest_pii_tag_normalizes_llm_response():
    from ..uc_gateway.gateway import DatabricksUnityCatalogGateway

    class _FakeResult:
        status = "SUCCEEDED"
        error = None
        rows = [["  Email.  "]]

    class _StubExecutor:
        def run(self, statement, timeout_s=60):
            assert "ai_query(" in statement
            return _FakeResult()

    gateway = DatabricksUnityCatalogGateway(_StubExecutor())
    suggestion = gateway.suggest_pii_tag("cat.schema.mask_email_fn", ["email"], "some-endpoint")

    assert suggestion.tag == "email"


def test_llm_pii_tagging_no_legacy_security_makes_no_calls():
    uc = FakeUnityCatalogGateway()
    table = TableRef("cat", "schema", "plain")
    uc.register_table(table)

    record = build_inventory_record(table, uc, run_id="run-1", enable_llm_pii_tagging=True)

    assert record.row_filter_suggested_pii_tag is None
    assert record.column_mask_suggested_pii_tags == {}
    assert uc.pii_suggestion_calls == []
