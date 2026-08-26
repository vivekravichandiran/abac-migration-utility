from __future__ import annotations

from ..config.models import RunConfig, ScopeType
from ..scope.scope_resolver import resolve_scope
from ..uc_gateway.gateway import UCGatewayError
from ..uc_gateway.models import TableRef
from .fake_gateway import FakeUnityCatalogGateway


def _fake_with_tables():
    fake = FakeUnityCatalogGateway()
    for schema in ("sales", "sales_staging", "hr"):
        fake.register_table(TableRef("cat1", schema, "t1"))
    fake.register_table(TableRef("cat2", "finance", "t1"))
    return fake


def test_selected_catalogs_all_schemas():
    fake = _fake_with_tables()
    config = RunConfig(
        scope_type=ScopeType.SELECTED_CATALOGS, catalogs=["cat1"],
        audit_catalog="audit_cat", audit_schema="audit_sch",
    )
    tables = resolve_scope(config, fake)
    assert {t.schema for t in tables} == {"sales", "sales_staging", "hr"}


def test_exclude_schema_regex():
    fake = _fake_with_tables()
    config = RunConfig(
        scope_type=ScopeType.SELECTED_CATALOGS, catalogs=["cat1"], exclude_schema_regex="_staging$",
        audit_catalog="audit_cat", audit_schema="audit_sch",
    )
    tables = resolve_scope(config, fake)
    assert {t.schema for t in tables} == {"sales", "hr"}


def test_specific_tables_scope():
    fake = _fake_with_tables()
    config = RunConfig(
        scope_type=ScopeType.SPECIFIC_TABLES, tables=["cat1.sales.t1", "cat2.finance.t1"],
        audit_catalog="audit_cat", audit_schema="audit_sch",
    )
    tables = resolve_scope(config, fake)
    assert set(tables) == {TableRef("cat1", "sales", "t1"), TableRef("cat2", "finance", "t1")}


def test_selected_schemas_scope():
    fake = _fake_with_tables()
    config = RunConfig(
        scope_type=ScopeType.SELECTED_SCHEMAS, schemas={"cat1": ["sales"]},
        audit_catalog="audit_cat", audit_schema="audit_sch",
    )
    tables = resolve_scope(config, fake)
    assert tables == [TableRef("cat1", "sales", "t1")]


def _permission_denied_error(securable: str = "cat1") -> UCGatewayError:
    return UCGatewayError("BAD_REQUEST", f"PERMISSION_DENIED: User does not have USE CATALOG on Catalog '{securable}'.")


def test_all_catalogs_skips_a_catalog_the_identity_cannot_use():
    """ALL_CATALOGS discovers every catalog in the metastore via SHOW
    CATALOGS, including ones the run-as identity was never granted USE
    CATALOG on (confirmed live against a real workspace) - it must skip
    those, not abort the whole scope resolution."""
    fake = _fake_with_tables()
    # Sorts before "cat1"/"cat2" so it's the first (and, given the one-shot
    # fault below, only) list_schemas call to fail.
    fake.catalogs.add("0_no_access_cat")  # visible via SHOW CATALOGS, but...
    fake.set_fault("list_schemas", _permission_denied_error("0_no_access_cat"))

    config = RunConfig(scope_type=ScopeType.ALL_CATALOGS, audit_catalog="audit_cat", audit_schema="audit_sch")
    tables = resolve_scope(config, fake)

    assert {t.catalog for t in tables} == {"cat1", "cat2"}


def test_selected_catalogs_does_not_swallow_permission_errors():
    """A permission error on an EXPLICITLY requested catalog is a real
    misconfiguration the caller must see, not something to silently skip."""
    fake = _fake_with_tables()
    fake.set_fault("list_schemas", _permission_denied_error("cat1"))
    config = RunConfig(
        scope_type=ScopeType.SELECTED_CATALOGS, catalogs=["cat1"],
        audit_catalog="audit_cat", audit_schema="audit_sch",
    )
    try:
        resolve_scope(config, fake)
        assert False, "expected UCGatewayError to propagate"
    except UCGatewayError:
        pass


def test_all_catalogs_reraises_non_permission_errors():
    fake = _fake_with_tables()
    fake.set_fault("list_schemas", UCGatewayError("INTERNAL_ERROR", "something else broke"))
    config = RunConfig(scope_type=ScopeType.ALL_CATALOGS, audit_catalog="audit_cat", audit_schema="audit_sch")
    try:
        resolve_scope(config, fake)
        assert False, "expected UCGatewayError to propagate"
    except UCGatewayError:
        pass
