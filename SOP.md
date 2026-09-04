# ABAC Migration Utility — Standard Operating Procedure (SOP)

**Audience:** anyone running this tool for the first time — no prior context
needed beyond "we have legacy Unity Catalog row filters / column masks and
want to convert them to ABAC policies."

This is the **practical, step-by-step** doc: for each of the 8 modes, exactly
what to click/type, in what order, and how to tell if it worked. For *why*
things are designed this way, see `README.md` (concept-level) and
`abac_migration/DESIGN.md` (full architecture spec) — this doc doesn't repeat
their reasoning, only the operating steps.

---

## 1. The 30-second mental model

- The tool is a Databricks Job. You run it with a **`mode`** parameter that
  picks what it does, plus a **scope** (which catalogs/schemas/tables to look
  at) and a **`dry_run`** flag (default `true` = look-but-don't-touch).
- Every run writes to two Delta tables you control the location of
  (`audit_catalog.audit_schema.inventory` and `...migration_audit`) — this is
  your permanent record of what was found and what was done.
  `migration_audit` is **append-only** (every run adds new rows, never
  updates old ones), so for "what's the current status of everything?" use
  the `...migration_audit_latest` **view** (auto-created alongside the
  tables) rather than querying the base table directly — see §7.
- There are **8 modes**. Six of them you'll use directly; two
  (`INVENTORY_AND_MIGRATE`, `ROLLBACK`) are variants you'll reach for less
  often.
- There are **two ways to migrate a table**: one shot (`MIGRATE`) or two
  separate steps you can pause between (`APPLY_ABAC` then `FINALIZE`). Both
  end up in the exact same place. Pick isolated mode if you want a manual
  checkpoint before removing the old security; pick atomic if you don't.

```
   ┌───────────┐
   │ INVENTORY │  always run this first on a new scope — read-only
   └─────┬─────┘
         │
   ┌─────┴──────────────────────────────┐
   │                                     │
┌──▼───────┐                    ┌────────▼────────┐
│ MIGRATE  │  (one shot)        │   APPLY_ABAC     │  (step 1 of 2)
│          │                    │  legacy + ABAC   │
│ legacy   │                    │  both active     │
│ removed, │                    └────────┬─────────┘
│ ABAC live│                             │  ...review, bake in...
└────┬─────┘                    ┌────────▼─────────┐
     │                          │    FINALIZE      │  (step 2 of 2)
     │                          │  legacy removed  │
     │                          └────────┬─────────┘
     └───────────────┬──────────────────┘
                      │
              ┌───────▼────────┐        ┌────────────┐       ┌──────────┐
              │     VERIFY     │  ...    │  RECONCILE │  ...  │ ROLLBACK │
              │  spot-check    │         │ drift check│       │ undo one │
              │  health, any   │         │ vs. audit  │       │ prior run│
              │  time          │         │            │       │          │
              └────────────────┘         └────────────┘       └──────────┘
```

---

## 2. Mode directory (quick reference)

| # | Mode | One-liner | Mutates Unity Catalog? |
|---|---|---|---|
| 1 | `INVENTORY` | Discover + classify every table in scope. Nothing changes. | No |
| 2 | `MIGRATE` | Create the ABAC policy **and** remove legacy security, per table, in one shot. | Yes (unless `dry_run=true`) |
| 3 | `INVENTORY_AND_MIGRATE` | Same as `MIGRATE` (inventory is always built anyway) — just the explicit name. | Yes (unless `dry_run=true`) |
| 4 | `APPLY_ABAC` | Isolated step 1/2: create the ABAC policy, **keep** legacy security in place. | Yes (unless `dry_run=true`) |
| 5 | `FINALIZE` | Isolated step 2/2: remove legacy security for tables `APPLY_ABAC` already handled. | Yes (unless `dry_run=true`) |
| 6 | `VERIFY` | Health check: does the expected ABAC policy still exist / is legacy really gone? | No |
| 7 | `RECONCILE` | Compare live state against the audit table's last-known-good record → drift or not. | No |
| 8 | `ROLLBACK` | Undo one specific prior run by `run_id`: restore legacy, remove that run's ABAC policy. | Yes (unless `dry_run=true`) |

---

## 3. One-time setup (do this once per workspace)

1. **Confirm the CLI is available and a profile exists** for the target
   workspace in `~/.databrickscfg` (`[uc_source]` / `[uc_target]` or your own
   profile name).

   ```bash
   databricks warehouses list --profile <your_profile>
   ```

2. **Pick/create a SQL warehouse** (DBR/SQL channel ≥ 18.1 — required for
   `CREATE GOVERNED TAG`). Note its `id`.

3. **Pick an audit location** — a catalog + schema the tool will create two
   Delta tables in (`inventory`, `migration_audit`). This can be any catalog
   you have `CREATE SCHEMA` on; it does **not** need to be one of the
   catalogs being migrated. Example: `ril_raw.abac_migration_audit`.

4. **Fill in `databricks.yml`** target variables (`warehouse_id`,
   `catalogs_json`, `audit_catalog`, `audit_schema`) for your workspace, then
   deploy:

   ```bash
   databricks bundle validate -t <target> --profile <your_profile>
   databricks bundle deploy   -t <target> --profile <your_profile>
   ```

   This creates 5 jobs in the workspace (Workflows tab):
   `abac_migration_job` (atomic), `abac_migration_inventory_job`,
   `abac_migration_apply_abac_job`, `abac_migration_finalize_job` (the 3
   isolated-phase jobs), and `abac_migration_full_e2e_test_job` (dev-only
   scenario test, ignore unless you're developing the tool itself).

You only need to redo step 4 when the code changes. Steps 1–3 are one-time
per workspace.

**Deploying from a CI runner with no Python toolchain.** There is no wheel
anywhere in this pipeline — no `setup.py`, no `bdist_wheel`, no library
install. `databricks bundle deploy` just syncs this repo's files straight
into the workspace (respecting `.gitignore` + `databricks.yml`'s
`sync.exclude`), the same way `git push` copies files - a pure
Databricks-CLI/API operation, not a Python one. Each job's driver notebook
(`notebooks/abac_migration_run.py`, `notebooks/abac_migration_full_e2e_test.py`)
adds the synced `abac_migration/` directory to `sys.path` before
`import abac_migration...`, since none of its `.py` files carry the
`# Databricks notebook source` header (so they sync as plain importable
files, not notebooks). That means `databricks bundle deploy` never needs
Python/pip/setuptools on whatever machine or CI runner is running it - any
CI system (Azure DevOps, GitHub Actions, GitLab CI, Jenkins, ...) just
needs to install the Databricks CLI and set `DATABRICKS_HOST`/
`DATABRICKS_TOKEN` (or OIDC), then run:

```bash
databricks bundle validate -t target
databricks bundle deploy   -t target
```

No pipeline-specific scaffolding is required beyond that.

---

## 4. Three ways to trigger a run — pick whichever is easiest for you

**A. Databricks UI (easiest for a new user / one-off runs)**

1. Workflows → find the job (e.g. `ABAC Migration Utility`) → **Run now with
   different parameters**.
2. Fill in the parameter boxes (see each mode's table below for what to
   set). Leave anything unlisted at its default.
3. Click **Run**. Click into the run → **Output** tab on the task to see the
   returned JSON report; the notebook's **Logs**/cell output shows the same
   information as human-readable text.

**B. Databricks CLI**

```bash
databricks bundle run <job_name> -t <target> --profile <your_profile>
# or, to override parameters:
databricks bundle run <job_name> -t <target> --profile <your_profile> \
  --notebook-params mode=INVENTORY,dry_run=false
```

> ⚠️ **Gotcha:** the CLI's `--notebook-params` flag parses its value as CSV,
> so a JSON value containing double quotes (e.g. `schemas={"cat":["sch"]}`)
> will fail to parse. For any parameter whose value is JSON (`catalogs`,
> `schemas`, `tables`, `policy_to_principals`, `policy_except_principals`),
> use option **C** (REST API) instead — it takes a normal JSON body with no
> such restriction.

**C. REST API (most reliable for JSON-shaped parameters / scripting)**

```bash
databricks api post /api/2.1/jobs/run-now --profile <your_profile> --json '{
  "job_id": <job_id>,
  "notebook_params": {
    "mode": "INVENTORY",
    "scope_type": "SELECTED_SCHEMAS",
    "schemas": "{\"ril_raw\": [\"my_schema\"]}",
    "dry_run": "false"
  }
}'
```

Then poll and read the result:

```bash
databricks api get "/api/2.1/jobs/runs/get?run_id=<run_id>" --profile <your_profile>
# once state.life_cycle_state == TERMINATED, get the task's own run_id from
# the "tasks" array, then:
databricks api get "/api/2.1/jobs/runs/get-output?run_id=<task_run_id>" --profile <your_profile>
```

The `notebook_output.result` field is the JSON report described in §5 below.

---

## 5. Reading the output (applies to every mode)

Every run returns/prints a JSON report shaped like:

```json
{
  "run_id": "...",
  "mode": "MIGRATE",
  "dry_run": false,
  "tables_in_scope": 10,
  "tables_eligible": 7,
  "tables_not_eligible": 3,
  "tables_succeeded": 6,
  "tables_abac_applied": 0,
  "tables_would_migrate": 0,
  "tables_already_migrated": 1,
  "tables_failed": 0,
  "pre_validation_errors": [],
  "other_results": []
}
```

- `tables_in_scope` / `tables_eligible` / `tables_not_eligible` — from
  discovery. A table is `NOT_ELIGIBLE` if it has no legacy row filter/mask,
  isn't a real table (view/streaming table), or you lack permission on it —
  never a failure, just "nothing to do here."
- `tables_succeeded` / `tables_abac_applied` / `tables_would_migrate` /
  `tables_already_migrated` / `tables_failed` — only populated for the
  mutating modes (`MIGRATE`/`INVENTORY_AND_MIGRATE`/`APPLY_ABAC`/`FINALIZE`).
- `pre_validation_errors` — non-empty means the run aborted **before
  touching anything** (e.g. bad config, unsupported DBR version). Fix and
  rerun.
- `other_results` — only populated for `VERIFY` / `RECONCILE` / `ROLLBACK`
  (see their sections below for the shape).

For anything beyond the summary counts, **query the audit tables directly**
— they're the permanent, per-object record (see §7 example queries).

---

## 6. Step-by-step SOP, per mode

### 6.1 `INVENTORY`

**Purpose:** find out what legacy row filters/column masks exist in a scope
and whether each is eligible for migration. **Always run this first** on any
new scope, before anything else.

**Preconditions:** none (safe to run any time, on any scope).

**Steps:**

1. Trigger `abac_migration_inventory_job` (or `abac_migration_job` with
   `mode=INVENTORY`, its default).
2. Set parameters:

   | Parameter | Example | Notes |
   |---|---|---|
   | `scope_type` | `SELECTED_CATALOGS` | or `SELECTED_SCHEMAS` / `SPECIFIC_TABLES` / `ALL_CATALOGS` |
   | `catalogs` | `["ril_raw"]` | required if `scope_type=SELECTED_CATALOGS` |
   | `audit_catalog` / `audit_schema` | `ril_raw` / `abac_migration_audit` | where the report gets written |
   | `dry_run` | **`false`** | ⚠️ see callout below |
   | `enable_llm_pii_tagging` | `true` (optional) | adds an LLM-suggested PII category per legacy function — advisory only, never affects eligibility |

   > ⚠️ **`dry_run` must be `false` for INVENTORY to actually write
   > anything.** `dry_run=true` (the job's safe default) makes *every*
   > persistence call a no-op, including writing your own `inventory` table
   > — there's nothing destructive about writing to your own audit tables,
   > so override it here.

3. Run it. Wait for `TERMINATED` / `SUCCESS`.
4. Check the report: `tables_in_scope`, `tables_eligible`. If
   `pre_validation_errors` is non-empty, fix config and rerun.
5. Query the `inventory` table (see §7) to review every table found, its
   eligibility reason, and (if enabled) suggested PII tags.

**You're done when:** you've reviewed the `inventory` table and know which
tables are `ELIGIBLE` and why the rest are `NOT_ELIGIBLE`.

---

### 6.2 `MIGRATE` (atomic path)

**Purpose:** for every eligible table, create+verify the new ABAC policy
**and** remove the legacy row filter/mask, in one run. Use this when you
don't need a manual pause between "new policy live" and "old policy gone."

**Preconditions:** you've run `INVENTORY` (recommended, not required —
`MIGRATE` re-discovers eligibility itself anyway) and reviewed the scope.

**Steps:**

1. Trigger `abac_migration_job`.
2. Set parameters:

   | Parameter | Example | Notes |
   |---|---|---|
   | `mode` | `MIGRATE` | |
   | `scope_type` / `catalogs` (or `schemas`/`tables`) | same as inventory | must match the scope you want to convert |
   | `audit_catalog` / `audit_schema` | same as inventory | |
   | `dry_run` | `true` **first**, then `false` | see next step |

3. **First run with `dry_run=true`.** Check the report: `tables_eligible`,
   `tables_would_migrate`, `tables_failed`. This exercises the exact same
   code path as a real run but makes zero live changes — use it to catch
   config/permission problems safely.
4. **Rerun with `dry_run=false`** once the dry run looks right. Same
   parameters otherwise.
5. Check the report: `tables_succeeded` should equal `tables_eligible`
   (minus any that were `tables_already_migrated` from a prior run). Any
   `tables_failed` > 0 → check `migration_audit.error_code` /
   `error_message` for that table (see §7) before re-running.
6. Rerunning `MIGRATE` on the same scope is safe — already-fully-migrated
   tables report `ALREADY_MIGRATED` and are left untouched; tables where a
   previous attempt partially failed are retried automatically.

**You're done when:** `tables_failed == 0` and every table you expect is
either `SUCCESS` or `ALREADY_MIGRATED` in `migration_audit`.

---

### 6.3 `INVENTORY_AND_MIGRATE`

**Purpose:** identical behavior to `MIGRATE` — inventory is always
(re)built as a side effect of resolving eligibility either way. Use this
name in a job config purely to make "this run does discovery *and*
migration" explicit/self-documenting for whoever reads the job later.

**Steps:** identical to §6.2 — substitute `mode=INVENTORY_AND_MIGRATE`.
There is no behavioral difference to test for separately.

---

### 6.4 `APPLY_ABAC` (isolated path, step 1 of 2)

**Purpose:** create+verify the new ABAC policy for every eligible table,
**without** touching the legacy row filter/mask. The table ends up with
**both** mechanisms active — never a security gap (the new policy is purely
additive). Use this when you want a checkpoint to review the new policies
before committing to removing the old ones.

**Preconditions:** run `INVENTORY` first and review the scope.

**Steps:**

1. Trigger `abac_migration_apply_abac_job`.
2. Set parameters (same shape as `MIGRATE`):

   | Parameter | Example | Notes |
   |---|---|---|
   | `mode` | `APPLY_ABAC` (already the job's default) | |
   | `scope_type` / `catalogs` etc. | your scope | |
   | `audit_catalog` / `audit_schema` | same as inventory | |
   | `dry_run` | `true` first, then `false` | same dry-run-first pattern as `MIGRATE` |

3. Run with `dry_run=true` first, review `tables_would_migrate`, then rerun
   with `dry_run=false`.
4. Check the report: `tables_abac_applied` should equal `tables_eligible`.
5. **Review the result** before moving on:
   - Query `migration_audit` — every affected row/object should show
     `status = ABAC_APPLIED` and `migration_phase = ABAC_APPLIED` (explicit
     "not final yet" marker).
   - Spot-check a table live: `DESCRIBE TABLE EXTENDED <table>` should still
     show the legacy **Row Filter** / **Column Masks** section, and
     `SHOW POLICIES ON TABLE <table>` should now also list the new
     `abac_migrated_...` policy/policies.
   - Query the table as different test users/roles to confirm the new ABAC
     policy behaves as expected (masking/filtering correctly) — this is your
     chance to catch a wrong policy **before** the old safety net is removed.
6. This is a safe point to pause for as long as you need — hours, days,
   whatever your review process requires. Nothing time-sensitive is pending.

**You're done when:** `tables_abac_applied == tables_eligible`, you've
spot-checked the new policies live, and you're confident enough to proceed
to `FINALIZE`.

---

### 6.5 `FINALIZE` (isolated path, step 2 of 2)

**Purpose:** remove the legacy row filter/mask for every table that already
has its ABAC policy applied (from a prior `APPLY_ABAC` or `MIGRATE` run),
and do a final verification. **Never creates a policy itself** — it only
ever removes.

**Preconditions:** `APPLY_ABAC` (or `MIGRATE`) has already run successfully
on this scope. Anything not already `ABAC_APPLIED` is skipped/reported
`NOT_ELIGIBLE` — `FINALIZE` will not create the policy for you if you skip
step 1.

**Steps:**

1. Trigger `abac_migration_finalize_job`.
2. Set parameters — **same scope as the `APPLY_ABAC` run** you're finalizing:

   | Parameter | Example | Notes |
   |---|---|---|
   | `mode` | `FINALIZE` (already the job's default) | |
   | `scope_type` / `catalogs` etc. | same scope as step 1 | |
   | `audit_catalog` / `audit_schema` | same as before | |
   | `dry_run` | `true` first, then `false` | |

3. Run with `dry_run=true` first, then `dry_run=false`.
4. Check the report: `tables_succeeded` should equal the number of tables
   that were `ABAC_APPLIED`.
5. Verify: query `migration_audit` — those rows should now show
   `status = SUCCESS`, `migration_phase = FINALIZED`. Spot-check a table
   live with `DESCRIBE TABLE EXTENDED <table>` — the legacy **Row Filter** /
   **Column Masks** section should now be **gone**; `SHOW POLICIES ON TABLE`
   should still list the ABAC policy.
6. Optionally run `VERIFY` (§6.6) right after, on the same scope, as an
   independent double-check.

**You're done when:** every table that went through `APPLY_ABAC` now shows
`migration_phase = FINALIZED` and no longer has a legacy row filter/mask.

---

### 6.6 `VERIFY`

**Purpose:** an independent, read-only health check — confirms the expected
ABAC policy still exists and the legacy mechanism is (or isn't yet) gone.
Run this any time, whether or not a migration just happened — e.g. as a
scheduled nightly check, or right after `FINALIZE` as a second opinion.

**Preconditions:** none.

**Steps:**

1. Trigger `abac_migration_job` with `mode=VERIFY`.
2. Set the scope parameters the same way as any other run.
3. Run (there's no `dry_run` concern here — `VERIFY` never mutates
   anything).
4. Read `other_results` in the JSON report (or the notebook's cell output) —
   one entry per table:

   ```json
   {
     "table_name": "cat.sch.tbl",
     "status": "SUCCESS",
     "step_results": [
       {"object_type": "ROW_FILTER", "status": "SUCCESS", "source_function": "...", "target_policy_name": "abac_migrated_row_filter", ...},
       {"object_type": "COLUMN_MASK", "masked_column": "email", "status": "SUCCESS", ...}
     ],
     "error_code": null
   }
   ```

   - `status = SUCCESS` → fully migrated, healthy.
   - `status = ABAC_APPLIED` → expected mid-pipeline state (ran `APPLY_ABAC`
     but not `FINALIZE` yet) — **not a failure**.
   - `status = NOT_ELIGIBLE` → this utility never touched this table.
   - `status = FAILED` → something's wrong (policy missing, or the legacy
     mechanism unexpectedly reappeared) — check `error_code` and investigate
     live before doing anything else to this table.

**You're done when:** every table you expect to be migrated reports
`SUCCESS` (or the expected `ABAC_APPLIED` if you're mid-isolated-path).

---

### 6.7 `RECONCILE`

**Purpose:** compares live Unity Catalog state against this table's **last
recorded** state in `migration_audit`, to catch drift — e.g. someone
manually deleted the ABAC policy, or manually restored the legacy row
filter outside this tool.

**Preconditions:** none, but it's only meaningful for tables this utility
has already touched (others just report `NEVER_MIGRATED_BY_THIS_UTILITY`).

**Steps:**

1. Trigger `abac_migration_job` with `mode=RECONCILE`.
2. Set the same scope + `audit_catalog`/`audit_schema` as the run(s) you
   want to check against.
3. Run.
4. Read `other_results`:

   ```json
   {"table_name": "cat.sch.tbl", "drift_detected": false, "reason": "LIVE_STATE_MATCHES_AUDIT"}
   ```

   Possible `reason` values:
   - `NEVER_MIGRATED_BY_THIS_UTILITY` — no drift, just never touched.
   - `LAST_RUN_WAS_NOT_SUCCESSFUL` — no drift; last attempt already failed,
     nothing to compare against.
   - `LIVE_STATE_MATCHES_AUDIT` — no drift, all good.
   - anything else with `drift_detected: true` — live state no longer
     matches what was recorded; the `reason` carries the specific
     verification error code (e.g. policy missing) — investigate manually.

5. **`RECONCILE` only reports drift — it never auto-repairs.** If drift is
   found, decide manually whether to re-run `MIGRATE`/`APPLY_ABAC` to
   restore the intended state, or investigate why it changed first.

**You're done when:** you've reviewed every `drift_detected: true` entry and
either fixed the underlying cause or accepted it as intentional.

---

### 6.8 `ROLLBACK`

**Purpose:** undo one **specific prior run** — restores the exact legacy
row filter/mask that run replaced, and removes only the ABAC policy that
specific run created (never a policy it didn't create).

**Preconditions:** you have the `run_id` of the `MIGRATE` /
`INVENTORY_AND_MIGRATE` run you want to undo (find it in the `run_id`
column of `migration_audit`, or from that run's own report).

**Steps:**

1. Trigger `abac_migration_job` with `mode=ROLLBACK`.
2. Set parameters:

   | Parameter | Example | Notes |
   |---|---|---|
   | `mode` | `ROLLBACK` | |
   | `run_id` | `<the run_id to undo>` | **required** — this replaces `scope_type`/`catalogs`/etc.; scope is derived from the run itself |
   | `audit_catalog` / `audit_schema` | same as the original run | must point at the same audit tables that run wrote to |
   | `dry_run` | `true` first, then `false` | |

3. Run with `dry_run=true` first, review `other_results`, then rerun with
   `dry_run=false`.
4. Read `other_results`:

   ```json
   {"table_name": "cat.sch.tbl", "status": "ROLLED_BACK", "step_results": [...], "error_message": null}
   ```

   - `ROLLED_BACK` — restored successfully.
   - `WOULD_ROLLBACK` — dry-run equivalent.
   - `SKIPPED` — no rollback metadata was captured for that row (nothing to
     restore from — shouldn't happen for a normal prior run).
   - `FAILED` — check `error_message`; the table may be left in a mixed
     state, investigate live before retrying.

5. Spot-check a rolled-back table live: `DESCRIBE TABLE EXTENDED` should
   show the legacy row filter/mask restored; `SHOW POLICIES ON TABLE`
   should no longer list the policy that run created.

**You're done when:** every table from that run reports `ROLLED_BACK` (or
you've resolved any `FAILED` entries manually).

---

## 7. Useful audit-table queries (any mode, any time)

Replace `<audit_catalog>.<audit_schema>` with your actual location.

```sql
-- Everything found in the last INVENTORY run, with eligibility + PII hints
SELECT catalog, schema, table, migration_eligibility, eligibility_reason,
       has_row_filter, row_filter_suggested_pii_tag,
       has_column_masks, column_mask_suggested_pii_tags
FROM <audit_catalog>.<audit_schema>.inventory
ORDER BY inventoried_at DESC;

-- All attempts, full history, most recent first (NOT "current state" -
-- the same table/object can appear many times across runs; see below)
SELECT catalog, schema, table, object_type, masked_column,
       status, migration_phase, error_code, error_message, completed_at
FROM <audit_catalog>.<audit_schema>.migration_audit
ORDER BY completed_at DESC;

-- Anything that ever failed
SELECT * FROM <audit_catalog>.<audit_schema>.migration_audit
WHERE status = 'FAILED' ORDER BY completed_at DESC;
```

`migration_audit` is **append-only** — every `INVENTORY`/`APPLY_ABAC`/`FINALIZE`/
`MIGRATE` run adds a *new* row-set per (catalog, schema, table, object_type,
masked_column), it never updates old ones. So a raw, unfiltered `SELECT *`
will show every historical attempt, including e.g. an old `ABAC_APPLIED`/
`PARTIAL` row from a table's `APPLY_ABAC` run sitting right next to its
later `FINALIZED`/`PASSED` row from `FINALIZE` — reading the wrong (older)
row for a given object is the single most common source of "is this a bug?"
confusion when eyeballing this table. **Always dedupe to the latest row per
object** when you want current state, either with the auto-created view
(recommended) or the equivalent inline query:

```sql
-- Recommended: current status of every object, one row each, always
-- up to date (auto-created/refreshed by ensure_tables_exist() on every run)
SELECT * FROM <audit_catalog>.<audit_schema>.migration_audit_latest
ORDER BY schema, table, object_type, masked_column;

-- Tables currently stuck mid-isolated-path (APPLY_ABAC done, FINALIZE not
-- run yet) - uses the latest-row view so already-finalized tables whose
-- history happens to contain an old ABAC_APPLIED row are correctly excluded
SELECT DISTINCT catalog, schema, table
FROM <audit_catalog>.<audit_schema>.migration_audit_latest
WHERE migration_phase = 'ABAC_APPLIED';

-- Equivalent inline query, if the view doesn't exist yet (e.g. an
-- audit schema created before this view was introduced)
SELECT * FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY catalog, schema, table, object_type, masked_column
    ORDER BY completed_at DESC
  ) AS rn
  FROM <audit_catalog>.<audit_schema>.migration_audit
) WHERE rn = 1;
```

Live spot-checks (no audit table needed):

```sql
DESCRIBE TABLE EXTENDED <catalog>.<schema>.<table>;   -- legacy Row Filter / Column Masks sections
SHOW POLICIES ON TABLE <catalog>.<schema>.<table>;    -- ABAC policies currently attached
SHOW GOVERNED TAGS LIKE 'abac_%';                     -- governed tags this tool minted
```

---

## 8. Recommended runbooks

**First time on a brand-new scope:**
`INVENTORY` (review results) → `MIGRATE` with `dry_run=true` (review) →
`MIGRATE` with `dry_run=false` → `VERIFY`.

**Large/cautious migration, want a review checkpoint:**
`INVENTORY` (review) → `APPLY_ABAC` dry-run then real (review live policies,
bake in as long as needed) → `FINALIZE` dry-run then real → `VERIFY`.

**Ongoing operations after any migration:**
Schedule `VERIFY` and/or `RECONCILE` periodically (e.g. nightly) against
already-migrated scopes to catch drift early.

**Something went wrong with one specific run:**
Find its `run_id` in `migration_audit` → `ROLLBACK` with `dry_run=true`
(review) → `ROLLBACK` with `dry_run=false` → confirm live.

---

## 9. Parameter reference

| Parameter | Applies to | Default | Notes |
|---|---|---|---|
| `mode` | all | `INVENTORY` | see §2 |
| `scope_type` | all except `ROLLBACK` | `SELECTED_CATALOGS` | `ALL_CATALOGS` \| `SELECTED_CATALOGS` \| `ALL_SCHEMAS` \| `SELECTED_SCHEMAS` \| `SPECIFIC_TABLES` |
| `catalogs` | when `scope_type=SELECTED_CATALOGS` | `[]` | JSON list, e.g. `["ril_raw"]` |
| `schemas` | when `scope_type=SELECTED_SCHEMAS` | `{}` | JSON map, e.g. `{"ril_raw":["sch1","sch2"]}` |
| `tables` | when `scope_type=SPECIFIC_TABLES` | `[]` | JSON list of fully-qualified table names |
| `exclude_schema_regex` | all except `ROLLBACK` | `""` | e.g. `^information_schema$` |
| `dry_run` | all | `true` | **override to `false` to actually persist/mutate anything** |
| `continue_on_error` | mutating modes | `true` | keep processing other tables after one fails |
| `max_parallelism` | mutating modes | `4` | thread-pool size for per-table conversion |
| `audit_catalog` / `audit_schema` | all | *(required, no default)* | where `inventory`/`migration_audit` live |
| `audit_table` / `inventory_table` | all | `migration_audit` / `inventory` | table names within `audit_schema` |
| `policy_strategy` | mutating modes | `TABLE_BASED` | `TABLE_BASED` \| `FUNCTION_BASED` |
| `policy_to_principals` | mutating modes | `["account users"]` | JSON list |
| `policy_except_principals` | mutating modes | `[]` | JSON list of users/groups/service principals to **exempt** from every ABAC policy this run creates (`TO ... EXCEPT <principal>`) — e.g. `["etl_service_principal"]`. Exempted principals see fully unmasked/unfiltered data. Empty = no exemptions (unchanged behavior) |
| `prefer_existing_tags` | mutating modes | `true` | reuse a compatible existing governed tag instead of minting a new one, if found |
| `enable_llm_pii_tagging` | `INVENTORY` only | `false` | LLM-suggested PII category per legacy function — advisory only |
| `pii_llm_endpoint` | `INVENTORY` only, when the above is `true` | `databricks-meta-llama-3-3-70b-instruct` | override if that model isn't enabled on your account |
| `run_id` | `ROLLBACK` (required); optional elsewhere | auto-generated UUID | identifies the run to undo, for `ROLLBACK` |
| `warehouse_id` | all (job-level, not a `RunConfig` field) | *(required)* | SQL warehouse the notebook authenticates/executes against |

---

## 10. Troubleshooting quick reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `pre_validation_errors` non-empty, nothing ran | bad config (missing `audit_catalog`, invalid scope, DBR too old) | fix the reported issue, rerun |
| `INVENTORY` "succeeded" but `inventory` table wasn't created/no new rows | ran with `dry_run=true` | rerun with `dry_run=false` |
| `FINALIZE` reports `NOT_ELIGIBLE`/`ABAC_NOT_APPLIED_YET` for a table | `APPLY_ABAC` hasn't run for that table yet | run `APPLY_ABAC` first, then `FINALIZE` |
| `tables_failed > 0` after `MIGRATE`/`APPLY_ABAC`/`FINALIZE` | check `migration_audit.error_code`/`error_message` for that table | fix underlying issue (permissions, dangling function, conflicting policy), rerun — it's idempotent |
| `VERIFY` reports `ABAC_APPLIED` and you expected `SUCCESS` | isolated path — `FINALIZE` hasn't run yet | run `FINALIZE`, or this is expected if you're intentionally mid-pipeline |
| `RECONCILE` reports `drift_detected: true` | someone/something changed live UC outside this tool | investigate manually; `RECONCILE` never auto-repairs |
| CLI `--notebook-params` fails to parse a JSON value | CSV-style flag parser chokes on embedded quotes/commas | use the REST API (`/api/2.1/jobs/run-now`) instead — see §4C |

---

## Further reading

- `README.md` — concept-level guide (what each mode does and why).
- `abac_migration/DESIGN.md` — full architecture, data model, error
  taxonomy, and API resilience design.
- `resources/jobs.yml` / `resources/phased_jobs.yml` — the actual job
  definitions this SOP drives.
