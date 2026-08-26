"""Shared data types for the UnityCatalogGateway seam (§5). Every plugin,
the policy strategy, and the tag provisioner speak only in these types -
never in raw SQL strings or raw API JSON.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, NamedTuple, Optional


def quote_ident(name: str) -> str:
    """Backtick-quotes a single SQL identifier component. Required for any
    catalog/schema/table/column name interpolated into a statement, since
    real-world names routinely violate the unquoted-identifier grammar
    (hyphens, leading digits, spaces, ...) - confirmed live against a
    catalog named `jh-demo` during ALL_CATALOGS-scope testing."""
    return f"`{name}`"


def quote_fqn(dotted: str) -> str:
    """Backtick-quotes each dot-separated part of an already-dotted
    identifier string (e.g. 'catalog.schema.table')."""
    return ".".join(quote_ident(p) for p in dotted.split("."))


class TableRef(NamedTuple):
    catalog: str
    schema: str
    table: str

    @property
    def full_name(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.table}"

    @property
    def quoted_full_name(self) -> str:
        return f"{quote_ident(self.catalog)}.{quote_ident(self.schema)}.{quote_ident(self.table)}"

    @property
    def schema_full_name(self) -> str:
        return f"{self.catalog}.{self.schema}"

    def __str__(self) -> str:
        return self.full_name


@dataclass(frozen=True)
class RowFilterInfo:
    function_fqn: str
    using_columns: list
    raw_text: str = ""


@dataclass(frozen=True)
class ColumnMaskInfo:
    column: str
    function_fqn: str
    raw_text: str = ""


@dataclass(frozen=True)
class TableSecurityState:
    """Result of discover-only inspection of one table's legacy security
    config (§2 discovery/ layer). Never produced by anything that mutates."""
    table: TableRef
    table_type: str = "MANAGED"
    row_filter: Optional[RowFilterInfo] = None
    column_masks: list = field(default_factory=list)  # list[ColumnMaskInfo]

    @property
    def has_row_filter(self) -> bool:
        return self.row_filter is not None

    @property
    def has_column_masks(self) -> bool:
        return len(self.column_masks) > 0


@dataclass(frozen=True)
class PolicyRef:
    policy_name: str
    policy_type: str  # "ROW_FILTER" | "COLUMN_MASK"
    catalog: str
    schema: str
    table: str
    comment: str = ""


@dataclass(frozen=True)
class PolicyDefinition:
    """Full detail as returned by DESCRIBE POLICY (§13) - what
    validate()/verify() compare against a desired PolicySpec."""
    name: str
    policy_type: str  # "ROW_FILTER" | "COLUMN_MASK"
    on_securable_type: str
    on_securable: str
    to_principals: list
    match_columns: list  # list[str], e.g. ["has_tag_value('k','v') AS alias"]
    function_fqn: str
    using_columns: list = field(default_factory=list)
    on_column_alias: Optional[str] = None


class PolicySpec(NamedTuple):
    """Desired-state spec produced by PolicyStrategy; the gateway turns this
    into the actual CREATE POLICY statement (§5)."""
    policy_name: str
    on_securable: str  # e.g. "TABLE catalog.schema.table"
    policy_type: Literal["ROW_FILTER", "COLUMN_MASK"]
    function_fqn: str
    match_columns: list  # list[MatchColumn]
    using_columns: list  # list[str] - aliases from match_columns, positional
    mask_target_alias: Optional[str] = None  # only for COLUMN_MASK
    to_principals: list = field(default_factory=lambda: ["account users"])
    comment: str = ""


class MatchColumn(NamedTuple):
    """One `MATCH COLUMNS has_tag_value(key, value) AS alias` clause."""
    tag_key: str
    tag_value: Optional[str]  # None => has_tag(key) instead of has_tag_value
    alias: str
    source_column: str  # the real physical column this alias represents (audit only)


@dataclass(frozen=True)
class PolicyApplyResult:
    success: bool
    policy_name: str
    statement_text: str = ""
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    dry_run: bool = False


@dataclass(frozen=True)
class TagRef:
    tag_key: str
    tag_value: Optional[str]
    is_governed: bool = True


@dataclass(frozen=True)
class GovernedTagDefinition:
    tag_key: str
    values: list = field(default_factory=list)  # [] means key-only
    description: str = ""


@dataclass(frozen=True)
class ColumnTagAssignment:
    column: str
    tag_key: str
    tag_value: Optional[str]
