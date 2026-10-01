# Changelog

All notable changes to the ABAC Migration Utility are documented in this
file, one entry per merged change, newest first.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is date-anchored `MAJOR.MINOR.0` (no PATCH releases yet - every
change so far has been additive/feature-level, never a hotfix on a
released version) rather than strict [SemVer](https://semver.org/), since
this tool has no external consumers/API contract to version against yet -
`MAJOR` is reserved for a future genuinely breaking change (e.g. a config
parameter rename with no backward-compatible default). Each entry links
the commit(s) it corresponds to for full traceability; commit messages
carry the complete technical detail (live-test evidence, exact bug repro,
etc.) - this file is the scannable summary, not a replacement for `git log`.

**Change control convention (read this before your next change):** add a
new entry at the top of this file, in the same PR/commit as the change
itself, before merging - the same discipline already applied to
`DESIGN.md`/`SOP.md`/`README.md`. Use `Added`/`Changed`/`Fixed`/`Removed`/
`Security` subsections (omit any that don't apply); bump `MINOR` for any
user-facing behavior/parameter change, `PATCH` only for a genuine post-
release hotfix once this tool has a first tagged release.

## [Unreleased]

Nothing yet.

## [0.12.0] - 2026-10-01

Widened the per-table inventory error handling introduced in [0.11.0]
(below) from a whitelist of three named reasons into a genuine catch-all:
**any** `UCGatewayError` on one table - including a kind this tool has
never specifically seen or named before - is now recorded as `NOT_ELIGIBLE`
and skipped, never re-raised. Explicit product decision: "one table's
failure must never impact other tables," full stop, not just for the
specific error shapes we happen to have already diagnosed.

### Changed
- `inventory_manager.py`: `build_inventory_record`'s except branch no
  longer ends with `else: raise` for anything outside
  `PERMISSION_DENIED`/`SAMPLE_TABLE_PERMISSIONS`/`FEDERATION_UNREACHABLE`/
  `UNSUPPORTED_DATA_SOURCE` - it now falls through to a new
  `eligibility_reason=UNKNOWN_GATEWAY_ERROR` instead. The raw exception
  text is still captured in `row_filter_expression_text` (same mechanism
  as the three named reasons), so nothing is silently swallowed - it's
  fully visible in the audit trail for investigation, just never allowed
  to abort the run. Deliberately still scoped to `UCGatewayError`
  specifically (the one documented seam to Databricks), not a bare
  `except Exception` - a bug in this tool's own code should still crash
  loudly.
- **Not changed in this pass**: `scope_resolver.py`'s catalog/schema-level
  `_skip_or_raise` remains a selective whitelist (`FEDERATION_UNREACHABLE`/
  `UNSUPPORTED_DATA_SOURCE` for every scope_type; `PERMISSION_DENIED` only
  for auto-discovered `ALL_CATALOGS`/`ALL_SCHEMAS`) - this change was
  scoped to the per-*table* inventory path only, per the exact ask. An
  unanticipated error while *listing* a catalog/schema still aborts scope
  resolution before inventory starts; extending the same catch-all there
  is a candidate follow-up if wanted.

### Verification
- Updated `test_non_permission_error_still_propagates_from_inventory` (which
  asserted the old `raise`-on-anything-else behavior) into
  `test_unanticipated_gateway_error_is_not_eligible_and_does_not_raise`,
  asserting the new catch-all instead.
- Full unit suite: 224/224 passing (no new test count change - existing
  test repurposed rather than duplicated).

## [0.11.0] - 2026-09-30

Fixed a live crash: a Lakehouse Federation catalog/schema/table (e.g.
`CREATE FOREIGN CATALOG ... USING CONNECTION ...` pointing at an external
PostgreSQL source) that Unity Catalog can't currently reach used to abort
the *entire* run with an uncaught `UCGatewayError` (`FAILED_JDBC.CONNECTION`)
the moment scope resolution or inventory touched it - even under
`SELECTED_CATALOGS`/`SELECTED_SCHEMAS` scope, where every other error class
is deliberately treated as a hard failure the caller must see.

### Fixed
- New `is_federation_unreachable()` helper (`uc_gateway/gateway.py`),
  matching the confirmed-live `FAILED_JDBC` marker - analogous to the
  existing `is_permission_denied()`, but skipped for **every** `scope_type`
  (not just the auto-discovered ALL_CATALOGS/ALL_SCHEMAS): a federated
  object can never be a valid ABAC/governed-tags target regardless of
  connectivity, so explicitly listing it doesn't change the outcome, and
  there's nothing for the caller to fix by seeing it raised.
- `scope_resolver.py`: a catalog/schema that can't even be listed this way
  is now skipped (with a printed diagnostic) instead of aborting the whole
  scope resolution, for every scope type.
- `inventory_manager.py`: the same error surfacing one layer deeper - an
  individual table (in an otherwise-listable schema) whose
  `DESCRIBE TABLE EXTENDED` itself needs a live connection (e.g. to fetch
  synced comments) and fails - is now recorded as an inventory row with
  `migration_eligibility=NOT_ELIGIBLE`, `eligibility_reason=
  FEDERATION_UNREACHABLE` instead of aborting the run. No DDL is ever
  issued against it (same "eligible tables only" filter every other
  NOT_ELIGIBLE reason already relies on). This inventory row IS the audit
  record of the failure - no separate error-logging mechanism needed. The
  raw exception text (`str(exc)`, e.g. `"[BAD_REQUEST] [FAILED_JDBC.
  CONNECTION] Failed JDBC jdbc:postgresql:... SQLSTATE: HV000"`) is
  captured too, in `row_filter_expression_text` - always `""` on this
  early-return path otherwise (there was never a real row filter to read),
  so no new column was needed to preserve it.
- New `is_unsupported_data_source()` helper (`uc_gateway/gateway.py`),
  matching marker `DATA_SOURCE_NOT_FOUND` - same shape and same
  `scope_resolver.py`/`inventory_manager.py` treatment as
  `is_federation_unreachable()` above (incl. reusing
  `row_filter_expression_text`, `eligibility_reason=
  UNSUPPORTED_DATA_SOURCE`), added same-day after discovering, while
  live-testing the fix above, a second real crash cause with a different
  root cause: a securable whose provider/connector the SQL warehouse's
  runtime doesn't have registered at all (confirmed live against a real
  pre-existing Vector Search index registered in Unity Catalog as a
  `FOREIGN` table, `ril_insurance.rag.docindex`) - distinct from
  `FEDERATION_UNREACHABLE` in that this has nothing to do with reachability
  of an external connection; `DESCRIBE TABLE EXTENDED` fails outright
  regardless.

Deliberately kept reactive-only (explicit product decision, 2026-09-30):
no second lookup call (e.g. a confirmatory `information_schema.tables` or
`DESCRIBE DETAIL` query) was added to *proactively* identify a federated/
foreign/synced table before touching it - every table still gets exactly
the one `DESCRIBE TABLE EXTENDED` call it always did. A brief attempt at
such a confirmatory lookup (to also catch a *reachable* federated table
that `DESCRIBE TABLE EXTENDED` misreports as plain `EXTERNAL`, per
Databricks docs) was implemented and then reverted in this same change,
per explicit instruction to avoid the extra round trip - see "Known
limitation" below.

### Verification
- 8 new unit tests total: `test_scope_resolver.py` x6 (3 for
  `FEDERATION_UNREACHABLE` + 3 mirroring for `UNSUPPORTED_DATA_SOURCE`,
  across SELECTED_CATALOGS/ALL_CATALOGS/SELECTED_SCHEMAS) and
  `test_inventory_manager.py` x2 (one per reason) - covering: all three
  scope types gracefully skip an unreachable/unsupported catalog or
  schema; a genuine `PERMISSION_DENIED` on an explicitly-requested catalog
  still correctly propagates (behavior unchanged); a per-table failure of
  either kind during inventory is recorded NOT_ELIGIBLE rather than raised.
- Full unit suite: 224/224 passing (216 pre-existing + 8 new).
- `UNSUPPORTED_DATA_SOURCE`/`is_unsupported_data_source()` **confirmed
  live, end-to-end, through the real (non-fake) `DatabricksUnityCatalogGateway`
  + `ResilientDatabricksSQL`**, 2026-09-30: ran `build_inventory_record()`
  against the real pre-existing table `ril_insurance.rag.docindex` on the
  `uc_target` workspace. Before this fix it raised an uncaught
  `UCGatewayError` (`[DATA_SOURCE_NOT_FOUND] Failed to find the data
  source: unsupported...`); after this fix it returns
  `migration_eligibility=NOT_ELIGIBLE`,
  `eligibility_reason=UNSUPPORTED_DATA_SOURCE`, with the raw error text in
  `row_filter_expression_text` - zero infrastructure created, zero
  catalogs/connections touched.
- `FEDERATION_UNREACHABLE`/`is_federation_unreachable()` itself (the
  `FAILED_JDBC` marker) remains **not yet re-verified live** against a
  real unreachable federated catalog - every attempt this session to
  provision a throwaway `CREATE CONNECTION ... TYPE postgresql` + foreign
  catalog for this specific purpose was blocked by an account-wide
  `QUOTA_EXCEEDED.UC_RESOURCE_QUOTA_EXCEEDED` (1000/1000 catalogs) shared
  across every reachable workspace/metastore available to this project.
  Also confirmed live, directly from a Unity Catalog error message, that
  there is no schema- or table-level entry point for JDBC federation in
  Databricks at all (`CREATE TABLE ... USING org.apache.spark.sql.jdbc` is
  explicitly rejected: `UC_FILE_SCHEME_FOR_TABLE_CREATION_NOT_SUPPORTED`) -
  `CREATE FOREIGN CATALOG` is the only path, so this specific marker
  cannot be live-tested without catalog-quota headroom. The fix is
  grounded directly in the exact `error_code`/message the user hit live
  (not a guess), but is flagged here rather than claimed as "confirmed
  live" per this project's own discipline.
- **Known limitation, not fixed by design**: a *reachable* Lakehouse
  Federation table never raises `FAILED_JDBC` at all, so this fix doesn't
  cover it. Databricks docs confirm `DESCRIBE TABLE EXTENDED` reports
  `Type: EXTERNAL` for a reachable HMS/Glue-federated foreign table (not
  `FOREIGN`) - "mimics the behavior of running this command on the
  hive_metastore catalog" (not independently confirmed either way for
  Connection-based federation, e.g. Postgres/MySQL/Snowflake). Such a table
  would currently pass `SUPPORTED_TABLE_TYPES` and be marked `ELIGIBLE` -
  real DDL would be fired against it. Confirmed live in this session via a
  manual `DESCRIBE DETAIL` on a synced table showing `format: postgresql`
  - a real, reachable signal that exists, but per explicit decision this
  tool does not query it (or any other confirmatory metadata) proactively;
  only an actual failure on the one call this tool already makes triggers
  a skip.

## [0.10.0] - 2026-09-28

Grant tag-policy access to configured SPNs/groups on every governed tag
this tool creates or reuses - so a downstream service principal can itself
attach those tags (`ALTER TABLE ... SET TAGS (...)`) or reference them by
key in its own functions/policies, without needing this tool's own
identity to grant it manually after every run.

### Added
- `tag_grantee_principals` (JSON list, default `[]`) / `tag_grantee_role`
  (`"ASSIGN"` default | `"MANAGE"`) `RunConfig` parameters - empty by
  default, zero behavior/latency change unless configured.
- `uc_gateway/access_control_client.py` - a new REST client for the
  Account Access Control Proxy API. This is the **one operation in the
  whole tool that is not plain SQL** - granting tag-policy access has no
  SQL grammar at all (confirmed live against the target workspace).
- `DatabricksUnityCatalogGateway.grant_tag_principals()` - never raises;
  a grant failure is recorded as `TagGrantResult(status="FAILED", ...)`
  and never aborts a table's migration.
- New `tag_grants` audit table (tag-scoped, not table-scoped) recording
  every grant attempt, including failures.
- `TagProvisioner.prepare()` grants the configured role to every
  **distinct** governed tag it resolves each call - newly minted **or
  reused** (self-healing: a tag minted before this parameter existed
  gets healed on the next run that touches it).
- Bundle wiring: `tag_grantee_principals`/`tag_grantee_role` variables in
  `databricks.yml`, exposed on `abac_migration_job` and all 3 phased jobs.
- Live end-to-end spike (`spike/test_tag_grant_full_feature_live.py`)
  confirming the real production code path against a real SPN.

### Verification
- 216/216 unit tests passing (38 new).
- Full 9-table live regression (`MANAGED`/`STREAMING_TABLE`/
  `MATERIALIZED_VIEW` x RLS/mask/both) re-run end to end - `INVENTORY` ->
  `APPLY_ABAC` -> live `SELECT` -> `FINALIZE` -> live `SELECT` ->
  `ROLLBACK` - zero regressions, `tag_grants` table created cleanly with
  0 rows (feature off by default).

Commit: [`baa256b`](https://github.com/vivekravichandiran/abac-migration-utility/commit/baa256b)

## [0.9.0] - 2026-09-15

### Added
- `FULL_REGRESSION_TEST_CASES.md` - master regression document covering
  all 9 combinations of `{MANAGED, STREAMING_TABLE, MATERIALIZED_VIEW} x
  {RLS-only, mask-only, both}` side by side in one catalog, across
  `INVENTORY`/`APPLY_ABAC`/`FINALIZE`/`ROLLBACK` plus a full re-run after
  rollback (idempotency check).
- `spike/test_full_regression_live.py` / `spike/test_full_regression_rollback_live.py`
  - reusable live-regression automation, not just one-off scripts.

### Verification
- 178/178 unit tests; 9/9 tables pass every phase; 0 defects found.

Commit: [`53646cc`](https://github.com/vivekravichandiran/abac-migration-utility/commit/53646cc)

## [0.8.0] - 2026-09-15

Track B of the streaming-tables ABAC plan.

### Added
- `MATERIALIZED_VIEW` added to `SUPPORTED_TABLE_TYPES` - only plain `VIEW`
  remains unsupported now.
- `gateway._alter_keyword_for(table_type)` - `ALTER MATERIALIZED VIEW ...`
  for materialized views (a plain `ALTER TABLE` hard-fails against one
  with `EXPECT_TABLE_NOT_VIEW.NO_ALTERNATIVE`), `ALTER TABLE ...`
  otherwise. Threaded through all 5 mutating gateway methods.
- `MATERIALIZED_VIEW_SUPPORT_TEST_CASES.md`.

### Fixed
- **DEF-01**: `describe_table_security()` mis-parsed a materialized view's
  trailing `Total Size (bytes)` row (which sits directly under `# Column
  Masks` with no separator in `DESCRIBE TABLE EXTENDED` output) as a
  phantom masked column. Now requires the row's function field to be
  backtick-quoted.

### Verification
- 178/178 unit tests (was 167). Live end-to-end verified against a real
  materialized view - full `INVENTORY` -> `APPLY_ABAC` -> `FINALIZE`
  cycle, correct enforcement at every stage.

Commit: [`599f44b`](https://github.com/vivekravichandiran/abac-migration-utility/commit/599f44b)

## [0.7.0] - 2026-09-15

Track A of the streaming-tables ABAC plan.

### Added
- `STREAMING_TABLE` added to `SUPPORTED_TABLE_TYPES` - confirmed live that
  plain `ALTER TABLE ...` DDL works unmodified against a real streaming
  table, no gateway changes needed (unlike `MATERIALIZED_VIEW`, tracked as
  the immediate follow-up - see 0.8.0).
- `STREAMING_TABLE_SUPPORT_TEST_CASES.md` (TC-01 through TC-05, with the
  DEF-01 defect first found here and fixed in 0.8.0).
- 6-flavor live regression (`{MANAGED, STREAMING_TABLE} x {RLS-only,
  mask-only, both}`) side by side in one catalog.

Commits: [`17374af`](https://github.com/vivekravichandiran/abac-migration-utility/commit/17374af), [`07edaad`](https://github.com/vivekravichandiran/abac-migration-utility/commit/07edaad), [`6c3731e`](https://github.com/vivekravichandiran/abac-migration-utility/commit/6c3731e)

## [0.6.0] - 2026-09-09

### Removed
- The per-column disambiguating tag-*value* fallback for `ROW_FILTER`/
  `COLUMN_MASK` collisions. **No tag VALUE is ever minted by this tool
  now, for either role.**

### Changed
- `COLUMN_MASK`: sharing one bare key-only governed tag across 2+ columns
  of the same table is confirmed live-safe (each resolves independently
  at mask-application time) - masks always stay key-only.
- `ROW_FILTER`: the same sharing pattern causes a deferred, read-time
  `UC_ABAC_AMBIGUOUS_COLUMN_MATCH` failure (confirmed live) - Unity
  Catalog only allows one active row filter per table anyway, so this is
  not a supportable configuration. `tag_provisioner._split_by_collision()`
  now skips assigning a tag to colliding columns entirely; the affected
  table's `ROW_FILTER` step fails gracefully with a dedicated
  `RLS_TAG_COLLISION_UNRESOLVABLE` error code (recorded to
  `migration_audit`, run not aborted) instead.

Commit: [`4a66400`](https://github.com/vivekravichandiran/abac-migration-utility/commit/4a66400)

## [0.5.0] - 2026-09-07

### Added
- `TableBasedPolicyStrategy` now namespaces its previously-constant
  `abac_migrated_row_filter`/`abac_migrated_mask_<column>` policy names
  with `tag_team_prefix` too, matching `CatalogBasedPolicyStrategy`.
- `ROLLBACK` is now a true best-effort pass: every plugin's `rollback()`
  call and every audit row is individually try/except-wrapped, so one
  failure never aborts the rest. Every outcome (`ROLLED_BACK`/
  `WOULD_ROLLBACK`/`FAILED`/`SKIPPED`) is now persisted to
  `migration_audit`, which `ROLLBACK` previously never wrote to at all.

### Fixed
- `CatalogBasedPolicyStrategy`'s existing-policy recovery (via column
  tags) used a bare `abac_rls_`/`abac_colmask_` `startswith` check with no
  `team_prefix` scoping - re-running `APPLY_ABAC` with a *new*
  `tag_team_prefix` against an already-migrated catalog incorrectly
  reported `ALREADY_MIGRATED`. Now reconstructs the full deterministic
  `(team_prefix, catalog, schema)` prefix before matching.

### Verification
- 161 tests passing (was 132). Live-verified end to end, including a
  mixed `FAILED`/`ROLLED_BACK` outcome for one `ROLLBACK` run without
  aborting.

Commit: [`be28ba1`](https://github.com/vivekravichandiran/abac-migration-utility/commit/be28ba1)

## [0.4.0] - 2026-09-04

### Added
- `policy_scope` config/job parameter (`TABLE` default | `CATALOG`) -
  a second, fully modular `PolicyStrategy` implementation
  (`CatalogBasedPolicyStrategy`) alongside the existing, unchanged
  `TableBasedPolicyStrategy`. `CATALOG` creates one ABAC policy `ON
  CATALOG` per legacy function, shared by every table that function used
  to guard, instead of one policy per table.
- `catalog_scope` bundle target (renamed from `catalog_scope_test`) and
  `ril_full_access_test` fixture catalog, dedicated to validating
  `policy_scope=CATALOG` end to end.
- Generalized `gateway.show_policies`/`describe_policy`/`drop_policy` to
  take an `on_securable` string (`ON TABLE ...` | `ON CATALOG ...`)
  instead of a bare `TableRef`, so both scopes share one gateway surface.

### Verification
- 132 tests passing. Live-verified `INVENTORY` -> `APPLY_ABAC` ->
  `FINALIZE` against `policy_scope=CATALOG`: exactly one shared policy per
  legacy function (not one per table), sibling tables correctly left
  untouched by a single-table `FINALIZE`.

Commits: [`128e8e1`](https://github.com/vivekravichandiran/abac-migration-utility/commit/128e8e1), [`ac975c1`](https://github.com/vivekravichandiran/abac-migration-utility/commit/ac975c1), [`aad5243`](https://github.com/vivekravichandiran/abac-migration-utility/commit/aad5243), [`0fb7367`](https://github.com/vivekravichandiran/abac-migration-utility/commit/0fb7367), [`8f0aef4`](https://github.com/vivekravichandiran/abac-migration-utility/commit/8f0aef4)

## [0.3.0] - 2026-09-04

### Added
- `policy_except_principals` (JSON array, default `[]`) - principals fully
  exempted (`TO ... EXCEPT principal [, ...]`) from every ABAC policy a
  run creates, e.g. a service principal running unmasked ETL.

### Fixed
- `PolicySpec.to_principals`' `NamedTuple` default used
  `dataclasses.field(default_factory=...)`, which `NamedTuple` never
  evaluates - harmless in practice since every real caller passed it
  explicitly, corrected while adding `except_principals`.

### Verification
- 101 tests passing.

Commit: [`bc3a801`](https://github.com/vivekravichandiran/abac-migration-utility/commit/bc3a801)

## [0.2.0] - 2026-09-01

### Changed
- Deployment model: switched from wheel packaging (`setup.py bdist_wheel`)
  to plain workspace-file sync via Databricks Asset Bundles - no Python
  toolchain needed in CI, just the Databricks CLI.

### Fixed
- `ensure_tables_exist()`'s `CREATE TABLE IF NOT EXISTS` was a no-op
  against a pre-existing audit/inventory table with an older/narrower
  schema, breaking `CREATE OR REPLACE VIEW migration_audit_latest` with
  `UNRESOLVED_COLUMN`. Added `_add_missing_columns()`/`_existing_columns()`
  to backfill via `ALTER TABLE ADD COLUMNS`.
- `list_column_tags()` didn't normalize the empty-string `tag_value`
  Databricks persists for a key-only tag to `None` - fed the raw `''`
  into `has_tag_value(key, '')` on every rerun that reused an existing
  tag, which Unity Catalog's policy compiler deterministically rejects.
  Confirmed live end to end after the fix (12/12 tables succeeding).

Commit: [`72fd6bf`](https://github.com/vivekravichandiran/abac-migration-utility/commit/72fd6bf)

## [0.1.0] - 2026-08-26

### Added
- Initial release: converts legacy Unity Catalog table-level row filters
  and column masks into ABAC row-filter/column-mask policies, with
  governed-tag provisioning, isolated (`INVENTORY` -> `APPLY_ABAC` ->
  `FINALIZE`) and atomic (`MIGRATE`) migration modes, audit/inventory
  tracking, LLM-assisted PII tagging, `ROLLBACK` support, and Databricks
  Asset Bundle deployment.

Commit: [`71326ca`](https://github.com/vivekravichandiran/abac-migration-utility/commit/71326ca)
