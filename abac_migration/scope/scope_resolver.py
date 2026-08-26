"""Expands `scope_type` + `catalogs`/`schemas`/`tables`/`exclude_schema_regex`
into a concrete `list[TableRef]` (§2). Never inspects table security config,
never mutates.

Per §1's layering rule ("scope/ never imports from migration/ or
discovery/"), this module calls the shared `UnityCatalogGateway` directly
for any live catalog/schema/table listing it needs (exactly like every
other component does, §5) rather than routing through the `discovery/`
package - `discovery/*.py` remain independently usable thin wrappers around
the same gateway calls, used by `inventory_manager` instead.
"""
from __future__ import annotations

import re

from ..config.models import RunConfig, ScopeType
from ..uc_gateway.gateway import UCGatewayError, UnityCatalogGateway, is_permission_denied
from ..uc_gateway.models import TableRef


def resolve_scope(config: RunConfig, uc: UnityCatalogGateway) -> list:
    if config.scope_type == ScopeType.SPECIFIC_TABLES:
        return [_parse_table_fqn(t) for t in config.tables]

    exclude_re = re.compile(config.exclude_schema_regex) if config.exclude_schema_regex else None

    if config.scope_type == ScopeType.ALL_CATALOGS:
        catalogs = uc.list_catalogs()
    elif config.scope_type == ScopeType.ALL_SCHEMAS:
        catalogs = list(config.catalogs) if config.catalogs else uc.list_catalogs()
    elif config.scope_type == ScopeType.SELECTED_CATALOGS:
        catalogs = list(config.catalogs)
    elif config.scope_type == ScopeType.SELECTED_SCHEMAS:
        catalogs = list(config.schemas.keys())
    else:
        raise ValueError(f"Unsupported scope_type: {config.scope_type}")

    result = []
    for catalog in catalogs:
        if config.scope_type == ScopeType.SELECTED_SCHEMAS:
            schema_names = config.schemas.get(catalog, [])
        else:
            # ALL_CATALOGS/ALL_SCHEMAS discover catalogs via SHOW CATALOGS,
            # which lists every catalog in the metastore regardless of
            # whether the run-as identity has USE CATALOG on it (confirmed
            # live: a real workspace always has catalogs - system/samples/
            # other teams' - the identity can see but not use). Explicitly
            # requested catalogs (SELECTED_CATALOGS/SELECTED_SCHEMAS) are
            # NOT swallowed this way - a permission error there is a real
            # misconfiguration the caller should see.
            try:
                schema_names = uc.list_schemas(catalog)
            except UCGatewayError as exc:
                if is_permission_denied(exc) and config.scope_type in (ScopeType.ALL_CATALOGS, ScopeType.ALL_SCHEMAS):
                    continue
                raise

        for schema in schema_names:
            if exclude_re and exclude_re.search(schema):
                continue
            try:
                result.extend(uc.list_tables(catalog, schema))
            except UCGatewayError as exc:
                if is_permission_denied(exc) and config.scope_type in (ScopeType.ALL_CATALOGS, ScopeType.ALL_SCHEMAS):
                    continue
                raise

    return result


def _parse_table_fqn(fqn: str) -> TableRef:
    parts = fqn.split(".")
    if len(parts) != 3:
        raise ValueError(f"Expected fully-qualified catalog.schema.table, got: {fqn!r}")
    return TableRef(*parts)
