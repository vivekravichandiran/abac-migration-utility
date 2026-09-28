from __future__ import annotations

import pytest

from ..config.config_loader import load_from_dict
from ..config.models import ConfigError, Mode, PolicyScope, RunConfig, ScopeType


def test_from_dict_parses_json_encoded_widget_strings():
    config = load_from_dict({
        "mode": "MIGRATE",
        "scope_type": "SELECTED_CATALOGS",
        "catalogs": '["ril_raw", "ril_curated"]',
        "dry_run": "false",
        "max_parallelism": "8",
        "audit_catalog": "audit_cat",
        "audit_schema": "audit_sch",
    })
    assert config.mode == Mode.MIGRATE
    assert config.catalogs == ["ril_raw", "ril_curated"]
    assert config.dry_run is False
    assert config.max_parallelism == 8


def test_missing_audit_catalog_raises():
    with pytest.raises(ConfigError):
        RunConfig(audit_catalog="", audit_schema="sch")


def test_selected_catalogs_requires_nonempty_catalogs():
    with pytest.raises(ConfigError):
        RunConfig(scope_type=ScopeType.SELECTED_CATALOGS, catalogs=[], audit_catalog="c", audit_schema="s")


def test_run_id_defaults_to_a_uuid_and_is_stable_on_the_instance():
    config = RunConfig(scope_type=ScopeType.ALL_CATALOGS, audit_catalog="c", audit_schema="s")
    assert config.run_id
    assert config.run_id == config.run_id


def test_apply_abac_and_finalize_modes_parse_from_widget_strings():
    config = load_from_dict({
        "mode": "APPLY_ABAC", "scope_type": "SELECTED_CATALOGS", "catalogs": '["ril_raw"]',
        "audit_catalog": "audit_cat", "audit_schema": "audit_sch",
    })
    assert config.mode == Mode.APPLY_ABAC

    config2 = load_from_dict({
        "mode": "FINALIZE", "scope_type": "SELECTED_CATALOGS", "catalogs": '["ril_raw"]',
        "audit_catalog": "audit_cat", "audit_schema": "audit_sch",
    })
    assert config2.mode == Mode.FINALIZE


def test_llm_pii_tagging_defaults_off_and_can_be_enabled_via_widgets():
    default_config = RunConfig(scope_type=ScopeType.ALL_CATALOGS, audit_catalog="c", audit_schema="s")
    assert default_config.enable_llm_pii_tagging is False
    assert default_config.pii_llm_endpoint

    enabled = load_from_dict({
        "mode": "INVENTORY", "scope_type": "ALL_CATALOGS", "audit_catalog": "audit_cat", "audit_schema": "audit_sch",
        "enable_llm_pii_tagging": "true", "pii_llm_endpoint": "some-other-endpoint",
    })
    assert enabled.enable_llm_pii_tagging is True
    assert enabled.pii_llm_endpoint == "some-other-endpoint"


def test_policy_scope_defaults_to_table():
    config = RunConfig(scope_type=ScopeType.ALL_CATALOGS, audit_catalog="c", audit_schema="s")
    assert config.policy_scope == PolicyScope.TABLE


def test_policy_scope_parses_catalog_from_widget_string():
    config = load_from_dict({
        "mode": "APPLY_ABAC", "scope_type": "ALL_CATALOGS", "audit_catalog": "audit_cat",
        "audit_schema": "audit_sch", "policy_scope": "CATALOG",
    })
    assert config.policy_scope == PolicyScope.CATALOG


def test_policy_scope_rejects_unknown_value():
    with pytest.raises(ValueError):
        load_from_dict({
            "mode": "INVENTORY", "scope_type": "ALL_CATALOGS", "audit_catalog": "audit_cat",
            "audit_schema": "audit_sch", "policy_scope": "SCHEMA_BASED_TYPO",
        })


def test_tag_team_prefix_defaults_to_empty():
    config = RunConfig(scope_type=ScopeType.ALL_CATALOGS, audit_catalog="c", audit_schema="s")
    assert config.tag_team_prefix == ""


def test_tag_team_prefix_parses_from_widget_string():
    config = load_from_dict({
        "mode": "APPLY_ABAC", "scope_type": "ALL_CATALOGS", "audit_catalog": "audit_cat",
        "audit_schema": "audit_sch", "tag_team_prefix": "mobility",
    })
    assert config.tag_team_prefix == "mobility"


# ---------------------------------------------------------------------------
# tag_grantee_principals / tag_grantee_role (§7.4 point 6): grants a
# configured account role to a list of principals on every governed tag
# this run creates/reuses. Empty principals list by default -> feature off,
# no behavior/latency change at all (see tag_provisioner.py).
# ---------------------------------------------------------------------------

def test_tag_grantee_principals_defaults_to_empty_list():
    config = RunConfig(scope_type=ScopeType.ALL_CATALOGS, audit_catalog="c", audit_schema="s")
    assert config.tag_grantee_principals == []


def test_tag_grantee_role_defaults_to_assign():
    config = RunConfig(scope_type=ScopeType.ALL_CATALOGS, audit_catalog="c", audit_schema="s")
    assert config.tag_grantee_role == "ASSIGN"


def test_tag_grantee_principals_parses_json_list_from_widget_string():
    config = load_from_dict({
        "mode": "APPLY_ABAC", "scope_type": "ALL_CATALOGS", "audit_catalog": "audit_cat",
        "audit_schema": "audit_sch",
        "tag_grantee_principals": '["b2dbcc98-7d9f-467d-a7b1-e8a026f94b73", "groups/data-platform"]',
    })
    assert config.tag_grantee_principals == ["b2dbcc98-7d9f-467d-a7b1-e8a026f94b73", "groups/data-platform"]


def test_tag_grantee_role_accepts_manage_and_normalizes_case():
    config = RunConfig(
        scope_type=ScopeType.ALL_CATALOGS, audit_catalog="c", audit_schema="s", tag_grantee_role="manage",
    )
    assert config.tag_grantee_role == "MANAGE"  # normalized to upper-case


def test_tag_grantee_role_rejects_unknown_value():
    with pytest.raises(ConfigError):
        RunConfig(
            scope_type=ScopeType.ALL_CATALOGS, audit_catalog="c", audit_schema="s",
            tag_grantee_role="OWNER",  # not one of ASSIGN/MANAGE
        )


def test_tag_grants_table_fqn_defaults_alongside_other_audit_tables():
    config = RunConfig(scope_type=ScopeType.ALL_CATALOGS, audit_catalog="c", audit_schema="s")
    assert config.tag_grants_table_fqn == f"{config.audit_full_schema}.tag_grants"
