from __future__ import annotations

from ..config.models import Mode, RunConfig, ScopeType
from ..migration.migration_engine import run
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
