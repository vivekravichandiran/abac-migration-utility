"""PolicyStrategy: deterministic ABAC policy naming/targeting (§7.3).
Takes already-resolved MatchColumn objects (produced by tag_provisioner,
§7.4) and turns them into a full PolicySpec. Never executes SQL itself -
the gateway does that (§2: "Execute SQL directly" is a Must-NOT here).
"""
from __future__ import annotations

from typing import Optional, Protocol

from ..uc_gateway.models import MatchColumn, PolicySpec, TableRef


class PolicyStrategy(Protocol):
    def plan_row_filter_policy(self, table: TableRef, function_fqn: str, match_columns: list) -> PolicySpec: ...

    def plan_column_mask_policy(
        self, table: TableRef, column: str, function_fqn: str,
        mask_match_column: MatchColumn, extra_match_columns: Optional[list] = None,
    ) -> PolicySpec: ...


class TableBasedPolicyStrategy:
    """Default strategy (§7.3): one ROW_FILTER policy per table, one
    COLUMN_MASK policy per masked column, all `ON TABLE`-scoped. Policy
    names are securable-scoped (confirmed §17) so no cross-table
    collision risk from using the same deterministic names everywhere."""

    ROW_FILTER_POLICY_NAME = "abac_migrated_row_filter"

    def __init__(self, to_principals: Optional[list] = None):
        self.to_principals = to_principals or ["account users"]

    @classmethod
    def mask_policy_name(cls, column: str) -> str:
        return f"abac_migrated_mask_{column}"

    def plan_row_filter_policy(self, table: TableRef, function_fqn: str, match_columns: list) -> PolicySpec:
        return PolicySpec(
            policy_name=self.ROW_FILTER_POLICY_NAME,
            on_securable=f"TABLE {table.quoted_full_name}",
            policy_type="ROW_FILTER",
            function_fqn=function_fqn,
            match_columns=match_columns,
            using_columns=[mc.alias for mc in match_columns],
            mask_target_alias=None,
            to_principals=self.to_principals,
            comment="Migrated from legacy table-level row filter by abac_migration utility.",
        )

    def plan_column_mask_policy(
        self, table: TableRef, column: str, function_fqn: str,
        mask_match_column: MatchColumn, extra_match_columns: Optional[list] = None,
    ) -> PolicySpec:
        extra = extra_match_columns or []
        return PolicySpec(
            policy_name=self.mask_policy_name(column),
            on_securable=f"TABLE {table.quoted_full_name}",
            policy_type="COLUMN_MASK",
            function_fqn=function_fqn,
            match_columns=[mask_match_column] + extra,
            # confirmed empirically (§13/§17): USING COLUMNS only carries
            # *extra* function args beyond the masked value itself, which is
            # passed implicitly as arg 1 - omit entirely when there are none.
            using_columns=[mc.alias for mc in extra],
            mask_target_alias=mask_match_column.alias,
            to_principals=self.to_principals,
            comment=f"Migrated from legacy column mask on {column} by abac_migration utility.",
        )


class FunctionBasedPolicyStrategy:
    """Deferred v2 extension (§7.3 'Alternative'): one policy across many
    tables via ON SCHEMA/ON CATALOG. Requires tables/columns to already
    carry *consistent* governed tags, which cannot be assumed for a first
    migration pass - deliberately not implemented in v1. Selecting
    policy_strategy=FUNCTION_BASED fails loudly rather than silently doing
    something unsafe."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "FunctionBasedPolicyStrategy is a documented v2 extension point "
            "(DESIGN.md §7.3) and is not implemented. Use policy_strategy="
            "TABLE_BASED."
        )
