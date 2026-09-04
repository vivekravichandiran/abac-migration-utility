"""UnityCatalogGateway: the ONLY seam between plugins/migration logic and
Databricks (§5). Two implementations:

- `DatabricksUnityCatalogGateway`: real implementation, executes the exact
  SQL statements confirmed live in DESIGN.md §13/§17 through an injected
  `SqlExecutor` (works with either a notebook's Spark session or the
  resilient REST client in `sql_statement_client.py`).
- `FakeUnityCatalogGateway` (tests/conftest.py): in-memory fake, not here.

Extended beyond the original conceptual §5 Protocol with governed-tag
methods, since §7.4 established tags as a hard, not optional, dependency
for `tag_provisioner.py`. Kept on the same gateway rather than a separate
interface, preserving "the only seam" property from §1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol

from .models import (
    ColumnMaskInfo,
    ColumnTagAssignment,
    GovernedTagDefinition,
    PolicyApplyResult,
    PolicyDefinition,
    PolicyRef,
    PolicySpec,
    RowFilterInfo,
    TableRef,
    TableSecurityState,
    quote_fqn,
    quote_ident,
)
from .retry import RetryPolicy, RetryStats

# Errors that mean "the thing we're checking for is absent", not "the API
# call failed" - callers rely on catching these to implement idempotent
# no-ops (e.g. dropping an already-dropped policy). Never retried, never
# raised as a hard failure by the specific methods that expect them.
NOT_FOUND_ERROR_CODES = frozenset({"POLICY_NOT_FOUND"})


def _is_not_found(res, marker: str) -> bool:
    """Not-found detection is intentionally message-substring based, not
    just error_code equality: empirically the same logical error
    (`POLICY_NOT_FOUND`) surfaces with error_code="POLICY_NOT_FOUND" from
    `DROP POLICY`, but error_code="BAD_REQUEST" (with the real marker only
    embedded in the message text) from `DESCRIBE POLICY` - via the same
    REST executor. error_code alone is not a reliable signal here."""
    return res.error_code == marker or (bool(res.error) and marker in res.error)


PERMISSION_DENIED_MARKER = "PERMISSION_DENIED"

# Confirmed live against ALL_CATALOGS scope on a real workspace: Databricks-
# managed `samples` catalog tables reject ABAC/policy operations outright
# (there is no owner to grant/deny anything, permissions are simply not a
# concept there) with this distinct error_code rather than PERMISSION_DENIED.
# Functionally identical outcome for this tool though - "not migratable by
# us" - so it's treated the same way as a permission gap, not a hard failure.
SAMPLE_TABLE_PERMISSIONS_MARKER = "SAMPLE_TABLE_PERMISSIONS"

_ACCESS_NOT_APPLICABLE_MARKERS = (PERMISSION_DENIED_MARKER, SAMPLE_TABLE_PERMISSIONS_MARKER)


def is_permission_denied(exc: "UCGatewayError") -> bool:
    """True when a UCGatewayError means "this securable is not something we
    can act on with the current identity/table kind" - not a bug, an
    expected outcome of ALL_CATALOGS/ALL_SCHEMAS scope discovering
    catalogs/tables the run-as identity was never granted access to, or
    Databricks-managed `samples` tables that reject policy operations
    entirely. Same message-substring caveat as `_is_not_found`: surfaces
    with error_code="BAD_REQUEST" and the real marker only in the message
    text."""
    return exc.error_code in _ACCESS_NOT_APPLICABLE_MARKERS or any(
        marker in (exc.message or "") for marker in _ACCESS_NOT_APPLICABLE_MARKERS
    )

# §7.4 point 4: a governed-tag value that was JUST added via
# ALTER GOVERNED TAG ... SET VALUES can take ~20-30s to propagate to the
# policy compiler ("Invalid tag value ..."). Confirmed live (2026-08-25,
# tag_provisioner rename to abac_rls/abac_colmask) that a brand-new governed
# tag *key* - i.e. the very first CREATE GOVERNED TAG for that key name on
# the account - takes noticeably longer (~2 min observed) and surfaces a
# *different* message ("Unknown tag policy key ...") rather than the
# existing-key/new-value message. Both are the same underlying
# semantic-but-transient condition, distinct from HTTP-level throttling
# (§10.1), so both get the same bounded retry loop.
#
# Confirmed live again (2026-09-01, `ril_abac_e2e_test` 12-table batch on
# the SOURCE workspace) that this can exceed the original 150s budget when
# an APPLY_ABAC run mints many brand-new tag *keys* back-to-back (here:
# ~20 new keys across 12 tables in one "Prepare Governed Tags" phase right
# before the parallel CREATE POLICY phase) - every one of the 12 tables
# failed with this exact "Invalid tag value ..." message, and manually
# re-running the identical `CREATE OR REPLACE POLICY` statement ~25 minutes
# later (well past the retry budget) succeeded immediately, confirming it
# was still just propagation lag, not a real error. Bumped the ceiling
# accordingly - still bounded/fails loudly past the ceiling, just sized for
# a bulk-provisioning run instead of the original single/few-table spike.
TAG_PROPAGATION_ERROR_SUBSTRINGS = ("Invalid tag value", "Unknown tag policy key")
TAG_PROPAGATION_MAX_WAIT_S = 420.0
TAG_PROPAGATION_POLL_INTERVAL_S = 10.0


# Fixed, closed vocabulary the LLM PII classifier (§ inventory LLM tagging)
# is instructed to pick from - keeps `row_filter_suggested_pii_tag` /
# `column_mask_suggested_pii_tags` a stable, filterable dimension in the
# inventory table rather than free-form model output. "other" covers a real
# but unlisted PII category the model still detected; "none" means it
# judged the function name+columns as *not* PII-related at all.
ALLOWED_PII_TAGS = frozenset({
    "ssn", "email", "phone", "credit_card", "date_of_birth", "address",
    "name", "national_id", "bank_account", "health", "salary",
    "business_unit", "region", "other", "none",
})


@dataclass(frozen=True)
class PiiSuggestion:
    """Result of an inventory-time, best-effort LLM classification of one
    legacy security function's likely PII category (§ inventory LLM
    tagging). Deliberately never raises - a classification failure (model
    unavailable, endpoint not enabled on this workspace, malformed response,
    etc.) degrades to `tag=None, error=<detail>` so it can never fail or
    slow down the rest of an INVENTORY run beyond one best-effort call."""
    tag: Optional[str]
    raw_response: Optional[str] = None
    error: Optional[str] = None


def _normalize_pii_suggestion(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    candidate = raw.strip().strip(".").strip("'\"").lower()
    # Models occasionally answer in a short phrase despite the prompt - take
    # the first token-ish chunk and fall back to substring containment
    # before giving up and bucketing as "other" (still useful signal: *some*
    # PII-ish category was detected, just not one of the fixed labels).
    first_word = candidate.split()[0].strip(",.") if candidate.split() else candidate
    if first_word in ALLOWED_PII_TAGS:
        return first_word
    for known in ALLOWED_PII_TAGS:
        if known in candidate:
            return known
    return "other"


class UCGatewayError(Exception):
    """Raised by the gateway for any non-transient statement failure. The
    `error_code` maps into the taxonomy in DESIGN.md §10."""

    def __init__(self, error_code: str, message: str, statement: str = ""):
        super().__init__(f"[{error_code}] {message}")
        self.error_code = error_code
        self.message = message
        self.statement = statement


class UnityCatalogGateway(Protocol):
    """The ONLY seam between plugins/migration logic and Databricks (§5).
    Extended with governed-tag methods per §7.4. Implemented by both
    `DatabricksUnityCatalogGateway` (real) and `FakeUnityCatalogGateway`
    (tests/conftest.py, in-memory) - fully mockable, per §1's layering rule."""

    def list_catalogs(self) -> list: ...
    def list_schemas(self, catalog: str) -> list: ...
    def list_tables(self, catalog: str, schema: str) -> list: ...
    def describe_table_security(self, table: TableRef) -> TableSecurityState: ...
    # `on_securable` is an already-backtick-quoted `ON` clause target, e.g.
    # ``TABLE `cat`.`sch`.`tbl` `` or ``CATALOG `cat` `` - produced by
    # `PolicyStrategy.on_securable_for()` (migration/policy_strategy.py),
    # never built ad hoc by a caller. This is what lets these three methods
    # serve both "table level application" and "catalog level application"
    # (§7.3) without a signature change per scope.
    def show_policies(self, on_securable: str) -> list: ...
    def describe_policy(self, on_securable: str, policy_name: str) -> Optional[PolicyDefinition]: ...
    def function_exists(self, function_fqn: str) -> bool: ...
    def can_execute_function(self, function_fqn: str) -> bool: ...
    def create_or_replace_policy(self, spec: PolicySpec, dry_run: bool) -> PolicyApplyResult: ...
    def drop_policy(self, on_securable: str, policy_name: str, dry_run: bool) -> None: ...
    def drop_row_filter(self, table: TableRef, dry_run: bool) -> None: ...
    def drop_column_mask(self, table: TableRef, column: str, dry_run: bool) -> None: ...
    def set_row_filter(self, table: TableRef, function_fqn: str, using_columns: list, dry_run: bool) -> None: ...
    def set_column_mask(self, table: TableRef, column: str, function_fqn: str, dry_run: bool) -> None: ...

    # governed tags (§7.4)
    def list_governed_tags(self) -> list: ...
    def describe_governed_tag(self, tag_key: str) -> Optional[GovernedTagDefinition]: ...
    def create_governed_tag(self, tag_key: str, values: list, description: str, dry_run: bool) -> None: ...
    def alter_governed_tag_set_values(self, tag_key: str, values: list, dry_run: bool) -> None: ...
    def drop_governed_tag(self, tag_key: str, dry_run: bool) -> None: ...
    def list_column_tags(self, table: TableRef) -> list: ...
    def set_column_tags(self, table: TableRef, column: str, tags: dict, dry_run: bool) -> None: ...

    # LLM-assisted PII classification (INVENTORY-only, advisory)
    def suggest_pii_tag(self, function_fqn: str, columns: list, endpoint: str) -> "PiiSuggestion": ...

    # generic escape hatch for audit/inventory table DDL+DML (§4) - these
    # are not Unity Catalog *security policy* operations, so they don't
    # warrant their own typed methods, but they still need to go through
    # the same seam/resilience wrapper as everything else.
    def run_sql(self, statement: str, dry_run: bool = False) -> list: ...


class SqlExecutor(Protocol):
    """Minimal seam the gateway needs: run one SQL statement, get back a
    result with `.status` ("SUCCEEDED"/"FAILED"/other), `.columns`,
    `.rows`, `.error`, `.error_code`. Both `sql_statement_client.py`'s
    ResilientDatabricksSQL and a notebook's spark.sql-backed wrapper satisfy
    this without either needing to know about the other."""

    def run(self, statement: str, timeout_s: int = 60):
        ...


_BACKTICKED_FQN_RE = re.compile(r"`([^`]+)`\.`([^`]+)`\.`([^`]+)`")


_MUTATING_PREFIXES = ("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "MERGE")


def _looks_mutating(statement: str) -> bool:
    return statement.strip().upper().startswith(_MUTATING_PREFIXES)


def _strip_backtick_fqn(text: str) -> str:
    m = _BACKTICKED_FQN_RE.search(text)
    if m:
        return ".".join(m.groups())
    return text.strip("`")


class DatabricksUnityCatalogGateway:
    """Real UnityCatalogGateway implementation. Every mutating call is
    dry-run aware at this boundary (§9) - plugins never branch on dry_run
    themselves."""

    def __init__(self, executor: SqlExecutor, retry_policy: Optional[RetryPolicy] = None):
        self._executor = executor
        self._retry_policy = retry_policy or RetryPolicy()

    # -- low-level helpers ---------------------------------------------

    def _execute(self, statement: str, treat_not_found_as: Optional[str] = None):
        res = self._executor.run(statement)
        if res.status == "SUCCEEDED":
            return res
        if treat_not_found_as and _is_not_found(res, treat_not_found_as):
            return res
        raise UCGatewayError(res.error_code or "UNKNOWN", res.error or "statement failed", statement)

    def _execute_with_tag_propagation_retry(self, statement: str):
        """§7.4 point 4: retries a CREATE POLICY that references a
        just-provisioned governed tag value, for a bounded window, when the
        only error is the known propagation-lag message."""
        stats = RetryStats()
        waited = 0.0
        while True:
            res = self._executor.run(statement)
            if res.status == "SUCCEEDED":
                return res
            # Matched on message substring only, not error_code: the REST
            # Statement Execution API surfaces this as error_code="BAD_REQUEST",
            # but a native Spark SqlExecutor (e.g. inside a notebook/job)
            # raises an exception whose structured error class is the more
            # specific "INVALID_PARAMETER_VALUE.UC_INVALID_POLICY_CONDITION"
            # instead - the message text is the only reliably portable signal
            # across both executor backends.
            is_propagation_lag = bool(res.error) and any(
                marker in res.error for marker in TAG_PROPAGATION_ERROR_SUBSTRINGS
            )
            if not is_propagation_lag or waited >= TAG_PROPAGATION_MAX_WAIT_S:
                raise UCGatewayError(res.error_code or "UNKNOWN", res.error or "statement failed", statement)
            import time
            time.sleep(TAG_PROPAGATION_POLL_INTERVAL_S)
            waited += TAG_PROPAGATION_POLL_INTERVAL_S
            stats.attempts += 1

    # -- discovery (read-only) -------------------------------------------

    def list_catalogs(self) -> list:
        res = self._execute("SHOW CATALOGS")
        return [row[0] for row in res.rows]

    def list_schemas(self, catalog: str) -> list:
        res = self._execute(f"SHOW SCHEMAS IN {quote_ident(catalog)}")
        return [row[0] for row in res.rows]

    def list_tables(self, catalog: str, schema: str) -> list:
        res = self._execute(f"SHOW TABLES IN {quote_ident(catalog)}.{quote_ident(schema)}")
        return [TableRef(catalog, schema, row[1]) for row in res.rows]

    def describe_table_security(self, table: TableRef) -> TableSecurityState:
        res = self._execute(f"DESCRIBE TABLE EXTENDED {table.quoted_full_name}")
        row_filter = None
        column_masks = []
        table_type = "MANAGED"
        rows = [list(r) for r in res.rows]

        for i, row in enumerate(rows):
            label = (row[0] or "").strip()
            if label == "Type":
                table_type = (row[1] or table_type).strip()
            elif label == "Row Filter":
                text = row[1] or ""
                fn = _strip_backtick_fqn(text)
                cols_match = re.search(r"ON\s*\(([^)]*)\)", text)
                using_columns = [c.strip() for c in cols_match.group(1).split(",")] if cols_match else []
                row_filter = RowFilterInfo(function_fqn=fn, using_columns=using_columns, raw_text=text)
            elif label == "# Column Masks":
                j = i + 1
                while j < len(rows) and rows[j][0] and not rows[j][0].startswith("#"):
                    col_name = rows[j][0].strip()
                    fn = _strip_backtick_fqn(rows[j][1] or "")
                    column_masks.append(ColumnMaskInfo(column=col_name, function_fqn=fn, raw_text=rows[j][1] or ""))
                    j += 1

        return TableSecurityState(table=table, table_type=table_type, row_filter=row_filter, column_masks=column_masks)

    def show_policies(self, on_securable: str) -> list:
        res = self._execute(f"SHOW POLICIES ON {on_securable}")
        refs = []
        for row in res.rows:
            row = list(row)
            refs.append(PolicyRef(
                policy_name=row[0], policy_type=row[1], catalog=row[2], schema=row[3],
                table=row[4], comment=(row[5] if len(row) > 5 else "") or "",
            ))
        return refs

    def describe_policy(self, on_securable: str, policy_name: str) -> Optional[PolicyDefinition]:
        try:
            res = self._execute(f"DESCRIBE POLICY {policy_name} ON {on_securable}",
                                 treat_not_found_as="POLICY_NOT_FOUND")
        except UCGatewayError:
            raise
        if _is_not_found(res, "POLICY_NOT_FOUND"):
            return None

        kv = {}
        for row in res.rows:
            row = list(row)
            key = (row[0] or "").strip()
            kv[key] = row[1] if len(row) > 1 else None

        match_columns_raw = kv.get("Match Columns", "") or ""
        match_columns = [m.strip() for m in match_columns_raw.split(",")] if match_columns_raw else []

        return PolicyDefinition(
            name=kv.get("Name", policy_name),
            policy_type=kv.get("Policy Type", ""),
            on_securable_type=kv.get("On Securable Type", ""),
            on_securable=kv.get("On Securable", ""),
            to_principals=[p.strip() for p in (kv.get("To Principals") or "").split(",") if p.strip()],
            match_columns=match_columns,
            function_fqn=kv.get("  Function Name") or kv.get("Function Name") or "",
            using_columns=[c.strip() for c in (kv.get("  Using Columns") or "").split(",") if c.strip()],
            on_column_alias=kv.get("  On Column") or None,
            # NOTE: key name inferred from the consistent "To Principals" /
            # "Match Columns" / etc. naming convention seen in every other
            # DESCRIBE POLICY field, NOT yet confirmed-live like the rest of
            # this file's parsing (§17 methodology) - the workspace token
            # available while adding this expired before it could be. Kept
            # a soft .get() (degrades to [] rather than raising) specifically
            # because of that; re-verify against a live DESCRIBE POLICY ...
            # EXCEPT output and correct the key here if it differs.
            except_principals=[p.strip() for p in (kv.get("Except Principals") or "").split(",") if p.strip()],
        )

    def function_exists(self, function_fqn: str) -> bool:
        catalog, schema, name = function_fqn.split(".")
        res = self._execute(
            f"SELECT 1 FROM {quote_ident(catalog)}.information_schema.routines "
            f"WHERE routine_schema = '{schema}' AND routine_name = '{name}'"
        )
        return len(res.rows) > 0

    def can_execute_function(self, function_fqn: str) -> bool:
        """Best-effort probe (§13 open TODO: exact EXECUTE-grant check is a
        known limitation). Treats a successful DESCRIBE FUNCTION as
        sufficient evidence the function is visible/usable; a permission
        error is treated as not-executable rather than propagated."""
        res = self._executor.run(f"DESCRIBE FUNCTION EXTENDED {quote_fqn(function_fqn)}")
        return res.status == "SUCCEEDED"

    # -- ABAC policy mutation ---------------------------------------------

    def create_or_replace_policy(self, spec: PolicySpec, dry_run: bool) -> PolicyApplyResult:
        statement = self._build_create_policy_statement(spec)
        if dry_run:
            return PolicyApplyResult(success=True, policy_name=spec.policy_name,
                                      statement_text=statement, dry_run=True)
        try:
            self._execute_with_tag_propagation_retry(statement)
        except UCGatewayError as exc:
            return PolicyApplyResult(success=False, policy_name=spec.policy_name, statement_text=statement,
                                      error_code=exc.error_code, error_message=exc.message)
        return PolicyApplyResult(success=True, policy_name=spec.policy_name, statement_text=statement)

    def _build_create_policy_statement(self, spec: PolicySpec) -> str:
        match_clauses = ", ".join(
            (f"has_tag_value('{mc.tag_key}', '{mc.tag_value}')" if mc.tag_value is not None
             else f"has_tag('{mc.tag_key}')") + f" AS {mc.alias}"
            for mc in spec.match_columns
        )
        principals = ", ".join(f"`{p}`" for p in spec.to_principals)
        comment_clause = f"COMMENT '{spec.comment}'\n" if spec.comment else ""
        # `EXCEPT principal [, ...]` (confirmed live grammar): principals
        # here are fully exempted - not subject to the row filter/column
        # mask at all, see full unmasked/unfiltered data. Omitted entirely
        # when empty (the default), identical to today's statement text.
        except_clause = (
            f" EXCEPT {', '.join(f'`{p}`' for p in spec.except_principals)}" if spec.except_principals else ""
        )

        if spec.policy_type == "ROW_FILTER":
            using_clause = f"\nUSING COLUMNS ({', '.join(spec.using_columns)})" if spec.using_columns else ""
            return (
                f"CREATE OR REPLACE POLICY {spec.policy_name}\n"
                f"ON {spec.on_securable}\n"
                f"{comment_clause}"
                f"ROW FILTER {spec.function_fqn}\n"
                f"TO {principals}{except_clause}\n"
                f"FOR TABLES\n"
                f"MATCH COLUMNS {match_clauses}"
                f"{using_clause}"
            )
        else:  # COLUMN_MASK
            using_clause = f"\nUSING COLUMNS ({', '.join(spec.using_columns)})" if spec.using_columns else ""
            return (
                f"CREATE OR REPLACE POLICY {spec.policy_name}\n"
                f"ON {spec.on_securable}\n"
                f"{comment_clause}"
                f"COLUMN MASK {spec.function_fqn}\n"
                f"TO {principals}{except_clause}\n"
                f"FOR TABLES\n"
                f"MATCH COLUMNS {match_clauses}\n"
                f"ON COLUMN {spec.mask_target_alias}"
                f"{using_clause}"
            )

    def drop_policy(self, on_securable: str, policy_name: str, dry_run: bool) -> None:
        statement = f"DROP POLICY {policy_name} ON {on_securable}"
        if dry_run:
            return
        # No IF EXISTS in the grammar (§13) - explicitly swallow POLICY_NOT_FOUND
        # to make this call idempotent, per the confirmed finding in §17.
        self._execute(statement, treat_not_found_as="POLICY_NOT_FOUND")

    def drop_row_filter(self, table: TableRef, dry_run: bool) -> None:
        statement = f"ALTER TABLE {table.quoted_full_name} DROP ROW FILTER"
        if dry_run:
            return
        self._execute(statement)

    def drop_column_mask(self, table: TableRef, column: str, dry_run: bool) -> None:
        statement = f"ALTER TABLE {table.quoted_full_name} ALTER COLUMN {quote_ident(column)} DROP MASK"
        if dry_run:
            return
        self._execute(statement)

    def set_row_filter(self, table: TableRef, function_fqn: str, using_columns: list, dry_run: bool) -> None:
        quoted_cols = ", ".join(quote_ident(c) for c in using_columns)
        statement = f"ALTER TABLE {table.quoted_full_name} SET ROW FILTER {function_fqn} ON ({quoted_cols})"
        if dry_run:
            return
        self._execute(statement)

    def set_column_mask(self, table: TableRef, column: str, function_fqn: str, dry_run: bool) -> None:
        statement = f"ALTER TABLE {table.quoted_full_name} ALTER COLUMN {quote_ident(column)} SET MASK {function_fqn}"
        if dry_run:
            return
        self._execute(statement)

    # -- governed tags (§7.4 extension) ------------------------------------

    def list_governed_tags(self) -> list:
        res = self._execute("SHOW GOVERNED TAGS")
        tags = []
        for row in res.rows:
            row = list(row)
            values_raw = row[3] if len(row) > 3 else "[]"
            values = []
            if isinstance(values_raw, str) and values_raw not in ("[]", ""):
                values = [v.strip().strip("'\"") for v in values_raw.strip("[]").split(",") if v.strip()]
            tags.append(GovernedTagDefinition(tag_key=row[0], values=values, description=row[2] or ""))
        return tags

    def describe_governed_tag(self, tag_key: str) -> Optional[GovernedTagDefinition]:
        try:
            res = self._execute(f"DESCRIBE GOVERNED TAG {tag_key}")
        except UCGatewayError as exc:
            if "NOT_FOUND" in (exc.error_code or ""):
                return None
            raise
        kv = {}
        for row in res.rows:
            row = list(row)
            kv[row[0]] = row[1] if len(row) > 1 else None
        values_raw = kv.get("Values", "") or ""
        values = [v.strip() for v in values_raw.split(",") if v.strip()]
        return GovernedTagDefinition(tag_key=kv.get("Tag Key", tag_key), values=values)

    def create_governed_tag(self, tag_key: str, values: list, description: str, dry_run: bool) -> None:
        values_clause = f" VALUES ({', '.join(repr(v) for v in values)})" if values else ""
        desc_clause = f" DESCRIPTION '{description}'" if description else ""
        statement = f"CREATE GOVERNED TAG {tag_key}{desc_clause}{values_clause}"
        if dry_run:
            return
        self._execute(statement)

    def alter_governed_tag_set_values(self, tag_key: str, values: list, dry_run: bool) -> None:
        # §7.4 point 3: this is a full-replace, not additive - callers must
        # pass the complete desired list (existing + new), never just deltas.
        values_clause = ", ".join(repr(v) for v in values)
        statement = f"ALTER GOVERNED TAG {tag_key} SET VALUES ({values_clause})"
        if dry_run:
            return
        self._execute(statement)

    def drop_governed_tag(self, tag_key: str, dry_run: bool) -> None:
        statement = f"DROP GOVERNED TAG {tag_key}"
        if dry_run:
            return
        self._execute(statement)

    def list_column_tags(self, table: TableRef) -> list:
        res = self._execute(
            f"SELECT column_name, tag_name, tag_value FROM {quote_ident(table.catalog)}.information_schema.column_tags "
            f"WHERE schema_name = '{table.schema}' AND table_name = '{table.table}'"
        )
        # A key-only tag (`SET TAGS ('key')`, no `= value`) is persisted by
        # Databricks as tag_value = '' (empty string), NOT NULL - confirmed
        # live via this exact query. Normalized to None here (the single
        # place raw column_tags rows enter the system) so every downstream
        # consumer - critically tag_provisioner._find_reusable_tag(), which
        # feeds this straight into MatchColumn.tag_value - sees the same
        # None-means-key-only convention _mint_and_assign() itself uses.
        # Without this, has_tag(key) (correct, used when a tag is first
        # minted) silently turns into has_tag_value(key, '') on every
        # SUBSEQUENT run that reuses the tag (prefer_existing_tags), which
        # UC's policy compiler deterministically rejects with "Invalid tag
        # value `` for key ..." - confirmed live (2026-09-01): NOT a
        # propagation-lag transient (retrying for the full bounded window
        # never helps), a 100%-reproducible bug on every idempotent rerun.
        return [
            ColumnTagAssignment(column=row[0], tag_key=row[1], tag_value=(row[2] if row[2] else None))
            for row in res.rows
        ]

    def set_column_tags(self, table: TableRef, column: str, tags: dict, dry_run: bool) -> None:
        # A None value means a key-only/presence tag (confirmed live: `SET
        # TAGS ('key')` with no `= value` is valid syntax) - used whenever
        # tag_provisioner decides has_tag(key) is safe (no same-table
        # collision), to avoid needing an allowed-value entry at all.
        tags_clause = ", ".join(f"'{k}'" if v is None else f"'{k}' = '{v}'" for k, v in tags.items())
        statement = f"ALTER TABLE {table.quoted_full_name} ALTER COLUMN {quote_ident(column)} SET TAGS ({tags_clause})"
        if dry_run:
            return
        self._execute(statement)

    # -- LLM-assisted PII classification (INVENTORY-only, advisory) -------

    def suggest_pii_tag(self, function_fqn: str, columns: list, endpoint: str) -> PiiSuggestion:
        """Invokes a Databricks Foundation Model API endpoint via the
        `ai_query()` SQL function - stays consistent with "every Databricks
        interaction is a SQL statement through this gateway" rather than
        introducing a second, REST-based LLM client. Read-only: this never
        looks at the table's row data, only the function's name and the
        column(s) it governs, and it never influences any migration
        decision - purely an advisory tag surfaced in the inventory table
        for a human to review."""
        columns_desc = ", ".join(columns) if columns else "(none)"
        prompt = (
            "You are classifying a Unity Catalog SQL function used to enforce "
            "a legacy row filter or column mask on a table. Based ONLY on its "
            "fully-qualified name and the column(s) it governs (never on any "
            "actual row data), respond with EXACTLY ONE lowercase word from "
            "this fixed set that best names the sensitive-data/PII category it "
            "most likely protects: ssn, email, phone, credit_card, "
            "date_of_birth, address, name, national_id, bank_account, health, "
            "salary, business_unit, region, other, none. Respond with ONLY "
            "that single word and nothing else.\n\n"
            f"Function: {function_fqn}\nColumn(s): {columns_desc}"
        )
        escaped_prompt = prompt.replace("'", "''")
        statement = f"SELECT ai_query('{endpoint}', '{escaped_prompt}') AS suggestion"
        try:
            res = self._executor.run(statement)
        except Exception as exc:  # noqa: BLE001 - never let an LLM/network hiccup fail inventory
            return PiiSuggestion(tag=None, error=str(exc))
        if res.status != "SUCCEEDED":
            return PiiSuggestion(tag=None, error=res.error or "ai_query statement failed")
        raw = res.rows[0][0] if res.rows else None
        return PiiSuggestion(tag=_normalize_pii_suggestion(raw), raw_response=raw)

    # -- generic escape hatch (audit/inventory persistence, §4) -----------

    def run_sql(self, statement: str, dry_run: bool = False) -> list:
        if dry_run and _looks_mutating(statement):
            return []
        res = self._execute(statement)
        return [list(r) for r in res.rows]
