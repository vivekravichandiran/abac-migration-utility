# Test Case Document — STREAMING_TABLE Eligibility & Full-Lifecycle Support

**Feature under test:** ABAC Migration Utility support for `STREAMING_TABLE`
objects (Track A of the streaming-tables ABAC plan; `MATERIALIZED_VIEW`
support is Track B, tracked separately and **not** in scope for this
document — see `DESIGN.md` §16 item 2).

**Code change under test:** `abac_migration/inventory/inventory_manager.py`
— `SUPPORTED_TABLE_TYPES` extended from `{MANAGED, EXTERNAL}` to
`{MANAGED, EXTERNAL, STREAMING_TABLE}`. No other production code changed.

**Test environment:** Databricks workspace `adb-7405616318078204`
(profile `ril_catalog_test_pat`, OAuth M2M service principal), Serverless
SQL warehouse `5fe1692f119e2528` ("Serverless Starter Warehouse").

**Test date:** 2026-09-15

**Overall result: ✅ ALL TEST CASES PASSED**

---

## 1. Automated unit test suite (regression)

| Item | Detail |
|---|---|
| Command | `python3 -m pytest abac_migration/tests/ -q` |
| Result | **167 passed**, 0 failed |
| New tests added for this feature | `test_streaming_table_is_eligible`, `test_materialized_view_is_still_not_eligible` (`test_inventory_manager.py`); `test_scenario_18_streaming_table_full_migrate_succeeds` (`test_table_converter.py`) |

---

## 2. Live test cases

### TC-01 — Baseline: streaming table is blocked before the fix

| Field | Detail |
|---|---|
| **Objective** | Confirm the pre-existing behavior (streaming tables rejected) before the code change, to have a true before/after comparison. |
| **Preconditions** | Real `STREAMING_TABLE` `ril_full_access_test.streaming_test.events_streaming_tbl` created via `CREATE OR REFRESH STREAMING TABLE ... AS SELECT * FROM STREAM source_events_tbl`, with a legacy row filter (`rf_region_streaming` on `region`) and column mask (`mask_email_streaming` on `customer_email`) applied via `ALTER STREAMING TABLE`. |
| **Steps** | 1. Run `Mode.INVENTORY` against the table with unmodified code (`SUPPORTED_TABLE_TYPES = {MANAGED, EXTERNAL}`). |
| **Expected result** | Table discovered correctly (type, row filter, masks) but classified `NOT_ELIGIBLE` / `UNSUPPORTED_TABLE_TYPE`. |
| **Actual result** | `migration_eligibility=NOT_ELIGIBLE`, `eligibility_reason=UNSUPPORTED_TABLE_TYPE`, `table_type=STREAMING_TABLE`, `has_row_filter=true`, `has_column_masks=true` — discovery layer worked perfectly, only the eligibility gate blocked it, exactly as designed. |
| **Status** | ✅ PASS |

### TC-02 — DDL keyword requirement check: `ALTER TABLE` vs `ALTER STREAMING TABLE`

| Field | Detail |
|---|---|
| **Objective** | Determine whether the gateway's existing DDL statements (`ALTER TABLE ...`, never `ALTER STREAMING TABLE ...`) actually work against a real streaming table, or whether gateway code changes would be required. |
| **Steps** | Ran, directly via SQL, against `events_streaming_tbl`: `ALTER TABLE ... DROP ROW FILTER`, `ALTER TABLE ... ALTER COLUMN ... DROP MASK`, `ALTER TABLE ... SET ROW FILTER ... ON (...)`, `ALTER TABLE ... ALTER COLUMN ... SET MASK ...`, `ALTER TABLE ... ALTER COLUMN ... SET TAGS (...)`. |
| **Expected result** | Unknown going in — this was the exploratory/discovery test that determined the implementation approach. |
| **Actual result** | **All 5 statements succeeded** with plain `ALTER TABLE ...` — no `ALTER STREAMING TABLE` keyword needed. Verified via `describe_table_security()` re-read after each statement that the mutation actually took effect (not a silent no-op). |
| **Status** | ✅ PASS — confirms **zero gateway/DDL changes are required** for `STREAMING_TABLE` support, only the eligibility-gate change. |

### TC-03 — Negative control: `MATERIALIZED_VIEW` behaves differently (out of scope, documented)

| Field | Detail |
|---|---|
| **Objective** | Confirm materialized views do **not** share the same "no DDL change needed" property, to correctly scope this fix to `STREAMING_TABLE` only. |
| **Steps** | Created `ril_full_access_test.streaming_test.events_mv` (`CREATE OR REFRESH MATERIALIZED VIEW`), then ran `ALTER TABLE ... SET ROW FILTER ...` / `ALTER TABLE ... ALTER COLUMN ... SET MASK ...` / `ALTER TABLE ... DROP ROW FILTER` against it. |
| **Expected result** | N/A — exploratory. |
| **Actual result** | All 3 plain `ALTER TABLE ...` statements **failed** with `BAD_REQUEST [EXPECT_TABLE_NOT_VIEW.NO_ALTERNATIVE] ... expects a table but ... is a view`. Re-running with `ALTER MATERIALIZED VIEW ...` **succeeded** for all 3, and a live `SELECT` confirmed correct enforcement. Also discovered `describe_table_security()`'s column-mask parser mis-reads a materialized view's `DESCRIBE TABLE EXTENDED` output (a trailing `Total Size (bytes)` row under `# Column Masks` is incorrectly parsed as a phantom masked column). |
| **Status** | ✅ PASS (as a negative/scoping control) — confirms `MATERIALIZED_VIEW` correctly requires separate follow-up work (Track B) and must **not** be added to `SUPPORTED_TABLE_TYPES` yet. Documented in `DESIGN.md` §16 item 2. |

### TC-04 — Full lifecycle on a single streaming table, with the real (fixed) code

| Field | Detail |
|---|---|
| **Objective** | Prove the actual code fix (`STREAMING_TABLE` added to `SUPPORTED_TABLE_TYPES`) works end-to-end, with no monkeypatching. |
| **Preconditions** | `ril_full_access_test.streaming_test.events_streaming_tbl` reset to pristine legacy-only state (row filter + column mask present, no ABAC policies). |
| **Steps** | 1. `Mode.INVENTORY` → 2. `Mode.APPLY_ABAC` → 3. Live `SELECT` → 4. `Mode.FINALIZE` → 5. Live `SELECT` again. |
| **Expected result** | INVENTORY: `ELIGIBLE`. APPLY_ABAC: `ABAC_APPLIED` for both RLS and mask, legacy still present. SELECT: only `region='east'` row visible, `customer_email` masked. FINALIZE: `SUCCESS`, legacy removed. SELECT again: same correct enforcement, now ABAC-only. |
| **Actual result** | Matched expected exactly at every step (see run IDs below). `SHOW POLICIES` confirmed `abac_migrated_row_filter` (ROW_FILTER) and `abac_migrated_mask_customer_email` (COLUMN_MASK) created. `migration_audit` recorded both `ABAC_APPLIED` and `FINALIZED` phase rows with `status=ABAC_APPLIED`/`SUCCESS` respectively, no errors. |
| **Evidence** | `run_id`: INVENTORY `0fa908af-4311-4e2a-b93d-5c44dc79c044`, APPLY_ABAC `0190d99d-c9f1-41de-b05b-e420fb5e8fbc`, FINALIZE `df86c375-dd9d-4e7d-8a38-257e1a6787e0`. Audit table: `ril_full_access_test.governance_audit_streaming_test.migration_audit`. |
| **Status** | ✅ PASS |

### TC-05 — Comprehensive 6-flavor regression: MANAGED vs STREAMING_TABLE, all 3 security flavors

| Field | Detail |
|---|---|
| **Objective** | Confirm the fix holds up identically across all combinations of table type × security flavor, in a single realistic multi-table catalog, not just the one RLS+mask combo tested in TC-04. |
| **Preconditions** | New catalog `ril_streaming_flavors_test` (full access granted to `vivek.ravichandiran@databricks.com`), schema `demo` with 6 tables: `managed_rls_tbl`, `managed_mask_tbl`, `managed_both_tbl` (all `MANAGED`) and `streaming_rls_tbl`, `streaming_mask_tbl`, `streaming_both_tbl` (all `STREAMING_TABLE`) — each with the appropriate legacy row filter and/or column mask (group-gated via a `data_admins`/`is_group_member()` control table), plus 3 backing source Delta tables for the streaming tables. |
| **Steps** | 1. `Mode.INVENTORY` (scope = whole `demo` schema) → 2. `Mode.APPLY_ABAC` → 3. Live `SELECT` on all 6 tables → 4. `Mode.FINALIZE` → 5. Live `SELECT` on all 6 tables again → 6. Inspect `migration_audit`. |
| **Expected result** | All 6 target tables (plus the 3 no-legacy-security source tables, correctly excluded) discovered with correct `table_type`; all 6 reach `ELIGIBLE` → `ABAC_APPLIED` → `SUCCESS`; RLS and masks enforced identically for MANAGED and STREAMING_TABLE at every stage. |
| **Actual result** | `tables_in_scope=9`, `eligible=6` (3 source tables correctly `NOT_ELIGIBLE`/`NO_LEGACY_SECURITY_FOUND`). All 6 target tables: `ELIGIBLE` at INVENTORY, `ABAC_APPLIED` at APPLY_ABAC (legacy confirmed still present on every table), `SUCCESS` at FINALIZE (legacy confirmed removed on every table). Live SELECT after both APPLY_ABAC and FINALIZE showed identical, correct results for every table: RLS-only tables showed all 4 seeded rows (service principal is in the `data_admins` group so nothing is filtered for it), mask-only and both-flavor tables showed `customer_email` correctly redacted (`***MANAGED-MASKED***` / `***STREAMING-MASKED***`), amounts/other columns untouched. `migration_audit` recorded correct `ABAC_APPLIED`/`FINALIZED` rows for every table/object with no errors. |
| **Evidence** | `run_id`: INVENTORY `7fead191-092f-4411-abd3-55d59e111207`, APPLY_ABAC `214dbf1d-6046-4430-bfc7-32c8187588cb`, FINALIZE `e6d7769c-55bb-4fa3-bc0d-a41e378777c7`. Audit tables: `ril_streaming_flavors_test.governance_audit.{inventory,migration_audit}`. |
| **Status** | ✅ PASS — 6/6 tables, 0 failures |

---

## 3. Results matrix (TC-05 detail)

| Table | Type | Flavor | INVENTORY | APPLY_ABAC | Legacy present after APPLY_ABAC? | FINALIZE | Legacy present after FINALIZE? | SELECT correct (both stages) |
|---|---|---|---|---|---|---|---|---|
| `managed_rls_tbl` | MANAGED | RLS only | ELIGIBLE | ABAC_APPLIED | Yes | SUCCESS | No | ✅ |
| `managed_mask_tbl` | MANAGED | Mask only | ELIGIBLE | ABAC_APPLIED | Yes | SUCCESS | No | ✅ |
| `managed_both_tbl` | MANAGED | Both | ELIGIBLE | ABAC_APPLIED | Yes | SUCCESS | No | ✅ |
| `streaming_rls_tbl` | STREAMING_TABLE | RLS only | ELIGIBLE | ABAC_APPLIED | Yes | SUCCESS | No | ✅ |
| `streaming_mask_tbl` | STREAMING_TABLE | Mask only | ELIGIBLE | ABAC_APPLIED | Yes | SUCCESS | No | ✅ |
| `streaming_both_tbl` | STREAMING_TABLE | Both | ELIGIBLE | ABAC_APPLIED | Yes | SUCCESS | No | ✅ |

---

## 4. Defects found

| ID | Description | Severity | Status |
|---|---|---|---|
| DEF-01 | `describe_table_security()`'s column-mask parser mis-reads a `MATERIALIZED_VIEW`'s `DESCRIBE TABLE EXTENDED` output — a trailing `Total Size (bytes)` administrative row directly under `# Column Masks` (with no blank/`#` separator before it) is incorrectly parsed as a phantom masked column. | Low (only affects `MATERIALIZED_VIEW`, which is not yet an in-scope table type) | Open — tracked in `DESIGN.md` §16 item 2 as part of Track B (`MATERIALIZED_VIEW` support), not yet fixed. |

No defects found affecting `STREAMING_TABLE` support (in-scope of this document).

---

## 5. Traceability — spike scripts

| Script | Purpose |
|---|---|
| `abac_migration/spike/setup_streaming_table_test.py` | Creates the single streaming-table fixture used in TC-01/TC-02/TC-04. |
| `abac_migration/spike/test_streaming_table_support_live.py` | Automates TC-04 (single-table full lifecycle). |
| `abac_migration/spike/setup_ril_streaming_flavors_test_catalog.py` | Creates the 6-table catalog used in TC-05. |
| `abac_migration/spike/test_streaming_flavors_live.py` | Automates TC-05 (6-flavor comprehensive regression). |

---

## 6. Sign-off

| Item | Result |
|---|---|
| Unit tests | 167/167 passed |
| Live test cases | 5/5 passed (TC-01 through TC-05) |
| Code changes required | 1 line (`inventory_manager.py`, `SUPPORTED_TABLE_TYPES`) |
| Regressions introduced | None — `MANAGED`/`EXTERNAL` behavior unchanged, `VIEW`/`MATERIALIZED_VIEW` remain correctly blocked |
| Recommendation | **Approved** — `STREAMING_TABLE` support (Track A) is safe to ship as-is. `MATERIALIZED_VIEW` support (Track B) requires separate implementation work before enabling (see `DESIGN.md` §16 item 2 and DEF-01 above). |
