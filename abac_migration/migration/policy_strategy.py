"""PolicyStrategy: deterministic ABAC policy naming/targeting AND scope
(§7.3). Takes already-resolved MatchColumn objects (produced by
tag_provisioner, §7.4) and turns them into a full PolicySpec. Never
executes SQL itself - the gateway does that (§2: "Execute SQL directly" is
a Must-NOT here). Also owns every scope-specific decision plugins would
otherwise need to know about: the `ON <securable>` clause, the deterministic
policy name, and - critically - how to recover a previously-applied
policy's identity when the legacy artifact that used to reveal it (the
table's own `Row Filter`/`Column Mask` metadata) has already been removed
(idempotent reruns / VERIFY after a FINALIZE). Plugins (rls_to_abac.py,
mask_to_abac.py) call ONLY this Protocol - never build an `ON ...` clause
or a policy name themselves - which is what keeps them scope-agnostic.

Two concrete implementations, selected by `RunConfig.policy_scope`
(config/models.py `PolicyScope.TABLE` | `PolicyScope.CATALOG`), corresponding
1:1 to the two supported end-to-end workflows requested for this tool:

- **"Table level application"** (`TableBasedPolicyStrategy`, the pre-
  existing/default behavior): one ROW_FILTER policy `ON TABLE` per table,
  one COLUMN_MASK policy `ON TABLE` per masked column. Steps:
  1) identify legacy row filters/column masks, 2) create one governed tag
  per legacy function, 3) tag the governed column(s), 4) `CREATE POLICY
  ... ON TABLE <table>`, 5) manual-review resting state with BOTH the
  legacy mechanism and the new ABAC policy live (`Mode.APPLY_ABAC`), 6)
  remove the legacy mechanism (`Mode.FINALIZE`). No code changes were
  needed for this mode when CatalogBasedPolicyStrategy was added - it
  already implemented exactly this flow. `RunConfig.tag_team_prefix` DOES
  affect this mode's own policy names too (not just its governed tag keys):
  the previously-constant `abac_migrated_row_filter` /
  `abac_migrated_mask_<column>` become
  `abac_<team_prefix>_migrated_row_filter` /
  `abac_<team_prefix>_migrated_mask_<column>` whenever a non-empty
  team_prefix is configured, so every object (tag AND policy) one team's
  run creates is consistently namespaced, regardless of policy_scope.
- **"Catalog level application"** (`CatalogBasedPolicyStrategy`, new): steps
  1-3 identical (still one governed tag per legacy function, still applied
  directly to the governed table's own column(s) - tagging is scope-
  independent). Step 4 changes: ONE policy per legacy function, `ON
  CATALOG <catalog-the-function-and-its-tables-live-in>` instead of one
  policy per table - covering every tagged table in that catalog at once,
  since `MATCH COLUMNS has_tag(...)`/`has_tag_value(...)` (§7.4) already
  matches by tag, not by which securable the policy happens to be
  attached to. Steps 5-6 are otherwise identical (manual-review resting
  state, then remove legacy). Trades "one policy per table" (more objects,
  each individually reviewable/droppable) for "one policy per function"
  (far fewer objects for a large migration, but a single policy now
  governs many tables at once - review/rollback is at the function level).
"""
from __future__ import annotations

from typing import NamedTuple, Optional, Protocol

from ..uc_gateway.gateway import UnityCatalogGateway
from ..uc_gateway.models import MatchColumn, PolicyDefinition, PolicySpec, TableRef, quote_ident
from .tag_provisioner import sanitize_name_part, tag_key_for_function

# Governed-tag-key prefixes (tag_provisioner.py's `tag_key_for_function`) -
# used by CatalogBasedPolicyStrategy to recognize "this column already
# carries a tag minted for one of our migrations" without needing to know
# the function name up front (see find_existing_row_filter_policy/
# find_existing_mask_policies docstrings below).
_RLS_TAG_PREFIX = "abac_rls_"
_MASK_TAG_PREFIX = "abac_colmask_"


class ExistingMaskPolicy(NamedTuple):
    """One (column, already-applied COLUMN_MASK PolicyDefinition) pairing,
    as recovered by find_existing_mask_policies() - independent of scope,
    since a CATALOG-scoped mask policy can still be tied back to exactly
    one physical column via the governed tag that column carries."""
    column: str
    policy_def: PolicyDefinition


class PolicyStrategy(Protocol):
    to_principals: list
    except_principals: list
    team_prefix: str

    def on_securable_for(self, table: TableRef) -> str:
        """The `ON <securable>` clause (already backtick-quoted, e.g.
        ``TABLE `cat`.`sch`.`tbl` `` or ``CATALOG `cat` ``) every policy this
        strategy plans OR looks up for `table` lives on. The single method
        that encodes "table scope" vs "catalog scope" for every other
        gateway call a plugin makes (`describe_policy`/`drop_policy`/
        `show_policies` all now take this string, never a bare TableRef)."""
        ...

    def row_filter_policy_name(self, function_fqn: str) -> str:
        """Deterministic ROW_FILTER policy name for `function_fqn`. Ignores
        `function_fqn` for TABLE_BASED (constant, one securable per table);
        derived from it for CATALOG_BASED (one securable now shared by many
        functions, so the name itself must disambiguate them)."""
        ...

    def mask_policy_name(self, column: str, function_fqn: str) -> str:
        """Deterministic COLUMN_MASK policy name. TABLE_BASED keys purely on
        `column` (matches legacy behavior exactly - a mask policy is `ON
        TABLE`-scoped, and the same table could have two DIFFERENT mask
        functions on two different columns, each needing its own policy).
        CATALOG_BASED keys purely on `function_fqn` instead (`column` is
        ignored) - one CATALOG-scoped policy already covers every column,
        on every table in the catalog, that carries that function's mask
        tag, regardless of what each of those columns happens to be named."""
        ...

    def find_existing_row_filter_policy(
        self, table: TableRef, uc: UnityCatalogGateway,
    ) -> Optional[PolicyDefinition]:
        """Recovers a previously-applied ROW_FILTER PolicyDefinition for
        `table` WITHOUT requiring the legacy row filter to still be live
        (i.e. still works after Mode.FINALIZE removed it) - needed for
        idempotent reruns/VERIFY. None if nothing is found."""
        ...

    def find_existing_mask_policies(self, table: TableRef, uc: UnityCatalogGateway) -> list:
        """Same idea as find_existing_row_filter_policy, for column masks -
        returns list[ExistingMaskPolicy], one entry per already-migrated
        masked column found for `table`."""
        ...

    def plan_row_filter_policy(self, table: TableRef, function_fqn: str, match_columns: list) -> PolicySpec: ...

    def plan_column_mask_policy(
        self, table: TableRef, column: str, function_fqn: str,
        mask_match_column: MatchColumn, extra_match_columns: Optional[list] = None,
    ) -> PolicySpec: ...


class TableBasedPolicyStrategy:
    """"Table level application" (§7.3, default): one ROW_FILTER policy per
    table, one COLUMN_MASK policy per masked column, all `ON TABLE`-scoped.
    Policy names are securable-scoped (confirmed §17) so no cross-table
    collision risk from using the same deterministic names everywhere.

    `ROW_FILTER_POLICY_NAME`/`MASK_POLICY_PREFIX` are the original,
    still-in-effect-when-team_prefix-is-empty literal constants (kept as-is,
    unrenamed, for backward compatibility with anything referencing them
    directly, e.g. tests). When a non-empty `team_prefix` is configured,
    `_row_filter_policy_name_str()`/`_mask_policy_prefix_str()` instead
    namespace them: `abac_migrated_row_filter` ->
    `abac_<team_prefix>_migrated_row_filter`, `abac_migrated_mask_` ->
    `abac_<team_prefix>_migrated_mask_` (see module docstring) - sanitized
    via the same `sanitize_name_part()` tag_provisioner.py's
    tag_key_for_function uses, so one raw team_prefix value always produces
    one identical sanitized segment everywhere it's used across this tool."""

    ROW_FILTER_POLICY_NAME = "abac_migrated_row_filter"
    MASK_POLICY_PREFIX = "abac_migrated_mask_"

    def __init__(
        self, to_principals: Optional[list] = None, except_principals: Optional[list] = None,
        team_prefix: str = "",
    ):
        self.to_principals = to_principals or ["account users"]
        # Principals fully exempted from every policy this strategy plans
        # (`EXCEPT principal [, ...]`, confirmed live grammar) - e.g. a
        # service principal that runs unmasked ETL, or a break-glass admin
        # group. Empty by default, which omits the EXCEPT clause entirely
        # and preserves prior behavior exactly.
        self.except_principals = except_principals or []
        # Namespaces this strategy's own policy names too now (not just the
        # governed TAG keys TagProvisioner mints, which is shared/
        # independent of policy_scope, see tag_provisioner.py) - see
        # _row_filter_policy_name_str()/_mask_policy_prefix_str() below.
        self.team_prefix = team_prefix

    def on_securable_for(self, table: TableRef) -> str:
        return f"TABLE {table.quoted_full_name}"

    def _row_filter_policy_name_str(self) -> str:
        if not self.team_prefix:
            return self.ROW_FILTER_POLICY_NAME
        return f"abac_{sanitize_name_part(self.team_prefix)}_migrated_row_filter"

    def _mask_policy_prefix_str(self) -> str:
        if not self.team_prefix:
            return self.MASK_POLICY_PREFIX
        return f"abac_{sanitize_name_part(self.team_prefix)}_migrated_mask_"

    def row_filter_policy_name(self, function_fqn: str) -> str:
        del function_fqn  # constant regardless of function - see class docstring
        return self._row_filter_policy_name_str()

    def mask_policy_name(self, column: str, function_fqn: str = "") -> str:
        del function_fqn  # ignored - keyed purely on column, see class docstring
        return f"{self._mask_policy_prefix_str()}{column}"

    def find_existing_row_filter_policy(self, table: TableRef, uc: UnityCatalogGateway) -> Optional[PolicyDefinition]:
        return uc.describe_policy(self.on_securable_for(table), self._row_filter_policy_name_str())

    def find_existing_mask_policies(self, table: TableRef, uc: UnityCatalogGateway) -> list:
        on_securable = self.on_securable_for(table)
        mask_prefix = self._mask_policy_prefix_str()
        found = []
        for ref in uc.show_policies(on_securable):
            if ref.policy_type == "COLUMN_MASK" and ref.policy_name.startswith(mask_prefix):
                policy_def = uc.describe_policy(on_securable, ref.policy_name)
                if policy_def is not None:
                    column = ref.policy_name[len(mask_prefix):]
                    found.append(ExistingMaskPolicy(column=column, policy_def=policy_def))
        return found

    def plan_row_filter_policy(self, table: TableRef, function_fqn: str, match_columns: list) -> PolicySpec:
        return PolicySpec(
            policy_name=self.row_filter_policy_name(function_fqn),
            on_securable=self.on_securable_for(table),
            policy_type="ROW_FILTER",
            function_fqn=function_fqn,
            match_columns=match_columns,
            using_columns=[mc.alias for mc in match_columns],
            mask_target_alias=None,
            to_principals=self.to_principals,
            except_principals=self.except_principals,
            comment="Migrated from legacy table-level row filter by abac_migration utility.",
        )

    def plan_column_mask_policy(
        self, table: TableRef, column: str, function_fqn: str,
        mask_match_column: MatchColumn, extra_match_columns: Optional[list] = None,
    ) -> PolicySpec:
        extra = extra_match_columns or []
        return PolicySpec(
            policy_name=self.mask_policy_name(column, function_fqn),
            on_securable=self.on_securable_for(table),
            policy_type="COLUMN_MASK",
            function_fqn=function_fqn,
            # confirmed empirically (§13/§17): USING COLUMNS only carries
            # *extra* function args beyond the masked value itself, which is
            # passed implicitly as arg 1 - omit entirely when there are none.
            match_columns=[mask_match_column] + extra,
            using_columns=[mc.alias for mc in extra],
            mask_target_alias=mask_match_column.alias,
            to_principals=self.to_principals,
            except_principals=self.except_principals,
            comment=f"Migrated from legacy column mask on {column} by abac_migration utility.",
        )


class CatalogBasedPolicyStrategy:
    """"Catalog level application" (§7.3, new): one ROW_FILTER/COLUMN_MASK
    policy per distinct legacy FUNCTION (not per table, not per column),
    `ON CATALOG <catalog>` - the catalog the function and every table it
    used to govern already live in (functions are migrated in place, never
    moved to a different catalog). A single catalog-scoped policy already
    covers every table in that catalog whose column(s) carry the matching
    governed tag (§7.4's `has_tag()`/`has_tag_value()` MATCH COLUMNS
    mechanism is tag-driven, not securable-driven - `ON CATALOG` vs `ON
    TABLE` changes nothing about which *columns* the policy actually
    matches, only how many distinct policy objects exist and which single
    securable owns them).

    Policy name == the tag_provisioner.py governed tag KEY that function's
    migrated column(s) already carry (`tag_key_for_function`, e.g.
    `abac_rls_cat_sch_rf_region_both`) - reused verbatim rather than
    inventing a second parallel naming scheme. `CREATE POLICY` and `CREATE
    GOVERNED TAG` are different object kinds with separate namespaces (no
    collision), and reusing the identical string gives free 1:1
    traceability: `DESCRIBE POLICY abac_rls_cat_sch_rf_region_both ON
    CATALOG cat` and `DESCRIBE GOVERNED TAG abac_rls_cat_sch_rf_region_both`
    are always talking about the same one migrated function.

    Trade-off vs TABLE_BASED, called out explicitly since it's a real
    operational difference: every table sharing a function's policy calls
    `create_or_replace_policy()` with an equivalent (same policy_name/
    on_securable/function_fqn/principals - only the `MATCH COLUMNS` alias
    variable name may differ cosmetically per call) statement - harmless
    (idempotent CREATE OR REPLACE) but means N nearly-identical DDL calls
    for a function shared by N tables in one run, not de-duplicated within
    the run. Left as a documented v1 limitation rather than adding cross-
    table coordination to table_converter.py, which is deliberately
    per-table-independent/parallelizable (§2) today.
    """

    def __init__(
        self, to_principals: Optional[list] = None, except_principals: Optional[list] = None,
        team_prefix: str = "",
    ):
        self.to_principals = to_principals or ["account users"]
        self.except_principals = except_principals or []
        # Must be the same value tag_provisioner.py's TagProvisioner was
        # constructed with for this run (both come from
        # RunConfig.tag_team_prefix via migration_engine.build_policy_strategy
        # / _run_migration) - this is what keeps the CATALOG-scoped policy
        # name identical to the governed tag key it reuses (class docstring).
        self.team_prefix = team_prefix

    def on_securable_for(self, table: TableRef) -> str:
        return f"CATALOG {quote_ident(table.catalog)}"

    def row_filter_policy_name(self, function_fqn: str) -> str:
        return tag_key_for_function(function_fqn, "row_filter", self.team_prefix)

    def mask_policy_name(self, column: str, function_fqn: str) -> str:
        del column  # deliberately ignored - keyed purely on function, see class docstring
        return tag_key_for_function(function_fqn, "mask", self.team_prefix)

    def _tag_prefix_for_table(self, role_prefix: str, table: TableRef) -> str:
        """Reconstructs the deterministic, (team_prefix, catalog, schema)-
        scoped PREFIX `tag_key_for_function()` would produce for ANY
        function in this table's own catalog/schema under THIS strategy's
        `team_prefix` - everything up to (but not including) the final
        `<function_name>` segment, which this recovery path doesn't know
        yet (that's the whole point of it - see docstring below). Used to
        filter `list_column_tags()` candidates precisely (REVISED - was
        `tag.tag_key.startswith(_RLS_TAG_PREFIX)`, the bare role prefix
        with no team/catalog/schema scoping at all): confirmed live, a
        `team_prefix="mobility"` run's discovery must never match a
        DIFFERENT team's (or a no-prefix run's) leftover column tag merely
        because it also starts with `abac_rls_` - the #1 practical failure
        mode the bare-prefix check had. A no-prefix run's discovery is
        still not perfectly immune to matching a *different* team's
        prefixed tag whose sanitized team segment happens to collide with
        this catalog/schema's own sanitized name, but requiring the full
        catalog+schema match (not just the role prefix) makes that
        vanishingly unlikely in practice, and is the most precise filter
        possible without already knowing the function name."""
        parts = [self.team_prefix, table.catalog, table.schema] if self.team_prefix else [table.catalog, table.schema]
        return role_prefix + "_".join(sanitize_name_part(p) for p in parts if p) + "_"

    def find_existing_row_filter_policy(self, table: TableRef, uc: UnityCatalogGateway) -> Optional[PolicyDefinition]:
        """Legacy row filter (if any) already tells us the function up
        front (rf.function_fqn), so this recovery path is ONLY needed once
        Mode.FINALIZE has removed it - the table's own governed column tags
        (always applied directly to the table regardless of policy scope,
        §7.4) are the only remaining signal for "which function used to
        govern this table", since the tag KEY itself deterministically
        encodes catalog+schema+function_name (tag_provisioner.py)."""
        on_securable = self.on_securable_for(table)
        prefix = self._tag_prefix_for_table(_RLS_TAG_PREFIX, table)
        for tag in uc.list_column_tags(table):
            if tag.tag_key.startswith(prefix):
                policy_def = uc.describe_policy(on_securable, tag.tag_key)
                if policy_def is not None:
                    return policy_def
        return None

    def find_existing_mask_policies(self, table: TableRef, uc: UnityCatalogGateway) -> list:
        on_securable = self.on_securable_for(table)
        prefix = self._tag_prefix_for_table(_MASK_TAG_PREFIX, table)
        found = []
        policy_def_cache: dict = {}
        for tag in uc.list_column_tags(table):
            if not tag.tag_key.startswith(prefix):
                continue
            if tag.tag_key not in policy_def_cache:
                policy_def_cache[tag.tag_key] = uc.describe_policy(on_securable, tag.tag_key)
            policy_def = policy_def_cache[tag.tag_key]
            if policy_def is not None:
                found.append(ExistingMaskPolicy(column=tag.column, policy_def=policy_def))
        return found

    def plan_row_filter_policy(self, table: TableRef, function_fqn: str, match_columns: list) -> PolicySpec:
        return PolicySpec(
            policy_name=self.row_filter_policy_name(function_fqn),
            on_securable=self.on_securable_for(table),
            policy_type="ROW_FILTER",
            function_fqn=function_fqn,
            match_columns=match_columns,
            using_columns=[mc.alias for mc in match_columns],
            mask_target_alias=None,
            to_principals=self.to_principals,
            except_principals=self.except_principals,
            comment=(
                f"Migrated (CATALOG-scoped) from legacy row filter {function_fqn} "
                "by abac_migration utility."
            ),
        )

    def plan_column_mask_policy(
        self, table: TableRef, column: str, function_fqn: str,
        mask_match_column: MatchColumn, extra_match_columns: Optional[list] = None,
    ) -> PolicySpec:
        extra = extra_match_columns or []
        return PolicySpec(
            policy_name=self.mask_policy_name(column, function_fqn),
            on_securable=self.on_securable_for(table),
            policy_type="COLUMN_MASK",
            function_fqn=function_fqn,
            match_columns=[mask_match_column] + extra,
            using_columns=[mc.alias for mc in extra],
            mask_target_alias=mask_match_column.alias,
            to_principals=self.to_principals,
            except_principals=self.except_principals,
            comment=(
                f"Migrated (CATALOG-scoped) from legacy column mask {function_fqn} "
                "by abac_migration utility."
            ),
        )
