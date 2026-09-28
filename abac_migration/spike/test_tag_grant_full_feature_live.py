"""End-to-end live spike for the SPN tag-grant feature (§7.4 point 6) -
NOT just the raw REST mechanics already probed directly against the API
(see access_control_client.py's module docstring for that prior spike).
This exercises the actual PRODUCTION code path: `TagProvisioner.prepare()`
-> `DatabricksUnityCatalogGateway.grant_tag_principals()` ->
`AccessControlProxyClient`, using the real `ResilientDatabricksSQL`-shaped
executor exactly as a real notebook run would construct it (host+token
opportunistically picked up by the gateway's `__init__`, no special
wiring).

Workspace: adb-7405616318078204 (`ril_catalog_test_pat` OAuth M2M profile -
run `python -m abac_migration.spike._oauth_m2m` first if this 401s, the
token is short-lived ~1h).
Catalog: ril_full_access_test (pre-existing fixture this SP has USE CATALOG
on, `hr` schema) - a throwaway tag key is minted there and dropped again at
the end; nothing else in that catalog is touched.
Real SPN: b2dbcc98-7d9f-467d-a7b1-e8a026f94b73 ("sp-fevm-shared-infra") -
the same principal granted directly against the raw API in the design-spike
session, confirmed to exist in this account's SCIM directory.
"""
from __future__ import annotations

from abac_migration.migration.tag_provisioner import TagProvisioner, TagRequest, tag_key_for_function
from abac_migration.uc_gateway.gateway import DatabricksUnityCatalogGateway, UCGatewayError
from abac_migration.uc_gateway.models import TableRef
from abac_migration.uc_gateway.sql_statement_client import ResilientDatabricksSQL

PROFILE = "ril_catalog_test_pat"
WAREHOUSE_ID = "5fe1692f119e2528"
CATALOG = "ril_full_access_test"
SCHEMA = "hr"
FUNCTION_FQN = f"{CATALOG}.{SCHEMA}.zz_spike_full_feature_grant_fn"
GRANTEE_SPN = "b2dbcc98-7d9f-467d-a7b1-e8a026f94b73"
EXPECTED_ROLE = "roles/tagPolicy.assigner"


def main() -> None:
    client = ResilientDatabricksSQL(profile=PROFILE, warehouse_id=WAREHOUSE_ID)
    gateway = DatabricksUnityCatalogGateway(client)

    assert gateway._ac_client is not None, (
        "Gateway did not opportunistically build an Access Control Proxy client from "
        "the executor's host/token - the whole point of this feature working with zero "
        "extra wiring in a real notebook run."
    )
    print(f"[1/6] gateway._ac_client constructed OK, host={gateway._ac_client.host}")

    tag_key = tag_key_for_function(FUNCTION_FQN, "row_filter")
    # Cleanup any leftover tag from a prior interrupted run of this spike.
    # NOTE: describe_governed_tag() is deliberately NOT used here - live
    # discovery (this run) shows `DESCRIBE GOVERNED TAG` on this workspace
    # for a nonexistent tag returns error_code="BAD_REQUEST" with "NOT_FOUND"
    # only inside the message text, not the code - describe_governed_tag()'s
    # existing not-found handling (checks exc.error_code) doesn't catch that
    # shape. Not part of this feature's critical path (describe_governed_tag
    # has zero production callers - grep confirms), so worked around here
    # rather than touched, to keep this spike scoped to the new feature.
    try:
        gateway.drop_governed_tag(tag_key, dry_run=False)
        print(f"[cleanup] dropped leftover governed tag {tag_key!r} from a prior run")
    except UCGatewayError:
        pass  # didn't exist yet - expected on a clean run

    table = TableRef(CATALOG, SCHEMA, "zz_spike_full_feature_grant_tbl")
    client.run(f"DROP TABLE IF EXISTS {table.quoted_full_name}")
    client.run(f"CREATE TABLE {table.quoted_full_name} (business_unit STRING, val STRING)")
    print(f"[setup] created throwaway table {table.full_name}")
    request = TagRequest(table=table, column="business_unit", role="row_filter", function_fqn=FUNCTION_FQN)

    # --- Run 1: mints a brand-new governed tag AND grants it in the same prepare() call ---
    provisioner = TagProvisioner(gateway, tag_grantee_principals=[GRANTEE_SPN], tag_grantee_role="ASSIGN")
    resolved = provisioner.prepare([request], dry_run=False)

    assert (table, "business_unit", "row_filter") in resolved
    match_column = resolved[(table, "business_unit", "row_filter")]
    assert match_column.tag_key == tag_key
    print(f"[2/6] minted governed tag {tag_key!r} via prepare(), MatchColumn={match_column}")

    assert len(provisioner.last_tag_grants) == 1, provisioner.last_tag_grants
    grant_1 = provisioner.last_tag_grants[0]
    print(f"[3/6] run 1 grant result: {grant_1}")
    assert grant_1.tag_key == tag_key
    assert grant_1.role == EXPECTED_ROLE
    assert grant_1.status in ("GRANTED", "ALREADY_GRANTED"), grant_1  # ALREADY_GRANTED only if this SPN was somehow already granted
    assert grant_1.error_code is None, grant_1

    # --- Independent verification: re-read the real rule set directly, bypassing prepare() ---
    ref = gateway._ac_client.get_tag_policy_ref(tag_key)
    rule_set = gateway._ac_client.get_rule_set(ref)
    grant_rules = rule_set.get("grant_rules", [])
    matching = [
        r for r in grant_rules
        if r.get("role") == EXPECTED_ROLE and f"servicePrincipals/{GRANTEE_SPN}" in r.get("principals", [])
    ]
    assert matching, f"Expected SPN grant not found in live rule_set: {grant_rules}"
    print(f"[4/6] confirmed live via direct GET: {matching}")

    # --- Run 2: same tag REUSED (not re-minted) - must still re-grant (self-healing) ---
    provisioner_2 = TagProvisioner(gateway, tag_grantee_principals=[GRANTEE_SPN], tag_grantee_role="ASSIGN")
    resolved_2 = provisioner_2.prepare([request], dry_run=False)

    assert resolved_2[(table, "business_unit", "row_filter")].tag_key == tag_key  # reused, not re-minted
    assert len(provisioner_2.last_tag_grants) == 1
    grant_2 = provisioner_2.last_tag_grants[0]
    print(f"[5/6] run 2 (reused tag) grant result: {grant_2}")
    assert grant_2.status == "ALREADY_GRANTED", grant_2  # idempotent - server already has this exact grant

    # --- Cleanup ---
    gateway.drop_governed_tag(tag_key, dry_run=False)
    client.run(f"DROP TABLE IF EXISTS {table.quoted_full_name}")
    print(f"[6/6] cleaned up: dropped governed tag {tag_key!r} and throwaway table {table.full_name}")

    print("\nALL ASSERTIONS PASSED - full feature confirmed live end-to-end.")


if __name__ == "__main__":
    main()
