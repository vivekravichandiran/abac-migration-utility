"""Creates the two business-unit groups + two test users used to verify
RLS/masking isolation, via the workspace SCIM API.
"""
from __future__ import annotations

import configparser
import os
import sys

import requests

from .config import GROUPS, PROFILE, TEST_USERS


def _load_profile(profile: str):
    cfg = configparser.ConfigParser()
    cfg.read(os.path.expanduser("~/.databrickscfg"))
    section = cfg[profile]
    return section["host"].rstrip("/"), section["token"]


class Scim:
    def __init__(self, profile: str):
        host, token = _load_profile(profile)
        self.host = host
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/scim+json",
        })

    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    def find_group(self, display_name: str):
        # Server-side `filter` query param is unreliable on this SCIM
        # implementation (returns 0 results even for existing groups), so
        # list all and filter client-side instead.
        r = self.session.get(self._url("/api/2.0/preview/scim/v2/Groups"), params={"count": 500})
        r.raise_for_status()
        for g in r.json().get("Resources", []):
            if g.get("displayName") == display_name:
                return g
        return None

    def create_group(self, display_name: str) -> dict:
        existing = self.find_group(display_name)
        if existing:
            print(f"  group '{display_name}' already exists (id={existing['id']})")
            return existing
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": display_name,
        }
        r = self.session.post(self._url("/api/2.0/preview/scim/v2/Groups"), json=body)
        r.raise_for_status()
        group = r.json()
        print(f"  created group '{display_name}' (id={group['id']})")
        return group

    def find_user(self, user_name: str):
        r = self.session.get(self._url("/api/2.0/preview/scim/v2/Users"), params={"count": 500})
        r.raise_for_status()
        for u in r.json().get("Resources", []):
            if u.get("userName") == user_name:
                return u
        return None

    def create_user(self, user_name: str, display_name: str) -> dict:
        existing = self.find_user(user_name)
        if existing:
            print(f"  user '{user_name}' already exists (id={existing['id']})")
            return existing
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": user_name,
            "displayName": display_name,
            "emails": [{"type": "work", "value": user_name, "primary": True}],
        }
        r = self.session.post(self._url("/api/2.0/preview/scim/v2/Users"), json=body)
        if r.status_code >= 400:
            print(f"  FAILED creating user '{user_name}': {r.status_code} {r.text}")
            r.raise_for_status()
        user = r.json()
        print(f"  created user '{user_name}' (id={user['id']})")
        return user

    def add_user_to_group(self, group: dict, user: dict) -> None:
        group_id = group["id"]
        r = self.session.get(self._url(f"/api/2.0/preview/scim/v2/Groups/{group_id}"))
        r.raise_for_status()
        current = r.json()
        members = current.get("members", [])
        if any(m["value"] == user["id"] for m in members):
            print(f"  user already a member of group {current['displayName']}")
            return
        members.append({"value": user["id"], "display": user.get("userName")})
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": current["displayName"],
            "members": members,
        }
        # PATCH with add op returns a 500 on this workspace's SCIM impl; PUT
        # with the full representation (incl. existing members) works instead.
        r = self.session.put(self._url(f"/api/2.0/preview/scim/v2/Groups/{group_id}"), json=body)
        r.raise_for_status()
        print(f"  added {user.get('userName')} to group {current['displayName']}")


def main():
    scim = Scim(PROFILE)
    print("Creating groups...")
    group_objs = {}
    for bu, group_name in GROUPS.items():
        group_objs[bu] = scim.create_group(group_name)

    print("Creating test users...")
    user_objs = {}
    for bu, info in TEST_USERS.items():
        user_objs[bu] = scim.create_user(info["user_name"], info["display_name"])

    print("Assigning users to groups...")
    for bu in GROUPS:
        scim.add_user_to_group(group_objs[bu], user_objs[bu])

    print("Done.")


if __name__ == "__main__":
    main()
