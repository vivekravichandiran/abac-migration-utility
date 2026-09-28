"""Account Access Control Proxy REST client - the one part of this tool
that is NOT plain SQL over the warehouse (§5, §7.4 point 6).

**Why this exists:** granting a principal permission to attach/use a
governed tag has no SQL grammar at all - confirmed against Databricks docs
AND live (2026-09-28, `ril_catalog_test_pat` / workspace
`adb-7405616318078204`) against this project's own target workspace.
`CREATE`/`MANAGE`/`ASSIGN` permissions on a governed tag ("tag policy") are
managed exclusively through the UI or this REST API family - an
account-level, rule-set-based access-control mechanism also used for
account groups/service principals/budget policies.

**Confirmed live mechanics** (see the same date/workspace above - this is
not speculative):

1. `GET /api/2.1/tag-policies/{tag_key}` (note: NOT
   `/api/2.1/unity-catalog/tag-policies/...` - that path 404s on this
   workspace, `/api/2.1/tag-policies/...` is the one that actually works)
   returns `{"tag_key", "id", "account_id", ...}` in ONE call - `id` is a
   UUID distinct from `tag_key` and IS the identifier the ACL resource path
   needs; `account_id` means no separate account-ID-discovery step is ever
   needed either.
2. `GET {workspace-host}/api/2.0/preview/accounts/access-control/rule-sets`
   with query params `name=accounts/<account_id>/tagPolicies/<id>/ruleSets/default`
   and `etag=` (empty string works for a first read - confirmed live) reads
   the current rule set - this is the workspace-proxied form (no
   `access-management`-scoped account-level token needed; the plain M2M
   "all-apis" scoped token this tool already uses everywhere else worked
   with ZERO extra OAuth scope). Response: `{"name", "etag", "grant_rules": [...]}`.
3. `PUT` the same URL with `{"name": ..., "rule_set": {"name": ..., "etag":
   ..., "grant_rules": [...]}}` is a **full replace** of `grant_rules` -
   confirmed live that blindly appending one new rule while preserving
   every existing one round-trips correctly (the creator's own pre-existing
   `roles/tagPolicy.manager` grant, from creating the tag, survived
   untouched after adding a second principal's `roles/tagPolicy.assigner`
   grant). Confirmed the server also de-duplicates identical rules
   server-side (adding the exact same rule twice left `grant_rules` at the
   same length AND didn't even bump the `etag`) - `grant_tag_role()` below
   still does its own pre-check to skip the round trip entirely when
   nothing would change, for efficiency/cleaner audit trail, not because
   the server needs it for correctness.
4. Confirmed error mode: granting to a principal that doesn't exist in the
   account returns `400 BAD_REQUEST "ServicePrincipal <id> not found"` (NOT
   a 403) - a clean, catchable, non-retryable error.

Every HTTP call goes through the same `retry.with_retries()` used by
`sql_statement_client.py` - same throttling-aware backoff, no
duplicated logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import requests

from .retry import RetryPolicy, RetryStats, with_retries


class AccessControlClientError(Exception):
    """Raised for any non-2xx response from this API family. Callers (the
    gateway) are expected to catch this and turn it into a non-fatal,
    audit-recorded TagGrantResult - a grant failure must never abort a
    migration run (same graceful-degradation posture as every other
    optional/best-effort step in this tool)."""

    def __init__(self, error_code: Optional[str], message: str):
        super().__init__(f"[{error_code}] {message}")
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class TagPolicyRef:
    tag_key: str
    tag_policy_id: str
    account_id: str


class AccessControlProxyClient:
    """Thin, dependency-free REST client for exactly the 3 calls this tool
    needs (§7.4 point 6) - not a general-purpose SDK wrapper. Takes a plain
    host + token, same as `sql_statement_client.ResilientDatabricksSQL`, so
    it can be constructed from the exact same notebook-provided credentials
    with no new secret/auth plumbing anywhere else in this project."""

    def __init__(self, host: str, token: str, retry_policy: Optional[RetryPolicy] = None):
        self.host = host.rstrip("/")
        self.retry_policy = retry_policy or RetryPolicy()
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    def _get(self, path: str, params: dict, label: str) -> requests.Response:
        stats = RetryStats()
        return with_retries(
            lambda: self.session.get(self._url(path), params=params), self.retry_policy, stats, label=label,
        )

    def _put(self, path: str, json_body: dict, label: str) -> requests.Response:
        stats = RetryStats()
        return with_retries(
            lambda: self.session.put(self._url(path), json=json_body), self.retry_policy, stats, label=label,
        )

    @staticmethod
    def _raise_for_error(resp: requests.Response, label: str) -> dict:
        if resp.status_code < 400:
            return resp.json()
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        raise AccessControlClientError(
            payload.get("error_code"), payload.get("message") or f"{label} failed: HTTP {resp.status_code}",
        )

    def get_tag_policy_ref(self, tag_key: str) -> TagPolicyRef:
        """`GET /api/2.1/tag-policies/{tag_key}` - see module docstring
        point 1. Raises AccessControlClientError (e.g. `NOT_FOUND`) if the
        tag_key isn't a governed tag at all."""
        resp = self._get(f"/api/2.1/tag-policies/{quote(tag_key, safe='')}", {}, "get_tag_policy")
        payload = self._raise_for_error(resp, "get_tag_policy")
        return TagPolicyRef(tag_key=tag_key, tag_policy_id=payload["id"], account_id=payload["account_id"])

    def _rule_set_name(self, ref: TagPolicyRef) -> str:
        return f"accounts/{ref.account_id}/tagPolicies/{ref.tag_policy_id}/ruleSets/default"

    def get_rule_set(self, ref: TagPolicyRef) -> dict:
        """See module docstring point 2. `etag=""` is confirmed live to work
        for a first read (no prior etag needed)."""
        resp = self._get(
            "/api/2.0/preview/accounts/access-control/rule-sets",
            {"name": self._rule_set_name(ref), "etag": ""},
            "get_rule_set",
        )
        return self._raise_for_error(resp, "get_rule_set")

    def update_rule_set(self, ref: TagPolicyRef, etag: str, grant_rules: list) -> dict:
        """See module docstring point 3 - `grant_rules` must be the FULL
        desired list (existing + new), never just the delta; callers must
        read-modify-write via get_rule_set() first."""
        name = self._rule_set_name(ref)
        body = {"name": name, "rule_set": {"name": name, "etag": etag, "grant_rules": grant_rules}}
        resp = self._put("/api/2.0/preview/accounts/access-control/rule-sets", body, "update_rule_set")
        return self._raise_for_error(resp, "update_rule_set")

    def grant_tag_role(self, tag_key: str, principals: list, role: str) -> str:
        """High-level read-modify-write: ensures every principal in
        `principals` (already fully-qualified, e.g.
        "servicePrincipals/<id>") has `role` (e.g.
        "roles/tagPolicy.assigner") on the governed tag `tag_key`, merging
        with (never replacing) whatever grant rules already exist. Returns
        "GRANTED" if a PUT was actually needed, "ALREADY_GRANTED" if every
        requested (principal, role) pair was already present (skips the PUT
        entirely - see module docstring point 3). Raises
        AccessControlClientError on any failure (tag not governed,
        principal not found, etc.) - callers convert this to a non-fatal,
        audit-recorded outcome."""
        ref = self.get_tag_policy_ref(tag_key)
        current = self.get_rule_set(ref)
        existing_rules = current.get("grant_rules", [])

        already_present = any(
            r.get("role") == role and set(r.get("principals", [])) >= set(principals) for r in existing_rules
        )
        if already_present:
            return "ALREADY_GRANTED"

        new_rule = {"principals": list(principals), "role": role}
        self.update_rule_set(ref, current.get("etag", ""), existing_rules + [new_rule])
        return "GRANTED"
