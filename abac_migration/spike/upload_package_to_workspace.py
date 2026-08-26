"""Uploads the abac_migration package's source files into the Databricks
workspace as plain workspace files (not notebooks), so a job's notebook
task can `sys.path.append(...)` and `import abac_migration` for real,
running the exact same code that is under test locally.
"""
from __future__ import annotations

import base64
import configparser
import os

import requests

PROFILE = "uc_source"
WORKSPACE_BASE = "/Workspace/Users/vivek.ravichandiran@databricks.com/abac_migration_pkg"
LOCAL_ROOT = "/Users/vivek.ravichandiran/ABACMigration/abac_migration"

EXCLUDE_DIR_NAMES = {"__pycache__", "spike", "tests", ".pytest_cache"}


def _load(profile):
    cfg = configparser.ConfigParser()
    cfg.read(os.path.expanduser("~/.databrickscfg"))
    section = cfg[profile]
    return section["host"].rstrip("/"), section["token"]


def iter_py_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_NAMES]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def main():
    host, token = _load(PROFILE)
    headers = {"Authorization": f"Bearer {token}"}

    files = list(iter_py_files(LOCAL_ROOT))
    print(f"Uploading {len(files)} files to {WORKSPACE_BASE} ...")

    for local_path in files:
        rel = os.path.relpath(local_path, LOCAL_ROOT)
        dest = f"{WORKSPACE_BASE}/abac_migration/{rel}".replace(os.sep, "/")
        dest_dir = dest.rsplit("/", 1)[0]

        r = requests.post(f"{host}/api/2.0/workspace/mkdirs", headers=headers, json={"path": dest_dir})
        if r.status_code >= 400:
            print(f"  mkdirs FAILED for {dest_dir}: {r.text}")
            continue

        with open(local_path, "rb") as f:
            content = f.read()
        b64 = base64.b64encode(content).decode("ascii")

        r = requests.post(f"{host}/api/2.0/workspace/import", headers=headers, json={
            "path": dest,
            "format": "AUTO",
            "language": "PYTHON",
            "content": b64,
            "overwrite": True,
        })
        status = "OK" if r.status_code < 400 else f"FAILED: {r.text}"
        print(f"  [{status}] {dest}")

    # sanity check: confirm it landed as a plain file, not converted to a notebook
    sample = f"{WORKSPACE_BASE}/abac_migration/config/models.py"
    r = requests.get(f"{host}/api/2.0/workspace/get-status", headers=headers, params={"path": sample})
    print("\nSample object status:", r.json())


if __name__ == "__main__":
    main()
