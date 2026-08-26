# ABAC Migration Utility

Converts legacy Unity Catalog row filters and column masks into
attribute-based (ABAC) `ROW_FILTER` / `COLUMN_MASK` policies, backed by
governed tags. Deployed as a Databricks Asset Bundle (`databricks.yml`) that
packages the `abac_migration` Python package as a wheel and runs it via
Databricks Jobs (serverless compute). See `abac_migration/DESIGN.md` for the
full architecture/design spec — this file is a practical, task-oriented
guide focused on **how to run the tool and what each mode does**.

## Quick start

```bash
databricks bundle deploy -t source          # or -t target
databricks bundle run abac_migration_job -t source \
  --params mode=INVENTORY,scope_type=SELECTED_CATALOGS,catalogs='["ril_raw"]',dry_run=true
```

All parameters map 1:1 to `RunConfig` fields (`abac_migration/config/models.py`)
and are read from job widgets by `abac_migration/config/config_loader.py`.
`audit_catalog`/`audit_schema` are always required (no hard-coded default);
every other parameter has a safe default, and `dry_run` defaults to `true`.

## Modes

The `mode` parameter (`abac_migration.config.models.Mode`) selects which
behavior `migration_engine.run()` executes. Every mode except `ROLLBACK`
first resolves scope (`scope_type`/`catalogs`/`schemas`/`tables`) into a
concrete list of tables; `ROLLBACK` instead looks up the tables touched by a
specific prior `run_id` from the audit table.

There are two ways to run a migration: **atomic** (`MIGRATE`/
`INVENTORY_AND_MIGRATE`, unchanged since the first version of this tool) or
**isolated/phased** (`APPLY_ABAC` then `FINALIZE`, run as separate jobs/runs
— see "Isolated-phase modes" below). Both reach the exact same end state;
the isolated path just lets you pause, review, and only remove legacy
security once you're confident the new ABAC policy is correct.

| Mode | Reads UC | Mutates UC | Persists to | Typical use |
|---|---|---|---|---|
| `INVENTORY` | yes | never | `inventory` table only | Safe discovery/reporting pass — see what's out there and what's eligible before touching anything |
| `MIGRATE` | yes | yes (unless `dry_run=true`) | `inventory` + `migration_audit` | Atomic: create+verify ABAC *and* remove legacy in one run, per eligible table |
| `INVENTORY_AND_MIGRATE` | yes | yes (unless `dry_run=true`) | `inventory` + `migration_audit` | Same as `MIGRATE` — inventory is always (re)built as part of migration anyway, so this is just the explicit/self-documenting name for "do both in one run" |
| `APPLY_ABAC` | yes | yes (unless `dry_run=true`) | `inventory` + `migration_audit` (`migration_phase=ABAC_APPLIED`) | **Isolated phase 1 of 2.** Creates the governed tag(s) + ABAC policy for every eligible object, then stops — legacy row filter/column mask is deliberately left in place. Table ends up with **both** mechanisms active at once. Never a security gap: the new policy is strictly additive, nothing protective was removed |
| `FINALIZE` | yes | yes (unless `dry_run=true`) | `migration_audit` (`migration_phase=FINALIZED`) | **Isolated phase 2 of 2.** For every object that already has its ABAC policy applied (a prior `APPLY_ABAC`/`MIGRATE` run), removes the legacy row filter/column mask and does the final verification. Refuses (`NOT_ELIGIBLE`/`ABAC_NOT_APPLIED_YET`) for anything `APPLY_ABAC` hasn't touched yet — it never creates a policy itself |
| `VERIFY` | yes | never | `migration_audit` (validation status) | Post-migration health check: confirms the ABAC policy still exists, and reports whether the legacy mechanism is gone (`SUCCESS`) or still present (`ABAC_APPLIED` — expected mid-pipeline state, not a failure) |
| `RECONCILE` | yes | never | `migration_audit` (drift flags) | Compares the audit table's last-known-good state against live UC to catch drift (e.g. someone else deleted the policy, or manually restored the legacy filter) |
| `ROLLBACK` | yes | yes (unless `dry_run=true`) | `migration_audit` | Undo one specific run: restores the original legacy row filter/masks and removes only the ABAC policies **that run** created |

### `INVENTORY`

Read-only. Resolves scope, then for every table calls
`inventory_manager.build_inventory_record()`, which discovers existing row
filters / column masks / ABAC policies and classifies each table as
`ELIGIBLE` or `NOT_ELIGIBLE` (with a reason — `NO_LEGACY_SECURITY_FOUND`,
`UNSUPPORTED_TABLE_TYPE`, `PERMISSION_DENIED`, ...). Every record is
appended to the `inventory` table. **No conversion happens** — this is the
mode to run first against a new scope to see what you're dealing with
before committing to a real migration.

### `MIGRATE` / `INVENTORY_AND_MIGRATE`

Does everything `INVENTORY` does, then additionally:

1. Filters to just the `ELIGIBLE` tables.
2. Runs the single-threaded "Prepare Governed Tags" phase
   (`tag_provisioner.TagProvisioner.prepare()`) once for every column that
   needs a tag across the whole batch — this has to happen before parallel
   conversion to avoid a read-modify-write race on
   `ALTER GOVERNED TAG ... SET VALUES` (declarative/full-replace, not
   additive).
3. Converts each eligible table in parallel (`max_parallelism` workers) via
   `table_converter.convert_table()`: discover → validate → capture
   rollback metadata → create ABAC policy → verify it → remove the legacy
   row filter/mask → verify final state. Each per-table/per-object result
   (`SUCCESS` / `FAILED` / `SKIPPED` / `ALREADY_MIGRATED` / `WOULD_MIGRATE`)
   is appended to `migration_audit`, one row per attempt (so history is
   preserved across re-runs, never overwritten).

If `dry_run=true` (the default), every mutating gateway call becomes a
no-op that reports what *would* have happened (`WOULD_MIGRATE`), so a dry
run exercises the exact same code path as a real one.

Idempotent by design: a table that's already fully migrated (ABAC policy
present, legacy mechanism gone) is reported `ALREADY_MIGRATED` and left
untouched; a table where legacy removal previously failed (so both
mechanisms are still present) is retried automatically rather than being
skipped.

There is currently no behavioral difference between `MIGRATE` and
`INVENTORY_AND_MIGRATE` in `migration_engine.run()` — inventory is always
built (and persisted) as a side effect of resolving eligibility, whichever
name you pick. `INVENTORY_AND_MIGRATE` exists as the explicit/intent-revealing
name for job configs that want to make it obvious a single run does both.

### Isolated-phase modes: `APPLY_ABAC` then `FINALIZE`

Deployed as two separate DAB jobs (`abac_migration_apply_abac_job`,
`abac_migration_finalize_job`) so they can be run as distinct steps —
typically with a manual review/bake-in period between them — instead of one
atomic `MIGRATE`. Both jobs share the same `notebooks/abac_migration_run.py`
entry point as the atomic job; only the `mode` parameter differs.

**`APPLY_ABAC`** does everything `MIGRATE` does *up to and including*
creating + verifying the new ABAC policy, then stops:

1. Filters to `ELIGIBLE` tables (same as `MIGRATE`).
2. Runs the same serialized "Prepare Governed Tags" phase — this is also
   where the one-tag-per-function scheme (see below) actually mints tags.
3. Per eligible object: create/verify the ABAC policy, record
   `StepStatus.ABAC_APPLIED` (or `WOULD_APPLY_ABAC` under `dry_run=true`).
   **Never removes the legacy row filter/column mask.** The audit row for
   this step carries `migration_phase=ABAC_APPLIED` — an explicit "this is
   not final yet" marker, distinct from the granular `status` column.

**`FINALIZE`** does the second half:

1. Resolves scope the same way, but skips tag preparation entirely — by
   construction, every object it touches was already tagged by a prior
   `APPLY_ABAC` (or atomic `MIGRATE`) run.
2. Per eligible object: if no matching ABAC policy exists yet, reports
   `NOT_ELIGIBLE`/`ABAC_NOT_APPLIED_YET` and does nothing (run `APPLY_ABAC`
   first). Otherwise, re-confirms the policy right before mutating, removes
   the legacy mechanism, and verifies the final state — `StepStatus.SUCCESS`
   with `migration_phase=FINALIZED` (or `WOULD_FINALIZE` under `dry_run=true`).

Both phases are independently idempotent and safe to re-run: `APPLY_ABAC`
re-running on an already-`ABAC_APPLIED` object just re-issues the same
`CREATE OR REPLACE POLICY` (no-op); `FINALIZE` re-running on an
already-`FINALIZED` object reports `ALREADY_MIGRATED`. Running
`APPLY_ABAC` immediately followed by `FINALIZE` (e.g. in a test) reaches
the identical live-UC end state as one atomic `MIGRATE` call.

### `VERIFY`

Read-only. For every table in scope, re-runs the same discover/verify
checks each plugin (`RLSMigrationPlugin`, `ColumnMaskMigrationPlugin`) uses
internally right after a migration, but standalone — independent of whether
a migration just ran. Confirms the expected ABAC policy still exists (with
the expected function/definition) and the corresponding legacy row
filter/mask is still absent. A table this utility never touched returns
`NOT_ELIGIBLE` (not a failure) rather than `FAILED`. Use this to spot-check
health of a scope at any time without touching UC.

### `RECONCILE`

Read-only. For every table in scope, looks up that table's **last known
successful** status in the `migration_audit` table
(`audit_repository.latest_status()`), then calls the same `verify_table()`
check `VERIFY` uses and compares:

- Never migrated by this utility → not drift (`NEVER_MIGRATED_BY_THIS_UTILITY`).
- Last recorded run wasn't successful → not drift (`LAST_RUN_WAS_NOT_SUCCESSFUL`).
- Live state still matches → not drift (`LIVE_STATE_MATCHES_AUDIT`).
- Live state no longer matches → `drift_detected=True` with the specific
  `error_code` from the failed verification (e.g. someone dropped the
  policy, or manually re-added the legacy row filter).

`RECONCILE` only **reports** drift — it never attempts to repair it
automatically (out of scope for v1 by design).

### `ROLLBACK`

The only mode that doesn't use `scope_type`/`catalogs`/`schemas`/`tables` —
instead it requires `run_id` (identifying a specific prior `MIGRATE` /
`INVENTORY_AND_MIGRATE` run) and looks up every row that run wrote to
`migration_audit` that has non-empty `rollback_metadata` (captured
**before** any mutation was made during that run, per table/object). For
each one, `rollback_manager.rollback_table()`:

1. Restores the original legacy row filter / column mask exactly as
   captured in `rollback_metadata`.
2. Removes **only** the ABAC policy this utility created for that specific
   object — it never touches a policy it didn't create itself.

Reported per-object as `ROLLED_BACK` / `WOULD_ROLLBACK` (dry run) /
`FAILED` / `SKIPPED` (no rollback metadata available for that row). Like
every other mutating mode, `dry_run=true` (default) short-circuits before
any real mutation.

## Governed tags: one tag key per legacy function

`tag_provisioner.py` mints one governed tag **key** per distinct legacy SQL
function (row-filter or mask function), derived deterministically from its
fully-qualified name — e.g. `cat.sch.rf_business_unit_fn` becomes tag key
`abac_rls_cat_sch_rf_business_unit_fn`. This replaced an earlier design that
used just two shared keys (`abac_rls` / `abac_colmask`) for every row
filter / mask account-wide. Per-function keys mean `SHOW GOVERNED TAGS` /
`DESCRIBE GOVERNED TAG` on any one key maps 1:1 back to the specific legacy
function that used to enforce that security, at the cost of many more tag
keys for a large migration. Tag *values* are still minted per
(table, column) — unique within a table, as required for `MATCH COLUMNS` to
unambiguously target one column (`abac_migration/DESIGN.md` §7.4).

## LLM-assisted PII tagging (INVENTORY-only)

Set `enable_llm_pii_tagging=true` (off by default) to have `INVENTORY`
classify each legacy row-filter/column-mask function's likely PII category
from its name and governed column(s) alone — never from row data — via a
Databricks Foundation Model API endpoint, invoked with `ai_query()` through
the same gateway used for every other SQL statement
(`uc_gateway.gateway.DatabricksUnityCatalogGateway.suggest_pii_tag`).
Results land in the `inventory` table as `row_filter_suggested_pii_tag`
(one value) and `column_mask_suggested_pii_tags` (a JSON-encoded
`{column: tag}` map), picked from a fixed vocabulary (`ssn`, `email`,
`phone`, `credit_card`, `date_of_birth`, `address`, `name`, `national_id`,
`bank_account`, `health`, `salary`, `business_unit`, `region`, `other`,
`none`). Purely advisory — it never influences eligibility or any migration
decision, and a classification failure (endpoint unavailable, network
error, etc.) degrades to `NULL` rather than failing the run. Override the
endpoint with `pii_llm_endpoint` (default `databricks-meta-llama-3-3-70b-instruct`)
if that specific model isn't enabled on your account.

## Safety invariant across all mutating modes

Both `MIGRATE`/`INVENTORY_AND_MIGRATE` and `ROLLBACK` follow the same
non-negotiable ordering: **the new mechanism is created and verified before
the old one is ever removed.** If verification of the new ABAC policy
fails, the legacy row filter/mask is left completely untouched and the
table is reported `FAILED` for manual inspection. If the *removal* of the
legacy mechanism fails after the ABAC policy was successfully verified, the
table is left with **both** mechanisms active simultaneously (over-protective,
never a security gap) and flagged loudly (`error_code=OLD_MECHANISM_REMOVAL_UNVERIFIED`)
rather than being silently reported as done — see `abac_migration/DESIGN.md` §8
for the full state machine.

## Further reading

- `abac_migration/DESIGN.md` — full architecture, data model, plugin
  interfaces, error taxonomy, API resilience strategy, and the live API
  verification spike findings that shaped this design.
- `resources/jobs.yml` — the atomic job (`abac_migration_job`, `mode`
  defaults to `INVENTORY_AND_MIGRATE`) and the full scenario end-to-end test
  job (`abac_migration_full_e2e_test_job`).
- `resources/phased_jobs.yml` — the three isolated-phase jobs
  (`abac_migration_inventory_job`, `abac_migration_apply_abac_job`,
  `abac_migration_finalize_job`), meant to be run in that order.
