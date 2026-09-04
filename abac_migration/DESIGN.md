# Databricks ABAC Migration Utility — Design Document

Status: **DESIGN — not yet implemented.** Implementation begins only after this
document is reviewed and approved.

Scope: migrates existing Unity Catalog **table-level Row Filters (RLS)** and
**table-level Column Masks** to Unity Catalog **ABAC policies**
(`CREATE POLICY ... ROW FILTER|COLUMN MASK ...`), across one workspace's
metastore at a time (the utility is workspace-agnostic — it is deployed as a
notebook/job into whichever workspace's catalogs need migrating, e.g.
`uc_source` or `uc_target`).

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Notebook (thin entry point)                                        │
│  abac_migration_driver.py                                           │
│  - reads job/notebook widget parameters                             │
│  - builds RunConfig, calls Orchestrator.run(config)                 │
│  - renders summary report                                           │
└───────────────────────────────┬───────────────────────────────────--┘
                                 │
                                 v
┌─────────────────────────────────────────────────────────────────────┐
│  Orchestrator (migration/migration_engine.py)                       │
│  Resolve Scope → Discover Tables → Inventory → Filter Eligible →    │
│  For each table: TableConversionEngine → Validate → Persist →       │
│  Generate Summary                                                    │
└───┬─────────────┬─────────────┬─────────────┬─────────────┬────────┘
    │             │             │             │             │
    v             v             v             v             v
┌────────┐  ┌───────────┐  ┌──────────┐  ┌───────────┐ ┌───────────┐
│ Scope  │  │ Discovery │  │Inventory │  │ Migration │ │  Audit /  │
│Resolver│  │  Layer    │  │ Manager  │  │  Engine   │ │ Rollback  │
└────────┘  └───────────┘  └──────────┘  └─────┬─────┘ └───────────┘
                                                │
                                                v
                                   ┌─────────────────────────┐
                                   │  TableConversionEngine   │
                                   │  (migration/table_       │
                                   │   converter.py)          │
                                   │  invokes applicable      │
                                   │  MigrationPlugins        │
                                   └─────┬──────────┬────────┘
                                         │          │
                                         v          v
                              ┌──────────────┐ ┌──────────────────┐
                              │RLSMigration  │ │ColumnMaskMigration│
                              │Plugin        │ │Plugin             │
                              └──────────────┘ └──────────────────┘
```

**Layering rules** (enforced, not just suggested):

- `scope/` never imports from `migration/` or `discovery/` — it only knows
  how to turn a scope spec into a flat list of `TableRef`s.
- `discovery/` never mutates Unity Catalog. Read-only.
- `migration/plugins/*` never call the orchestrator or know about "scope" —
  they only operate on one `TableRef` plus injected clients.
- `audit/` and `rollback/` depend only on the data model (§4), not on plugin
  internals.
- The **only** hard dependency the lowest-level converter has is a UC client
  interface (`UnityCatalogGateway`, see §5), which is mockable — this is what
  makes `convert_table()` unit-testable in isolation (§14).

---

## 2. Component Responsibilities

| Component | Responsibility | Must NOT do |
|---|---|---|
| `config/config_loader.py` | Parse/validate notebook widgets or a config dict into a typed `RunConfig`. Apply defaults. | Talk to UC. Know about plugins. |
| `scope/scope_resolver.py` | Expand `scope_type` + `catalogs`/`schemas`/`tables`/`exclude_schema_regex` into a concrete `list[TableRef]`. | Inspect table security config. Perform migration. |
| `discovery/catalog_discovery.py`, `schema_discovery.py`, `table_discovery.py` | List catalogs/schemas/tables (metadata only) to support scope expansion ("ALL catalogs", "ALL schemas in catalog X"). | Filter by eligibility (that's `inventory`'s job). |
| `discovery/rls_discovery.py` | For one table: does a table-level Row Filter exist? Which function, which columns? | Create/remove anything. |
| `discovery/mask_discovery.py` | For one table: which columns have Column Masks, and which functions? | Create/remove anything. |
| `inventory/inventory_manager.py` | For each `TableRef` in scope, combine discovery results + existing-ABAC-policy discovery + eligibility rules into an `InventoryRecord`. | Decide *how* to migrate. |
| `inventory/inventory_repository.py` | Persist/read `InventoryRecord`s to the audit catalog. | Contain business logic. |
| `migration/table_converter.py` | The pluggable `convert_table(table_ref, options) -> ConversionResult` entry point. Orchestrates plugin `discover→validate→convert→verify` (+`rollback` on request), enforces the safety ordering (§8), independent of catalog-wide orchestration. | Know about scope, parallelism, or other tables. |
| `migration/plugins/base_plugin.py` | `MigrationPlugin` abstract interface (§5). | — |
| `migration/plugins/rls_to_abac.py` | `RLSMigrationPlugin`: discover/validate/convert/verify/rollback for the RLS→ABAC conversion of one table. | Touch column masks. |
| `migration/plugins/mask_to_abac.py` | `ColumnMaskMigrationPlugin`: same, for one or more masked columns. | Touch row filters. |
| `migration/policy_strategy.py` | `PolicyStrategy` abstraction: deterministic ABAC policy naming/targeting (§7.3). | Execute SQL directly (returns a spec; the plugin executes it). |
| `migration/migration_engine.py` | Top-level orchestrator: scope → inventory → eligible filter → parallel dispatch to `table_converter` → summary. Implements `continue_on_error`, `max_parallelism`. | Contain per-table conversion logic (delegates to `table_converter`). |
| `validation/pre_validation.py` | Function-exists/accessible/compatible checks *before* any UC mutation. | Mutate UC. |
| `validation/post_validation.py` | Confirms applied ABAC policy matches expected spec, confirms old RLS/mask removed. | — |
| `validation/drift_detection.py` | Compares audit table's last-known state vs live UC state for `RECONCILE` mode. | Mutate UC (unless explicitly asked to "heal" — out of scope for v1; report only). |
| `rollback/rollback_manager.py` | Given `rollback_metadata`, restores original RLS/mask and removes only the ABAC policy this utility created. | Touch any policy/RLS/mask not recorded as created by this utility. |
| `audit/audit_repository.py` | Append-only writes/reads of `MigrationAuditRecord` rows. | Be treated as source of truth for current UC state (UC always is — see §7). |
| `notebook/abac_migration_driver` | Thin entrypoint: widgets → `RunConfig` → `MigrationEngine.run()` → render summary tables/visualizations. | Contain business logic beyond parameter marshalling and display. |

---

## 3. End-to-End Execution Flow

```
Resolve Scope                (scope_resolver)
   │  -> list[TableRef]
   v
Discover Tables              (discovery/*)   [skipped if scope already table-level]
   │
   v
Inventory                    (inventory_manager)
   │  -> list[InventoryRecord]  (persisted to audit_catalog.audit_schema.inventory_table)
   v
Filter Eligible Tables        (eligibility rules, §7.5)
   │
   v
Prepare Governed Tags          (§7.4 — serialized, single-threaded: batch-provision
   │                             every new tag value this run needs, once, before
   │                             any parallel per-table conversion begins)
   │  -> ELIGIBLE / NOT_ELIGIBLE (with reason)
   v
For each eligible table (parallel, max_parallelism workers):
   │
   v
TableConversionEngine.convert_table(table_ref, options)
   │
   ├── RLSMigrationPlugin.discover/validate/convert/verify   (if RLS present)
   ├── ColumnMaskMigrationPlugin.discover/validate/convert/verify  (if masks present, per column)
   │
   v
ConversionResult  (SUCCESS | FAILED | SKIPPED | ALREADY_MIGRATED | NOT_ELIGIBLE)
   │
   v
Persist Result                (audit_repository — one MigrationAuditRecord per table per run)
   │
   v
Generate Summary               (counts by status, rendered in notebook + returned as dict/JSON)
```

Mode behavior over this same flow:

| Mode | Discover/Inventory | Filter Eligible | Convert | Validate | Persist |
|---|---|---|---|---|---|
| `INVENTORY` | yes | yes (for reporting) | no | no | inventory only |
| `MIGRATE` | yes (needed for conversion) | yes | yes (`phase=FULL`: create ABAC + remove legacy) | yes | inventory + audit |
| `INVENTORY_AND_MIGRATE` | yes | yes | yes (`phase=FULL`) | yes | inventory + audit |
| `APPLY_ABAC` | yes | yes | yes (`phase=APPLY_ABAC`: create+verify ABAC only, legacy left alone) | yes | inventory + audit (`migration_phase=ABAC_APPLIED`) |
| `FINALIZE` | yes | yes | yes (`phase=FINALIZE`: remove legacy only, requires ABAC already applied) | yes | audit (`migration_phase=FINALIZED`) |
| `VERIFY` | yes | n/a | no | yes (compare expected-vs-actual for already-migrated tables; reports `ABAC_APPLIED` distinctly, not `FAILED`, when both mechanisms are legitimately still present) | audit (validation_status update) |
| `RECONCILE` | yes | n/a | no | drift check only | audit (drift flags) |
| `ROLLBACK` | reads audit's `rollback_metadata` | n/a | rollback only | post-rollback validation | audit |

### 3.1 Isolated-phase modes: `APPLY_ABAC` / `FINALIZE`

Added to let a migration be run as separate, independently-schedulable
steps (e.g. as 3 separate Databricks Jobs: Inventory → Apply ABAC →
Finalize) instead of one atomic `MIGRATE`, with an explicit non-final
resting state in between that a human can review before legacy security is
ever removed:

- `APPLY_ABAC` runs the exact same discover/validate/tag-prepare pipeline
  as `MIGRATE`, but `table_converter.convert_table(..., phase="APPLY_ABAC")`
  stops right after the new ABAC policy is created and verified — it never
  calls `drop_row_filter`/`drop_column_mask`. Per-object status is
  `StepStatus.ABAC_APPLIED` (or `WOULD_APPLY_ABAC` under `dry_run=true`),
  persisted with `migration_phase=ABAC_APPLIED` in `migration_audit` — the
  explicit "not final" marker requested for this state.
- `FINALIZE` skips tag preparation entirely (nothing it does ever needs a
  `MatchColumn` — it only removes things) and calls
  `convert_table(..., phase="FINALIZE")`, which requires
  `PlannedObject.abac_already_applied=True` (set by the plugin's `validate()`
  when a deterministically-named ABAC policy matching the legacy function
  already exists) before it will remove anything; otherwise it reports
  `NOT_ELIGIBLE`/`ABAC_NOT_APPLIED_YET`. On success, `StepStatus.SUCCESS`
  with `migration_phase=FINALIZED`.

Both phases share the exact same `RLSMigrationPlugin`/`ColumnMaskMigrationPlugin`
`validate()` logic as the atomic path (no duplicated eligibility/conflict
rules) — only `convert()`'s dispatch on `ConvertOptions.phase` differs. This
guarantees `APPLY_ABAC` followed by `FINALIZE` reaches the identical live-UC
end state as one atomic `MIGRATE` call.

`dry_run=true` short-circuits `TableConversionEngine` immediately before any
mutating call (`convert()`/`rollback()`), producing a `WOULD_MIGRATE` /
`WOULD_ROLLBACK` result with the same shape as a real result, so the summary
report and audit trail are structurally identical whether or not changes were
actually applied (see §9).

---

## 4. Data Model (Inventory / Audit / Rollback)

All three tables live under a **dedicated, configurable audit catalog/schema**
(default suggestion: `abac_migration.audit`, overridable via
`audit_catalog`/`audit_schema`/`audit_table` parameters — never hard-coded).

### 4.1 `inventory` table (one row per table per inventory run — append-only, keyed by `(run_id, catalog, schema, table)`)

| Column | Type | Notes |
|---|---|---|
| `run_id` | STRING | UUID for this orchestrator invocation |
| `inventoried_at` | TIMESTAMP | |
| `catalog` | STRING | |
| `schema` | STRING | |
| `table` | STRING | |
| `full_name` | STRING | `catalog.schema.table`, convenience column |
| `table_type` | STRING | `MANAGED`, `EXTERNAL`, `VIEW`, `MATERIALIZED_VIEW`, `STREAMING_TABLE` |
| `has_row_filter` | BOOLEAN | |
| `row_filter_function` | STRING | fully qualified, nullable |
| `row_filter_columns` | ARRAY<STRING> | `USING COLUMNS` input columns, nullable |
| `row_filter_expression_text` | STRING | raw `DESCRIBE TABLE EXTENDED` text, for audit readability |
| `has_column_masks` | BOOLEAN | |
| `column_masks` | ARRAY<STRUCT<column STRING, function STRING>> | one entry per masked column |
| `has_existing_abac_policy` | BOOLEAN | any policy already attached directly to this table |
| `existing_abac_policy_names` | ARRAY<STRING> | |
| `migration_eligibility` | STRING | `ELIGIBLE` / `NOT_ELIGIBLE` |
| `eligibility_reason` | STRING | nullable, populated when not eligible |
| `current_migration_status` | STRING | looked up from latest audit record, nullable if never attempted |
| `row_filter_suggested_pii_tag` | STRING | nullable, LLM-suggested PII category for the row-filter function (`enable_llm_pii_tagging`, §11); advisory only, never affects eligibility |
| `column_mask_suggested_pii_tags` | STRING (JSON-encoded `{column: tag}`) | nullable, same LLM suggestion per masked column |

### 4.2 `migration_audit` table (one row per table per **migration attempt** — append-only, so history is preserved across re-runs)

| Column | Type | Notes |
|---|---|---|
| `run_id` | STRING | |
| `attempt_id` | STRING | UUID, unique per table-conversion attempt (supports retries within a run) |
| `catalog`, `schema`, `table` | STRING | |
| `object_type` | STRING | `ROW_FILTER` \| `COLUMN_MASK` (one row per object being migrated — a table with 1 RLS + 3 masks yields 4 rows per attempt) |
| `masked_column` | STRING | nullable, only for `COLUMN_MASK` rows |
| `source_security_type` | STRING | `TABLE_ROW_FILTER` \| `TABLE_COLUMN_MASK` |
| `source_function` | STRING | fully qualified function used by the old mechanism |
| `source_definition` | STRING | raw text captured pre-migration (for rollback + audit) |
| `target_policy_name` | STRING | deterministic name from `PolicyStrategy` |
| `target_policy_type` | STRING | `ROW_FILTER` \| `COLUMN_MASK` |
| `target_definition` | STRING | the literal `CREATE POLICY ...` statement executed (or that *would* be executed, in dry-run) |
| `status` | STRING | `SUCCESS` \| `FAILED` \| `SKIPPED` \| `ALREADY_MIGRATED` \| `NOT_ELIGIBLE` \| `WOULD_MIGRATE` (dry-run, `MIGRATE`/`INVENTORY_AND_MIGRATE`) \| `ABAC_APPLIED` \| `WOULD_APPLY_ABAC` (dry-run, `APPLY_ABAC`) \| `WOULD_FINALIZE` (dry-run, `FINALIZE`) \| `DRIFT` (reconcile) \| `ROLLED_BACK` |
| `error_code` | STRING | nullable, taxonomy in §10 |
| `error_message` | STRING | nullable |
| `validation_status` | STRING | `PASSED` \| `PARTIAL` (`ABAC_APPLIED` — not final) \| `FAILED` \| `NOT_RUN` |
| `rollback_metadata` | STRING (JSON) | see §4.3 |
| `migration_phase` | STRING | `FINALIZED` \| `ABAC_APPLIED` (not final — legacy still present alongside the new ABAC policy) \| `DRY_RUN` \| `FAILED` \| `NOT_APPLICABLE`; derived from `status` (§3.1) — the coarse, audit-facing "is this final yet" signal requested for the isolated-phase modes, orthogonal to the more granular `status` |
| `started_at`, `completed_at` | TIMESTAMP | |
| `dry_run` | BOOLEAN | |

The **table-level** `ConversionResult` returned by `convert_table()` (§5)
aggregates the underlying per-object rows (1 row-filter + N mask rows) into a
single object-oriented summary for the caller, while the audit table keeps
the finer per-object granularity needed for partial-failure diagnostics
(e.g. RLS migrated fine, one of three masks failed).

### 4.3 `rollback_metadata` JSON shape (captured **before** any mutation, stored in `migration_audit.rollback_metadata`)

```json
{
  "captured_at": "2026-08-24T10:00:00Z",
  "table": "catalog.schema.table",
  "original_row_filter": {
    "function": "catalog.schema.rf_business_unit",
    "using_columns": ["business_unit"]
  },
  "original_column_masks": [
    {"column": "email", "function": "catalog.schema.mask_email"}
  ],
  "abac_policies_created_by_this_run": [
    {"policy_name": "abac_migrated_row_filter", "on_securable": "TABLE catalog.schema.table", "policy_type": "ROW_FILTER"},
    {"policy_name": "abac_migrated_mask_email", "on_securable": "TABLE catalog.schema.table", "policy_type": "COLUMN_MASK"}
  ]
}
```

Rollback only ever acts on the `abac_policies_created_by_this_run` list and
only ever *restores* the `original_*` block — it never touches any other
policy on the table (§8, §9).

### 4.4 "UC is the source of truth" corollary

Both tables are **append-only audit logs**, not mutable state. "Current
status of table X" is always computed as:
`latest row in migration_audit for (catalog,schema,table) ORDER BY completed_at DESC`
— but that computed status is only a *hint*; every mode re-verifies against
live UC state before acting (§7).

---

## 5. Plugin Interfaces (conceptual — Python typing, not final code)

```python
class TableRef(NamedTuple):
    catalog: str
    schema: str
    table: str

    @property
    def full_name(self) -> str: ...


class UnityCatalogGateway(Protocol):
    """The ONLY seam between plugins and Databricks. Fully mockable in tests."""
    def describe_table_security(self, table: TableRef) -> TableSecurityState: ...
    def show_policies(self, table: TableRef) -> list[PolicyRef]: ...
    def describe_policy(self, table: TableRef, policy_name: str) -> PolicyDefinition: ...
    def function_exists(self, function_fqn: str) -> bool: ...
    def can_execute_function(self, function_fqn: str) -> bool: ...
    def create_or_replace_policy(self, spec: PolicySpec, dry_run: bool) -> PolicyApplyResult: ...
    def drop_policy(self, table: TableRef, policy_name: str, dry_run: bool) -> None: ...
    def drop_row_filter(self, table: TableRef, dry_run: bool) -> None: ...
    def drop_column_mask(self, table: TableRef, column: str, dry_run: bool) -> None: ...
    def set_row_filter(self, table: TableRef, function_fqn: str, using_columns: list[str], dry_run: bool) -> None: ...
    def set_column_mask(self, table: TableRef, column: str, function_fqn: str, dry_run: bool) -> None: ...


class MigrationPlugin(Protocol):
    """One plugin type per security mechanism. Core engine never branches on
    "is this RLS or masks" — it just calls whichever plugins are applicable."""

    object_type: str  # "ROW_FILTER" | "COLUMN_MASK" | (future: "GRANT", ...)

    def applies_to(self, table: TableRef, uc: UnityCatalogGateway) -> bool:
        """Cheap check: does this table have this kind of legacy security?"""

    def discover(self, table: TableRef, uc: UnityCatalogGateway) -> DiscoveryResult:
        """Read current legacy config + any existing ABAC policy. No mutation."""

    def validate(self, table: TableRef, discovery: DiscoveryResult, uc: UnityCatalogGateway) -> ValidationResult:
        """Pre-migration checks: function exists/accessible/compatible;
        determine desired target policy spec via PolicyStrategy; compare
        against any existing ABAC policy to decide ALREADY_MIGRATED vs
        NEEDS_MIGRATION vs NOT_ELIGIBLE."""

    def convert(self, table: TableRef, validation: ValidationResult, uc: UnityCatalogGateway, options: ConvertOptions) -> ConversionStepResult:
        """Executes CREATE POLICY -> verify -> remove legacy mechanism, per
        the safety-ordered state machine in §8. Never called if validate()
        said ALREADY_MIGRATED or NOT_ELIGIBLE (idempotency short-circuit)."""

    def verify(self, table: TableRef, uc: UnityCatalogGateway) -> ValidationResult:
        """Post-migration / VERIFY-mode / RECONCILE-mode check that live UC
        state matches expected ABAC policy and legacy mechanism is gone."""

    def rollback(self, table: TableRef, rollback_metadata: dict, uc: UnityCatalogGateway, dry_run: bool) -> ConversionStepResult:
        """Restore original legacy mechanism; drop only the policies this
        run created (from rollback_metadata, never by re-deriving names)."""
```

```python
class PolicyStrategy(Protocol):
    """Determines the deterministic identity/name/target of the ABAC policy
    for a given legacy security object. Kept separate from the plugin so the
    naming/targeting approach can evolve without touching conversion logic."""

    def plan_row_filter_policy(self, table: TableRef, function_fqn: str, using_columns: list[str]) -> PolicySpec: ...
    def plan_column_mask_policy(self, table: TableRef, column: str, function_fqn: str) -> PolicySpec: ...


class PolicySpec(NamedTuple):
    policy_name: str
    on_securable: str          # e.g. "TABLE catalog.schema.table"
    policy_type: Literal["ROW_FILTER", "COLUMN_MASK"]
    function_fqn: str
    using_columns: list[str]
    mask_target_column: str | None   # only for COLUMN_MASK
    to_principals: list[str]         # see §7.3 — default "account users"
    except_principals: list[str]     # `TO ... EXCEPT ...` — default [] (no exemptions)
    comment: str
```

The engine invokes `table_converter.convert_table(table_ref, options)`,
which:

1. Builds `[RLSMigrationPlugin(), ColumnMaskMigrationPlugin()]` (extensible —
   a future `GrantPolicyMigrationPlugin` just gets added to this list; the
   engine does not change).
2. For each plugin where `applies_to()` is true: `discover → validate →
   (convert unless already-migrated/not-eligible/dry-run-short-circuit) →
   verify`.
3. Aggregates per-plugin `ConversionStepResult`s into one `ConversionResult`
   (§6 fields) for the table.

---

## 6. `ConversionResult` (return type of `convert_table`)

| Field | Type | |
|---|---|---|
| `status` | enum | `SUCCESS`, `FAILED`, `SKIPPED`, `ALREADY_MIGRATED`, `NOT_ELIGIBLE`, and (dry-run/reconcile specific) `WOULD_MIGRATE`, `DRIFT` |
| `table_name` | str | |
| `rls_status` | enum \| null | same status enum, scoped to the RLS plugin only |
| `column_mask_status` | dict[str, enum] | per masked column |
| `source_functions` | dict | `{"row_filter": "...", "column_masks": {"email": "...", ...}}` |
| `target_policies` | dict | `{"row_filter": "abac_migrated_row_filter", "column_masks": {"email": "abac_migrated_mask_email", ...}}` |
| `error_code` | str \| null | |
| `error_message` | str \| null | |
| `started_at` / `completed_at` | datetime | |
| `validation_status` | enum | `PASSED` / `FAILED` / `NOT_RUN` |
| `rollback_metadata` | dict | §4.3 shape |

Table-level `status` is derived from the per-plugin statuses with a
"weakest link" rule: if any applicable plugin `FAILED`, table status is
`FAILED` even if the other plugin `SUCCESS`, but **each plugin's own work
that already succeeded is not rolled back automatically** — it's left in a
partially-migrated, self-consistent state (RLS successfully on ABAC, masks
still legacy) and flagged for operator attention; `continue_on_error` governs
whether the *run* continues to the *next table*, not whether one table's
independent plugin results affect each other.

---

## 7. Idempotency Strategy

1. **UC is always re-queried**, never trusted from the audit table, before
   any mutating action (`validate()` step, always live).
2. `validate()` computes:
   - `desired = policy_strategy.plan_*_policy(...)`
   - `actual = uc.show_policies(table)` (+ `describe_policy` for the specific
     name if present)
   - If `actual` policy exists **and** its `function_name`/`using_columns`/
     `on_column` match `desired` **and** the corresponding legacy mechanism
     is already gone → `ALREADY_MIGRATED` (no-op, `convert()` skipped).
   - If `actual` policy exists but differs from `desired` → `NOT_ELIGIBLE`
     with reason `EXISTING_ABAC_POLICY_CONFLICT` (never silently overwritten
     — a human must resolve; this directly satisfies "never delete or modify
     an unrelated pre-existing ABAC policy").
   - If no legacy RLS/mask and no ABAC policy → `NOT_ELIGIBLE`, reason
     `NO_LEGACY_SECURITY_FOUND` (nothing to migrate).
   - Otherwise → proceed to `convert()`.
3. **Policy naming is deterministic** (via `PolicyStrategy`, §7.3), so a
   second run resolves the *same* name and finds it already applied instead
   of creating a duplicate — this is the concrete mechanism behind
   "never blindly create duplicate policies."

### 7.3 Default Policy Strategy: `TableBasedPolicyStrategy` (REVISED after §17 spike)

**This section was rewritten after empirically testing every statement below
(§17) — the original assumption that `USING COLUMNS`/`ON COLUMN` could
reference a table's literal column name directly, with no tag dependency,
turned out to be *false*. Confirmed finding:** `MATCH COLUMNS` is
**mandatory** whenever `USING COLUMNS` or `ON COLUMN` is used, and `MATCH
COLUMNS` conditions **only support `has_tag()`/`has_tag_value()`** — there is
no way to match a column by its literal name. This means **governed tags are
not optional** for this migration utility; provisioning a tag per migrated
column/row-filter-argument is a required step in the conversion pipeline,
not an edge case.

Revised strategy:

- One `ROW_FILTER` policy per table, named deterministically, e.g.
  `abac_migrated_row_filter` (confirmed: policy names are scoped to the
  securable they're defined on, so no cross-table naming collision risk).
- One `COLUMN_MASK` policy **per masked column**, e.g.
  `abac_migrated_mask_<column>`.
- For **each** column referenced by the legacy row filter's `USING COLUMNS`
  or by a legacy column mask, the plugin must ensure a governed tag
  key/value pair uniquely identifies that column **within its own table's
  scope** (uniqueness only needs to hold per-table, since the policy itself
  is `ON TABLE`-scoped — confirmed empirically, §17), then reference it via
  `MATCH COLUMNS has_tag_value(key, value) AS alias`.
- Column mask functions that take exactly one parameter (the column value)
  must **omit** `USING COLUMNS` entirely — the column's value is passed
  implicitly as the first argument. `USING COLUMNS` is only for *additional*
  function parameters beyond the masked value (confirmed empirically: a
  1-arg mask function errors with an argument-count mismatch if
  `USING COLUMNS` is supplied redundantly).
- `TO principal` defaults to a configurable broad principal (default
  `` `account users` ``) **because the actual access restriction logic lives
  inside the existing security function** (e.g. our own `rf_business_unit`
  internally checks group membership) — the policy's `TO` clause is not used
  as the restriction mechanism, matching "do not change the underlying
  security function" and "preserve existing security semantics" exactly.
  Empirically confirmed to produce identical effective results to the
  original table-level RLS/mask for the same querying identity (§17).
- `EXCEPT principal [, ...]` is an optional, empty-by-default addition to
  the same `TO` clause — the `CREATE POLICY` grammar supports it on both
  `row_filter_body` and `column_mask_body`
  (`TO principal [, ...] [EXCEPT principal [, ...]]`; docs example:
  `TO 'All Users' EXCEPT 'HR admins'`). Configured via
  `policy_except_principals` (RunConfig/job parameter, JSON array). Listed
  principals are **fully exempt** — they see unfiltered/unmasked data,
  regardless of what the underlying security function would otherwise
  compute — the intended use case being a service principal that must run
  unmasked ETL, or a break-glass admin group. Left empty, the `EXCEPT`
  clause is omitted entirely and the generated SQL is byte-for-byte
  unchanged from before this option existed.

### 7.4 Governed Tag Provisioning (new required sub-component: `migration/tag_provisioner.py`)

Because `MATCH COLUMNS` can only match on governed tags, the pipeline gains
a step between `validate()` and `convert()`'s `CREATE POLICY` call:

1. **Prefer reuse over creation.** Before minting a new tag, check whether
   an existing governed tag/value *already* uniquely identifies the target
   column within its table (query
   `information_schema.column_tags` / `SHOW GOVERNED TAGS`). Databricks ships
   built-in classification governed tags out of the box (confirmed via
   `SHOW GOVERNED TAGS` in §17 — e.g. `class.email_address`, `class.us_ssn`,
   `class.credit_card`, `class.phone_number`, ...), and a workspace may also
   have already run automated Data Classification (mentioned in the ABAC GA
   announcement) which auto-tags columns with these. If a suitable tag is
   already present, **the plugin reuses it and creates zero new governed
   tag objects** — minimizing pollution of what is an **account-wide, shared
   namespace** (confirmed: `CREATE GOVERNED TAG` is an account-level
   resource, §17 — a real risk if this utility mints one tag key per
   migrated column across a large migration).
2. **Fallback: mint a deterministic synthetic tag, one KEY per legacy
   function** (REVISED — was originally 2 fixed keys, `abac_rls`/
   `abac_colmask`, shared across the whole utility; changed on request to
   give each legacy function its own governed tag key so
   `SHOW GOVERNED TAGS`/`DESCRIBE GOVERNED TAG` maps 1:1 back to the
   specific function that used to enforce that security). If no suitable
   existing tag is found, `tag_key_for_function(function_fqn, role)`
   (`migration/tag_provisioner.py`) derives the key deterministically from
   the function's sanitized, fully-qualified `catalog.schema.function_name`,
   prefixed with `abac_rls_`/`abac_colmask_` (e.g.
   `cat.sch.rf_business_unit_fn` → `abac_rls_cat_sch_rf_business_unit_fn`;
   any non-`[A-Za-z0-9_]` character in any component — including hyphens,
   a confirmed-live real case, e.g. catalog `jh-demo` — becomes `_`).
   **No hash/digest is ever appended** (REVISED — an intermediate design
   used just the function's short name plus a hash-suffixed fallback for
   cross-schema collisions; replaced on request with the fully-qualified
   form above, which is unique by construction with no hash needed).
   Pathologically long/unusual names are hard-truncated (still with no
   hash) to stay well under any plausible key-length limit. Two
   `TagRequest`s for the same function+role always resolve to the same key
   even across different tables — one function can guard several tables.
   The one remaining edge case — sanitization collapsing two genuinely
   different raw names onto the same key, or a pre-existing unrelated
   governed tag already occupying the exact deterministic key — raises
   `TagKeyCollisionError` loudly rather than silently merging/hijacking.
   **Value is only added when actually needed for disambiguation** (REVISED
   — see `tag_provisioner._split_by_collision()`). Confirmed live: a
   plain **key-only** governed tag (`CREATE GOVERNED TAG key`, no
   `VALUES`) plus `MATCH COLUMNS has_tag(key)` compiles fine at `CREATE
   POLICY` time even when the same key ends up on 2+ columns of the same
   table, but then fails **every read** with `UC_ABAC_AMBIGUOUS_COLUMN_MATCH:
   ... had 2 matches, exactly 1 match is allowed`. So: if a function guards
   exactly one column within a given table (the overwhelmingly common
   case, even when that same function also guards a column in a
   *different* table — `MATCH COLUMNS` is table-scoped), that column gets
   a **key-only** tag, no value, no "Allowed values" entry at all. Only
   when a function guards **more than one column of the same table**
   (e.g. a row filter with 2 `USING COLUMNS`, or a generic mask function
   reused for 2 columns) do those specific colliding columns each get
   their own **unique value** — a short hash of
   `<catalog>.<schema>.<table>.<column>.<role>` (256-char tag value length
   limit, confirmed via docs) — added to that key's allowed-values list, so
   each is matched via `has_tag_value(key, value)` instead. A
   cross-run variant of the same check (a NEW column colliding with an
   *already-tagged* column from a prior run, discovered via
   `list_column_tags`) is handled the same way.
   Trade-off accepted deliberately: many more tag keys for a large
   migration (one per distinct function, not a constant 2) in exchange for
   per-function auditability.
3. **`ALTER GOVERNED TAG ... SET VALUES (...)` is declarative/full-replace,
   not additive** (confirmed via docs and empirically) — the provided list
   *replaces* the entire allowed-values list. This is a **write-write race
   hazard** under `max_parallelism > 1`: two concurrent table conversions
   both trying to add their own unique value via a read-current→append→
   write-back cycle can clobber each other's addition. **Mitigation:**
   governed-tag value provisioning is pulled out of the per-table parallel
   phase and run as a single **serialized "Prepare Tags" step** in the
   orchestrator (§3, §9 diagram updated below) that batches every new tag
   value needed for the whole run into one `SET VALUES` call per key
   (union of existing + newly needed values), executed once, single-
   threaded, before the parallel per-table conversion phase begins.
4. **Propagation delay is real and must be handled.** Empirically confirmed
   (§17): after `ALTER GOVERNED TAG ... SET VALUES`, a `CREATE POLICY`
   referencing a brand-new value can fail with
   `UC_INVALID_POLICY_CONDITION: Invalid tag value ... for key ...` for
   roughly **20-30 seconds** even though `DESCRIBE GOVERNED TAG` already
   shows the value as registered — i.e. this is a control-plane cache
   propagation lag, not a data-plane consistency issue. The resilience layer
   (§10.1) treats this specific error as **retryable-with-backoff for a
   bounded window** (a new classification distinct from HTTP-level
   throttling — semantic-but-transient, not HTTP 429/503):
   `create_or_replace_policy()` in the gateway retries on
   `UC_INVALID_POLICY_CONDITION` containing `"Invalid tag value"` up to a
   capped wait (e.g. 90s total) specifically when the policy's own tag value
   was provisioned earlier in the same run, then fails hard (real
   misconfiguration) if it never resolves.
5. **Tags are cleaned up on rollback only if this utility created them**,
   mirroring the "never touch pre-existing objects" rule for policies:
   `rollback_metadata` (§4.3) gains a `tags_created_by_this_run` list; a
   rollback removes the specific tag *values* it added (again via a
   serialized `SET VALUES` step reconstructing the list minus its own
   entries) but never deletes a governed tag *key*, and never touches a tag
   the plugin decided to *reuse* in step 1. `DROP GOVERNED TAG` (confirmed
   working syntax, §17) is available for a full teardown of synthetic keys,
   but is a deliberate, separate, human-invoked operation — never part of
   automatic per-table rollback.

### 7.3.1 `PolicyScope`: Table Level vs. Catalog Level Application (both implemented)

Steps 1-3 (identify legacy RLS/column masks, mint one governed tag per
legacy function, apply that tag to the relevant column(s)) are **identical
regardless of scope** — `tag_provisioner.py` and the discovery/tagging
phases have no scope awareness at all. Everything scope-specific is
isolated behind the `PolicyStrategy` Protocol (`migration/policy_strategy.py`),
injected once into both plugins (`rls_to_abac.py`/`mask_to_abac.py`) and
`inventory_manager.py` at run start via `migration_engine.build_policy_strategy(config)`.
Selected per run by the `policy_scope` job/config parameter (§11) — never
mixed within one run:

**A. `TABLE` — "Table level application" (`TableBasedPolicyStrategy`, default, no behavior change from §7.3 above)**

1. Identify legacy row filter / column masks on the table.
2. Create one governed tag per legacy function (`tag_provisioner.py`, §7.4).
3. Apply that tag to the relevant column(s) on the table.
4. Create the ABAC policy scoped `ON TABLE` — one `ROW_FILTER` policy per
   table (`abac_migrated_row_filter`) and one `COLUMN_MASK` policy per
   masked column (`abac_migrated_mask_<column>`), per §7.3.
5. Manual review (`APPLY_ABAC` leaves both mechanisms live —
   `migration_audit.migration_phase=ABAC_APPLIED`, explicitly not final —
   so an operator can confirm the new `ON TABLE` policy produces identical
   results before anything legacy is touched).
6. Remove the table-level legacy row filter/column masks (`FINALIZE`).

**B. `CATALOG` — "Catalog level application" (`CatalogBasedPolicyStrategy`)**

Steps 1-3 identical to A. Then:

4. Create the ABAC policy scoped `ON CATALOG` (the catalog the legacy
   function itself lives in) instead of `ON TABLE` — one policy **per
   legacy function**, named after `tag_key_for_function(function_fqn, role)`
   (the same deterministic key used for the governed tag, so
   `SHOW POLICIES`/`SHOW GOVERNED TAGS` line up 1:1), using
   `MATCH COLUMNS has_tag(key)`/`has_tag_value(key, value)` exactly as in
   A. Because the policy lives once `ON CATALOG`, every table across that
   catalog whose column carries the matching tag is covered by the *same*
   single policy object — this is the "proper" ABAC end-state (one policy,
   many tables) called out as a future extension in earlier drafts of this
   document, now implemented as a first-class, selectable option rather
   than deferred.
5. Manual review — identical intent to A: confirm the table still has BOTH
   the legacy mechanism and (now catalog-scoped) ABAC coverage before
   anything is removed. Existing-ABAC-policy detection for this scope
   cannot use `SHOW POLICIES ON TABLE` (the policy isn't attached to the
   table) — `CatalogBasedPolicyStrategy.find_existing_row_filter_policy`/
   `find_existing_mask_policies` instead recover state from the governed
   **column tag** already present on the table's column(s), which is
   scope-agnostic ground truth.
6. Remove the table-level legacy row filter/column masks (`FINALIZE`) —
   **only this table's legacy mechanism is removed**; the shared `ON
   CATALOG` policy itself is left untouched (and keeps protecting every
   sibling table using the same tag/function), so finalizing table A never
   affects table B's in-flight migration even though they share one policy
   object underneath.

Trade-off of B vs. A: far fewer policy objects for a large migration (one
per distinct legacy function, not one per table/masked-column), at the
cost of a coarser blast radius if that one policy is ever misconfigured or
dropped (it now protects every table using that function, not just one).
Both scopes are exercised end-to-end in
`tests/test_catalog_scope_e2e.py`/`test_migration_engine.py`.

### 7.5 Eligibility rules (evaluated during `INVENTORY`)

**Revised after the full live end-to-end run (§18.1)** — the original table
below over-scoped two conditions to the whole table when they are actually
per-object. A real fixture with 3 masked columns, one referencing a
function dropped after being set, was marked `NOT_ELIGIBLE` in its
entirety and silently skipped by `migration_engine` before ever reaching
`table_converter` — the other two, perfectly migratable columns were never
attempted and got no audit row at all. That directly contradicted the
documented "weakest link" per-object independence (§6, `mask_to_abac.py`,
scenario 11) which `table_converter`'s plugins already implement
correctly. Fix: only true whole-table conditions gate at inventory time;
missing/inaccessible functions and existing-policy conflicts are left
entirely to `RLSMigrationPlugin`/`ColumnMaskMigrationPlugin` to evaluate
and report **per row-filter / per masked-column**, exactly as scenarios
5, 6, 9, 10, 11 always expected `convert_table()` itself to do.

| Condition | Eligibility | Reason code |
|---|---|---|
| No row filter AND no column masks | NOT_ELIGIBLE | `NO_LEGACY_SECURITY_FOUND` |
| Table type unsupported for ABAC policies (verify per target DBR — flagged §11) | NOT_ELIGIBLE | `UNSUPPORTED_TABLE_TYPE` |
| Everything else (table is *attempted*; per-object outcome, including a missing function or a conflicting policy on any one object, is decided by the plugins during `convert_table()` — `SOURCE_FUNCTION_UNAVAILABLE`/`EXISTING_ABAC_POLICY_CONFLICT` are now per-object `ConversionStepResult` outcomes, not table-level inventory reasons) | ELIGIBLE | — |

---

## 8. Safety / State Machine

Per the doc's explicit requirement, the **only** allowed sequence:

```
READ EXISTING SECURITY CONFIGURATION   (discover)
        │
        v
VALIDATE SOURCE                        (validate: function exists/accessible/compatible)
        │
        v
CAPTURE rollback_metadata              (before any mutation)
        │
        v
CREATE ABAC POLICY  (CREATE [OR REPLACE] POLICY ...)
        │
        v
APPLY / CONFIRM ATTACHED               (policy is attached at CREATE time; this step
        │                                re-reads it back via SHOW/DESCRIBE POLICY)
        v
VALIDATE ABAC                          (post_validation: matches desired spec)
        │
        ├─ FAIL ──> status=FAILED, legacy RLS/mask left completely untouched,
        │           new (unverified) policy left in place for inspection,
        │           rollback_metadata still recorded for a manual rollback run
        │
        v (pass)
REMOVE OLD RLS/MASK                    (ALTER TABLE ... DROP ROW FILTER /
        │                                ALTER TABLE ... ALTER COLUMN ... DROP MASK)
        v
VALIDATE FINAL STATE                   (legacy mechanism confirmed gone AND
        │                                ABAC policy confirmed present)
        │
        ├─ FAIL ──> status=FAILED, error_code=OLD_MECHANISM_REMOVAL_UNVERIFIED;
        │           table now has BOTH mechanisms active simultaneously, which is
        │           safe (both would enforce, strictest wins / additive — never a
        │           security *gap*) but flagged loudly for manual follow-up
        │
        v (pass)
status = SUCCESS
```

The state machine per table-object (RLS or one masked column) is:

```
DISCOVERED -> VALIDATED -> POLICY_CREATED -> POLICY_VERIFIED -> LEGACY_REMOVED -> COMPLETE
     │             │              │                 │                 │
     │             │              │                 │                 └─(remove fails)─> FAILED (dual-mechanism, flagged)
     │             │              │                 └─(verify fails)──────────────────> FAILED (policy kept for inspection)
     │             │              └─(create fails)────────────────────────────────────> FAILED (legacy untouched)
     │             └─(not eligible / already migrated)────────────────────────────────> ALREADY_MIGRATED / NOT_ELIGIBLE (terminal, no mutation)
     └─(discovery finds nothing)───────────────────────────────────────────────────────> NOT_ELIGIBLE
```

Crucially: **at no point does "remove legacy" happen before "verify ABAC."**
The one failure mode that leaves two mechanisms active simultaneously
(over-protective, never under-protective) is explicitly preferred over any
path that could leave a table briefly unprotected.

---

## 9. Dry Run

`dry_run=true` makes `UnityCatalogGateway`'s mutating methods
(`create_or_replace_policy`, `drop_policy`, `drop_row_filter`,
`drop_column_mask`, `set_row_filter`, `set_column_mask`) **no-ops that return
what would have happened** rather than being skipped by the plugins — i.e.
dry-run is implemented once, at the gateway boundary, not scattered as
`if dry_run` checks through every plugin. This guarantees dry-run and real
runs exercise identical plugin/engine code paths (reduces "works in dry-run,
breaks for real" risk).

Output shape mirrors the doc's example exactly:

```json
{
  "table": "sales.prod.customer",
  "current": {"row_filter": "security.customer_rls"},
  "proposed": {"policy_name": "abac_migrated_row_filter", "function": "security.customer_rls"},
  "actions": ["CREATE ABAC", "APPLY ABAC", "REMOVE TABLE RLS"],
  "result": "WOULD_MIGRATE"
}
```

---

## 10. Error Handling Strategy

- **Error taxonomy** (`error_code`), non-exhaustive, extensible enum:
  `SOURCE_FUNCTION_NOT_FOUND`, `SOURCE_FUNCTION_NOT_ACCESSIBLE`,
  `SOURCE_FUNCTION_INCOMPATIBLE`, `POLICY_CREATE_FAILED`,
  `POLICY_VERIFY_FAILED`, `EXISTING_ABAC_POLICY_CONFLICT`,
  `LEGACY_REMOVAL_FAILED`, `FINAL_STATE_VERIFY_FAILED`, `PERMISSION_DENIED`,
  `UNSUPPORTED_TABLE_TYPE`, `TRANSIENT_API_ERROR`, `UNKNOWN`.
- **Retryable vs terminal**: only `TRANSIENT_API_ERROR`-classified exceptions
  (timeouts, 429/503-style) get a bounded retry (configurable
  `max_retries`, default small, exponential backoff) at the gateway level;
  everything else fails fast for that table.
- **`continue_on_error`**: governs the *orchestrator* loop over tables only.
  A single table's exception is caught at the `table_converter` boundary,
  converted into a `FAILED` `ConversionResult` (never a raw exception
  bubbling up), and persisted; the orchestrator moves to the next table
  when `continue_on_error=true`, or raises/stops the run when `false`.
- **Partial-table failure** (RLS ok, one of three masks fails): each
  masked-column conversion is its own independent unit; failure of one does
  not block the others in the same table (per-object plugin invocation, not
  one monolithic table-level try/except).
- All exceptions are logged with `run_id`/`attempt_id`/`table` correlation
  IDs and always result in an audit row — **no silent failures**.

---

### 10.1 API Resilience Strategy (throttling / retry / backoff)

Every mutating and read call to Databricks (SQL statements via the notebook's
Spark session, or any REST call the gateway makes — e.g. Jobs API in the
sales-demo spike tooling, or the SQL Statement Execution API used by the
API-verification spike in §17) goes through a single **resilient call
wrapper** at the `uc_gateway` boundary, never re-implemented ad hoc per
plugin/module:

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter: bool = True
    retryable_status_codes: frozenset[int] = frozenset({429, 503, 504})
    retryable_exceptions: tuple[type[Exception], ...] = (
        TimeoutError, ConnectionError,
    )

def with_retries(fn: Callable[[], Response], policy: RetryPolicy) -> Response:
    """Executes fn(), retrying on throttling/transient failures only."""
```

Rules:

- **Classification, not blanket retry.** Only `retryable_status_codes`
  (429 Too Many Requests, 503/504) and `retryable_exceptions` (timeouts,
  connection resets) are retried. Anything else (400/401/403/404, or a
  Databricks `error_code` like `PRINCIPAL_DOES_NOT_EXIST`,
  `POLICY_NOT_FOUND`, `TABLE_OR_VIEW_NOT_FOUND`) fails immediately — a
  retry would never fix a genuine validation/permission/semantic error and
  would only slow down `continue_on_error` reporting.
- **Respect `Retry-After`.** On HTTP 429, Databricks REST APIs return a
  `Retry-After` header; when present, that value is used as the wait time
  instead of the computed backoff. This applies to REST-based interactions
  (Jobs API, SQL Statement Execution API); native `spark.sql()` calls made
  from inside the notebook don't carry HTTP headers, so for those the
  wrapper falls back to catching the equivalent thrown throttling exception
  and using pure exponential backoff.
- **Exponential backoff with jitter**: `delay = min(max_delay_s, base_delay_s
  * 2**attempt) * (1 + random.uniform(-0.2, 0.2) if jitter else 1)`, capped
  at `max_retries` attempts, after which the original error is raised (and
  becomes a normal `TRANSIENT_API_ERROR`-classified `FAILED` result, per
  §10 — retries are an internal resilience detail, not a way to hide
  persistent failures from the audit trail).
- **Idempotent-safe retries only.** Because `CREATE OR REPLACE POLICY` /
  `ALTER TABLE ... SET ROW FILTER` / `DROP POLICY IF EXISTS` /
  `DROP ROW FILTER`/`DROP MASK` are all naturally idempotent statements (re-
  running them converges to the same end state), it's safe for the retry
  wrapper to resend a mutating statement whose *response* was lost to a
  timeout without first checking whether the original attempt actually
  landed — the worst case is a harmless no-op re-application, never a
  duplicate object. This is a direct consequence of the idempotency design
  in §7 and is why `with_retries` doesn't need a separate "was this already
  applied" check before retrying.
- **Per-call budget, not per-run.** Retry state is local to a single
  `uc_gateway` call; it does not accumulate across an entire table
  conversion or run, so one throttled table doesn't consume a shared budget
  that starves other tables under `max_parallelism`.
- **Concurrency-aware throttling avoidance.** `max_parallelism` (§11) is
  also the primary lever for *avoiding* throttling in the first place —
  operators running against a metastore with many catalogs should tune it
  down if they observe frequent 429s in the audit `error_code` distribution
  (a `RECONCILE`/summary-report metric worth surfacing: count of retried
  calls and count of exhausted-retry failures per run).
- **Verified empirically, not just designed on paper** — see §17 for the
  actual resilient client implementation used to run the API-verification
  spike, and its behavior under real Databricks calls.

## 11. Notebook / Job Parameter Design

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `mode` | str enum | `INVENTORY` | `INVENTORY`\|`MIGRATE`\|`INVENTORY_AND_MIGRATE`\|`APPLY_ABAC`\|`FINALIZE`\|`VERIFY`\|`RECONCILE`\|`ROLLBACK` (§3.1 for the isolated-phase pair) |
| `scope_type` | str enum | `SELECTED_CATALOGS` | `ALL_CATALOGS`\|`SELECTED_CATALOGS`\|`ALL_SCHEMAS`\|`SELECTED_SCHEMAS`\|`SPECIFIC_TABLES` |
| `catalogs` | JSON array (str) | `[]` | |
| `schemas` | JSON object (str -> array[str]) | `{}` | per doc's example shape |
| `tables` | JSON array (str, fully qualified) | `[]` | for `SPECIFIC_TABLES` scope |
| `exclude_schema_regex` | str | `""` | applied per-catalog during scope resolution |
| `dry_run` | bool | `true` | **defaults to true** — safest default for a destructive-capable utility |
| `continue_on_error` | bool | `true` | |
| `max_parallelism` | int | `4` | thread-pool size in `migration_engine`; table_converter remains safely callable with `max_parallelism=1` for troubleshooting a single table |
| `audit_catalog` / `audit_schema` / `audit_table` | str | none — required, no hard-coded default per-customer name | |
| `policy_scope` | str enum | `TABLE` | `TABLE`\|`CATALOG` (§7.3.1) — "table level application" (`ON TABLE`, one policy per table/masked column) vs. "catalog level application" (`ON CATALOG`, one policy per legacy function shared by every table that used it) |
| `policy_to_principals` | JSON array (str) | `["account users"]` | overridable if an org wants a narrower default `TO` clause |
| `policy_except_principals` | JSON array (str) | `[]` | principals fully exempted (`TO ... EXCEPT principal [, ...]`, confirmed live CREATE POLICY grammar) from every ABAC policy this run creates - e.g. a service principal that runs unmasked ETL, or a break-glass admin group. Empty = no `EXCEPT` clause, unchanged prior behavior |
| `enable_llm_pii_tagging` | bool | `false` | `INVENTORY`-only, advisory: classify each legacy function's likely PII category via `ai_query()` from its name+columns alone (never row data) |
| `pii_llm_endpoint` | str | `databricks-meta-llama-3-3-70b-instruct` | Foundation Model API endpoint used by `enable_llm_pii_tagging` |
| `run_id` | str | generated UUID if blank | allows resuming/correlating a specific run, e.g. for `ROLLBACK` mode targeting one prior run |

All parameters are read via `dbutils.widgets` in the thin notebook and
converted to a typed `RunConfig` by `config/config_loader.py` (the only
place that knows about widgets vs. a plain dict, so the same config loader
also supports being called from a unit test with a plain dict, no notebook
runtime required).

---

## 12. Proposed Project / File Structure

```
abac_migration/
├── DESIGN.md                          <- this document
├── config/
│   ├── __init__.py
│   ├── config_loader.py               # widgets/dict -> RunConfig
│   └── models.py                      # RunConfig, enums (Mode, ScopeType, ...)
├── scope/
│   ├── __init__.py
│   └── scope_resolver.py              # RunConfig -> list[TableRef]
├── discovery/
│   ├── __init__.py
│   ├── catalog_discovery.py
│   ├── schema_discovery.py
│   ├── table_discovery.py
│   ├── rls_discovery.py
│   └── mask_discovery.py
├── uc_gateway/
│   ├── __init__.py
│   ├── gateway.py                     # UnityCatalogGateway Protocol + real impl (SQL via Spark session in-notebook)
│   ├── retry.py                       # RetryPolicy, with_retries() - resilient call wrapper (§10.1) - built + tested in §17
│   ├── sql_statement_client.py        # resilient SQL Statement Execution API client (used outside notebooks, e.g. spikes/local tooling)
│   └── models.py                      # TableSecurityState, PolicyRef, PolicyDefinition, PolicySpec, PolicyApplyResult
├── inventory/
│   ├── __init__.py
│   ├── inventory_manager.py
│   └── inventory_repository.py
├── migration/
│   ├── __init__.py
│   ├── migration_engine.py            # top-level orchestrator
│   ├── table_converter.py             # convert_table(table_ref, options) -> ConversionResult
│   ├── policy_strategy.py             # PolicyStrategy, TableBasedPolicyStrategy, CatalogBasedPolicyStrategy (§7.3.1)
│   ├── tag_provisioner.py             # governed-tag reuse/provisioning, serialized "Prepare Tags" phase (§7.4)
│   └── plugins/
│       ├── __init__.py
│       ├── base_plugin.py             # MigrationPlugin Protocol, DiscoveryResult, ValidationResult, ConversionStepResult
│       ├── rls_to_abac.py
│       └── mask_to_abac.py
├── validation/
│   ├── __init__.py
│   ├── pre_validation.py
│   ├── post_validation.py
│   └── drift_detection.py
├── rollback/
│   ├── __init__.py
│   └── rollback_manager.py
├── audit/
│   ├── __init__.py
│   └── audit_repository.py            # + inline DDL for the 2 audit tables (§4)
├── notebook/
│   └── abac_migration_driver.py       # thin entry point (Databricks notebook source format)
└── tests/
    ├── conftest.py                    # FakeUnityCatalogGateway (in-memory) fixture
    ├── test_scope_resolver.py
    ├── test_inventory_manager.py
    ├── test_policy_strategy.py
    ├── test_tag_provisioner.py
    ├── test_rls_plugin.py
    ├── test_mask_plugin.py
    ├── test_table_converter.py         # the 15 required scenarios (§14)
    ├── test_drift_detection.py
    ├── test_rollback_manager.py
    └── test_retry.py                  # unit tests for uc_gateway/retry.py (simulated throttling, no live calls)
```

This mirrors the doc's suggested tree with two additions: `uc_gateway/`
(making the UC-access seam an explicit, separately-owned module rather than
implicit inside plugins — this is what makes `table_converter` unit-testable
per the doc's "must be unit-testable without requiring the entire
orchestration framework, mock the UC/API layer" requirement) and `tests/`.

A separate, git-ignored `spike/` folder (not shown above, sibling to
`abac_migration/`) holds the throwaway verification harness used to produce
the confirmed findings in §13/§17 — `setup_spike.py`, `api_spike_test.py`,
`test_retry_wrapper.py`. It is reference/historical material proving the
design's API assumptions, not part of the shipped utility, and its throwaway
Unity Catalog objects (`ril_raw.abac_api_spike.*`, the `abac_migration_col_id`
governed tag) were torn down after the spike completed.

---

## 13. Databricks API / Dependency List

**Status: every statement below has now been executed live against a real
Unity Catalog workspace (`uc_source`) as part of the §17 spike — not just
checked against documentation.** Each row shows the exact syntax that
actually worked (or the exact error that proved a variant does *not* work).

| Purpose | Statement / API | Confidence |
|---|---|---|
| Detect existing table-level row filter/masks | `DESCRIBE TABLE EXTENDED <table>` (parse `Row Filter`, `# Column Masks` rows) | **CONFIRMED — executed live** |
| Detect existing ABAC policies on a table | `SHOW POLICIES ON TABLE <table>` / `SHOW EFFECTIVE POLICIES ON TABLE <table>` | **CONFIRMED — executed live**, returns `(policy_name, policy_type, catalog, schema, table, comment)` rows |
| Full policy detail | `DESCRIBE POLICY <name> ON TABLE <table>` | **CONFIRMED — executed live**, returns key-value rows incl. `Match Columns`, `Using Columns`/`On Column`, `Function Name` |
| Bulk policy audit across a catalog | `SELECT policy_name, policy_type, on_securable_type, securable_name, match_columns FROM <catalog>.information_schema.abac_policy_definitions WHERE schema_name = '<schema>'` | **CONFIRMED — executed live** |
| Create ABAC row-filter policy | `CREATE OR REPLACE POLICY name ON TABLE t ROW FILTER fn TO`` `account users` `` FOR TABLES MATCH COLUMNS has_tag_value(key,value) AS alias USING COLUMNS (alias)` | **CONFIRMED — executed live.** ⚠️ **Revision of original design**: a literal `USING COLUMNS (business_unit)` (no `MATCH COLUMNS`) FAILS: `Undefined column aliases: business_unit. Referenced aliases must be defined in match_columns`. `MATCH COLUMNS` is mandatory whenever `USING COLUMNS` is used. |
| `MATCH COLUMNS` condition grammar | Only `has_tag(key)` / `has_tag_value(key,value)` (+ deprecated camelCase aliases) | **CONFIRMED — executed live.** A literal-column-name condition (`column_name = 'foo'`) FAILS: `Arithmetic and comparison operators are not allowed`. There is **no way to match a column by name** — governed tags are mandatory, not optional (see §7.4). |
| Create ABAC column-mask policy | `CREATE OR REPLACE POLICY name ON TABLE t COLUMN MASK fn TO`` `account users` `` FOR TABLES MATCH COLUMNS has_tag_value(key,value) AS alias ON COLUMN alias` (no `USING COLUMNS` for a 1-arg mask fn) | **CONFIRMED — executed live.** Same `MATCH COLUMNS`-is-mandatory finding as row filters. Also confirmed: adding a redundant `USING COLUMNS (alias)` for a 1-argument mask function FAILS with an argument-count mismatch (`requires 2 argument(s), but the referred function ... takes 1 argument(s)`) — the masked value is passed implicitly as arg 1. |
| Governed tags are a hard prerequisite for ABAC policies | n/a | **CONFIRMED** both empirically and via docs ("Governed tags applied to target objects" is a listed *requirement* for creating row filter/column mask policies) |
| Create a governed tag (account-level) | `CREATE GOVERNED TAG tag_key [DESCRIPTION desc] [VALUES (v1, v2, ...)]` | **CONFIRMED — executed live.** Requires account-level `CREATE` privilege (workspace/account admins have it by default — our workspace-admin token succeeded, in contrast to the earlier finding that this same token could *not* create resolvable account-level *groups*, §identity — these are separate privilege domains). Key-only tags (no `VALUES`) only permit key-presence assignment, **not** arbitrary values (confirmed: assigning a custom value to a key-only-declared tag causes policy creation to fail with `Invalid tag value ... for key ...`, even though `ALTER TABLE ... SET TAGS` itself accepts the write without complaint). |
| Key-only tag + `has_tag(key)` (no value at all) | `ALTER TABLE t ALTER COLUMN c SET TAGS ('key')` (no `= value`) then `MATCH COLUMNS has_tag('key') AS alias` | **CONFIRMED — executed live (2026-08-26 spike).** Works exactly like `has_tag_value` when the key is unique-within-the-table. ⚠️ **Also confirmed the failure mode this must avoid**: tagging a 2nd column of the *same* table with the *same* key-only tag lets `CREATE OR REPLACE POLICY` succeed (no validation at creation time), but every subsequent `SELECT` on that table then fails with `[UC_ABAC_AMBIGUOUS_COLUMN_MATCH] ... Using alias 'mc_dept' had 2 matches, exactly 1 match is allowed` — i.e. this is a deferred, read-time failure, not a creation-time one. `tag_provisioner._split_by_collision()` exists specifically to detect and avoid this by only falling back to per-column unique values when 2+ columns of the same table would otherwise share a bare key. |
| Grow a governed tag's allowed values | `ALTER GOVERNED TAG tag_key SET VALUES (v1, v2, ..., vN)` | **CONFIRMED — executed live.** ⚠️ **Declarative full replace, not additive** — the given list replaces the entire allowed-values list (confirmed via docs: "Any previously defined values not included in the new list are removed"). Existing **tag assignments** on columns survive this (confirmed: re-queried `information_schema.column_tags` after growing the list, prior assignments intact) — only the *allowed-values catalog* is replaced, not the applied instances. This is a **race hazard under concurrency** (§7.4 point 3). |
| Propagation delay after growing tag values | n/a (control-plane cache lag) | **CONFIRMED — executed live.** `CREATE POLICY` referencing a value added via `ALTER GOVERNED TAG ... SET VALUES` moments earlier fails for ~20-30s with `Invalid tag value ... for key ...` even though `DESCRIBE GOVERNED TAG` already reflects the new value immediately. Resolved on retry after the delay. Must be handled by the resilience layer (§10.1) as a bounded-window retryable condition. |
| Tag a column | `ALTER TABLE t ALTER COLUMN c SET TAGS ('key' = 'value')` | **CONFIRMED — executed live** |
| List column tags | `SELECT column_name, tag_name, tag_value FROM <catalog>.information_schema.column_tags WHERE table_name = '<table>'` | **CONFIRMED — executed live** |
| List all governed tags (incl. built-ins) | `SHOW GOVERNED TAGS` | **CONFIRMED — executed live.** Databricks ships a rich set of built-in classification governed tags out of the box (e.g. `class.email_address`, `class.us_ssn`, `class.credit_card`, `class.phone_number`, `class.name`, `class.location`, ~20 total observed) — these should be **preferred over minting synthetic tags** when they already correctly identify the target column (§7.4 point 1). |
| Inspect one governed tag's definition | `DESCRIBE GOVERNED TAG tag_key` | **CONFIRMED — executed live**, returns `Tag Key`, `Id`, `Values`, `Create/Update Time` |
| Remove a governed tag entirely | `DROP GOVERNED TAG tag_key` | **CONFIRMED — executed live** (used for spike cleanup). Per design (§7.4 point 5), reserved for deliberate human-invoked teardown, never automatic per-table rollback. |
| Coexistence of legacy RLS/mask and an ABAC policy on the same table | n/a | **CONFIRMED — executed live**: `CREATE POLICY` succeeds while a legacy `ALTER TABLE ... SET ROW FILTER`/`SET MASK` is still active on the same table/column — no conflict error. Querying the table with both active produces results consistent with both being enforced (verified: same masked/filtered outcome as either mechanism alone, using the same underlying function) — validates the safety-first ordering in §8 (both mechanisms can safely coexist mid-migration). |
| Remove ABAC policy | `DROP POLICY name ON TABLE t` | **CONFIRMED — executed live.** ⚠️ **Revision**: `IF EXISTS` is **not** supported by the grammar (`DROP POLICY IF EXISTS ...` fails with `PARSE_SYNTAX_ERROR: missing 'ON'`) — confirmed against current docs too (no `IF EXISTS` in the syntax). Re-dropping an already-dropped policy raises `POLICY_NOT_FOUND` (`BAD_REQUEST`), which `rollback_manager` must explicitly catch and treat as an idempotent no-op, rather than relying on `IF EXISTS` syntax sugar. |
| Remove legacy row filter | `ALTER TABLE t DROP ROW FILTER` | **CONFIRMED — executed live**, including while an ABAC policy remains on the table (post-condition: `SHOW POLICIES` still lists the ABAC policy, query now enforced by ABAC alone). |
| Remove legacy column mask | `ALTER TABLE t ALTER COLUMN c DROP MASK` | **CONFIRMED — executed live**, same post-condition check. |
| List catalogs/schemas/tables | `information_schema.catalogs/schemata/tables`, or `SHOW CATALOGS`/`SHOW SCHEMAS`/`SHOW TABLES` | Confirmed via docs + prior-session usage; implementation should prefer the notebook's native Spark SQL session over REST, since the notebook *is* the execution entry point |
| Function existence/accessibility | `information_schema.routines` + a permission probe | Mechanism confirmed to exist; **exact "is this function usable by the ABAC policy owner" check remains a TODO** — Databricks requires `EXECUTE` on the UDF to create the policy, so `validate()` should attempt a lightweight probe and treat denial as `SOURCE_FUNCTION_NOT_ACCESSIBLE` |
| Target DBR/API version compatibility | `CREATE POLICY`/`DESCRIBE POLICY` require **DBR 16.4+**; `CREATE/ALTER GOVERNED TAG` require **DBR 18.1+** | Confirmed via docs (two *different* minimum versions!) — `RunConfig`/`pre_validation` must check for the higher of the two (18.1+) since governed tags are now a hard dependency, and fail fast with a clear error rather than a confusing parse error |

All previously-flagged "implementation TODO/spike" items (§16 v1) are now
**resolved** by the above — see §16 for the residual, smaller set of open
items that remain after this pass, and §17 for the full spike transcript.

No REST endpoints or SQL statements outside of what's shown above are
assumed by this design.

---

## 14. Risks and Edge Cases

| Risk / Edge case | Mitigation |
|---|---|
| Same security function shared by many tables | `TableBasedPolicyStrategy` creates one policy per table regardless (policy names are securable-scoped, so no collision); `CatalogBasedPolicyStrategy` (§7.3.1, `policy_scope=CATALOG`) instead consolidates them into one shared `ON CATALOG` policy by design — selectable per run |
| Table has RLS but no masks, or vice versa, or neither | `applies_to()` per plugin makes each independently optional; `NOT_ELIGIBLE` when neither present |
| Multiple masked columns, one function shared across columns | Discovery returns a list; each `(column, function)` pair converted independently — partial failure isolated per column (§10) |
| Existing ABAC policy already present (someone migrated manually, or pre-existing unrelated policy) | `EXISTING_ABAC_POLICY_CONFLICT` if it doesn't match desired spec — never silently overwritten; `ALREADY_MIGRATED` if it matches (idempotent) |
| Function deleted/renamed/permission-revoked between inventory and migration | `pre_validation` re-checks live state immediately before `convert()`, not just at inventory time |
| Concurrent runs (two people/jobs running the utility at once against overlapping scope) | `run_id`-scoped audit rows avoid record collisions; actual UC mutation via `CREATE POLICY` is itself atomic per statement, but the design does **not** claim distributed-lock-level exactly-once — documented as a v1 limitation; recommend `max_parallelism` scoped to a job with exclusive scope, not concurrently overlapping jobs |
| View / materialized view / streaming table variants | Table-type-specific ABAC support must be checked against target DBR before enabling (`UNSUPPORTED_TABLE_TYPE` guard, §7.5) rather than assumed |
| **Governed tags are an account-wide shared namespace** (new, §7.4) | `tag_provisioner` prefers reusing existing/built-in classification tags (`class.*`) over minting new ones; when minting is required, one deterministic key is minted **per distinct legacy function** (REVISED, was originally 2 fixed keys for the whole utility) — bounds namespace growth to the number of distinct legacy functions being migrated, not the number of migrated columns, while keeping each key traceable back to one function |
| **`ALTER GOVERNED TAG ... SET VALUES` is declarative/full-replace** (new, §7.4) | Governed-tag value provisioning is deliberately pulled out of the parallel per-table phase into one serialized "Prepare Tags" step per run, batching all needed values into a single read-union-write per key — eliminates the read-modify-write race under `max_parallelism > 1` |
| **Governed tag propagation delay (~20-30s) before a new value is usable in `CREATE POLICY`** (new, §7.4, confirmed empirically) | Resilience layer (§10.1) retries `UC_INVALID_POLICY_CONDITION`/"Invalid tag value" errors with backoff for a bounded window specifically when that value was provisioned earlier in the same run; a `Prepare Governed Tags` step also naturally runs before the parallel conversion phase, giving propagation a head start |
| **Minimum DBR version mismatch between features** (new, §13) | `CREATE POLICY` needs DBR 16.4+, but `CREATE/ALTER GOVERNED TAG` needs DBR 18.1+ — `pre_validation` must check for 18.1+ (the higher requirement) given governed tags are now a hard dependency, not the lower 16.4 figure originally assumed |
| Very large scope (thousands of tables) | Scope resolution and inventory are streamed/paginated, not materialized entirely in driver memory where avoidable; `max_parallelism` bounds concurrent UC calls; audit writes batched |
| Partial migration leaves both mechanisms active simultaneously (verify-final-state failure) | Explicitly the *safe* failure mode (over-protective, not a gap) — surfaced loudly in the summary report as `FAILED`/`OLD_MECHANISM_REMOVAL_UNVERIFIED` requiring manual follow-up, never auto-retried silently |
| Rollback requested after the source function has since been dropped/altered | `rollback_metadata` captures the function *reference*, not its body; if the function no longer exists, `rollback()` fails fast with a clear error rather than silently reapplying a broken RLS/mask |
| ~~`DROP POLICY` / `ALTER TABLE ... DROP ROW FILTER/MASK` exact syntax unverified~~ | **RESOLVED (§17)**: all confirmed working live. `DROP POLICY` has no `IF EXISTS`; `rollback_manager` catches `POLICY_NOT_FOUND` explicitly instead. |
| ~~Column mask `ON COLUMN alias` without `MATCH COLUMNS` unverified~~ | **RESOLVED (§17)**: confirmed `MATCH COLUMNS` is *always* required (not just for multi-table policies) — this was the single biggest design correction from the spike; see §7.3/§7.4. |

---

## 15. Test Strategy

All 15 required scenarios map to `tests/test_table_converter.py` (plus
dedicated files for scope/inventory/policy-strategy/drift/rollback), using a
`FakeUnityCatalogGateway` (in-memory dict-backed fake implementing the
`UnityCatalogGateway` Protocol) — no real Databricks connection needed for
any of these:

| # | Scenario | Fixture setup | Assertion |
|---|---|---|---|
| 1 | Table with RLS only | fake has row filter, no masks | `rls_status=SUCCESS`, `column_mask_status={}` |
| 2 | Table with Column Mask only | fake has masks, no row filter | `rls_status=None`, masks all `SUCCESS` |
| 3 | Table with both | both present | both `SUCCESS`, aggregated `status=SUCCESS` |
| 4 | Table with no security functions | neither present | `status=NOT_ELIGIBLE`, reason `NO_LEGACY_SECURITY_FOUND` |
| 5 | Missing function | RLS references a function fake reports as nonexistent | `status=FAILED`, `error_code=SOURCE_FUNCTION_NOT_FOUND`, no mutation calls recorded on fake |
| 6 | Existing ABAC policy (conflicting) | fake already has a differing policy | `status=NOT_ELIGIBLE`, `EXISTING_ABAC_POLICY_CONFLICT` |
| 7 | Already migrated table | fake has matching policy + legacy already removed | `status=ALREADY_MIGRATED`, zero mutation calls |
| 8 | ABAC creation failure | fake's `create_or_replace_policy` raises | `status=FAILED`, `POLICY_CREATE_FAILED`, legacy untouched (assert fake's row filter still present) |
| 9 | ABAC validation failure | fake creates policy but `show_policies`/`describe_policy` returns mismatched spec | `status=FAILED`, `POLICY_VERIFY_FAILED`, legacy untouched, new policy left for inspection |
| 10 | Old RLS removal failure | fake's `drop_row_filter` raises after policy verified | `status=FAILED`, `LEGACY_REMOVAL_FAILED`, both mechanisms present in fake's final state (assert both) |
| 11 | Multiple masked columns | 3 columns, middle one's `create_or_replace_policy` raises | columns 1,3 `SUCCESS`, column 2 `FAILED`, table-level `status=FAILED` (weakest-link, §6) |
| 12 | Dry run | `dry_run=True` | fake records zero *actual* mutations (a `dry_run_calls` list instead), `status=WOULD_MIGRATE`, output shape matches §9 |
| 13 | Retry/idempotent execution | run `convert_table` twice in sequence against the same fake | 2nd call returns `ALREADY_MIGRATED`, fake shows no duplicate policy objects |
| 14 | Configuration drift | audit says `SUCCESS`, then fake's policy is externally removed before `RECONCILE` | `drift_detection` reports `DRIFT` |
| 15 | Rollback | run `convert_table` (real success) then `rollback()` | fake's legacy row filter/masks restored exactly, only the utility-created policy names removed, any unrelated pre-existing policy on the fake untouched |

Additional non-required-but-recommended tests: `scope_resolver` regex
exclusion behavior; `PolicyStrategy` deterministic-naming stability across
repeated calls; `continue_on_error=false` stopping the run on first
`FAILED`; `max_parallelism` not affecting correctness (same results at
parallelism 1 vs 4 against the fake).

---

## 16. Open Items Before Implementation (updated after §17 spike)

**Items 1-3 from the original list are now RESOLVED** — see §13/§17. What
remains:

1. Decide the audit catalog/schema default names to request as required
   parameters (no hard-coded default per "do not hard-code customer-specific
   names" — but *some* placeholder is still needed for local dev/testing;
   suggest `abac_migration` / `audit` as the dev-time example only).
2. Confirm minimum supported table types for ABAC row filter/column mask
   policies on the target DBR version (views, materialized/streaming
   tables) before enabling those table types in scope resolution.
3. **NEW (discovered during §17 spike): decide the exact reuse-vs-mint
   heuristic for governed tags** (§7.4 point 1) — e.g. should the utility
   ever trust a pre-existing `class.email_address`-style tag as sufficient
   evidence to skip minting `abac_colmask`, or always mint its
   own (safer, more tables tagged, but more account-level tag churn)? This
   is a product/policy decision, not an API-uncertainty one, and is a good
   candidate for a config flag (`prefer_existing_tags: bool`, default
   `true`) rather than a hard-coded choice.
4. **NEW: confirm account-level `CREATE`/`MANAGE` privilege on governed tags
   is available in the *real* target account** (not just this dev/spike
   account) — this worked for our spike token, but per docs it requires
   account-level `CREATE` privilege specifically for governed tags (a
   different privilege domain than the account-group creation that failed
   earlier in this project, §identity) and should be explicitly checked in
   `pre_validation` rather than assumed, exactly like the account-group
   lesson learned earlier.
5. **NEW: decide the exact synthetic tag value format** given the 256-char
   governed-tag-value length limit (confirmed via docs) — e.g. use a short
   hash of `<catalog>.<schema>.<table>.<column>` rather than the raw FQN, to
   stay safely under the limit for deeply-nested/long-named objects, while
   keeping the audit trail able to reverse-map hash → column (store the
   mapping in the audit tables, §4, not derive it from the hash).

---

## 17. API Verification Spike (executed, not just designed)

Per explicit instruction to test every proposed Databricks API before
proceeding to implementation, and to make API usage resilient to throttling
with retry/exponential backoff, the following was actually built and run
against the live `uc_source` workspace (not just documented):

**Built:**
- `abac_migration/uc_gateway/retry.py` — `RetryPolicy` + `with_retries()`:
  classifies HTTP 429/503/504 and connection/timeout exceptions as
  retryable, honors a `Retry-After` header when present, exponential
  backoff with jitter otherwise, hard cap on attempts, never retries
  genuine 4xx/semantic errors (§10.1).
- `abac_migration/uc_gateway/sql_statement_client.py` — a SQL Statement
  Execution API client where every HTTP call goes through `with_retries()`.
- `abac_migration/spike/setup_spike.py` — provisioned an isolated throwaway
  table `ril_raw.abac_api_spike.spike_orders` with a real legacy row filter
  and column mask (same pattern as the sales ABAC demo), so nothing in the
  spike ever touched the actual `sales_abac_demo` tables.
- `abac_migration/spike/api_spike_test.py` — ran through the full proposed
  API surface end to end: discovery (`SHOW POLICIES`, `DESCRIBE POLICY`,
  `abac_policy_definitions`), creation (`CREATE POLICY` for both row filter
  and column mask), coexistence with legacy mechanisms, teardown
  (`ALTER TABLE ... DROP ROW FILTER/MASK`, `DROP POLICY`).
- `abac_migration/spike/test_retry_wrapper.py` — 5 unit tests validating the
  retry wrapper's throttling/backoff/jitter/non-retry behavior using
  simulated responses (safer and more deterministic than trying to force
  real 429s against a live, shared, low-traffic workspace).

**Outcome:** every statement in §13 was executed live at least once; two
statements failed on the first attempt in a way that changed the design
(governed tags being mandatory, and `DROP POLICY` having no `IF EXISTS`) —
both are now reflected in §7.3/§7.4/§13/§14 rather than left as
documentation-only assumptions. All spike-created Unity Catalog objects
(the throwaway schema/table/functions and the `abac_migration_col_id`
governed tag) were torn down at the end of the spike; nothing was left
behind in `uc_source`.
