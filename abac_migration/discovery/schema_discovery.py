"""List schemas within a catalog (metadata only, §2). Read-only."""
from __future__ import annotations

from ..uc_gateway.gateway import UnityCatalogGateway


def list_schemas(catalog: str, uc: UnityCatalogGateway) -> list:
    return uc.list_schemas(catalog)
