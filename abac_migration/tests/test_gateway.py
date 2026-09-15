"""Regression tests for DatabricksUnityCatalogGateway - specifically
list_column_tags()'s '' -> None normalization.

Bug this guards against (confirmed live, 2026-09-01, `ril_abac_e2e_test`):
a key-only governed tag (`ALTER TABLE ... SET TAGS ('key')`, no `= value`)
is persisted by Databricks as tag_value = '' (empty string) in
information_schema.column_tags, NOT NULL. Without normalizing that '' to
None here, tag_provisioner._find_reusable_tag() passes the raw '' straight
into MatchColumn.tag_value on every run that REUSES an existing tag
(prefer_existing_tags, the default), which flips
`gateway._build_create_policy_statement()`'s `has_tag(key)` (correct) into
`has_tag_value(key, '')` - and UC's policy compiler deterministically
rejects that with "Invalid tag value `` for key ...", no matter how long
you retry. Only the very FIRST run (fresh mint, tag_value hardcoded to
`None` in tag_provisioner._mint_and_assign) was unaffected; every
subsequent idempotent rerun failed 100% of the time until this fix.
"""
from __future__ import annotations

from ..uc_gateway.gateway import DatabricksUnityCatalogGateway
from ..uc_gateway.models import MatchColumn, PolicySpec, TableRef


class _StubExecutor:
    def __init__(self, rows):
        self.rows = rows
        self.statements: list[str] = []

    def run(self, statement, timeout_s=60):
        self.statements.append(statement)

        class _Result:
            status = "SUCCEEDED"
            error = None
            rows = self.rows

        return _Result()


def test_list_column_tags_normalizes_empty_string_value_to_none():
    executor = _StubExecutor(rows=[["department", "abac_rls_cat_sch_rf_dept", ""]])
    gateway = DatabricksUnityCatalogGateway(executor)

    tags = gateway.list_column_tags(TableRef("cat", "sch", "employees"))

    assert len(tags) == 1
    assert tags[0].column == "department"
    assert tags[0].tag_key == "abac_rls_cat_sch_rf_dept"
    assert tags[0].tag_value is None  # NOT ""


def test_list_column_tags_preserves_a_real_non_empty_value():
    executor = _StubExecutor(rows=[["ssn", "abac_colmask_cat_sch_mask_ssn", "a1b2c3"]])
    gateway = DatabricksUnityCatalogGateway(executor)

    tags = gateway.list_column_tags(TableRef("cat", "sch", "employees"))

    assert tags[0].tag_value == "a1b2c3"


# ---------------------------------------------------------------------------
# show_policies()/describe_policy()/drop_policy() take an already-built
# `on_securable` string (from PolicyStrategy.on_securable_for(), §7.3) not a
# bare TableRef - the one signature change that lets these three methods
# serve both "table level application" (`ON TABLE ...`) and "catalog level
# application" (`ON CATALOG ...`) without a second set of methods.
# ---------------------------------------------------------------------------

def test_show_policies_builds_on_clause_verbatim_for_table_scope():
    executor = _StubExecutor(rows=[])
    gateway = DatabricksUnityCatalogGateway(executor)

    gateway.show_policies("TABLE `cat`.`sch`.`tbl`")

    assert executor.statements == ["SHOW POLICIES ON TABLE `cat`.`sch`.`tbl`"]


def test_show_policies_builds_on_clause_verbatim_for_catalog_scope():
    executor = _StubExecutor(rows=[])
    gateway = DatabricksUnityCatalogGateway(executor)

    gateway.show_policies("CATALOG `cat`")

    assert executor.statements == ["SHOW POLICIES ON CATALOG `cat`"]


class _StubExecutorWithErrorCode(_StubExecutor):
    """_StubExecutor's `_Result` omits `error_code` (fine for the SUCCEEDED-
    only tests above) - `describe_policy()` always checks it, even on a
    SUCCEEDED response, so this variant sets it to None explicitly."""

    def run(self, statement, timeout_s=60):
        self.statements.append(statement)

        class _Result:
            status = "SUCCEEDED"
            error = None
            error_code = None
            rows = self.rows

        return _Result()


def test_describe_policy_builds_on_clause_for_catalog_scope():
    executor = _StubExecutorWithErrorCode(rows=[["Name", "abac_rls_cat_sch_fn"], ["Policy Type", "ROW_FILTER"]])
    gateway = DatabricksUnityCatalogGateway(executor)

    gateway.describe_policy("CATALOG `cat`", "abac_rls_cat_sch_fn")

    assert executor.statements == ["DESCRIBE POLICY abac_rls_cat_sch_fn ON CATALOG `cat`"]


def test_drop_policy_builds_on_clause_for_catalog_scope():
    executor = _StubExecutor(rows=[])
    gateway = DatabricksUnityCatalogGateway(executor)

    gateway.drop_policy("CATALOG `cat`", "abac_rls_cat_sch_fn", dry_run=False)

    assert executor.statements == ["DROP POLICY abac_rls_cat_sch_fn ON CATALOG `cat`"]


def test_list_column_tags_preserves_a_genuinely_null_value():
    executor = _StubExecutor(rows=[["department", "abac_rls_cat_sch_rf_dept", None]])
    gateway = DatabricksUnityCatalogGateway(executor)

    tags = gateway.list_column_tags(TableRef("cat", "sch", "employees"))

    assert tags[0].tag_value is None


# -- EXCEPT principal(s) SQL generation ---------------------------------
# Grammar confirmed via Databricks docs (CREATE POLICY, sql-ref-syntax-ddl-
# create-policy): `TO principal [, ...] [ EXCEPT principal [, ...] ]`, for
# both row_filter_body and column_mask_body. Not yet re-verified live
# against this repo's own workspace (token expired mid-change) - the
# examples in the docs (`TO 'All Users' EXCEPT 'HR admins'`) match the
# clause shape built below exactly, though.

def _row_filter_spec(except_principals):
    mc = MatchColumn(tag_key="abac_rls_cat_sch_rf_dept", tag_value=None, alias="mc_department", source_column="department")
    return PolicySpec(
        policy_name="abac_migrated_row_filter", on_securable="TABLE `cat`.`sch`.`t1`",
        policy_type="ROW_FILTER", function_fqn="cat.sch.rf_dept", match_columns=[mc],
        using_columns=["mc_department"], to_principals=["account users"], except_principals=except_principals,
    )


def _column_mask_spec(except_principals):
    mc = MatchColumn(tag_key="abac_colmask_cat_sch_mask_ssn", tag_value=None, alias="mc_ssn", source_column="ssn")
    return PolicySpec(
        policy_name="abac_migrated_mask_ssn", on_securable="TABLE `cat`.`sch`.`t1`",
        policy_type="COLUMN_MASK", function_fqn="cat.sch.mask_ssn", match_columns=[mc],
        using_columns=[], mask_target_alias="mc_ssn", to_principals=["account users"],
        except_principals=except_principals,
    )


def test_build_create_policy_statement_omits_except_clause_when_empty():
    gateway = DatabricksUnityCatalogGateway(_StubExecutor(rows=[]))

    stmt = gateway._build_create_policy_statement(_row_filter_spec([]))

    assert "EXCEPT" not in stmt
    assert "TO `account users`\n" in stmt  # unchanged from before this feature


def test_build_create_policy_statement_adds_except_clause_for_row_filter():
    gateway = DatabricksUnityCatalogGateway(_StubExecutor(rows=[]))

    stmt = gateway._build_create_policy_statement(_row_filter_spec(["etl_service_principal"]))

    assert "TO `account users` EXCEPT `etl_service_principal`\n" in stmt


def test_build_create_policy_statement_adds_except_clause_for_column_mask():
    gateway = DatabricksUnityCatalogGateway(_StubExecutor(rows=[]))

    stmt = gateway._build_create_policy_statement(_column_mask_spec(["etl_service_principal"]))

    assert "TO `account users` EXCEPT `etl_service_principal`\n" in stmt


def test_build_create_policy_statement_multiple_except_principals():
    gateway = DatabricksUnityCatalogGateway(_StubExecutor(rows=[]))

    stmt = gateway._build_create_policy_statement(
        _row_filter_spec(["etl_service_principal", "break_glass_admins"])
    )

    assert "EXCEPT `etl_service_principal`, `break_glass_admins`\n" in stmt


# ---------------------------------------------------------------------------
# MATERIALIZED_VIEW support (Track B, 2026-09-15):
# 1. describe_table_security()'s column-mask parser must not mis-read a
#    materialized view's trailing "Total Size (bytes)" administrative row
#    (which sits directly under "# Column Masks" with no separator) as a
#    phantom masked column - confirmed live, DEF-01 in
#    STREAMING_TABLE_SUPPORT_TEST_CASES.md.
# 2. The 5 table_type-aware mutating methods must emit `ALTER MATERIALIZED
#    VIEW ...` (not `ALTER TABLE ...`) whenever table_type=="MATERIALIZED_VIEW",
#    and keep emitting plain `ALTER TABLE ...` for every other table_type
#    (default "MANAGED", and explicitly STREAMING_TABLE too - confirmed live
#    it does NOT need the ALTER STREAMING TABLE keyword, unlike MVs).
# ---------------------------------------------------------------------------

def test_describe_table_security_mv_no_masks_does_not_pick_up_phantom_column():
    """Real DESCRIBE TABLE EXTENDED output for a masked-column-free
    MATERIALIZED_VIEW still includes a `# Column Masks` header (UC always
    emits it) immediately followed by `Total Size (bytes)` - with the old
    (blank/`#`-prefix-only) loop terminator this was mis-parsed as ONE
    phantom masked column named "Total Size (bytes)" with function "2137"."""
    executor = _StubExecutor(rows=[
        ["Type", "MATERIALIZED_VIEW"],
        ["# Column Masks", ""],
        ["Total Size (bytes)", "2137"],
        ["Num Files", "1"],
    ])
    gateway = DatabricksUnityCatalogGateway(executor)

    state = gateway.describe_table_security(TableRef("cat", "sch", "an_mv"))

    assert state.table_type == "MATERIALIZED_VIEW"
    assert state.column_masks == []
    assert not state.has_column_masks


def test_describe_table_security_mv_real_mask_still_parses_correctly():
    """The same fix must not regress a MATERIALIZED_VIEW that DOES have a
    real mask - its function FQN field is always backtick-quoted, unlike
    the phantom "Total Size (bytes)" row's plain numeric field."""
    executor = _StubExecutor(rows=[
        ["Type", "MATERIALIZED_VIEW"],
        ["# Column Masks", ""],
        ["ssn", "`cat`.`sch`.`mask_ssn`"],
        ["Total Size (bytes)", "2137"],
    ])
    gateway = DatabricksUnityCatalogGateway(executor)

    state = gateway.describe_table_security(TableRef("cat", "sch", "an_mv"))

    assert len(state.column_masks) == 1
    assert state.column_masks[0].column == "ssn"
    assert state.column_masks[0].function_fqn == "cat.sch.mask_ssn"


def test_drop_row_filter_uses_alter_table_by_default():
    executor = _StubExecutor(rows=[])
    gateway = DatabricksUnityCatalogGateway(executor)

    gateway.drop_row_filter(TableRef("cat", "sch", "t1"), dry_run=False)

    assert executor.statements == ["ALTER TABLE `cat`.`sch`.`t1` DROP ROW FILTER"]


def test_drop_row_filter_uses_alter_table_for_streaming_table():
    executor = _StubExecutor(rows=[])
    gateway = DatabricksUnityCatalogGateway(executor)

    gateway.drop_row_filter(TableRef("cat", "sch", "t1"), dry_run=False, table_type="STREAMING_TABLE")

    assert executor.statements == ["ALTER TABLE `cat`.`sch`.`t1` DROP ROW FILTER"]


def test_drop_row_filter_uses_alter_materialized_view():
    executor = _StubExecutor(rows=[])
    gateway = DatabricksUnityCatalogGateway(executor)

    gateway.drop_row_filter(TableRef("cat", "sch", "t1"), dry_run=False, table_type="MATERIALIZED_VIEW")

    assert executor.statements == ["ALTER MATERIALIZED VIEW `cat`.`sch`.`t1` DROP ROW FILTER"]


def test_drop_column_mask_uses_alter_materialized_view():
    executor = _StubExecutor(rows=[])
    gateway = DatabricksUnityCatalogGateway(executor)

    gateway.drop_column_mask(TableRef("cat", "sch", "t1"), "ssn", dry_run=False, table_type="MATERIALIZED_VIEW")

    assert executor.statements == ["ALTER MATERIALIZED VIEW `cat`.`sch`.`t1` ALTER COLUMN `ssn` DROP MASK"]


def test_set_row_filter_uses_alter_materialized_view():
    executor = _StubExecutor(rows=[])
    gateway = DatabricksUnityCatalogGateway(executor)

    gateway.set_row_filter(
        TableRef("cat", "sch", "t1"), "cat.sch.rf_dept", ["department"], dry_run=False, table_type="MATERIALIZED_VIEW",
    )

    assert executor.statements == ["ALTER MATERIALIZED VIEW `cat`.`sch`.`t1` SET ROW FILTER cat.sch.rf_dept ON (`department`)"]


def test_set_column_mask_uses_alter_materialized_view():
    executor = _StubExecutor(rows=[])
    gateway = DatabricksUnityCatalogGateway(executor)

    gateway.set_column_mask(
        TableRef("cat", "sch", "t1"), "ssn", "cat.sch.mask_ssn", dry_run=False, table_type="MATERIALIZED_VIEW",
    )

    assert executor.statements == ["ALTER MATERIALIZED VIEW `cat`.`sch`.`t1` ALTER COLUMN `ssn` SET MASK cat.sch.mask_ssn"]


def test_set_column_tags_uses_alter_materialized_view():
    executor = _StubExecutor(rows=[])
    gateway = DatabricksUnityCatalogGateway(executor)

    gateway.set_column_tags(
        TableRef("cat", "sch", "t1"), "ssn", {"abac_rls_cat_sch_rf_dept": None}, dry_run=False,
        table_type="MATERIALIZED_VIEW",
    )

    assert executor.statements == ["ALTER MATERIALIZED VIEW `cat`.`sch`.`t1` ALTER COLUMN `ssn` SET TAGS ('abac_rls_cat_sch_rf_dept')"]
