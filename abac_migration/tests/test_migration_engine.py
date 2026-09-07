from __future__ import annotations

from ..config.models import Mode, PolicyScope, RunConfig, ScopeType
from ..migration.migration_engine import build_policy_strategy, run
from ..migration.policy_strategy import CatalogBasedPolicyStrategy, TableBasedPolicyStrategy
from ..migration.tag_provisioner import tag_key_for_function
from ..uc_gateway.models import TableRef
from .fake_gateway import FakeUnityCatalogGateway

RF_FN = "cat.sch.rf_fn"


def _config(**overrides) -> RunConfig:
    defaults = dict(
        mode=Mode.INVENTORY_AND_MIGRATE, scope_type=ScopeType.SELECTED_CATALOGS, catalogs=["cat"],
        dry_run=False, audit_catalog="cat", audit_schema="audit",
    )
    defaults.update(overrides)
    return RunConfig(**defaults)


def _fake_with_n_tables(n: int) -> FakeUnityCatalogGateway:
    fake = FakeUnityCatalogGateway()
    for i in range(n):
        table = TableRef("cat", "sch", f"t{i}")
        fake.set_row_filter_state(table, RF_FN, ["business_unit"])
    return fake


def test_parallelism_1_vs_4_yield_same_results():
    fake1 = _fake_with_n_tables(6)
    fake4 = _fake_with_n_tables(6)

    summary1 = run(_config(max_parallelism=1), fake1)
    summary4 = run(_config(max_parallelism=4), fake4)

    assert summary1.tables_succeeded == summary4.tables_succeeded == 6
    assert summary1.tables_failed == summary4.tables_failed == 0


def test_continue_on_error_false_stops_after_first_failure():
    from ..uc_gateway.models import PolicyApplyResult

    fake = _fake_with_n_tables(3)
    original_create = fake.create_or_replace_policy

    def flaky(spec, dry_run):
        if "t1" in spec.on_securable:
            return PolicyApplyResult(success=False, policy_name=spec.policy_name, error_code="POLICY_CREATE_FAILED")
        return original_create(spec, dry_run)

    fake.create_or_replace_policy = flaky

    summary = run(_config(continue_on_error=False, max_parallelism=1), fake)

    assert summary.tables_failed >= 1


def test_inventory_only_mode_does_not_mutate():
    fake = _fake_with_n_tables(3)
    summary = run(_config(mode=Mode.INVENTORY), fake)

    assert summary.tables_eligible == 3
    assert fake.mutation_calls == []
    assert all(fake.row_filters[f"cat.sch.t{i}"] is not None for i in range(3))


# ---------------------------------------------------------------------------
# Isolated-phase modes end-to-end through the engine: APPLY_ABAC creates the
# ABAC policy + governed tags but leaves legacy alone; FINALIZE (run as a
# separate mode/run_id, as a real "3 jobs" deployment would) removes it.
# ---------------------------------------------------------------------------

from ..migration.plugins.base_plugin import StepStatus  # noqa: E402


def test_apply_abac_mode_creates_policies_and_leaves_legacy_in_place():
    fake = _fake_with_n_tables(3)

    summary = run(_config(mode=Mode.APPLY_ABAC), fake)

    assert summary.tables_abac_applied == 3
    assert summary.tables_succeeded == 0
    for i in range(3):
        assert fake.row_filters[f"cat.sch.t{i}"] is not None  # legacy untouched
        assert "abac_migrated_row_filter" in fake.policies[f"cat.sch.t{i}"]


def test_finalize_mode_after_apply_abac_removes_legacy_and_reaches_success():
    fake = _fake_with_n_tables(3)
    run(_config(mode=Mode.APPLY_ABAC), fake)

    summary = run(_config(mode=Mode.FINALIZE, run_id="finalize-run"), fake)

    assert summary.tables_succeeded == 3
    for i in range(3):
        assert fake.row_filters[f"cat.sch.t{i}"] is None
        assert "abac_migrated_row_filter" in fake.policies[f"cat.sch.t{i}"]


def test_finalize_mode_without_prior_apply_abac_is_not_eligible_and_mutates_nothing():
    fake = _fake_with_n_tables(2)

    summary = run(_config(mode=Mode.FINALIZE), fake)

    assert summary.tables_succeeded == 0
    assert fake.mutation_calls == []
    for i in range(2):
        assert fake.row_filters[f"cat.sch.t{i}"] is not None


def test_apply_abac_persists_audit_rows_with_abac_applied_migration_phase():
    fake = _fake_with_n_tables(1)
    config = _config(mode=Mode.APPLY_ABAC)

    run(config, fake)

    rows = fake.generic_sql_log
    insert_stmts = [s for s in rows if s.startswith("INSERT INTO") and "migration_audit" in s]
    assert insert_stmts, "expected an audit row insert for the APPLY_ABAC run"
    assert any("ABAC_APPLIED" in s for s in insert_stmts)


def test_finalize_persists_audit_rows_with_finalized_migration_phase():
    fake = _fake_with_n_tables(1)
    run(_config(mode=Mode.APPLY_ABAC), fake)

    run(_config(mode=Mode.FINALIZE, run_id="finalize-run"), fake)

    insert_stmts = [s for s in fake.generic_sql_log if s.startswith("INSERT INTO") and "migration_audit" in s]
    assert any("FINALIZED" in s for s in insert_stmts)


# ---------------------------------------------------------------------------
# policy_scope config (§7.3): "table level application" (default) vs
# "catalog level application", selected once per run via YAML/job param,
# never branched on anywhere except build_policy_strategy().
# ---------------------------------------------------------------------------

def test_build_policy_strategy_defaults_to_table_based():
    strategy = build_policy_strategy(_config())
    assert isinstance(strategy, TableBasedPolicyStrategy)


def test_build_policy_strategy_selects_catalog_based():
    strategy = build_policy_strategy(_config(policy_scope=PolicyScope.CATALOG))
    assert isinstance(strategy, CatalogBasedPolicyStrategy)


def test_build_policy_strategy_propagates_principals_regardless_of_scope():
    strategy = build_policy_strategy(_config(
        policy_scope=PolicyScope.CATALOG,
        policy_to_principals=["some_group"], policy_except_principals=["etl_sp"],
    ))
    assert strategy.to_principals == ["some_group"]
    assert strategy.except_principals == ["etl_sp"]


def test_catalog_scope_apply_abac_mode_shares_one_policy_across_tables():
    fake = _fake_with_n_tables(3)

    summary = run(_config(mode=Mode.APPLY_ABAC, policy_scope=PolicyScope.CATALOG), fake)

    assert summary.tables_abac_applied == 3
    for i in range(3):
        assert fake.row_filters[f"cat.sch.t{i}"] is not None  # legacy untouched
    tag_key = tag_key_for_function(RF_FN, "row_filter")
    # One shared CATALOG-scoped policy for all 3 tables, not one each.
    assert len(fake.policies.get("cat", {})) == 1
    assert tag_key in fake.policies["cat"]


def test_catalog_scope_finalize_mode_removes_legacy_for_all_tables():
    fake = _fake_with_n_tables(3)
    run(_config(mode=Mode.APPLY_ABAC, policy_scope=PolicyScope.CATALOG), fake)

    summary = run(_config(mode=Mode.FINALIZE, run_id="finalize-run", policy_scope=PolicyScope.CATALOG), fake)

    assert summary.tables_succeeded == 3
    for i in range(3):
        assert fake.row_filters[f"cat.sch.t{i}"] is None
    tag_key = tag_key_for_function(RF_FN, "row_filter")
    assert tag_key in fake.policies["cat"]  # shared policy still lives on


# ---------------------------------------------------------------------------
# tag_team_prefix (§7.4): namespaces every governed tag key this run mints
# and, under policy_scope=CATALOG, every policy name too - end to end
# through the real engine (build_policy_strategy -> TagProvisioner ->
# plugins -> table_converter), not just the isolated unit tests in
# test_tag_provisioner.py / test_policy_strategy.py.
# ---------------------------------------------------------------------------

def test_apply_abac_with_team_prefix_mints_namespaced_governed_tag_table_scope():
    fake = _fake_with_n_tables(1)

    summary = run(_config(mode=Mode.APPLY_ABAC, tag_team_prefix="mobility"), fake)

    assert summary.tables_abac_applied == 1
    expected_tag_key = tag_key_for_function(RF_FN, "row_filter", "mobility")
    assert expected_tag_key in fake.governed_tags
    # TABLE scope's policy name is ALSO namespaced now (not just the tag key).
    assert "abac_mobility_migrated_row_filter" in fake.policies["cat.sch.t0"]


def test_apply_abac_with_team_prefix_catalog_scope_shares_one_namespaced_policy():
    fake = _fake_with_n_tables(3)

    summary = run(_config(mode=Mode.APPLY_ABAC, policy_scope=PolicyScope.CATALOG, tag_team_prefix="mobility"), fake)

    assert summary.tables_abac_applied == 3
    expected_key = tag_key_for_function(RF_FN, "row_filter", "mobility")
    assert len(fake.policies.get("cat", {})) == 1
    assert expected_key in fake.policies["cat"]
    # The no-prefix key/policy name must never have been created.
    assert tag_key_for_function(RF_FN, "row_filter") not in fake.policies["cat"]


def test_finalize_after_apply_abac_with_team_prefix_still_finds_and_removes_legacy():
    fake = _fake_with_n_tables(2)
    run(_config(mode=Mode.APPLY_ABAC, tag_team_prefix="mobility"), fake)

    summary = run(_config(mode=Mode.FINALIZE, run_id="finalize-run", tag_team_prefix="mobility"), fake)

    assert summary.tables_succeeded == 2
    for i in range(2):
        assert fake.row_filters[f"cat.sch.t{i}"] is None
    assert "abac_mobility_migrated_row_filter" in fake.policies["cat.sch.t0"]


def test_finalize_with_mismatched_team_prefix_never_finds_the_policy():
    # Explicit documentation-backed contract (job description, DESIGN.md):
    # tag_team_prefix must be identical across Apply-ABAC -> Finalize. A
    # mismatch must be a safe no-op (NOT_ELIGIBLE), never a false SUCCESS
    # and never a crash.
    fake = _fake_with_n_tables(1)
    run(_config(mode=Mode.APPLY_ABAC, tag_team_prefix="mobility"), fake)

    summary = run(_config(mode=Mode.FINALIZE, run_id="finalize-run", tag_team_prefix="payments"), fake)

    assert summary.tables_succeeded == 0
    assert fake.row_filters["cat.sch.t0"] is not None  # legacy untouched - never removed
