"""List catalogs (metadata only, §2). Read-only - never filters by
eligibility (that is inventory's job) and never mutates."""
from __future__ import annotations

from ..uc_gateway.gateway import UnityCatalogGateway


def list_catalogs(uc: UnityCatalogGateway) -> list:
    return uc.list_catalogs()
