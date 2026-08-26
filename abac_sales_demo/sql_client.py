"""Thin wrapper around the Databricks SQL Statement Execution API.

Reads host/token from a profile in ~/.databrickscfg so no secrets are
hard-coded in source. Used to run DDL/DML for the ABAC sales demo.
"""
from __future__ import annotations

import configparser
import os
import time
from dataclasses import dataclass
from typing import Any

import requests


def _load_profile(profile: str) -> tuple[str, str]:
    cfg = configparser.ConfigParser()
    cfg.read(os.path.expanduser("~/.databrickscfg"))
    section = cfg[profile]
    return section["host"].rstrip("/"), section["token"]


@dataclass
class StatementResult:
    status: str
    columns: list[str]
    rows: list[list[Any]]
    error: str | None = None


class DatabricksSQL:
    def __init__(self, profile: str, warehouse_id: str):
        self.host, self.token = _load_profile(profile)
        self.warehouse_id = warehouse_id
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    def ensure_warehouse_running(self, timeout_s: int = 180) -> None:
        wid = self.warehouse_id
        r = self.session.get(self._url(f"/api/2.0/sql/warehouses/{wid}"))
        r.raise_for_status()
        state = r.json().get("state")
        if state == "RUNNING":
            return
        self.session.post(self._url(f"/api/2.0/sql/warehouses/{wid}/start"))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = self.session.get(self._url(f"/api/2.0/sql/warehouses/{wid}"))
            r.raise_for_status()
            state = r.json().get("state")
            if state == "RUNNING":
                return
            time.sleep(5)
        raise TimeoutError(f"Warehouse {wid} did not start in {timeout_s}s (state={state})")

    def run(self, statement: str, catalog: str | None = None, schema: str | None = None,
             on_behalf_of_user: str | None = None, timeout_s: int = 120) -> StatementResult:
        body: dict[str, Any] = {
            "warehouse_id": self.warehouse_id,
            "statement": statement,
            "wait_timeout": "30s",
        }
        if catalog:
            body["catalog"] = catalog
        if schema:
            body["schema"] = schema
        r = self.session.post(self._url("/api/2.0/sql/statements"), json=body)
        r.raise_for_status()
        payload = r.json()
        statement_id = payload["statement_id"]
        deadline = time.time() + timeout_s
        while payload["status"]["state"] in ("PENDING", "RUNNING"):
            if time.time() > deadline:
                raise TimeoutError(f"Statement {statement_id} timed out")
            time.sleep(1.5)
            r = self.session.get(self._url(f"/api/2.0/sql/statements/{statement_id}"))
            r.raise_for_status()
            payload = r.json()

        state = payload["status"]["state"]
        if state != "SUCCEEDED":
            err = payload["status"].get("error", {}).get("message", "unknown error")
            return StatementResult(status=state, columns=[], rows=[], error=err)

        columns: list[str] = []
        rows: list[list[Any]] = []
        manifest = payload.get("manifest", {})
        schema_cols = manifest.get("schema", {}).get("columns", [])
        columns = [c["name"] for c in schema_cols]
        result = payload.get("result", {})
        if result:
            rows = result.get("data_array", []) or []
        return StatementResult(status=state, columns=columns, rows=rows)

    def exec_or_raise(self, statement: str, **kwargs) -> StatementResult:
        res = self.run(statement, **kwargs)
        if res.status != "SUCCEEDED":
            raise RuntimeError(f"SQL failed: {res.error}\n--- statement ---\n{statement}")
        return res
