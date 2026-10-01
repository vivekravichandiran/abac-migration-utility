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


def _federation_unreachable_error(securable: str = "cat1") -> UCGatewayError:
    return UCGatewayError(
        "BAD_REQUEST",
        f"[FAILED_JDBC.CONNECTION] Failed JDBC jdbc:postgresql:*(redacted) on catalog '{securable}': "
        f"Failed to connect to the database. SQLSTATE: HV000",
    )


def test_selected_catalogs_skips_an_unreachable_federated_catalog():
    """Unlike a plain permission error, an unreachable Lakehouse Federation
    catalog/schema is skipped for EVERY scope_type, including an explicitly
    requested SELECTED_CATALOGS - a federated table can never be a valid
    ABAC/governed-tags target regardless of connectivity, so there's nothing
    for the caller to fix by seeing this raised (confirmed live 2026-09-30,
    a Postgres-backed foreign catalog crashed the whole run with this error
    before this fix)."""
    fake = _fake_with_tables()
    fake.catalogs.add("federated_cat")
    fake.set_fault("list_schemas", _federation_unreachable_error("federated_cat"))
    config = RunConfig(
        scope_type=ScopeType.SELECTED_CATALOGS, catalogs=["federated_cat", "cat1"],
        audit_catalog="audit_cat", audit_schema="audit_sch",
    )
    tables = resolve_scope(config, fake)
    assert {t.catalog for t in tables} == {"cat1"}


def test_all_catalogs_skips_an_unreachable_federated_catalog_too():
    fake = _fake_with_tables()
    fake.catalogs.add("0_federated_cat")
    fake.set_fault("list_schemas", _federation_unreachable_error("0_federated_cat"))
    config = RunConfig(scope_type=ScopeType.ALL_CATALOGS, audit_catalog="audit_cat", audit_schema="audit_sch")
    tables = resolve_scope(config, fake)
    assert {t.catalog for t in tables} == {"cat1", "cat2"}


def test_selected_schemas_skips_an_unreachable_federated_schema():
    fake = _fake_with_tables()
    fake.set_fault("list_tables", _federation_unreachable_error("cat1.sales"))
    config = RunConfig(
        scope_type=ScopeType.SELECTED_SCHEMAS, schemas={"cat1": ["sales"], "cat2": ["finance"]},
        audit_catalog="audit_cat", audit_schema="audit_sch",
    )
    tables = resolve_scope(config, fake)
    assert tables == [TableRef("cat2", "finance", "t1")]


def _unsupported_data_source_error(securable: str = "cat1") -> UCGatewayError:
    return UCGatewayError(
        "BAD_REQUEST",
        f"[DATA_SOURCE_NOT_FOUND] Failed to find the data source: unsupported for '{securable}'. "
        f"Make sure the provider name is correct and the package is properly registered and "
        f"compatible with your Spark version. SQLSTATE: 42K02",
    )


def test_selected_catalogs_skips_a_catalog_with_an_unsupported_data_source():
    """Same shape as the federation-unreachable case (confirmed live
    2026-09-30 against a real pre-existing Vector Search index registered
    as a FOREIGN table): the SQL warehouse's runtime has no connector for
    this securable's provider at all, so even listing fails outright,
    independent of permissions and independent of scope_type."""
    fake = _fake_with_tables()
    fake.catalogs.add("unsupported_cat")
    fake.set_fault("list_schemas", _unsupported_data_source_error("unsupported_cat"))
    config = RunConfig(
        scope_type=ScopeType.SELECTED_CATALOGS, catalogs=["unsupported_cat", "cat1"],
        audit_catalog="audit_cat", audit_schema="audit_sch",
    )
    tables = resolve_scope(config, fake)
    assert {t.catalog for t in tables} == {"cat1"}


def test_all_catalogs_skips_a_catalog_with_an_unsupported_data_source_too():
    fake = _fake_with_tables()
    fake.catalogs.add("0_unsupported_cat")
    fake.set_fault("list_schemas", _unsupported_data_source_error("0_unsupported_cat"))
    config = RunConfig(scope_type=ScopeType.ALL_CATALOGS, audit_catalog="audit_cat", audit_schema="audit_sch")
    tables = resolve_scope(config, fake)
    assert {t.catalog for t in tables} == {"cat1", "cat2"}


def test_selected_schemas_skips_a_schema_with_an_unsupported_data_source():
    fake = _fake_with_tables()
    fake.set_fault("list_tables", _unsupported_data_source_error("cat1.sales"))
    config = RunConfig(
        scope_type=ScopeType.SELECTED_SCHEMAS, schemas={"cat1": ["sales"], "cat2": ["finance"]},
        audit_catalog="audit_cat", audit_schema="audit_sch",
    )
    tables = resolve_scope(config, fake)
    assert tables == [TableRef("cat2", "finance", "t1")]
