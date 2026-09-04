"""For each TableRef in scope, combine discovery results + existing-ABAC-
policy discovery + eligibility rules (§7.5) into an InventoryRecord (§2,
§4.1). Never decides *how* to migrate - that's the plugins'/table_converter's
job.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from ..config.models import DEFAULT_PII_LLM_ENDPOINT
from ..discovery.mask_discovery import discover_column_masks
from ..discovery.rls_discovery import discover_row_filter
from ..migration.policy_strategy import PolicyStrategy, TableBasedPolicyStrategy
from ..uc_gateway.gateway import UCGatewayError, UnityCatalogGateway, is_permission_denied
from ..uc_gateway.models import TableRef

# §16 item 2: exact minimum supported table types per target DBR remain to
# be confirmed; conservatively only MANAGED/EXTERNAL are treated as
# eligible until that's verified, per the UNSUPPORTED_TABLE_TYPE guard in §7.5.
SUPPORTED_TABLE_TYPES = frozenset({"MANAGED", "EXTERNAL"})


@dataclass(frozen=True)
class InventoryRecord:
    run_id: str
    inventoried_at: dt.datetime
    catalog: str
    schema: str
    table: str
    full_name: str
    table_type: str
    has_row_filter: bool
    row_filter_function: Optional[str]
    row_filter_columns: list
    row_filter_expression_text: str
    has_column_masks: bool
    column_masks: list  # list[{"column": str, "function": str}]
    has_existing_abac_policy: bool
    existing_abac_policy_names: list
    migration_eligibility: str  # "ELIGIBLE" | "NOT_ELIGIBLE"
    eligibility_reason: Optional[str] = None
    current_migration_status: Optional[str] = None
    # Best-effort LLM classification of the legacy function's likely PII
    # category, from its name + governed column(s) alone (§ inventory LLM
    # tagging). None when disabled, not applicable (no legacy security), or
    # the classification call itself failed - never blocks/affects eligibility.
    row_filter_suggested_pii_tag: Optional[str] = None
    column_mask_suggested_pii_tags: dict = field(default_factory=dict)  # {column: tag}


def build_inventory_record(
    table: TableRef, uc: UnityCatalogGateway, run_id: str,
    policy_strategy: Optional[PolicyStrategy] = None,
    enable_llm_pii_tagging: bool = False,
    pii_llm_endpoint: str = DEFAULT_PII_LLM_ENDPOINT,
) -> InventoryRecord:
    strategy = policy_strategy or TableBasedPolicyStrategy()
    try:
        state = uc.describe_table_security(table)
        row_filter = discover_row_filter(table, uc)
        column_masks = discover_column_masks(table, uc)
        # Scope-aware existing-ABAC-policy detection (§7.3): delegates to
        # the strategy rather than a bare `uc.show_policies(table)` so this
        # is precise for BOTH "table level application" (direct, ON TABLE
        # policies only) AND "catalog level application" (recovered via
        # this table's own governed column tags, since a CATALOG-scoped
        # policy is never directly "on" any one table - see
        # CatalogBasedPolicyStrategy.find_existing_*_policy docstrings).
        existing_row_filter = strategy.find_existing_row_filter_policy(table, uc)
        existing_masks = strategy.find_existing_mask_policies(table, uc)
        existing_policy_names = (
            ([existing_row_filter.name] if existing_row_filter is not None else [])
            + [m.policy_def.name for m in existing_masks]
        )
    except UCGatewayError as exc:
        if not is_permission_denied(exc):
            raise
        # A catalog/schema can be listable (SHOW SCHEMAS/TABLES only needs
        # USE CATALOG/USE SCHEMA) while an individual table still denies
        # SELECT/MODIFY to this identity - don't let one inaccessible table
        # abort inventory for every other table in scope.
        return InventoryRecord(
            run_id=run_id, inventoried_at=dt.datetime.utcnow(),
            catalog=table.catalog, schema=table.schema, table=table.table, full_name=table.full_name,
            table_type="UNKNOWN", has_row_filter=False, row_filter_function=None, row_filter_columns=[],
            row_filter_expression_text="", has_column_masks=False, column_masks=[],
            has_existing_abac_policy=False, existing_abac_policy_names=[],
            migration_eligibility="NOT_ELIGIBLE", eligibility_reason="PERMISSION_DENIED",
        )

    eligibility, reason = _evaluate_eligibility(state, existing_policy_names)

    row_filter_pii_tag = None
    column_mask_pii_tags: dict = {}
    if enable_llm_pii_tagging:
        row_filter_pii_tag, column_mask_pii_tags = _suggest_pii_tags(
            uc, row_filter, column_masks, pii_llm_endpoint,
        )

    return InventoryRecord(
        run_id=run_id,
        inventoried_at=dt.datetime.utcnow(),
        catalog=table.catalog, schema=table.schema, table=table.table, full_name=table.full_name,
        table_type=state.table_type,
        has_row_filter=row_filter is not None,
        row_filter_function=row_filter.function_fqn if row_filter else None,
        row_filter_columns=row_filter.using_columns if row_filter else [],
        row_filter_expression_text=row_filter.raw_text if row_filter else "",
        has_column_masks=len(column_masks) > 0,
        column_masks=[{"column": m.column, "function": m.function_fqn} for m in column_masks],
        has_existing_abac_policy=len(existing_policy_names) > 0,
        existing_abac_policy_names=existing_policy_names,
        migration_eligibility=eligibility,
        eligibility_reason=reason,
        row_filter_suggested_pii_tag=row_filter_pii_tag,
        column_mask_suggested_pii_tags=column_mask_pii_tags,
    )


def _suggest_pii_tags(uc: UnityCatalogGateway, row_filter, column_masks: list, endpoint: str):
    """One ai_query() call per distinct legacy function found on the table -
    never raises, never blocks inventory (see PiiSuggestion/suggest_pii_tag).
    Two functions on the same table (row filter + a mask) are classified
    independently since they may protect entirely different data."""
    row_filter_tag = None
    if row_filter is not None:
        suggestion = uc.suggest_pii_tag(row_filter.function_fqn, row_filter.using_columns, endpoint)
        row_filter_tag = suggestion.tag

    mask_tags = {}
    for mask in column_masks:
        suggestion = uc.suggest_pii_tag(mask.function_fqn, [mask.column], endpoint)
        if suggestion.tag:
            mask_tags[mask.column] = suggestion.tag
    return row_filter_tag, mask_tags


def _evaluate_eligibility(state, existing_policies):
    """Table-level gate deliberately only covers conditions that are true of
    the WHOLE table (nothing to migrate at all / unsupported table type).

    Per-object conditions - a missing/inaccessible function for one mask,
    or an existing conflicting policy for one object - are intentionally
    NOT evaluated here anymore, even though they used to be (§7.5 originally
    listed them as table-level NOT_ELIGIBLE reasons). A live end-to-end run
    against a real workspace showed why that was wrong: `table_converter`'s
    plugins already evaluate exactly these conditions per row-filter/per
    masked-column and report a granular FAILED/NOT_ELIGIBLE result for that
    one object without blocking siblings (the "weakest link" behavior unit
    -tested by scenario 11 and documented on ColumnMaskMigrationPlugin).
    Pre-filtering the WHOLE table out here for a single bad column silently
    skipped the other, perfectly migratable columns/row-filter and produced
    no audit trail at all for the bad one - worse than letting `convert()`
    report it. Table-level eligibility now only decides whether the table
    is attempted; per-object outcomes are entirely the plugins' call.

    `existing_policies` is checked too (second regression found by the same
    live run): a table that was ALREADY successfully migrated has no legacy
    row filter/masks left at all by definition, so `has_row_filter`/
    `has_column_masks` are both False - without this check every already
    -migrated table would wrongly become NOT_ELIGIBLE on a rerun and be
    silently dropped from the scope entirely, instead of being attempted and
    correctly resolving to ALREADY_MIGRATED (mirrors `applies_to()` on both
    plugins, which already consider a matching existing policy "applicable").
    """
    if not state.has_row_filter and not state.has_column_masks and not existing_policies:
        return "NOT_ELIGIBLE", "NO_LEGACY_SECURITY_FOUND"

    if state.table_type not in SUPPORTED_TABLE_TYPES:
        return "NOT_ELIGIBLE", "UNSUPPORTED_TABLE_TYPE"

    return "ELIGIBLE", None
