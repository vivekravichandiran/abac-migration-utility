"""For one table: does a table-level Row Filter exist, and which function/
columns (§2)? Read-only - used by inventory_manager during the INVENTORY
step. migration/plugins/rls_to_abac.py independently re-discovers via
uc_gateway directly at MIGRATE time (§7: "UC is always re-queried") -
both call the same uc.describe_table_security() underneath; this module
exists so inventory has its own explicit, narrowly-scoped call site.
"""
from __future__ import annotations

from typing import Optional

from ..uc_gateway.gateway import UnityCatalogGateway
from ..uc_gateway.models import RowFilterInfo, TableRef


def discover_row_filter(table: TableRef, uc: UnityCatalogGateway) -> Optional[RowFilterInfo]:
    return uc.describe_table_security(table).row_filter
