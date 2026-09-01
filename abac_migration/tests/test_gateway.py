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
from ..uc_gateway.models import TableRef


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


def test_list_column_tags_preserves_a_genuinely_null_value():
    executor = _StubExecutor(rows=[["department", "abac_rls_cat_sch_rf_dept", None]])
    gateway = DatabricksUnityCatalogGateway(executor)

    tags = gateway.list_column_tags(TableRef("cat", "sch", "employees"))

    assert tags[0].tag_value is None
