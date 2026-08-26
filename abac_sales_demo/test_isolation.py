"""Runs the verify_isolation notebook impersonating each test user (via Jobs
API run_as) against a given catalog, to prove RLS + masking actually isolate
data between the two business-unit test identities.
"""
from __future__ import annotations

import configparser
import json
import os
import sys
import time

import requests

from .config import PROFILE, SCHEMA_NAME, TEST_USERS

NOTEBOOK_LOCAL_PATH = os.path.join(os.path.dirname(__file__), "notebooks", "verify_isolation.py")
NOTEBOOK_WORKSPACE_PATH = "/Shared/abac_sales_demo/verify_isolation"


def _load_profile(profile: str):
    cfg = configparser.ConfigParser()
    cfg.read(os.path.expanduser("~/.databrickscfg"))
    section = cfg[profile]
    return section["host"].rstrip("/"), section["token"]


class Client:
    def __init__(self, profile: str):
        self.host, token = _load_profile(profile)
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    def import_notebook(self):
        parent = NOTEBOOK_WORKSPACE_PATH.rsplit("/", 1)[0]
        r = self.session.post(self._url("/api/2.0/workspace/mkdirs"), json={"path": parent})
        r.raise_for_status()
        with open(NOTEBOOK_LOCAL_PATH, "rb") as f:
            content = f.read()
        import base64
        body = {
            "path": NOTEBOOK_WORKSPACE_PATH,
            "format": "SOURCE",
            "language": "PYTHON",
            "content": base64.b64encode(content).decode(),
            "overwrite": True,
        }
        r = self.session.post(self._url("/api/2.0/workspace/import"), json=body)
        r.raise_for_status()
        print(f"  imported notebook to {NOTEBOOK_WORKSPACE_PATH}")

    def submit_run(self, catalog: str, run_as_user: str, use_serverless: bool = True):
        task = {
            "task_key": "verify_isolation",
            "notebook_task": {
                "notebook_path": NOTEBOOK_WORKSPACE_PATH,
                "base_parameters": {"catalog": catalog, "schema": SCHEMA_NAME},
            },
        }
        if not use_serverless:
            task["new_cluster"] = {
                "spark_version": "15.4.x-scala2.12",
                "num_workers": 0,
                "node_type_id": "Standard_DS3_v2",
                "spark_conf": {"spark.master": "local[*]"},
                "azure_attributes": {"availability": "ON_DEMAND_AZURE"},
            }
        body = {
            "run_name": f"abac-verify-{run_as_user}-{catalog}",
            "tasks": [task],
            "run_as": {"user_name": run_as_user},
        }
        r = self.session.post(self._url("/api/2.1/jobs/runs/submit"), json=body)
        if r.status_code >= 400:
            print(f"  submit_run FAILED: {r.status_code} {r.text}")
            r.raise_for_status()
        return r.json()["run_id"]

    def wait_for_run(self, run_id: int, timeout_s: int = 900):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = self.session.get(self._url("/api/2.1/jobs/runs/get"), params={"run_id": run_id})
            r.raise_for_status()
            data = r.json()
            state = data.get("state", {})
            life_cycle = state.get("life_cycle_state")
            if life_cycle in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR"):
                return data
            print(f"    run {run_id} state={life_cycle} ...")
            time.sleep(15)
        raise TimeoutError(f"run {run_id} did not finish in {timeout_s}s")

    def get_run_output(self, run_id: int):
        # For multi-task runs, need the task run_id, not the job run_id.
        r = self.session.get(self._url("/api/2.1/jobs/runs/get"), params={"run_id": run_id})
        r.raise_for_status()
        data = r.json()
        tasks = data.get("tasks", [])
        target_run_id = tasks[0]["run_id"] if tasks else run_id
        r = self.session.get(self._url("/api/2.1/jobs/runs/get-output"), params={"run_id": target_run_id})
        r.raise_for_status()
        return r.json()


def run_for_catalog(catalog: str, use_serverless: bool = True) -> dict:
    client = Client(PROFILE)
    client.import_notebook()

    results = {}
    for bu, info in TEST_USERS.items():
        user_name = info["user_name"]
        print(f"Submitting run as {user_name} ({bu} group) against {catalog}...")
        run_id = client.submit_run(catalog, user_name, use_serverless=use_serverless)
        run_data = client.wait_for_run(run_id)
        state = run_data.get("state", {})
        if state.get("result_state") != "SUCCESS":
            output = client.get_run_output(run_id)
            print(f"  RUN FAILED for {user_name}: {state} \n error: {output.get('error', '')}\n{output.get('error_trace','')[:2000]}")
            results[bu] = {"error": state, "raw_output": output}
            continue
        output = client.get_run_output(run_id)
        notebook_output = output.get("notebook_output", {})
        result_str = notebook_output.get("result")
        parsed = json.loads(result_str) if result_str else {}
        results[bu] = parsed
        print(f"  OK: {user_name} -> visible business units (customers): {parsed.get('customers_visible_business_units')}")
    return results


def main():
    catalog = sys.argv[1] if len(sys.argv) > 1 else "ril_raw"
    use_serverless = "--no-serverless" not in sys.argv
    results = run_for_catalog(catalog, use_serverless=use_serverless)
    print("\n=== RESULTS ===")
    print(json.dumps(results, indent=2, default=str))
    out_path = os.path.join(os.path.dirname(__file__), f"isolation_result_{catalog}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
