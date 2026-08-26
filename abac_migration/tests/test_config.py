from __future__ import annotations

import pytest

from ..config.config_loader import load_from_dict
from ..config.models import ConfigError, Mode, RunConfig, ScopeType


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
