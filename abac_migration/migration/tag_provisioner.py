"""Governed-tag provisioning (§7.4) - the new required sub-component
discovered during the §17 API spike: MATCH COLUMNS can only match on
governed tags, so every column referenced by a legacy row filter or column
mask needs a governed tag that identifies it within its own table before a
CREATE POLICY statement can be built.

Value is OPTIONAL, only added when actually needed for disambiguation. A
plain key-only tag (`CREATE GOVERNED TAG key`, no `VALUES`) plus
`MATCH COLUMNS has_tag(key)` is sufficient - and preferred, since it needs
no "Allowed values" entry at all - whenever that key will only ever land on
ONE column per table. A per-column-unique VALUE (`has_tag_value(key,
value)`) is only minted when it's actually required: confirmed live that
`has_tag(key)` compiles fine at `CREATE POLICY` time even when two columns
in the SAME table share that key, but then fails on every read with
`UC_ABAC_AMBIGUOUS_COLUMN_MATCH: ... had 2 matches, exactly 1 match is
allowed` - see `_split_by_collision()`. This only happens when the same
legacy function guards more than one column of the same table (rare, but
real - e.g. a generic reusable mask function applied to two columns).

Tag KEY granularity: one governed tag key per distinct legacy SQL function,
named `abac_<rls|colmask>_<catalog>_<schema>_<function_name>` - e.g.
`cat.sch.rf_region_both` -> `abac_rls_cat_sch_rf_region_both`. Including the
full catalog.schema qualification (not just the function's own short name)
makes the key deterministically unique with NO hash/digest suffix ever
needed: two different functions can only produce the same key if they are
the exact same catalog.schema.function_name to begin with. Each of
catalog/schema/function-name is sanitized independently (any character
outside `[A-Za-z0-9_]` - including hyphens, a confirmed-live real case, see
`quote_ident()`'s docstring re: catalog `jh-demo` - is replaced with `_`)
before being joined. Not one shared key for "all row filters" / "all column
masks" account-wide either - this keeps each function's migrated columns
independently discoverable/auditable by tag key (`SHOW GOVERNED TAGS` /
`DESCRIBE GOVERNED TAG abac_rls_<cat>_<sch>_<fn>` maps 1:1 back to the
legacy function that used to enforce that security), at the cost of many
more tag keys for a large migration - a deliberate trade requested over
both the original 2-keys-for-everything design AND a later short-name-only
variant (which needed hash-suffix disambiguation whenever two different
functions in different schemas happened to share a short name - dropped
entirely now that the key is fully qualified).

`_mint_and_assign()` still guards against the one remaining, extremely
unlikely edge case: sanitization collapsing two genuinely different raw
names onto the same sanitized key (e.g. `rf-region` and `rf_region` in the
same schema), or a pre-existing, unrelated governed tag that happens to
already occupy the exact deterministic key. Both raise `TagKeyCollisionError`
loudly rather than silently merging two functions under one tag or
inventing a hash-suffixed fallback key (no hash values are used anywhere in
this tag-naming scheme, by design).

`TagProvisioner.prepare()` is meant to be called exactly ONCE per run, in a
single-threaded "Prepare Governed Tags" phase (§3 flow diagram), BEFORE the
parallel per-table conversion phase begins - this is what avoids the
read-modify-write race on `ALTER GOVERNED TAG ... SET VALUES` (§7.4 point 3,
confirmed declarative/full-replace, not additive).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal, Optional

from ..uc_gateway.gateway import UnityCatalogGateway
from ..uc_gateway.models import MatchColumn, TableRef

SYNTHETIC_TAG_DESCRIPTION_TEMPLATE = (
    "Created by the ABAC migration utility for legacy function {function_fqn} "
    "to let MATCH COLUMNS uniquely target its migrated column(s). "
    "See DESIGN.md section 7.4."
)

# Comfortably under any plausible governed-tag-key length limit even after
# the "abac_rls_"/"abac_colmask_" prefix and the catalog.schema.function_name
# qualification. Deliberately truncated with NO hash suffix if ever
# exceeded (see module docstring) - pathologically long catalog/schema/
# function names are rare enough that this is an acceptable trade.
_MAX_SANITIZED_NAME_LEN = 180


class TagKeyCollisionError(RuntimeError):
    """Raised instead of silently merging two functions under one governed
    tag, or falling back to a hash-suffixed key. See module docstring for
    when this can happen (essentially: sanitization collapsing two distinct
    raw names onto the same string, or a pre-existing unrelated tag already
    occupying the exact deterministic key)."""


@dataclass(frozen=True)
class TagRequest:
    table: TableRef
    column: str
    role: Literal["row_filter", "mask"]
    # The legacy function this column's security was enforced by - drives
    # the per-function tag KEY (see _tag_key_for_function). Two TagRequests
    # for the same function+role always resolve to the same tag key, even
    # across different tables/columns, since one function can guard several
    # tables (§7.3 FunctionBasedPolicyStrategy is deferred, but the tag layer
    # underneath is already function-scoped in anticipation of it).
    function_fqn: str


TagResolutionKey = tuple  # (TableRef, str, str) - kept as plain tuple for hashability


def _alias_for(column: str) -> str:
    # Aliases just need to be valid SQL identifiers; prefixing avoids any
    # collision with SQL keywords that happen to match a column name.
    return f"mc_{column}"


def _fqn_parts(function_fqn: str) -> tuple:
    """Splits a (possibly backtick-quoted) catalog.schema.function_name
    identifier into its 3 components, stripping backticks - e.g.
    `` `cat`.`sch`.`rf_region_both` `` -> `("cat", "sch", "rf_region_both")`.
    Assumes exactly 3 dot-separated parts once backticks are stripped
    (function_fqn arrives already-normalized this way from
    uc_gateway.gateway._strip_backtick_fqn) - a literal '.' inside a
    backtick-quoted component is a documented, unhandled edge case.
    Degrades gracefully (empty catalog/schema) for an already-unqualified
    name, e.g. in tests."""
    stripped = function_fqn.replace("`", "")
    parts = stripped.split(".")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return "", "", parts[0]


def _short_function_name(function_fqn: str) -> str:
    """Just the function's own (unqualified) name - strips catalog/schema
    qualification and backtick-quoting. Used for the human-readable
    description only now (see SYNTHETIC_TAG_DESCRIPTION_TEMPLATE) - the tag
    KEY itself uses the fully-qualified form via _tag_key_for_function."""
    return _fqn_parts(function_fqn)[-1]


def _sanitize(part: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", part)


def _tag_key_for_function(function_fqn: str, role: Literal["row_filter", "mask"]) -> str:
    """One governed tag KEY per (catalog, schema, function, role):
    `abac_<rls|colmask>_<catalog>_<schema>_<function_name>`, each component
    independently sanitized (non `[A-Za-z0-9_]` characters, including
    hyphens, replaced with `_`) - e.g. `jh-demo.some_schema.rf_region_both`
    -> `abac_rls_jh_demo_some_schema_rf_region_both`. Deterministic and
    stable across runs/tables for the same function_fqn; no hash/digest is
    ever appended (see module docstring)."""
    role_abbrev = "rls" if role == "row_filter" else "colmask"
    catalog, schema, func_name = _fqn_parts(function_fqn)
    sanitized = "_".join(_sanitize(part) for part in (catalog, schema, func_name) if part)
    if len(sanitized) > _MAX_SANITIZED_NAME_LEN:
        sanitized = sanitized[:_MAX_SANITIZED_NAME_LEN]
    return f"abac_{role_abbrev}_{sanitized}"


def _synthetic_value_for(request: TagRequest) -> str:
    # 256-char governed-tag-value limit (confirmed via docs, §16 item 5) -
    # a fixed-length hash comfortably stays under it regardless of how long
    # catalog/schema/table/column names are; the audit trail stores the
    # real column name directly (§4), so no hash->column reverse-mapping is
    # ever needed off of the tag value itself. (Unrelated to the tag KEY
    # naming scheme above, which never uses a hash - this is a per-column
    # disambiguating VALUE under an already-human-readable key.)
    raw = f"{request.table.full_name}.{request.column}.{request.role}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class TagProvisioner:
    def __init__(self, uc: UnityCatalogGateway, prefer_existing_tags: bool = True):
        self._uc = uc
        self._prefer_existing_tags = prefer_existing_tags

    def prepare(self, requests: list, dry_run: bool = False) -> dict:
        """Resolves every TagRequest to a MatchColumn, minting/growing
        governed tags in as few serialized calls as possible. Returns
        {(table, column, role): MatchColumn}."""
        if not requests:
            return {}

        resolved = {}
        governed_tags = {t.tag_key: t for t in self._uc.list_governed_tags()}

        table_tags_cache = {}
        for req in requests:
            if req.table not in table_tags_cache:
                table_tags_cache[req.table] = self._uc.list_column_tags(req.table)

        to_mint = []
        for req in requests:
            reused = self._find_reusable_tag(req, table_tags_cache[req.table], governed_tags)
            if reused is not None:
                resolved[(req.table, req.column, req.role)] = reused
            else:
                to_mint.append(req)

        if to_mint:
            self._mint_and_assign(to_mint, governed_tags, resolved, dry_run, table_tags_cache)

        return resolved

    def _find_reusable_tag(
        self, request: TagRequest, table_tags: list, governed_tags: dict
    ) -> Optional[MatchColumn]:
        if not self._prefer_existing_tags:
            return None

        this_column_tags = [t for t in table_tags if t.column == request.column]
        for tag in this_column_tags:
            governed_def = governed_tags.get(tag.tag_key)
            if governed_def is None:
                continue  # not a governed tag - has_tag_value() can't reference it
            if governed_def.values and tag.tag_value not in governed_def.values:
                continue  # shouldn't happen, but never trust an inconsistent read

            clashing_columns = [
                t for t in table_tags
                if t.column != request.column and t.tag_key == tag.tag_key and t.tag_value == tag.tag_value
            ]
            if clashing_columns:
                # Not unique within this table's scope - MATCH COLUMNS would
                # ambiguously match more than one column (§7.3).
                continue

            return MatchColumn(
                tag_key=tag.tag_key, tag_value=tag.tag_value,
                alias=_alias_for(request.column), source_column=request.column,
            )
        return None

    def _mint_and_assign(
        self, to_mint: list, governed_tags: dict, resolved: dict, dry_run: bool, table_tags_cache: dict,
    ) -> None:
        by_key: dict = {}
        key_owner_fqn: dict = {}
        for req in to_mint:
            tag_key = _tag_key_for_function(req.function_fqn, req.role)
            owner_fqn = key_owner_fqn.setdefault(tag_key, req.function_fqn)
            if owner_fqn != req.function_fqn:
                raise TagKeyCollisionError(
                    f"Governed tag key '{tag_key}' would be shared by two different "
                    f"functions ({owner_fqn!r} and {req.function_fqn!r}) after "
                    "sanitization - refusing to silently merge them under one tag."
                )
            existing_def = governed_tags.get(tag_key)
            if existing_def is not None:
                expected_description = SYNTHETIC_TAG_DESCRIPTION_TEMPLATE.format(function_fqn=req.function_fqn)
                if existing_def.description != expected_description:
                    raise TagKeyCollisionError(
                        f"Governed tag '{tag_key}' already exists but was not created by "
                        f"this tool for function {req.function_fqn!r} "
                        f"(existing description: {existing_def.description!r}) - refusing "
                        "to reuse/overwrite a tag that isn't provably this function's own."
                    )
            by_key.setdefault(tag_key, []).append(req)

        for tag_key, reqs in by_key.items():
            existing_def = governed_tags.get(tag_key)
            key_only_reqs, valued_reqs = self._split_by_collision(tag_key, reqs, table_tags_cache)
            new_values = {_synthetic_value_for(r): r for r in valued_reqs}

            if new_values:
                if existing_def is None:
                    self._uc.create_governed_tag(
                        tag_key, values=list(new_values.keys()),
                        description=SYNTHETIC_TAG_DESCRIPTION_TEMPLATE.format(function_fqn=reqs[0].function_fqn),
                        dry_run=dry_run,
                    )
                else:
                    union_values = sorted(set(existing_def.values) | set(new_values.keys()))
                    self._uc.alter_governed_tag_set_values(tag_key, union_values, dry_run=dry_run)
            elif existing_def is None:
                # No column needs disambiguation - a plain key-only governed
                # tag (no allowed values) is sufficient; every column will
                # be matched via has_tag(key) instead of has_tag_value().
                self._uc.create_governed_tag(
                    tag_key, values=[],
                    description=SYNTHETIC_TAG_DESCRIPTION_TEMPLATE.format(function_fqn=reqs[0].function_fqn),
                    dry_run=dry_run,
                )

            for value, req in new_values.items():
                self._uc.set_column_tags(req.table, req.column, {tag_key: value}, dry_run=dry_run)
                resolved[(req.table, req.column, req.role)] = MatchColumn(
                    tag_key=tag_key, tag_value=value, alias=_alias_for(req.column), source_column=req.column,
                )
            for req in key_only_reqs:
                self._uc.set_column_tags(req.table, req.column, {tag_key: None}, dry_run=dry_run)
                resolved[(req.table, req.column, req.role)] = MatchColumn(
                    tag_key=tag_key, tag_value=None, alias=_alias_for(req.column), source_column=req.column,
                )

    @staticmethod
    def _split_by_collision(tag_key: str, reqs: list, table_tags_cache: dict) -> tuple:
        """A column only needs its own unique tag VALUE (and hence an
        allowed-values entry) when `has_tag(tag_key)` alone would be
        ambiguous within that column's table - i.e. some OTHER column in
        the same table already carries (or, in this very batch, will also
        carry) this exact key (confirmed live: two same-keyed columns +
        `has_tag(key)` compiles fine at CREATE POLICY time but fails every
        query with UC_ABAC_AMBIGUOUS_COLUMN_MATCH). Otherwise a plain
        key-only tag is preferred - it needs no allowed-value entry at all,
        avoiding "Allowed values" clutter for the overwhelmingly common
        one-column-per-table case. Returns (key_only_reqs, valued_reqs)."""
        reqs_by_table: dict = {}
        for r in reqs:
            reqs_by_table.setdefault(r.table, []).append(r)

        key_only, valued = [], []
        for table, table_reqs in reqs_by_table.items():
            pre_existing_same_key = any(t.tag_key == tag_key for t in table_tags_cache.get(table, []))
            if len(table_reqs) > 1 or pre_existing_same_key:
                valued.extend(table_reqs)
            else:
                key_only.extend(table_reqs)
        return key_only, valued
