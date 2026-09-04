"""One-off helper for the `catalog_scope` target (OAuth M2M service
principal auth): mints a short-lived OAuth M2M (client_credentials) access
token and writes it into ~/.databrickscfg as a *separate* token-based
profile (`ril_catalog_test_pat`), because the two existing helper classes
this repo's spike scripts already use -
`uc_gateway.sql_statement_client.ResilientDatabricksSQL` and
`abac_sales_demo.identity.Scim` - only know how to read a plain `token =`
value from a databrickscfg profile section, not OAuth client_id/secret.

Kept deliberately separate from the `ril_catalog_test` profile (used
as-is by `databricks bundle` CLI commands, which DO support OAuth M2M
natively via client_id/client_secret in databrickscfg) so refreshing this
token never touches/breaks that one.

Credentials are read from environment variables - NEVER hardcode a
client_id/client_secret in this file, since this repo is public:
  ABAC_OAUTH_HOST          e.g. https://adb-<id>.<n>.azuredatabricks.net
  ABAC_OAUTH_CLIENT_ID
  ABAC_OAUTH_CLIENT_SECRET

Token expires in ~1h - call refresh() again if a long-running script
starts failing with 401s.
"""
from __future__ import annotations

import configparser
import os

import requests

PAT_PROFILE = "ril_catalog_test_pat"
CFG_PATH = os.path.expanduser("~/.databrickscfg")


def refresh() -> str:
    host = os.environ["ABAC_OAUTH_HOST"].rstrip("/")
    client_id = os.environ["ABAC_OAUTH_CLIENT_ID"]
    client_secret = os.environ["ABAC_OAUTH_CLIENT_SECRET"]

    r = requests.post(
        f"{host}/oidc/v1/token",
        data={"grant_type": "client_credentials", "scope": "all-apis"},
        auth=(client_id, client_secret),
        timeout=30,
    )
    r.raise_for_status()
    token = r.json()["access_token"]

    cfg = configparser.ConfigParser()
    cfg.read(CFG_PATH)
    if not cfg.has_section(PAT_PROFILE):
        cfg.add_section(PAT_PROFILE)
    cfg.set(PAT_PROFILE, "host", host)
    cfg.set(PAT_PROFILE, "token", token)
    with open(CFG_PATH, "w") as f:
        cfg.write(f)
    print(f"Refreshed OAuth token -> ~/.databrickscfg [{PAT_PROFILE}]")
    return token


if __name__ == "__main__":
    refresh()
