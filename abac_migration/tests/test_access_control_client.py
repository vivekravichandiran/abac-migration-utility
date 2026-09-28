"""Tests for AccessControlProxyClient (§7.4 point 6) - the REST client for
granting governed-tag access, confirmed live against the real Account
Access Control Proxy API (see access_control_client.py's module docstring).
No real network calls: `client.session` is swapped for `_FakeSession`,
which returns canned `_FakeResponse` objects for the exact 2 endpoints this
client calls.
"""
from __future__ import annotations

import pytest

from ..uc_gateway.access_control_client import (
    AccessControlClientError,
    AccessControlProxyClient,
    TagPolicyRef,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    """Records every call and returns pre-programmed responses keyed by
    (method, path) - callers register responses via `.on(method, path, resp)`
    before exercising the client."""

    def __init__(self):
        self.calls = []
        self._responses = {}

    def on(self, method: str, path: str, response: _FakeResponse) -> None:
        self._responses[(method, path)] = response

    def _respond(self, method, url, **kwargs):
        path = url.split("://", 1)[-1].split("/", 1)[1]
        path = "/" + path
        self.calls.append((method, path, kwargs))
        key = (method, path)
        if key not in self._responses:
            raise AssertionError(f"_FakeSession has no programmed response for {key}; calls so far: {self.calls}")
        return self._responses[key]

    def get(self, url, params=None):
        return self._respond("GET", url, params=params)

    def put(self, url, json=None):
        return self._respond("PUT", url, json=json)


TAG_POLICY_PATH = "/api/2.1/tag-policies/abac_rls_cat_sch_fn"
RULE_SET_PATH = "/api/2.0/preview/accounts/access-control/rule-sets"


def _client_with_fake_session() -> tuple[AccessControlProxyClient, _FakeSession]:
    client = AccessControlProxyClient("https://example.databricks.net", "fake-token")
    fake = _FakeSession()
    client.session = fake
    return client, fake


def test_get_tag_policy_ref_parses_id_and_account_id():
    client, fake = _client_with_fake_session()
    fake.on("GET", TAG_POLICY_PATH, _FakeResponse(200, {
        "tag_key": "abac_rls_cat_sch_fn", "id": "5c66e0f8-38f7-4bfa-afad-2dde60c68c7d",
        "account_id": "f9ba5888-fdb9-4e53-9e5f-724c437d1779",
    }))

    ref = client.get_tag_policy_ref("abac_rls_cat_sch_fn")

    assert ref == TagPolicyRef(
        tag_key="abac_rls_cat_sch_fn", tag_policy_id="5c66e0f8-38f7-4bfa-afad-2dde60c68c7d",
        account_id="f9ba5888-fdb9-4e53-9e5f-724c437d1779",
    )


def test_get_tag_policy_ref_raises_on_error_response():
    client, fake = _client_with_fake_session()
    fake.on("GET", TAG_POLICY_PATH, _FakeResponse(404, {"error_code": "NOT_FOUND", "message": "no such tag policy"}))

    with pytest.raises(AccessControlClientError) as exc_info:
        client.get_tag_policy_ref("abac_rls_cat_sch_fn")

    assert exc_info.value.error_code == "NOT_FOUND"
    assert "no such tag policy" in exc_info.value.message


def test_get_rule_set_sends_empty_etag_for_first_read():
    client, fake = _client_with_fake_session()
    ref = TagPolicyRef(tag_key="k", tag_policy_id="tp-id", account_id="acct-id")
    fake.on("GET", RULE_SET_PATH, _FakeResponse(200, {"name": "n", "etag": "e1", "grant_rules": []}))

    result = client.get_rule_set(ref)

    assert result == {"name": "n", "etag": "e1", "grant_rules": []}
    method, path, kwargs = fake.calls[0]
    assert kwargs["params"] == {"name": "accounts/acct-id/tagPolicies/tp-id/ruleSets/default", "etag": ""}


def test_update_rule_set_sends_full_grant_rules_list_not_a_delta():
    client, fake = _client_with_fake_session()
    ref = TagPolicyRef(tag_key="k", tag_policy_id="tp-id", account_id="acct-id")
    fake.on("PUT", RULE_SET_PATH, _FakeResponse(200, {"name": "n", "etag": "e2", "grant_rules": []}))

    existing_and_new = [
        {"principals": ["servicePrincipals/creator"], "role": "roles/tagPolicy.manager"},
        {"principals": ["servicePrincipals/new-spn"], "role": "roles/tagPolicy.assigner"},
    ]
    client.update_rule_set(ref, "e1", existing_and_new)

    method, path, kwargs = fake.calls[0]
    assert kwargs["json"]["rule_set"]["etag"] == "e1"
    assert kwargs["json"]["rule_set"]["grant_rules"] == existing_and_new
    assert kwargs["json"]["rule_set"]["name"] == "accounts/acct-id/tagPolicies/tp-id/ruleSets/default"


def test_grant_tag_role_skips_put_when_already_granted():
    client, fake = _client_with_fake_session()
    fake.on("GET", TAG_POLICY_PATH, _FakeResponse(200, {"id": "tp-id", "account_id": "acct-id"}))
    fake.on("GET", RULE_SET_PATH, _FakeResponse(200, {
        "etag": "e1",
        "grant_rules": [{"principals": ["servicePrincipals/spn-1"], "role": "roles/tagPolicy.assigner"}],
    }))

    status = client.grant_tag_role("abac_rls_cat_sch_fn", ["servicePrincipals/spn-1"], "roles/tagPolicy.assigner")

    assert status == "ALREADY_GRANTED"
    assert not any(m == "PUT" for m, _, _ in fake.calls)  # no PUT round trip at all


def test_grant_tag_role_merges_new_rule_with_existing_rules_on_put():
    client, fake = _client_with_fake_session()
    fake.on("GET", TAG_POLICY_PATH, _FakeResponse(200, {"id": "tp-id", "account_id": "acct-id"}))
    fake.on("GET", RULE_SET_PATH, _FakeResponse(200, {
        "etag": "e1",
        "grant_rules": [{"principals": ["servicePrincipals/creator"], "role": "roles/tagPolicy.manager"}],
    }))
    fake.on("PUT", RULE_SET_PATH, _FakeResponse(200, {"etag": "e2", "grant_rules": []}))

    status = client.grant_tag_role("abac_rls_cat_sch_fn", ["servicePrincipals/spn-1"], "roles/tagPolicy.assigner")

    assert status == "GRANTED"
    put_call = next(c for c in fake.calls if c[0] == "PUT")
    grant_rules = put_call[2]["json"]["rule_set"]["grant_rules"]
    assert {"principals": ["servicePrincipals/creator"], "role": "roles/tagPolicy.manager"} in grant_rules
    assert {"principals": ["servicePrincipals/spn-1"], "role": "roles/tagPolicy.assigner"} in grant_rules
    assert len(grant_rules) == 2


def test_grant_tag_role_propagates_error_on_nonexistent_principal():
    client, fake = _client_with_fake_session()
    fake.on("GET", TAG_POLICY_PATH, _FakeResponse(200, {"id": "tp-id", "account_id": "acct-id"}))
    fake.on("GET", RULE_SET_PATH, _FakeResponse(200, {"etag": "e1", "grant_rules": []}))
    fake.on("PUT", RULE_SET_PATH, _FakeResponse(400, {
        "error_code": "BAD_REQUEST", "message": "ServicePrincipal deadbeef-0000-0000-0000-000000000000 not found",
    }))

    with pytest.raises(AccessControlClientError) as exc_info:
        client.grant_tag_role(
            "abac_rls_cat_sch_fn", ["servicePrincipals/deadbeef-0000-0000-0000-000000000000"],
            "roles/tagPolicy.assigner",
        )

    assert exc_info.value.error_code == "BAD_REQUEST"
    assert "not found" in exc_info.value.message
