"""For one table: which columns have Column Masks, and which functions
(§2)? Read-only - same relationship to migration/plugins/mask_to_abac.py
as rls_discovery.py has to rls_to_abac.py (see its docstring)."""
from __future__ import annotations

from ..uc_gateway.gateway import UnityCatalogGateway
from ..uc_gateway.models import TableRef


def discover_column_masks(table: TableRef, uc: UnityCatalogGateway) -> list:
    return uc.describe_table_security(table).column_masks
