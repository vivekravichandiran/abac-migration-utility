"""Resilient SQL Statement Execution API client used by the API-verification
spike (and, later, as a reference implementation for the real uc_gateway
when it needs to run outside a notebook's native Spark session, e.g. for
local testing/spikes like this one).

Every HTTP call goes through uc_gateway.retry.with_retries().
"""
from __future__ import annotations

import configparser
import os
import time
from dataclasses import dataclass
from typing import Any

import requests

from .retry import RetryPolicy, RetryStats, with_retries


def _load_profile(profile: str) -> tuple[str, str]:
    cfg = configparser.ConfigParser()
    cfg.read(os.path.expanduser("~/.databrickscfg"))
    section = cfg[profile]
    return section["host"].rstrip("/"), section["token"]


@dataclass
class StatementResult:
    status: str
    columns: list
    rows: list
    error: str | None = None
    error_code: str | None = None
    retry_stats: RetryStats | None = None


class ResilientDatabricksSQL:
    def __init__(self, profile: str, warehouse_id: str, retry_policy: RetryPolicy | None = None):
        host, token = _load_profile(profile)
        self._init_common(host, token, warehouse_id, retry_policy)

    @classmethod
    def from_host_and_token(
        cls, host: str, token: str, warehouse_id: str, retry_policy: RetryPolicy | None = None
    ) -> "ResilientDatabricksSQL":
        """Alternate constructor for contexts with no `~/.databrickscfg`
        profile on disk - e.g. a job/notebook task, where the host + a
        short-lived token are obtained from the notebook's own execution
        context (see notebooks/abac_migration_run.py) rather than a local
        CLI profile."""
        self = cls.__new__(cls)
        self._init_common(host, token, warehouse_id, retry_policy)
        return self

    def _init_common(self, host: str, token: str, warehouse_id: str, retry_policy: RetryPolicy | None) -> None:
        self.host = host.rstrip("/")
        self.token = token
        self.warehouse_id = warehouse_id
        self.retry_policy = retry_policy or RetryPolicy()
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        self.total_calls = 0
        self.total_retried_calls = 0

    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    def _post(self, path: str, json_body: dict, label: str) -> requests.Response:
        stats = RetryStats()
        resp = with_retries(lambda: self.session.post(self._url(path), json=json_body),
                             self.retry_policy, stats, label=label)
        self.total_calls += 1
        if stats.attempts > 1:
            self.total_retried_calls += 1
        return resp

    def _get(self, path: str, params: dict | None, label: str) -> requests.Response:
        stats = RetryStats()
        resp = with_retries(lambda: self.session.get(self._url(path), params=params or {}),
                             self.retry_policy, stats, label=label)
        self.total_calls += 1
        if stats.attempts > 1:
            self.total_retried_calls += 1
        return resp

    def ensure_warehouse_running(self, timeout_s: int = 180) -> None:
        wid = self.warehouse_id
        r = self._get(f"/api/2.0/sql/warehouses/{wid}", None, "get_warehouse")
        r.raise_for_status()
        if r.json().get("state") == "RUNNING":
            return
        self._post(f"/api/2.0/sql/warehouses/{wid}/start", {}, "start_warehouse")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = self._get(f"/api/2.0/sql/warehouses/{wid}", None, "get_warehouse")
            r.raise_for_status()
            if r.json().get("state") == "RUNNING":
                return
            time.sleep(5)
        raise TimeoutError(f"Warehouse {wid} did not start in {timeout_s}s")

    def run(self, statement: str, timeout_s: int = 60) -> StatementResult:
        body: dict[str, Any] = {
            "warehouse_id": self.warehouse_id,
            "statement": statement,
            "wait_timeout": "30s",
        }
        r = self._post("/api/2.0/sql/statements", body, "submit_statement")
        if r.status_code >= 400:
            try:
                payload = r.json()
            except Exception:
                payload = {}
            return StatementResult(status="FAILED", columns=[], rows=[],
                                    error=payload.get("message", r.text),
                                    error_code=payload.get("error_code"))
        payload = r.json()
        statement_id = payload["statement_id"]
        deadline = time.time() + timeout_s
        while payload["status"]["state"] in ("PENDING", "RUNNING"):
            if time.time() > deadline:
                raise TimeoutError(f"Statement {statement_id} timed out")
            time.sleep(1.2)
            r = self._get(f"/api/2.0/sql/statements/{statement_id}", None, "poll_statement")
            r.raise_for_status()
            payload = r.json()

        state = payload["status"]["state"]
        if state != "SUCCEEDED":
            err = payload["status"].get("error", {})
            return StatementResult(status=state, columns=[], rows=[],
                                    error=err.get("message"), error_code=err.get("error_code"))

        manifest = payload.get("manifest", {})
        columns = [c["name"] for c in manifest.get("schema", {}).get("columns", [])]
        result = payload.get("result", {}) or {}
        rows = result.get("data_array", []) or []
        return StatementResult(status=state, columns=columns, rows=rows)
