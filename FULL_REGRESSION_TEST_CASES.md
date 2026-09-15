# Full Regression Test Case Document — All Table Types × All Security Flavors

> **This is the master regression document.** It supersedes the narrower
> per-feature documents (`STREAMING_TABLE_SUPPORT_TEST_CASES.md`,
> `MATERIALIZED_VIEW_SUPPORT_TEST_CASES.md`) as the place to look for
> "does the whole utility still work, for every table type, together" —
> those two documents remain as the detailed record of *how* each table
> type's support was implemented and first proven; this document is the
> combined regression proof that all of it works **side by side, in one
> catalog, in one run**, including modes (`ROLLBACK`) and properties
> (idempotency) not covered by either of those documents.

## Latest results (read this first)

| Date | Scope | Result |
|---|---|---|
| **2026-09-15** | Unit suite (178 tests) + full `INVENTORY → APPLY_ABAC → live SELECT → FINALIZE → live SELECT` lifecycle × **9 tables** (`MANAGED`/`STREAMING_TABLE`/`MATERIALIZED_VIEW` × RLS-only/mask-only/both) + `ROLLBACK` of all 9 + a full 2nd `INVENTORY → APPLY_ABAC → FINALIZE` re-run after rollback (idempotency) | ✅ **ALL PASSED — 0 defects found** |

| Check | Result |
|---|---|
| Unit tests (`pytest abac_migration/tests/ -q`) | **178/178 passed** |
| INVENTORY correctness (table_type, eligibility, mask-column count) — 9/9 tables | ✅ PASS |
| APPLY_ABAC (`ABAC_APPLIED`, legacy still present) — 9/9 tables | ✅ PASS |
| Live `SELECT` enforcement after APPLY_ABAC (RLS + mask correct) — 9/9 tables | ✅ PASS |
| FINALIZE (`SUCCESS`, legacy removed via correct DDL keyword) — 9/9 tables | ✅ PASS |
| Live `SELECT` enforcement after FINALIZE (ABAC-only) — 9/9 tables | ✅ PASS |
| `SHOW POLICIES` sanity check — 9/9 tables | ✅ PASS |
| `ROLLBACK` (legacy restored, ABAC policies dropped, correct DDL keyword) — 9/9 tables | ✅ PASS |
| Re-run `INVENTORY → APPLY_ABAC → FINALIZE` after rollback (idempotency) — 9/9 tables | ✅ PASS |
| `migration_audit` — any unexpected `error_code` across all runs above | **None found** |

No code changes were required as a result of this regression pass — it is
a **pure verification** that the Track A (`STREAMING_TABLE`) and Track B
(`MATERIALIZED_VIEW`) fixes, plus the pre-existing `MANAGED` path, all
continue to work correctly when exercised together in one catalog across
every mode the utility supports (`INVENTORY`, `APPLY_ABAC`, `FINALIZE`,
`ROLLBACK`).

---

## 1. Scope

**In scope for this regression pass:**

| Table type | RLS only | Mask only | Both |
|---|---|---|---|
| `MANAGED` | `managed_rls_tbl` | `managed_mask_tbl` | `managed_both_tbl` |
| `STREAMING_TABLE` | `streaming_rls_tbl` | `streaming_mask_tbl` | `streaming_both_tbl` |
| `MATERIALIZED_VIEW` | `mv_rls_tbl` | `mv_mask_tbl` | `mv_both_tbl` |

9 tables total, all in one catalog/schema (`ril_full_regression_test.demo`),
so the "many table types in one inventory/apply/finalize run" path is
exercised for real, not just type-by-type in isolation.

**Modes exercised:** `INVENTORY`, `APPLY_ABAC`, `FINALIZE`, `ROLLBACK`
(`MIGRATE`/full atomic mode and `CATALOG`-scope policy application are
covered by pre-existing unit tests and earlier live testing rounds — not
re-proven here since this round's purpose is the table-type × flavor
matrix specifically).

**Out of scope / not re-tested here:** plain `VIEW` (still, correctly,
`NOT_ELIGIBLE`/`UNSUPPORTED_TABLE_TYPE` — unchanged, no storage of its own).

**Related documents:**
- `STREAMING_TABLE_SUPPORT_TEST_CASES.md` — Track A implementation + tests
  (the `STREAMING_TABLE` eligibility change and its negative-control
  testing, including where DEF-01 was first found).
- `MATERIALIZED_VIEW_SUPPORT_TEST_CASES.md` — Track B implementation +
  tests (the `ALTER MATERIALIZED VIEW` DDL-keyword fix, `table_type`
  threading, and the DEF-01 fix + regression check).
- `DESIGN.md` §16 item 2 — running history of both tracks.

---

## 2. Test environment

| Item | Detail |
|---|---|
| Workspace | `adb-7405616318078204` (profile `ril_catalog_test_pat`, OAuth M2M service principal) |
| SQL warehouse | `5fe1692f119e2528` ("Serverless Starter Warehouse", serverless) |
| Test catalog | `ril_full_regression_test` (fresh catalog created for this round — see §3) |
| Audit tables | `ril_full_regression_test.governance_audit.{inventory,migration_audit}` |
| Test date | 2026-09-15 |

---

## 3. Fixture setup

Script: `abac_migration/spike/setup_ril_full_regression_test_catalog.py`
(idempotent — safe to re-run).

Creates, in one fresh catalog:
- `governance` schema: `group_membership` control table (seeded with the
  test service principal as a `data_admins` member) + `is_group_member()`
  UDF — same pattern used by every other `ril_*` spike catalog.
- `demo` schema:
  - 3 `MANAGED` tables with the legacy row filter/mask applied directly
    (`ALTER TABLE ... SET ROW FILTER` / `... SET MASK`).
  - 3 dedicated backing source tables + 3 `STREAMING_TABLE`s
    (`CREATE OR REFRESH STREAMING TABLE ... AS SELECT * FROM STREAM ...`),
    each with its **own** dedicated source (reusing one source across
    multiple streaming tables breaks Delta's streaming checkpoint
    lineage — learned live while building the Track A fixture), legacy
    row filter/mask applied via plain `ALTER TABLE ...` (confirmed live,
    Track A, to need no special DDL).
  - 3 dedicated backing source tables + 3 `MATERIALIZED_VIEW`s
    (`CREATE OR REFRESH MATERIALIZED VIEW ... AS SELECT * FROM ...`), same
    one-dedicated-source-per-object pattern for consistency, legacy row
    filter/mask applied via `ALTER MATERIALIZED VIEW ...` (confirmed live,
    Track B, to be required — plain `ALTER TABLE ...` fails outright
    against a real materialized view).
- `GRANT ALL PRIVILEGES ON CATALOG ril_full_regression_test TO
  vivek.ravichandiran@databricks.com`.

Post-setup type/legacy-state check (from the script's own output):

```
managed_rls_tbl:     Type=MANAGED            RowFilter=True  ColMasksHeader=False
managed_mask_tbl:    Type=MANAGED            RowFilter=False ColMasksHeader=True
managed_both_tbl:    Type=MANAGED            RowFilter=True  ColMasksHeader=True
streaming_rls_tbl:   Type=STREAMING_TABLE    RowFilter=True  ColMasksHeader=False
streaming_mask_tbl:  Type=STREAMING_TABLE    RowFilter=False ColMasksHeader=True
streaming_both_tbl:  Type=STREAMING_TABLE    RowFilter=True  ColMasksHeader=True
mv_rls_tbl:          Type=MATERIALIZED_VIEW  RowFilter=True  ColMasksHeader=False
mv_mask_tbl:         Type=MATERIALIZED_VIEW  RowFilter=False ColMasksHeader=True
mv_both_tbl:         Type=MATERIALIZED_VIEW  RowFilter=True  ColMasksHeader=True
```

Exactly the intended 3×3 matrix, confirmed before any migration code runs.

---

## 4. Automated unit test suite (regression)

| Item | Detail |
|---|---|
| Command | `python3 -m pytest abac_migration/tests/ -q` |
| Result | **178 passed, 0 failed** |
| Note | No new unit tests were added for this round — this is a pure live-integration regression pass over code already covered by the 178 unit tests documented in `STREAMING_TABLE_SUPPORT_TEST_CASES.md` §1 and `MATERIALIZED_VIEW_SUPPORT_TEST_CASES.md` §1. |

---

## 5. Live regression matrix — Run 1 (`INVENTORY → APPLY_ABAC → FINALIZE`)

Script: `abac_migration/spike/test_full_regression_live.py`.

Run IDs: INVENTORY `25f51169-ab32-414a-a0be-2313e598110a`, APPLY_ABAC
`d6e54bf5-f900-4666-a8f6-187379e78ca1`, FINALIZE
`a921ab5c-c1ad-4907-acd1-29088a25df2f`.

### 5.1 INVENTORY

`tables_in_scope=15` (9 targets + 6 dedicated backing sources),
`tables_eligible=9`. Every target table matched its expected `table_type`
and `migration_eligibility=ELIGIBLE`, with `column_masks` containing
**exactly** the expected columns (DEF-01 regression check — no phantom
entries for any of the 3 `MATERIALIZED_VIEW` tables):

| Table | table_type | has_row_filter | has_column_masks | column_masks |
|---|---|---|---|---|
| `managed_rls_tbl` | MANAGED | true | false | `[]` |
| `managed_mask_tbl` | MANAGED | false | true | `customer_email` |
| `managed_both_tbl` | MANAGED | true | true | `customer_email` |
| `streaming_rls_tbl` | STREAMING_TABLE | true | false | `[]` |
| `streaming_mask_tbl` | STREAMING_TABLE | false | true | `customer_email` |
| `streaming_both_tbl` | STREAMING_TABLE | true | true | `customer_email` |
| `mv_rls_tbl` | MATERIALIZED_VIEW | true | false | `[]` |
| `mv_mask_tbl` | MATERIALIZED_VIEW | false | true | `customer_email` |
| `mv_both_tbl` | MATERIALIZED_VIEW | true | true | `customer_email` |

### 5.2 APPLY_ABAC

`succeeded=0, abac_applied=9, failed=0`. Every table reached
`ABAC_APPLIED` for both its RLS and mask step (as applicable); legacy
row filter/mask confirmed still present on every table (non-final,
dual-mechanism state) with the correct mask count (no DEF-01 regression):

| Table | status | rls | mask(`customer_email`) |
|---|---|---|---|
| `managed_rls_tbl` | ABAC_APPLIED | ABAC_APPLIED | — |
| `managed_mask_tbl` | ABAC_APPLIED | — | ABAC_APPLIED |
| `managed_both_tbl` | ABAC_APPLIED | ABAC_APPLIED | ABAC_APPLIED |
| `streaming_rls_tbl` | ABAC_APPLIED | ABAC_APPLIED | — |
| `streaming_mask_tbl` | ABAC_APPLIED | — | ABAC_APPLIED |
| `streaming_both_tbl` | ABAC_APPLIED | ABAC_APPLIED | ABAC_APPLIED |
| `mv_rls_tbl` | ABAC_APPLIED | ABAC_APPLIED | — |
| `mv_mask_tbl` | ABAC_APPLIED | — | ABAC_APPLIED |
| `mv_both_tbl` | ABAC_APPLIED | ABAC_APPLIED | ABAC_APPLIED |

### 5.3 Live `SELECT` after APPLY_ABAC (both mechanisms active)

Run as the seeded `data_admins` member, so RLS shows **all** regions
(masks are not group-gated, so they still redact for everyone):

| Table | Sample row(s) |
|---|---|
| `managed_rls_tbl` | 4 rows, all regions visible, no masking (RLS-only table) |
| `managed_mask_tbl` | `customer_email` = `***MANAGED-MASKED***` on all 3 rows |
| `managed_both_tbl` | all regions visible, `customer_email` = `***MANAGED-MASKED***` |
| `streaming_rls_tbl` | 4 rows, all regions visible |
| `streaming_mask_tbl` | `customer_email` = `***STREAMING-MASKED***` on all 3 rows |
| `streaming_both_tbl` | all regions visible, `customer_email` = `***STREAMING-MASKED***` |
| `mv_rls_tbl` | 4 rows, all regions visible |
| `mv_mask_tbl` | `customer_email` = `***MV-MASKED***` on all 3 rows |
| `mv_both_tbl` | all regions visible, `customer_email` = `***MV-MASKED***` |

All 9 `SELECT`s `status=SUCCEEDED`, no errors. Type-specific mask
functions (`***MANAGED-MASKED***`/`***STREAMING-MASKED***`/`***MV-MASKED***`)
confirm each table type's *own* ABAC `COLUMN_MASK` policy — not some
other table's — is the one actually enforcing.

### 5.4 FINALIZE

`succeeded=9, failed=0`. Every table reached `SUCCESS`; legacy row
filter/mask confirmed **fully removed** from every table, including the 3
`MATERIALIZED_VIEW` tables via `ALTER MATERIALIZED VIEW ... DROP ROW
FILTER` / `... DROP MASK` (not a failed `ALTER TABLE ...`):

| Table | status | rls | mask(`customer_email`) |
|---|---|---|---|
| `managed_rls_tbl` | SUCCESS | SUCCESS | — |
| `managed_mask_tbl` | SUCCESS | — | SUCCESS |
| `managed_both_tbl` | SUCCESS | SUCCESS | SUCCESS |
| `streaming_rls_tbl` | SUCCESS | SUCCESS | — |
| `streaming_mask_tbl` | SUCCESS | — | SUCCESS |
| `streaming_both_tbl` | SUCCESS | SUCCESS | SUCCESS |
| `mv_rls_tbl` | SUCCESS | SUCCESS | — |
| `mv_mask_tbl` | SUCCESS | — | SUCCESS |
| `mv_both_tbl` | SUCCESS | SUCCESS | SUCCESS |

### 5.5 Live `SELECT` after FINALIZE (ABAC-only)

Identical output to §5.3 for every one of the 9 tables — the switch from
"both mechanisms active" to "ABAC-only" was seamless, exactly as designed.

### 5.6 `SHOW POLICIES ON TABLE` sanity check

| Table | Policies |
|---|---|
| `managed_rls_tbl` | `abac_migrated_row_filter` |
| `managed_mask_tbl` | `abac_migrated_mask_customer_email` |
| `managed_both_tbl` | `abac_migrated_mask_customer_email`, `abac_migrated_row_filter` |
| `streaming_rls_tbl` | `abac_migrated_row_filter` |
| `streaming_mask_tbl` | `abac_migrated_mask_customer_email` |
| `streaming_both_tbl` | `abac_migrated_mask_customer_email`, `abac_migrated_row_filter` |
| `mv_rls_tbl` | `abac_migrated_row_filter` |
| `mv_mask_tbl` | `abac_migrated_mask_customer_email` |
| `mv_both_tbl` | `abac_migrated_mask_customer_email`, `abac_migrated_row_filter` |

### 5.7 `migration_audit` check

24 rows (2 phases × {1 or 2 object types} × 9 tables) queried for the
APPLY_ABAC + FINALIZE run IDs above — **zero** rows with a non-null
`error_code`.

---

## 6. Live `ROLLBACK` regression — all 9 tables together

Script: `abac_migration/spike/test_full_regression_rollback_live.py
a921ab5c-c1ad-4907-acd1-29088a25df2f` (the FINALIZE run_id from §5).

**Objective:** confirm `ROLLBACK` mode — not covered by either the Track A
or Track B live test documents — correctly restores legacy security and
removes ABAC policies for every table type, including choosing the right
DDL keyword (`ALTER TABLE` vs `ALTER MATERIALIZED VIEW`) via live
re-discovery (rollback has no `ConvertOptions` to read `table_type` from).

| Step | Result |
|---|---|
| `ROLLBACK` mode run against FINALIZE run_id | 12/12 step results `ROLLED_BACK`, 0 errors (9 tables × 1 or 2 objects each) |
| Legacy row filter/mask restored, per table | 9/9 correct (`has_row_filter`/`column_masks` match original fixture exactly) |
| `SHOW POLICIES` — ABAC policies removed | 9/9 tables now report **zero** policies |
| Live `SELECT` after rollback | 9/9 `SUCCEEDED`, identical data to §5.3/§5.5 (same functions, now enforced via the legacy mechanism instead of ABAC) |
| `migration_audit` `ROLLED_BACK` rows | 12 rows, all `status=ROLLED_BACK`, **zero** with a non-null `error_code` |

Rollback run_id: `a921ab5c-c1ad-4907-acd1-29088a25df2f` (rollback runs are
recorded against the run_id being rolled back, per the utility's design).

---

## 7. Idempotency check — Run 2 (`INVENTORY → APPLY_ABAC → FINALIZE` re-run after rollback)

To confirm the utility can cleanly re-migrate a catalog that was rolled
back (a realistic operational scenario — e.g. a rollback for
investigation, followed by a retry), `test_full_regression_live.py` was
run a second time, from scratch, against the now-rolled-back catalog.

Run IDs: INVENTORY `a5f9c059-7dc4-4bf3-9d4e-fc871ad7bab5`, APPLY_ABAC
`5a97ded6-dee7-4f0c-b2d4-0b0a5a7fd1dc`, FINALIZE
`d0c2f72b-b7a2-46db-ad68-a89407bcd2fc`.

| Check | Result |
|---|---|
| INVENTORY (9/9 tables, ELIGIBLE, correct mask columns) | ✅ PASS |
| APPLY_ABAC (9/9 tables, ABAC_APPLIED) | ✅ PASS |
| Live `SELECT` after APPLY_ABAC | ✅ PASS (identical to Run 1) |
| FINALIZE (9/9 tables, SUCCESS, legacy removed) | ✅ PASS |
| Live `SELECT` after FINALIZE | ✅ PASS (identical to Run 1) |
| `migration_audit` errors | None |

Confirms the full lifecycle is safely re-runnable end to end, for every
table type, after a rollback — no leftover state from Run 1 or the
rollback interfered with Run 2.

**Final catalog state after this document's testing:** all 9 tables are
in the fully-migrated, ABAC-only state (Run 2's FINALIZE), matching the
intended steady-state of a completed migration.

---

## 8. Defects found

**None.** No new defects were found in this regression pass. (DEF-01,
found during Track A's negative-control testing, was fixed during Track B
and is re-confirmed fixed here for all 3 `MATERIALIZED_VIEW` flavors in
§5.1's INVENTORY check.)

---

## 9. Traceability — spike scripts

| Script | Purpose |
|---|---|
| `abac_migration/spike/setup_ril_full_regression_test_catalog.py` | Creates the `ril_full_regression_test` catalog with all 9 target tables (+6 dedicated backing sources) in their pristine legacy-only state. Idempotent. |
| `abac_migration/spike/test_full_regression_live.py` | Automates §5 (`INVENTORY → APPLY_ABAC → live SELECT → FINALIZE → live SELECT → SHOW POLICIES → audit check`) across all 9 tables in one run. Re-runnable (used for both Run 1 and, after rollback, Run 2). |
| `abac_migration/spike/test_full_regression_rollback_live.py` | Automates §6 (`ROLLBACK` of a given run_id, then re-verifies legacy restored/ABAC removed/live SELECT/audit across all 9 tables). Takes the FINALIZE run_id as a CLI argument. |

---

## 10. Sign-off

| Item | Result |
|---|---|
| Unit tests | 178/178 passed |
| Live regression (Run 1: INVENTORY/APPLY_ABAC/FINALIZE) | 9/9 tables passed at every stage |
| Live ROLLBACK regression | 9/9 tables passed (all step results `ROLLED_BACK`, legacy restored, ABAC removed) |
| Idempotency (Run 2, post-rollback re-migration) | 9/9 tables passed at every stage |
| Defects found | None |
| Regressions introduced | None — `MANAGED`/`STREAMING_TABLE`/`MATERIALIZED_VIEW` all continue to work correctly, together, in every mode tested |
| Recommendation | **Approved** — the ABAC Migration Utility is confirmed working end-to-end for all 3 supported non-`VIEW` table types, all 3 security flavors, and all 4 modes tested (`INVENTORY`, `APPLY_ABAC`, `FINALIZE`, `ROLLBACK`), both individually (Track A/B docs) and together (this document). |
