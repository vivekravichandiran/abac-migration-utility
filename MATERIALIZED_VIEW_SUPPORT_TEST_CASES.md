# Test Case Document — MATERIALIZED_VIEW Eligibility & Full-Lifecycle Support

> **See also:** `FULL_REGRESSION_TEST_CASES.md` for the master regression
> document that proves `MATERIALIZED_VIEW` continues to work correctly
> *together* with `MANAGED` and `STREAMING_TABLE` (side by side, in one
> catalog, across `INVENTORY`/`APPLY_ABAC`/`FINALIZE`/`ROLLBACK`) — this
> document only covers `MATERIALIZED_VIEW` in isolation, as first implemented.

**Feature under test:** ABAC Migration Utility support for `MATERIALIZED_VIEW`
objects (Track B of the streaming-tables ABAC plan — `STREAMING_TABLE`
support was Track A, see `STREAMING_TABLE_SUPPORT_TEST_CASES.md` and
`DESIGN.md` §16 item 2).

**Code changes under test:**
- `abac_migration/inventory/inventory_manager.py` — `SUPPORTED_TABLE_TYPES`
  extended from `{MANAGED, EXTERNAL, STREAMING_TABLE}` to
  `{MANAGED, EXTERNAL, STREAMING_TABLE, MATERIALIZED_VIEW}`.
- `abac_migration/uc_gateway/gateway.py` — new `_alter_keyword_for(table_type)`
  helper (`ALTER MATERIALIZED VIEW` for `MATERIALIZED_VIEW`, `ALTER TABLE`
  for everything else); all 5 mutating methods (`drop_row_filter`,
  `drop_column_mask`, `set_row_filter`, `set_column_mask`,
  `set_column_tags`) take a new `table_type: str = "MANAGED"` parameter and
  use it to choose the DDL keyword. Also fixes DEF-01 (see below) in
  `describe_table_security()`'s column-mask parsing loop.
- `abac_migration/migration/plugins/base_plugin.py` — `PlannedObject` and
  `ConvertOptions` both gain a `table_type: str = "MANAGED"` field.
- `abac_migration/migration/tag_provisioner.py` — `TagRequest` gains the
  same field; `_mint_and_assign()`'s `set_column_tags()` call passes it
  through.
- `abac_migration/migration/plugins/rls_to_abac.py` /
  `mask_to_abac.py` — `validate()` populates `table_type` from discovery;
  `_convert_one()`/`_finalize_one()` pass `options.table_type` into
  `drop_row_filter()`/`drop_column_mask()`; `rollback()` (which has no
  `ConvertOptions`) re-discovers `table_type` live via a fresh
  `describe_table_security()` call before calling `set_row_filter()`/
  `set_column_mask()`.
- `abac_migration/migration/table_converter.py` — `convert_table()` captures
  `table_type` from the discovery it already performs and threads it into
  `ConvertOptions`.
- `abac_migration/tests/fake_gateway.py` — the 5 mutating methods accept the
  same `table_type` parameter and record `(op, table, table_type)` tuples in
  a new `ddl_calls_with_table_type` list for test assertions, without
  touching the pre-existing `mutation_calls` shape.

**Test environment:** Databricks workspace `adb-7405616318078204`
(profile `ril_catalog_test_pat`, OAuth M2M service principal), Serverless
SQL warehouse `5fe1692f119e2528` ("Serverless Starter Warehouse") — same
environment as the `STREAMING_TABLE` (Track A) testing.

**Test date:** 2026-09-15

**Overall result: ✅ ALL TEST CASES PASSED**

---

## 1. Automated unit test suite (regression)

| Item | Detail |
|---|---|
| Command | `python3 -m pytest abac_migration/tests/ -q` |
| Result | **178 passed**, 0 failed (up from 167 before this feature; net +11 new/changed tests) |
| New/changed tests for this feature | `test_materialized_view_is_now_eligible` (replaces the old `test_materialized_view_is_still_not_eligible`, `test_inventory_manager.py`); `test_scenario_19_materialized_view_full_migrate_uses_alter_mv_ddl`, `test_scenario_20_materialized_view_rollback_uses_alter_mv_ddl` (`test_table_converter.py`); `test_describe_table_security_mv_no_masks_does_not_pick_up_phantom_column`, `test_describe_table_security_mv_real_mask_still_parses_correctly`, `test_drop_row_filter_uses_alter_table_by_default`, `test_drop_row_filter_uses_alter_table_for_streaming_table`, `test_drop_row_filter_uses_alter_materialized_view`, `test_drop_column_mask_uses_alter_materialized_view`, `test_set_row_filter_uses_alter_materialized_view`, `test_set_column_mask_uses_alter_materialized_view`, `test_set_column_tags_uses_alter_materialized_view` (`test_gateway.py`). |

---

## 2. Live test cases

### TC-01 — DEF-01 regression check: INVENTORY no longer reports a phantom masked column

| Field | Detail |
|---|---|
| **Objective** | Confirm the `describe_table_security()` parser fix actually resolves DEF-01 (from `STREAMING_TABLE_SUPPORT_TEST_CASES.md`) against the real object, not just in a unit test with a stubbed executor. |
| **Preconditions** | `ril_full_access_test.streaming_test.events_mv` — a real `MATERIALIZED_VIEW` (`CREATE OR REFRESH MATERIALIZED VIEW ... AS SELECT * FROM source_events_tbl`) with a legacy row filter (`rf_region_mv` on `region`) and column mask (`mask_email_mv` on `customer_email`) applied directly via `ALTER MATERIALIZED VIEW ...` (fixture: `abac_migration/spike/setup_materialized_view_test.py`). |
| **Steps** | Run `Mode.INVENTORY` against the table with the real (fixed) code. |
| **Expected result** | `table_type=MATERIALIZED_VIEW`, `migration_eligibility=ELIGIBLE`, `has_row_filter=true`, `has_column_masks=true`, and `column_masks` containing **exactly one** entry (`customer_email`) — never a second phantom entry for `"Total Size (bytes)"`. |
| **Actual result** | `inventory` row: `['MATERIALIZED_VIEW', 'ELIGIBLE', None, 'true', 'true', '[{"column":"customer_email","function":"ril_full_access_test.streaming_test.mask_email_mv"}]']` — exactly one real mask column, no phantom entry. |
| **Status** | ✅ PASS — confirms DEF-01 fixed against a real materialized view, not just the unit-test stub. |

### TC-02 — Full lifecycle on a single materialized view, with the real (fixed) code

| Field | Detail |
|---|---|
| **Objective** | Prove the actual code fix (`MATERIALIZED_VIEW` added to `SUPPORTED_TABLE_TYPES`, `table_type`-aware gateway DDL) works end-to-end, with no monkeypatching. |
| **Preconditions** | Same `events_mv` fixture as TC-01, pristine legacy-only state (row filter + column mask present, no ABAC policies). |
| **Steps** | 1. `Mode.INVENTORY` → 2. `Mode.APPLY_ABAC` → 3. Live `SELECT` → 4. `Mode.FINALIZE` → 5. Live `SELECT` again. |
| **Expected result** | INVENTORY: `ELIGIBLE`. APPLY_ABAC: `ABAC_APPLIED` for both RLS and mask, legacy still present (exactly 1 mask, not 2 — DEF-01 regression check). SELECT: only `region='east'` row visible, `customer_email` masked as `***MV-MASKED***`. FINALIZE: `SUCCESS`, legacy removed (via `ALTER MATERIALIZED VIEW ... DROP`, not a failed `ALTER TABLE ...`). SELECT again: same correct enforcement, now ABAC-only. |
| **Actual result** | Matched expected exactly at every step (see run IDs below). `SHOW POLICIES ON TABLE ril_full_access_test.streaming_test.events_mv` after FINALIZE confirmed `abac_migrated_row_filter` (ROW_FILTER) and `abac_migrated_mask_customer_email` (COLUMN_MASK) live; `DESCRIBE TABLE EXTENDED` confirmed no `Row Filter` line and no `# Column Masks` section at all (legacy fully gone, no phantom entry either). `migration_audit` recorded both `ABAC_APPLIED` and `FINALIZED` phase rows with `status=ABAC_APPLIED`/`SUCCESS` respectively, no errors. |
| **Evidence** | `run_id`: INVENTORY `acbb681d-d0dc-47ef-b226-b5a7df3b2529`, APPLY_ABAC `b8760444-ee75-42f6-8b47-546687757a8f`, FINALIZE `8870260b-c6e4-4d36-b5aa-237580ef41df`. Audit tables: `ril_full_access_test.governance_audit_mv_test.{inventory,migration_audit}`. |
| **Status** | ✅ PASS |

---

## 3. Results detail (TC-02)

| Stage | RLS status | Mask status (`customer_email`) | Legacy present? | Live `SELECT` (`id`, `region`, `customer_email`, `amount`) |
|---|---|---|---|---|
| After APPLY_ABAC | ABAC_APPLIED | ABAC_APPLIED | Yes (row filter + 1 mask) | `[1, 'east', '***MV-MASKED***', 150.0]` |
| After FINALIZE | SUCCESS | SUCCESS | No | `[1, 'east', '***MV-MASKED***', 150.0]` |

Identical, correct output before and after `FINALIZE` — confirms the switch
from "both mechanisms active" to "ABAC-only" was seamless from the reader's
perspective, exactly as designed.

---

## 4. Defects found (this round)

None. DEF-01 (found during Track A's TC-03 negative control) is fixed and
verified here — see TC-01/TC-02 above and `STREAMING_TABLE_SUPPORT_TEST_CASES.md`
§4 for its updated status.

---

## 5. Traceability — spike scripts

| Script | Purpose |
|---|---|
| `abac_migration/spike/setup_materialized_view_test.py` | Creates/recreates the `events_mv` materialized-view fixture (idempotent) used in TC-01/TC-02. |
| `abac_migration/spike/test_materialized_view_support_live.py` | Automates TC-01 + TC-02 (INVENTORY DEF-01 check, then full single-table lifecycle). |

---

## 6. Sign-off

| Item | Result |
|---|---|
| Unit tests | 178/178 passed |
| Live test cases | 2/2 passed (TC-01, TC-02) |
| Code changes required | `table_type` threaded through `PlannedObject`/`TagRequest`/`ConvertOptions` into `gateway.py`'s 5 mutating methods (see "Code changes under test" above), plus the `describe_table_security()` parser fix |
| Regressions introduced | None — `MANAGED`/`EXTERNAL`/`STREAMING_TABLE` all default to `table_type="MANAGED"`-equivalent `ALTER TABLE` DDL, unchanged; full 178-test suite (including all `STREAMING_TABLE` and pre-existing scenarios) still passes |
| Recommendation | **Approved** — `MATERIALIZED_VIEW` support (Track B) is safe to ship. Both Track A (`STREAMING_TABLE`) and Track B (`MATERIALIZED_VIEW`) are now complete; only plain `VIEW` remains unsupported (no underlying storage of its own). |
