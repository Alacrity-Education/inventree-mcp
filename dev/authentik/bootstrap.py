#!/usr/bin/env python3
"""Configure the disposable dev Authentik for the InvenTree MCP dev stack (standard library only).

Adapted from KINA's scripts/e2e/authentik/bootstrap.py (Api, wait_ready, wait_api, get_or_create kept as is).
Uses the admin API token from AUTHENTIK_BOOTSTRAP_TOKEN and creates (get-or-create, safe to re-run):
  - groups inventree-users, inventree-mcp, inventree-guests;
  - users e2e-member (users + mcp), e2e-interactive (users), e2e-guest (guests), e2e-nonmember (no group), all with
    the fixed dev password;
  - scope mapping "groups" (the user's group names);
  - OAuth2/OIDC provider "InvenTree dev" (confidential, authorization_code + refresh_token, strict redirect URI,
    RS256, scopes openid email profile offline_access groups, implicit consent, access token 10 minutes, refresh
    token 30 days) and application slug "inventree" with bindings for the three groups (policy engine "any").

Prints {issuer, client_id, client_secret} as JSON on stdout and writes it to dev/authentik/.bootstrap.json
(git-ignored). The client secret is generated once and reused on re-runs.
  AUTHENTIK_URL=http://127.0.0.1:19000 AUTHENTIK_PUBLIC_URL=http://authentik.localtest.me:19000 \
  AUTHENTIK_BOOTSTRAP_TOKEN=... python3 bootstrap.py
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / ".bootstrap.json"
APP_SLUG = "inventree"
CLIENT_ID = "inventree-dev"
PROVIDER_NAME = "InvenTree dev"
REDIRECT_URI = "http://localhost:8080/accounts/authentik/login/callback/"
DEV_PASSWORD = "dev-password-123"
G_USERS, G_MCP, G_GUESTS = "inventree-users", "inventree-mcp", "inventree-guests"
USERS = {  # username -> groups
    "e2e-member": [G_USERS, G_MCP],
    "e2e-interactive": [G_USERS],
    "e2e-guest": [G_GUESTS],
    "e2e-nonmember": [],
}
GROUPS_EXPRESSION = 'return {"groups": [group.name for group in request.user.groups.all()]}'
TIMEOUT = 15


class Api:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body=None, ok=(200, 201, 204)):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + "/api/v3" + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/json",
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        if status not in ok:
            raise RuntimeError(f"{method} {path} -> {status}: {raw[:500]!r}")
        return json.loads(raw) if raw else None

    def find(self, path: str, **query):
        results = self.call("GET", path + "?" + urllib.parse.urlencode({**query, "page_size": 200}))["results"]
        return results[0] if results else None

    def get_or_create(self, path: str, lookup: dict, body: dict):
        found = self.find(path, **lookup)
        return found if found else self.call("POST", path, body)


def wait_ready(base: str, seconds: int = 300) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/-/health/ready/", timeout=5) as response:
                if response.status in (200, 204):
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    raise RuntimeError(f"Authentik at {base} not ready after {seconds}s")


def wait_api(api: Api, seconds: int = 240) -> None:
    """The bootstrap token and the default flows are created by the worker's startup tasks; wait for them."""
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            if api.find("/flows/instances/", slug="default-provider-authorization-implicit-consent") \
                    and api.find("/flows/instances/", slug="default-authentication-flow"):
                return
        except RuntimeError as e:
            last = e
        time.sleep(3)
    raise RuntimeError(f"Authentik API/default flows not available: {last}")


def bootstrap(base: str, public: str, token: str, client_secret: str) -> dict:
    api = Api(base, token)
    wait_api(api)
    authz_flow = api.find("/flows/instances/", slug="default-provider-authorization-implicit-consent")["pk"]
    invalidation = api.find("/flows/instances/", slug="default-provider-invalidation-flow")
    keypair = next(k for k in api.call("GET", "/crypto/certificatekeypairs/?page_size=200")["results"]
                   if k["name"] == "authentik Self-signed Certificate")

    # The managed scope mappings come from the worker's blueprints, which may still be applying right after startup.
    deadline = time.time() + 240
    while True:
        scope_mappings = api.call("GET", "/propertymappings/provider/scope/?page_size=200")["results"]
        managed = {m.get("managed"): m["pk"] for m in scope_mappings}
        if all(f"goauthentik.io/providers/oauth2/scope-{s}" in managed
               for s in ("openid", "email", "profile", "offline_access")):
            break
        if time.time() > deadline:
            raise RuntimeError("Authentik managed scope mappings not available after 240s")
        time.sleep(3)
    mapping_pks = [managed[f"goauthentik.io/providers/oauth2/scope-{s}"]
                   for s in ("openid", "email", "profile", "offline_access")]
    groups_mapping = api.get_or_create("/propertymappings/provider/scope/", {"name": "InvenTree dev groups"}, {
        "name": "InvenTree dev groups", "scope_name": "groups", "description": "Group names for InvenTree",
        "expression": GROUPS_EXPRESSION})
    mapping_pks.append(groups_mapping["pk"])

    group_pks = {name: api.get_or_create("/core/groups/", {"name": name}, {"name": name})["pk"]
                 for name in (G_USERS, G_MCP, G_GUESTS)}

    users = {}
    for username, groups in USERS.items():
        pks = [group_pks[g] for g in groups]
        user = api.get_or_create("/core/users/", {"username": username}, {
            "username": username, "name": username, "email": f"{username}@example.test", "is_active": True,
            "path": "users", "groups": pks})
        # Re-runs restore the intended membership and password.
        api.call("PATCH", f"/core/users/{user['pk']}/", {"groups": pks, "is_active": True})
        api.call("POST", f"/core/users/{user['pk']}/set_password/", {"password": DEV_PASSWORD})
        users[username] = user["pk"]

    provider_body = {
        "name": PROVIDER_NAME, "authorization_flow": authz_flow, "client_type": "confidential",
        # Authentik 2026.8: a provider created through the API has no grant types unless they are listed.
        "grant_types": ["authorization_code", "refresh_token"],
        "client_id": CLIENT_ID, "client_secret": client_secret,
        "redirect_uris": [{"matching_mode": "strict", "url": REDIRECT_URI}],
        "signing_key": keypair["pk"], "property_mappings": mapping_pks, "sub_mode": "hashed_user_id",
        "include_claims_in_id_token": True, "access_token_validity": "minutes=10",
        "refresh_token_validity": "days=30"}
    if invalidation:
        provider_body["invalidation_flow"] = invalidation["pk"]
    provider = api.find("/providers/oauth2/", name=PROVIDER_NAME)
    if provider:
        provider = api.call("PATCH", f"/providers/oauth2/{provider['pk']}/", provider_body)
    else:
        provider = api.call("POST", "/providers/oauth2/", provider_body)

    # The application list is filtered by the caller's access unless superuser_full_list is set.
    app = api.get_or_create("/core/applications/", {"slug": APP_SLUG, "superuser_full_list": "true"}, {
        "name": "InvenTree", "slug": APP_SLUG, "provider": provider["pk"], "policy_engine_mode": "any"})
    existing = api.call("GET", f"/policies/bindings/?target={app['pk']}&page_size=200")["results"]
    bound = {b.get("group") for b in existing}
    for order, name in enumerate((G_USERS, G_MCP, G_GUESTS)):
        if group_pks[name] not in bound:
            api.call("POST", "/policies/bindings/", {"target": app["pk"], "group": group_pks[name], "order": order,
                                                     "enabled": True, "negate": False, "timeout": 30})
    return {"issuer": f"{public.rstrip('/')}/application/o/{APP_SLUG}/", "client_id": CLIENT_ID,
            "client_secret": client_secret}


if __name__ == "__main__":
    url = os.environ.get("AUTHENTIK_URL", "http://127.0.0.1:19000")
    public_url = os.environ.get("AUTHENTIK_PUBLIC_URL", "http://authentik.localtest.me:19000")
    token = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN")
    if not token:
        sys.exit("AUTHENTIK_BOOTSTRAP_TOKEN is not set (dev/up.sh exports it from dev/.env)")
    secret = None
    if OUTPUT.exists():
        secret = json.loads(OUTPUT.read_text()).get("client_secret")
    wait_ready(url)
    result = bootstrap(url, public_url, token, secret or secrets.token_urlsafe(32))
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    OUTPUT.chmod(0o600)
    json.dump(result, sys.stdout, indent=2)
    print()
