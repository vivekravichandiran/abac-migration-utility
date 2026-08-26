"""List tables within a catalog.schema (metadata only, §2). Read-only."""
from __future__ import annotations

from ..uc_gateway.gateway import UnityCatalogGateway


def list_tables(catalog: str, schema: str, uc: UnityCatalogGateway) -> list:
    return uc.list_tables(catalog, schema)
