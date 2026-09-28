"""The only place that knows about dbutils.widgets vs. a plain dict (§2).
Both paths converge on RunConfig.from_dict so behavior is identical whether
invoked from the notebook or from a unit test.
"""
from __future__ import annotations

from .models import DEFAULT_PII_LLM_ENDPOINT, RunConfig

WIDGET_NAMES = [
    "mode",
    "scope_type",
    "catalogs",
    "schemas",
    "tables",
    "exclude_schema_regex",
    "dry_run",
    "continue_on_error",
    "max_parallelism",
    "audit_catalog",
    "audit_schema",
    "audit_table",
    "inventory_table",
    "tag_grants_table",
    "policy_scope",
    "policy_to_principals",
    "policy_except_principals",
    "tag_team_prefix",
    "prefer_existing_tags",
    "tag_grantee_principals",
    "tag_grantee_role",
    "enable_llm_pii_tagging",
    "pii_llm_endpoint",
    "run_id",
]

WIDGET_DEFAULTS = {
    "mode": "INVENTORY",
    "scope_type": "SELECTED_CATALOGS",
    "catalogs": "[]",
    "schemas": "{}",
    "tables": "[]",
    "exclude_schema_regex": "",
    "dry_run": "true",
    "continue_on_error": "true",
    "max_parallelism": "4",
    "audit_catalog": "",
    "audit_schema": "",
    "audit_table": "migration_audit",
    "inventory_table": "inventory",
    "tag_grants_table": "tag_grants",
    # "TABLE" ("table level application") | "CATALOG" ("catalog level
    # application") - see config/models.py PolicyScope / DESIGN.md §7.3.
    "policy_scope": "TABLE",
    "policy_to_principals": '["account users"]',
    "policy_except_principals": "[]",
    # Optional namespace segment, e.g. "mobility" -> governed tag keys named
    # abac_rls_mobility_<cat>_<sch>_<fn> instead of abac_rls_<cat>_<sch>_<fn>
    # - see config/models.py RunConfig.tag_team_prefix / DESIGN.md §7.4.
    "tag_team_prefix": "",
    "prefer_existing_tags": "true",
    # SPN application ID(s) (or, less commonly, "groups/<name>" /
    # "users/<email>") to grant tag-policy access to for every governed tag
    # this run creates/reuses - e.g. '["b2dbcc98-7d9f-467d-a7b1-e8a026f94b73"]'.
    # Empty (default): feature off, no grant calls at all. See
    # config/models.py RunConfig.tag_grantee_principals / DESIGN.md §7.4
    # point 6.
    "tag_grantee_principals": "[]",
    # "ASSIGN" (attach/use only, the default) or "MANAGE" (full control of
    # the tag policy) - see config/models.py VALID_TAG_GRANTEE_ROLES.
    "tag_grantee_role": "ASSIGN",
    "enable_llm_pii_tagging": "false",
    "pii_llm_endpoint": DEFAULT_PII_LLM_ENDPOINT,
    "run_id": "",
}


def load_from_widgets(dbutils) -> RunConfig:
    """dbutils is the Databricks notebook global; typed as Any here since it
    is only available inside a notebook runtime and never imported."""
    for name in WIDGET_NAMES:
        dbutils.widgets.text(name, WIDGET_DEFAULTS[name])
    raw = {name: dbutils.widgets.get(name) for name in WIDGET_NAMES}
    return RunConfig.from_dict(raw)


def load_from_dict(raw: dict) -> RunConfig:
    """Entry point used by tests and any non-notebook caller (e.g. a CLI
    wrapper, or the API-verification spike's successor tooling)."""
    return RunConfig.from_dict(raw)
