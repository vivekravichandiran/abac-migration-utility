"""§9/§14 resilience requirement: ROLLBACK must never abort the whole job
over one bad/failing audit row - every row is attempted independently, each
outcome (success OR failure) is persisted to migration_audit, and the loop
always moves on to the next row. Exercises migration_engine._run_rollback
directly (not through the full run() orchestrator) against a minimal
audit_repo double, since FakeUnityCatalogGateway.run_sql() doesn't simulate
real SELECT results (see _StubAuditRepo docstring below) - append() (a real
INSERT, already exercised the same way by every other audit test in this
suite) still goes through the fake gateway unmodified.
"""
from __future__ import annotations

import json

from ..config.models import Mode, RunConfig, ScopeType
from ..migration.migration_engine import _run_rollback
from ..migration.plugins.base_plugin import StepStatus
from ..migration.policy_strategy import TableBasedPolicyStrategy
from ..uc_gateway.models import PolicyDefinition, TableRef
from .fake_gateway import FakeUnityCatalogGateway

_MIGRATION_AUDIT_COLUMNS = [
    "run_id", "attempt_id", "catalog", "schema", "table", "object_type", "masked_column",
    "source_security_type", "source_function", "source_definition", "target_policy_name",
    "target_policy_type", "target_definition", "status", "error_code", "error_message",
    "validation_status", "rollback_metadata", "migration_phase", "started_at", "completed_at", "dry_run",
]

RF_FN = "cat.sch.rf_fn"


def _audit_row(catalog, schema, table, rollback_metadata_raw, run_id="run-1", object_type="ROW_FILTER"):
    """One migration_audit row shaped exactly like a real
    `audit_repo.rows_for_run()` SELECT * would return - a plain tuple in
    _MIGRATION_AUDIT_COLUMNS order."""
    values = {
        "run_id": run_id, "attempt_id": "attempt-1", "catalog": catalog, "schema": schema, "table": table,
        "object_type": object_type, "masked_column": None, "source_security_type": None,
        "source_function": RF_FN, "source_definition": None, "target_policy_name": "abac_migrated_row_filter",
        "target_policy_type": object_type, "target_definition": None, "status": "SUCCESS",
        "error_code": None, "error_message": None, "validation_status": "PASSED",
        "rollback_metadata": rollback_metadata_raw, "migration_phase": "FINALIZED",
        "started_at": None, "completed_at": None, "dry_run": False,
    }
    return tuple(values[c] for c in _MIGRATION_AUDIT_COLUMNS)


class _StubAuditRepo:
    """Minimal audit_repo double exposing only the two methods
    _run_rollback/_persist_rollback_result actually call. rows_for_run()
    returns a fixed, caller-supplied set of canned rows (standing in for
    what a real `SELECT * ... WHERE run_id = ...` would have returned);
    append() just records every persisted MigrationAuditRecord for
    assertions - no fake SQL parsing needed for either."""

    def __init__(self, rows):
        self._rows = rows
        self.appended = []

    def rows_for_run(self, run_id):
        return self._rows

    def append(self, record, dry_run=False):
        self.appended.append(record)


def _config(**overrides) -> RunConfig:
    defaults = dict(
        mode=Mode.ROLLBACK, scope_type=ScopeType.SELECTED_CATALOGS, catalogs=["cat"],
        dry_run=False, audit_catalog="cat", audit_schema="audit", run_id="run-1",
    )
    defaults.update(overrides)
    return RunConfig(**defaults)


def _seed_table_with_applied_policy(fake: FakeUnityCatalogGateway, table: TableRef) -> None:
    fake.register_table(table)
    fake.functions.add(RF_FN)
    fake.add_existing_policy(table, PolicyDefinition(
        name="abac_migrated_row_filter", policy_type="ROW_FILTER", on_securable_type="TABLE",
        on_securable=table.full_name, to_principals=["account users"], match_columns=["mc_region"],
        function_fqn=RF_FN, using_columns=["region"],
    ))


_ROLLBACK_METADATA = {
    "original_row_filter": {"function": RF_FN, "using_columns": ["region"]},
    "abac_policies_created_by_this_run": [
        {"policy_name": "abac_migrated_row_filter", "policy_type": "ROW_FILTER"},
    ],
}


def test_rollback_all_rows_succeed_and_are_all_persisted():
    fake = FakeUnityCatalogGateway()
    t1, t2 = TableRef("cat", "sch", "t1"), TableRef("cat", "sch", "t2")
    _seed_table_with_applied_policy(fake, t1)
    _seed_table_with_applied_policy(fake, t2)
    raw = json.dumps(_ROLLBACK_METADATA)
    audit_repo = _StubAuditRepo([_audit_row("cat", "sch", "t1", raw), _audit_row("cat", "sch", "t2", raw)])

    results = _run_rollback(_config(), fake, audit_repo, TableBasedPolicyStrategy())

    assert len(results) == 2
    assert all(r.status == StepStatus.ROLLED_BACK for r in results)
    assert len(audit_repo.appended) == 2
    assert all(r.status == "ROLLED_BACK" and r.migration_phase == "ROLLED_BACK" for r in audit_repo.appended)
    assert fake.row_filters[t1.full_name].function_fqn == RF_FN
    assert fake.row_filters[t2.full_name].function_fqn == RF_FN


def test_rollback_one_row_failing_does_not_abort_the_others_and_both_are_persisted():
    fake = FakeUnityCatalogGateway()
    t1, t2 = TableRef("cat", "sch", "t1"), TableRef("cat", "sch", "t2")
    _seed_table_with_applied_policy(fake, t1)
    _seed_table_with_applied_policy(fake, t2)
    raw = json.dumps(_ROLLBACK_METADATA)
    audit_repo = _StubAuditRepo([_audit_row("cat", "sch", "t1", raw), _audit_row("cat", "sch", "t2", raw)])

    # t1's restorative `SET ROW FILTER` call raises once (simulated
    # transient failure) - must not prevent t2's rollback from running or
    # succeeding, and must not raise out of _run_rollback itself.
    fake.set_fault("set_row_filter", RuntimeError("simulated transient failure"))

    results = _run_rollback(_config(), fake, audit_repo, TableBasedPolicyStrategy())

    assert len(results) == 2
    by_table = {r.table_name: r for r in results}
    assert by_table[t1.full_name].status == StepStatus.FAILED
    assert by_table[t2.full_name].status == StepStatus.ROLLED_BACK
    # t2 actually got its legacy row filter restored despite t1's failure.
    assert fake.row_filters[t2.full_name].function_fqn == RF_FN

    # BOTH outcomes recorded - previously nothing was ever persisted for
    # ROLLBACK, successful or not.
    assert len(audit_repo.appended) == 2
    persisted = {(r.catalog, r.schema, r.table): r for r in audit_repo.appended}
    assert persisted[("cat", "sch", "t1")].status == "FAILED"
    assert persisted[("cat", "sch", "t1")].migration_phase == "ROLLBACK_FAILED"
    assert persisted[("cat", "sch", "t1")].error_code == "ROLLBACK_FAILED"
    assert persisted[("cat", "sch", "t2")].status == "ROLLED_BACK"
    assert persisted[("cat", "sch", "t2")].migration_phase == "ROLLED_BACK"


def test_rollback_malformed_metadata_json_is_recorded_not_raised_and_others_still_run():
    fake = FakeUnityCatalogGateway()
    t1, t2 = TableRef("cat", "sch", "t1"), TableRef("cat", "sch", "t2")
    _seed_table_with_applied_policy(fake, t1)
    _seed_table_with_applied_policy(fake, t2)
    # t1's rollback_metadata is corrupt (not valid JSON) - json.loads() will
    # raise inside _run_rollback's per-row try/except, not inside
    # rollback_table() at all (that function never even gets called for
    # this row).
    audit_repo = _StubAuditRepo([
        _audit_row("cat", "sch", "t1", "{not-valid-json"),
        _audit_row("cat", "sch", "t2", json.dumps(_ROLLBACK_METADATA)),
    ])

    # Must not raise - this is the exact bug being fixed: previously an
    # uncaught exception here would propagate out of _run_rollback (and
    # therefore run()) and fail the entire job.
    results = _run_rollback(_config(), fake, audit_repo, TableBasedPolicyStrategy())

    assert len(results) == 2
    by_table = {r.table_name: r for r in results}
    assert by_table[t1.full_name].status == StepStatus.FAILED
    assert "Unexpected error" in by_table[t1.full_name].error_message
    assert by_table[t2.full_name].status == StepStatus.ROLLED_BACK
    assert fake.row_filters[t2.full_name].function_fqn == RF_FN

    assert len(audit_repo.appended) == 2
    persisted = {(r.catalog, r.schema, r.table): r for r in audit_repo.appended}
    assert persisted[("cat", "sch", "t1")].status == "FAILED"
    assert persisted[("cat", "sch", "t1")].migration_phase == "ROLLBACK_FAILED"
    assert persisted[("cat", "sch", "t2")].status == "ROLLED_BACK"


def test_rollback_dry_run_reports_would_rollback_and_mutates_nothing():
    fake = FakeUnityCatalogGateway()
    t1 = TableRef("cat", "sch", "t1")
    _seed_table_with_applied_policy(fake, t1)
    raw = json.dumps(_ROLLBACK_METADATA)
    audit_repo = _StubAuditRepo([_audit_row("cat", "sch", "t1", raw)])

    results = _run_rollback(_config(dry_run=True), fake, audit_repo, TableBasedPolicyStrategy())

    assert results[0].status == StepStatus.WOULD_ROLLBACK
    assert fake.row_filters[t1.full_name] is None  # unchanged - still removed, not restored
    assert audit_repo.appended[0].status == "WOULD_ROLLBACK"
    assert audit_repo.appended[0].migration_phase == "DRY_RUN"


def test_rollback_row_with_no_metadata_is_skipped_without_error():
    fake = FakeUnityCatalogGateway()
    t1 = TableRef("cat", "sch", "t1")
    _seed_table_with_applied_policy(fake, t1)
    audit_repo = _StubAuditRepo([_audit_row("cat", "sch", "t1", None)])

    results = _run_rollback(_config(), fake, audit_repo, TableBasedPolicyStrategy())

    assert results == []
    assert audit_repo.appended == []
