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
    "policy_scope",
    "policy_to_principals",
    "policy_except_principals",
    "prefer_existing_tags",
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
    # "TABLE" ("table level application") | "CATALOG" ("catalog level
    # application") - see config/models.py PolicyScope / DESIGN.md §7.3.
    "policy_scope": "TABLE",
    "policy_to_principals": '["account users"]',
    "policy_except_principals": "[]",
    "prefer_existing_tags": "true",
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
