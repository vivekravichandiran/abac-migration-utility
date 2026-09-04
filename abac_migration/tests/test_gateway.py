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
