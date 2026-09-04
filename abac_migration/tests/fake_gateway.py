"""In-memory, dict-backed fake implementing the `UnityCatalogGateway`
Protocol (§12, §15). No real Databricks connection needed for any plugin/
table_converter/tag_provisioner test - every test in this suite is built
against this fake.
"""
from __future__ import annotations

from ..uc_gateway.gateway import PiiSuggestion
from ..uc_gateway.models import (
    ColumnMaskInfo,
    ColumnTagAssignment,
    GovernedTagDefinition,
    PolicyApplyResult,
    PolicyDefinition,
    PolicyRef,
    RowFilterInfo,
    TableRef,
    TableSecurityState,
)

# Deterministic keyword->tag mapping used by the fake's suggest_pii_tag(),
# standing in for a real LLM call so PII-tagging tests don't need network
# access. Order matters (checked top to bottom, first match wins).
_FAKE_PII_KEYWORD_MAP = [
    ("ssn", "ssn"), ("social_security", "ssn"),
    ("email", "email"),
    ("phone", "phone"),
    ("credit_card", "credit_card"), ("card", "credit_card"),
    ("dob", "date_of_birth"), ("birth", "date_of_birth"),
    ("address", "address"),
    ("national_id", "national_id"),
    ("bank_account", "bank_account"), ("iban", "bank_account"),
    ("health", "health"), ("medical", "health"),
    ("salary", "salary"), ("compensation", "salary"),
    ("business_unit", "business_unit"),
    ("region", "region"),
    ("name", "name"),
]


def _securable_key(on_securable: str) -> str:
    """Normalizes a strategy-produced `ON <securable>` string (e.g.
    ``TABLE `cat`.`sch`.`tbl` `` or ``CATALOG `cat` ``) into a plain,
    backtick-free dict key by dropping the leading securable-type keyword -
    ``TABLE `cat`.`sch`.`tbl` `` -> `"cat.sch.tbl"` (identical to
    `table.full_name`, preserving every pre-existing test's direct
    `fake.policies[table.full_name]` access), ``CATALOG `cat` `` -> `"cat"`.
    Mirrors what the real gateway does implicitly by just interpolating the
    already-quoted string into SQL; this in-memory fake needs an explicit
    hashable key instead."""
    return on_securable.split(" ", 1)[1].replace("`", "")


class FakeUnityCatalogGateway:
    def __init__(self):
        self.catalogs = set()
        self.schemas = {}
        self.tables = {}
        self.row_filters = {}
        self.column_masks = {}
        self.policies = {}
        self.governed_tags = {}
        self.column_tags = {}
        self.functions = set()
        self.non_executable_functions = set()

        self.mutation_calls = []
        self.dry_run_calls = []
        self.generic_sql_log = []
        self.pii_suggestion_calls = []

        self._raise_on = {}
        self._create_policy_failure = None  # (error_code, error_message) or None
        self._describe_policy_override = {}

    # -- test-only setup helpers ------------------------------------------

    def register_table(self, table: TableRef, table_type: str = "MANAGED") -> None:
        self.catalogs.add(table.catalog)
        self.schemas.setdefault(table.catalog, set()).add(table.schema)
        self.tables[table.full_name] = table_type
        self.row_filters.setdefault(table.full_name, None)
        self.column_masks.setdefault(table.full_name, {})
        self.policies.setdefault(table.full_name, {})
        self.column_tags.setdefault(table.full_name, [])

    def set_row_filter_state(self, table: TableRef, function_fqn: str, using_columns: list) -> None:
        self.register_table(table)
        self.row_filters[table.full_name] = RowFilterInfo(
            function_fqn=function_fqn, using_columns=list(using_columns),
            raw_text=f"`{function_fqn}` ON ({', '.join(using_columns)})",
        )
        self.functions.add(function_fqn)

    def set_column_mask_state(self, table: TableRef, column: str, function_fqn: str) -> None:
        self.register_table(table)
        self.column_masks[table.full_name][column] = ColumnMaskInfo(
            column=column, function_fqn=function_fqn, raw_text=f"`{function_fqn}`",
        )
        self.functions.add(function_fqn)

    def add_existing_policy(self, table: TableRef, policy_def: PolicyDefinition) -> None:
        """Seeds a TABLE-scoped ("table level application") existing policy."""
        self.register_table(table)
        self.policies[table.full_name][policy_def.name] = policy_def

    def add_existing_catalog_policy(self, catalog: str, policy_def: PolicyDefinition) -> None:
        """Seeds a CATALOG-scoped ("catalog level application") existing
        policy - i.e. a policy `CatalogBasedPolicyStrategy` would have
        created, not tied to any single table."""
        self.catalogs.add(catalog)
        self.policies.setdefault(catalog, {})[policy_def.name] = policy_def

    def add_column_tag(self, table: TableRef, column: str, tag_key: str, tag_value) -> None:
        self.register_table(table)
        self.column_tags[table.full_name].append(ColumnTagAssignment(column=column, tag_key=tag_key, tag_value=tag_value))

    def register_governed_tag(self, tag_key: str, values: list = None, description: str = "") -> None:
        self.governed_tags[tag_key] = GovernedTagDefinition(tag_key=tag_key, values=list(values or []), description=description)

    def set_fault(self, method_name: str, exc: Exception) -> None:
        self._raise_on[method_name] = exc

    def clear_fault(self, method_name: str) -> None:
        self._raise_on.pop(method_name, None)

    def fail_next_create_policy(self, error_code: str = "POLICY_CREATE_FAILED", error_message: str = "injected failure") -> None:
        self._create_policy_failure = (error_code, error_message)

    def override_describe_policy(self, on_securable, policy_name: str, policy_def: PolicyDefinition) -> None:
        """`on_securable` accepts either the strategy-style string (e.g.
        ``TABLE `cat`.`sch`.`tbl` ``) or, for backward compatibility with
        every pre-existing table-scope test, a bare `TableRef` (implicitly
        treated as `TABLE <that table>`)."""
        key = _securable_key(f"TABLE {on_securable.quoted_full_name}") if isinstance(on_securable, TableRef) else _securable_key(on_securable)
        self._describe_policy_override[(key, policy_name)] = policy_def

    def _maybe_raise(self, method_name: str) -> None:
        exc = self._raise_on.pop(method_name, None)
        if exc is not None:
            raise exc

    # -- UnityCatalogGateway Protocol -------------------------------------

    def list_catalogs(self) -> list:
        return sorted(self.catalogs)

    def list_schemas(self, catalog: str) -> list:
        self._maybe_raise("list_schemas")
        return sorted(self.schemas.get(catalog, set()))

    def list_tables(self, catalog: str, schema: str) -> list:
        self._maybe_raise("list_tables")
        prefix = f"{catalog}.{schema}."
        return [TableRef(*fn.split(".")) for fn in self.tables if fn.startswith(prefix)]

    def describe_table_security(self, table: TableRef) -> TableSecurityState:
        self._maybe_raise("describe_table_security")
        return TableSecurityState(
            table=table, table_type=self.tables.get(table.full_name, "MANAGED"),
            row_filter=self.row_filters.get(table.full_name),
            column_masks=list(self.column_masks.get(table.full_name, {}).values()),
        )

    def show_policies(self, on_securable: str) -> list:
        self._maybe_raise("show_policies")
        key = _securable_key(on_securable)
        parts = key.split(".")
        catalog = parts[0] if len(parts) >= 1 else None
        schema = parts[1] if len(parts) >= 2 else None
        table_name = parts[2] if len(parts) >= 3 else None
        return [
            PolicyRef(policy_name=name, policy_type=d.policy_type, catalog=catalog, schema=schema, table=table_name)
            for name, d in self.policies.get(key, {}).items()
        ]

    def describe_policy(self, on_securable: str, policy_name: str):
        self._maybe_raise("describe_policy")
        key = _securable_key(on_securable)
        override = self._describe_policy_override.get((key, policy_name))
        if override is not None:
            return override
        return self.policies.get(key, {}).get(policy_name)

    def function_exists(self, function_fqn: str) -> bool:
        self._maybe_raise("function_exists")
        return function_fqn in self.functions

    def can_execute_function(self, function_fqn: str) -> bool:
        self._maybe_raise("can_execute_function")
        return function_fqn in self.functions and function_fqn not in self.non_executable_functions

    def create_or_replace_policy(self, spec, dry_run: bool) -> PolicyApplyResult:
        if dry_run:
            self.dry_run_calls.append(("create_or_replace_policy", spec.policy_name))
            return PolicyApplyResult(success=True, policy_name=spec.policy_name, statement_text="-- dry run --", dry_run=True)

        if self._create_policy_failure is not None:
            error_code, error_message = self._create_policy_failure
            self._create_policy_failure = None
            return PolicyApplyResult(success=False, policy_name=spec.policy_name, error_code=error_code, error_message=error_message)

        self._maybe_raise("create_or_replace_policy")
        self.mutation_calls.append(("create_or_replace_policy", spec.policy_name))
        # spec.on_securable is e.g. "TABLE `cat`.`schema`.`table`" or
        # "CATALOG `cat`" (real gateway always backtick-quotes it) - _securable_key
        # strips the leading keyword + backticks to get the dict key used
        # everywhere else in this fake ("cat.schema.table" / "cat").
        securable_key = _securable_key(spec.on_securable)
        on_securable_type = spec.on_securable.split(" ", 1)[0]
        policy_def = PolicyDefinition(
            name=spec.policy_name, policy_type=spec.policy_type, on_securable_type=on_securable_type,
            on_securable=securable_key,
            to_principals=list(spec.to_principals), match_columns=[mc.alias for mc in spec.match_columns],
            function_fqn=spec.function_fqn, using_columns=list(spec.using_columns), on_column_alias=spec.mask_target_alias,
            except_principals=list(spec.except_principals),
        )
        self.policies.setdefault(securable_key, {})[spec.policy_name] = policy_def
        return PolicyApplyResult(success=True, policy_name=spec.policy_name, statement_text="-- fake --")

    def drop_policy(self, on_securable: str, policy_name: str, dry_run: bool) -> None:
        if dry_run:
            self.dry_run_calls.append(("drop_policy", policy_name))
            return
        self._maybe_raise("drop_policy")
        self.mutation_calls.append(("drop_policy", policy_name))
        self.policies.get(_securable_key(on_securable), {}).pop(policy_name, None)

    def drop_row_filter(self, table: TableRef, dry_run: bool) -> None:
        if dry_run:
            self.dry_run_calls.append(("drop_row_filter", table.full_name))
            return
        self._maybe_raise("drop_row_filter")
        self.mutation_calls.append(("drop_row_filter", table.full_name))
        self.row_filters[table.full_name] = None

    def drop_column_mask(self, table: TableRef, column: str, dry_run: bool) -> None:
        if dry_run:
            self.dry_run_calls.append(("drop_column_mask", table.full_name, column))
            return
        self._maybe_raise("drop_column_mask")
        self.mutation_calls.append(("drop_column_mask", table.full_name, column))
        self.column_masks.get(table.full_name, {}).pop(column, None)

    def set_row_filter(self, table: TableRef, function_fqn: str, using_columns: list, dry_run: bool) -> None:
        if dry_run:
            self.dry_run_calls.append(("set_row_filter", table.full_name))
            return
        self._maybe_raise("set_row_filter")
        self.mutation_calls.append(("set_row_filter", table.full_name))
        self.row_filters[table.full_name] = RowFilterInfo(function_fqn=function_fqn, using_columns=list(using_columns))

    def set_column_mask(self, table: TableRef, column: str, function_fqn: str, dry_run: bool) -> None:
        if dry_run:
            self.dry_run_calls.append(("set_column_mask", table.full_name, column))
            return
        self._maybe_raise("set_column_mask")
        self.mutation_calls.append(("set_column_mask", table.full_name, column))
        self.column_masks.setdefault(table.full_name, {})[column] = ColumnMaskInfo(column=column, function_fqn=function_fqn)

    def list_governed_tags(self) -> list:
        self._maybe_raise("list_governed_tags")
        return list(self.governed_tags.values())

    def describe_governed_tag(self, tag_key: str):
        return self.governed_tags.get(tag_key)

    def create_governed_tag(self, tag_key: str, values: list, description: str, dry_run: bool) -> None:
        if dry_run:
            self.dry_run_calls.append(("create_governed_tag", tag_key))
            return
        self._maybe_raise("create_governed_tag")
        self.mutation_calls.append(("create_governed_tag", tag_key))
        self.governed_tags[tag_key] = GovernedTagDefinition(tag_key=tag_key, values=list(values), description=description)

    def alter_governed_tag_set_values(self, tag_key: str, values: list, dry_run: bool) -> None:
        if dry_run:
            self.dry_run_calls.append(("alter_governed_tag_set_values", tag_key))
            return
        self._maybe_raise("alter_governed_tag_set_values")
        self.mutation_calls.append(("alter_governed_tag_set_values", tag_key))
        existing = self.governed_tags.get(tag_key)
        self.governed_tags[tag_key] = GovernedTagDefinition(
            tag_key=tag_key, values=list(values), description=existing.description if existing else "",
        )

    def drop_governed_tag(self, tag_key: str, dry_run: bool) -> None:
        if dry_run:
            self.dry_run_calls.append(("drop_governed_tag", tag_key))
            return
        self._maybe_raise("drop_governed_tag")
        self.mutation_calls.append(("drop_governed_tag", tag_key))
        self.governed_tags.pop(tag_key, None)

    def list_column_tags(self, table: TableRef) -> list:
        return list(self.column_tags.get(table.full_name, []))

    def set_column_tags(self, table: TableRef, column: str, tags: dict, dry_run: bool) -> None:
        if dry_run:
            self.dry_run_calls.append(("set_column_tags", table.full_name, column))
            return
        self._maybe_raise("set_column_tags")
        self.mutation_calls.append(("set_column_tags", table.full_name, column))
        remaining = [t for t in self.column_tags.setdefault(table.full_name, []) if not (t.column == column and t.tag_key in tags)]
        for key, value in tags.items():
            remaining.append(ColumnTagAssignment(column=column, tag_key=key, tag_value=value))
        self.column_tags[table.full_name] = remaining

    def run_sql(self, statement: str, dry_run: bool = False) -> list:
        self.generic_sql_log.append(statement)
        return []

    def suggest_pii_tag(self, function_fqn: str, columns: list, endpoint: str) -> PiiSuggestion:
        self._maybe_raise("suggest_pii_tag")
        self.pii_suggestion_calls.append((function_fqn, tuple(columns), endpoint))
        haystack = f"{function_fqn} {' '.join(columns)}".lower()
        for keyword, tag in _FAKE_PII_KEYWORD_MAP:
            if keyword in haystack:
                return PiiSuggestion(tag=tag, raw_response=tag)
        return PiiSuggestion(tag="other", raw_response="other")
