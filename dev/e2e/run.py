#!/usr/bin/env python3
"""End-to-end suite of the InvenTree MCP plugin against the dev stack (WP5): the go-live checklist, scripted.

Drives the stack brought up by dev/up.sh (InvenTree http://localhost:8080 behind Caddy, Authentik
http://authentik.localtest.me:19000, the e2e-* test users of dev/authentik/bootstrap.py). Standard library only.
The flow steps come from the existing scripts, imported, not copied: dev/oauth_smoke.py (registration, PKCE
authorize URL, consent and code exchange, refresh), dev/membership_e2e.py (façade connect through the login bridge
and Authentik, Django state through dev/shell.sh, ageing the membership check, the Authentik outage),
dev/permissions_e2e.py (admin session on the plugin API, real tool calls) and dev/authentik/ (Browser, browse,
sso_login, Authentik groups).

Scenarios (numbers as in docs/plans/WP5-hardening-tests-rollout.md and docs/OPERATIONS.md):
   1 discovery      401 challenge, PRM, AS metadata, well-known redirects / proxy rewrites, CORS headers
   2 member         e2e-member connects (DCR, PKCE, SSO through the bridge, consent, code, token), tools/list,
                    a paginated list and a detail call with the OAuth token and with an API token; refresh with
                    a fresh check (upstream token unchanged), refresh with an aged check (upstream token rotated)
   3 removed        removed from inventree-mcp in Authentik: aged refresh invalid_grant, bearer 401, authorize
                    "Access revoked"; added back, a new SSO login lifts the block
   4 outage         authentik-server stopped: refresh within grace works, past grace invalid_grant, nothing
                    revoked; started again, the same refresh token works (stops and starts the container)
   5 non-members    e2e-interactive -> "No MCP access" page, e2e-guest logs in without roles -> refused,
                    e2e-nonmember -> refused by Authentik
   6 narrowing      "MCP Users" gets MCP view on parts only: part tools only, stock tool refused, the same
                    user's API token still reads /api/stock/
   7 reduction      clearing an MCP flag through api/rulesets/ revokes the live refresh token at once
   8 clients        untrusted CIMD client_id and unregistered redirect URI -> error page, never a redirect;
                    trusted CIMD client (simulated row): loopback redirect keeps consent, https redirect skips it
   9 logs           server, worker and proxy logs of this run hold no test user address, no token, no code
  10 claude-code    claude mcp add / list (needs authentication) / remove

Usage: python3 dev/e2e/run.py [--base-url http://localhost:8080] [--only N|NAME ...] [--skip-outage] [--list]

Every line is PASS, FAIL, SKIP or a step of an imported helper ("[label] ..."); the exit code is 1 when any
scenario failed. The runner switches on OAUTH_ENABLED and MEMBERSHIP_ENFORCED, sets MCP_REQUIRED_GROUPS="MCP Users",
and restores in finally blocks whatever it changes: plugin settings, Authentik groups of the test users, the MCP
roles of "MCP Users", the block state of e2e-member, API tokens, the OAuth clients it registered, the simulated
CIMD row, and authentik-server (started again). Tokens, codes and secrets are never printed (lengths or hash
prefixes only).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import sys
import traceback
import urllib.parse
from collections.abc import Callable
from pathlib import Path

DEV = Path(__file__).resolve().parent.parent
REPO = DEV.parent
sys.path.insert(0, str(DEV))
sys.path.insert(0, str(DEV / "authentik"))
import bootstrap
import membership_e2e as membership
import oauth_smoke as oauth
import permissions_e2e as permissions
import sso_smoke as sso

USER = membership.USER  # e2e-member
MEMBER_GROUPS = membership.MEMBER_GROUPS
REQUIRED_GROUP = membership.REQUIRED_GROUP  # "MCP Users"
MANAGED_SETTINGS = ("OAUTH_ENABLED", "MEMBERSHIP_ENFORCED", "MCP_REQUIRED_GROUPS")
RUN_SETTINGS = {
    "OAUTH_ENABLED": True,
    "MEMBERSHIP_ENFORCED": True,
    "MCP_REQUIRED_GROUPS": REQUIRED_GROUP,
}
TRUSTED_ORIGIN = "https://claude.ai"
CIMD_CLIENT_ID = "https://claude.ai/oauth/inventree-mcp-e2e-client"  # never fetched: the row is pre-created
CIMD_HTTPS_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_SERVER = "inventree-dev"
MCP_ACCEPT = "application/json, text/event-stream"


# -- results ------------------------------------------------------------------------------------------------------


class ScenarioFailed(Exception):
    """A check of the current scenario failed; the scenario stops there."""


class Skipped(Exception):
    """The whole scenario is skipped (reason in the message)."""


def check(name: str, ok: bool, detail: str = "") -> None:
    print(
        ("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""),
        flush=True,
    )
    if not ok:
        raise ScenarioFailed(name)


def skip(name: str, reason: str) -> None:
    print(f"SKIP {name}  ({reason})", flush=True)


def fingerprint(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode()).hexdigest()[:10]


# -- secrets of this run (for scenario 9) -----------------------------------------------------------------------
# The imported helpers keep authorization codes to themselves (approve_and_exchange returns only the token response,
# Authentik's codes live inside browse()). Every HTTP call of the run goes through sso_smoke.Browser.request, so a
# recording wrapper harvests codes from Location headers and response bodies (Authentik's flow executor returns its
# redirect, code included, in JSON), tokens from JSON bodies, and the client ids the run registered.

SECRETS: set[str] = set()
REGISTERED_CLIENTS: set[str] = set()
CODE_PATTERN = re.compile(r"[?&#]code=([A-Za-z0-9._~%-]{16,})")
TOKEN_KEYS = ("access_token", "refresh_token", "id_token", "token")


def remember(secret: str | None) -> None:
    if isinstance(secret, str) and len(secret) >= 16:
        SECRETS.add(secret)


_original_request = sso.Browser.request


def _recording_request(self, method, url, form=None, body=None, headers=None):
    status, response_headers, text = _original_request(
        self, method, url, form=form, body=body, headers=headers
    )
    for source in (response_headers.get("Location") or "", text):
        for match in CODE_PATTERN.finditer(source):
            remember(urllib.parse.unquote(match.group(1)))
    if text[:1] == "{":
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for key in TOKEN_KEYS:
                remember(payload.get(key))
            if (
                method == "POST"
                and status == 201
                and url.rstrip("/").endswith("/oauth/register")
            ):
                REGISTERED_CLIENTS.add(payload.get("client_id", ""))
    return status, response_headers, text


sso.Browser.request = _recording_request


# -- shared helpers ------------------------------------------------------------------------------------------------


def shell(code: str) -> dict:
    """Django code in the server container (membership_e2e.shell, with its prelude: user, plugin, models)."""
    return membership.shell(membership.PRELUDE + code)


def mcp_call(
    agent, base: str, authorization: str | None, method: str = "tools/list", params=None
):
    """(status, headers, payload) of one JSON-RPC call on /mcp/ with any Authorization header."""
    headers = {"Accept": MCP_ACCEPT}
    if authorization:
        headers["Authorization"] = authorization
    status, response_headers, text = agent.request(
        "POST",
        f"{base}/plugin/inventree-mcp/mcp/",
        body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers,
    )
    try:
        payload = json.loads(text or "{}")
    except ValueError:
        payload = {}
    return status, response_headers, payload


def tool_names(payload: dict) -> set[str]:
    return permissions.tool_names(payload)


def api_token() -> str:
    # named "membership-e2e"; membership.delete_api_tokens() deletes it
    key = membership.create_api_token()
    remember(key)
    return key


def page_title(text: str) -> str:
    match = re.search(r"<title>(.*?)</title>", text, re.DOTALL)
    return " ".join(match.group(1).split()) if match else text[:60].replace("\n", " ")


def mcp_rules(admin, group: int) -> dict[int, dict]:
    return {
        row["pk"]: {flag: row[flag] for flag in permissions.FLAGS}
        for row in admin.api("GET", f"rulesets/?group={group}")
    }


def restore_mcp_rules(admin, snapshot: dict[int, dict]) -> None:
    current = mcp_rules(admin, permissions.group_pk(admin))
    for pk, flags in snapshot.items():
        if current.get(pk) != flags:
            admin.api("PATCH", f"rulesets/{pk}/", flags)


def bridge_attempt(base: str, username: str):
    """Authorize as an anonymous browser, submit the login bridge's SSO form and drive Authentik as *username*.

    Returns browse()'s (kind, url, status, text). Unlike oauth_smoke.sso_login_through_bridge it accepts a refusal
    at the end (that helper exits unless the authorize page answers 200)."""
    agent, browser = sso.Browser(), sso.Browser()
    asm = membership.authorization_server(agent, base)
    client_id = oauth.register(agent, asm, "dev/e2e/run.py")
    url, _verifier = oauth.authorize_url(asm, client_id)
    status, headers, _ = browser.request("GET", url)
    bridge = sso.absolute(url, headers.get("Location", ""))
    check(
        f"5 {username}: anonymous authorize goes to the login bridge",
        status == 302 and "/oauth/login/" in bridge,
    )
    _status, _, html = browser.request("GET", bridge)
    form = next((f for f in oauth.parse_forms(html) if f["id"] == "sso-form"), None)
    check(f"5 {username}: the login bridge renders the SSO form", form is not None)
    status, headers, _ = browser.request(
        "POST",
        sso.absolute(bridge, form["action"]),
        form=form["fields"],
        headers={"Referer": bridge, "X-CSRFToken": browser.cookie("csrftoken") or ""},
    )
    location = sso.absolute(bridge, headers.get("Location", ""))
    return url, sso.browse(
        browser,
        location,
        username,
        sso.PASSWORD,
        stop=lambda target: target.startswith(oauth.CALLBACK_ORIGIN),
    )


# -- the scenarios ---------------------------------------------------------------------------------------------------


class Context:
    def __init__(self, base: str, skip_outage: bool):
        self.base = base
        self.mcp_url = f"{base}/plugin/inventree-mcp/mcp/"
        self.skip_outage = skip_outage
        self.authentik = bootstrap.api_from_env()
        self.started = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
        self._admin = None
        self.version = ""
        self.warnings: list[str] = []

    @property
    def admin(self):
        if self._admin is None:
            self._admin = permissions.Admin(self.base)
        return self._admin


def scenario_discovery(ctx: Context) -> None:
    """1: 401 + resource_metadata, PRM, AS metadata, well-known redirects and proxy rewrites, CORS headers."""
    agent = sso.Browser()
    status, headers, _ = mcp_call(agent, ctx.base, None)
    challenge = headers.get("WWW-Authenticate", "")
    match = re.search(r'resource_metadata="([^"]+)"', challenge)
    check(
        "1 POST /mcp/ without a token: 401 with resource_metadata",
        status == 401 and match is not None,
        challenge[:90],
    )
    prm_url = match.group(1)
    status, headers, _ = mcp_call(agent, ctx.base, "Bearer not-a-real-token")
    check(
        '1 POST /mcp/ with an unknown bearer: 401 with error="invalid_token"',
        status == 401
        and 'error="invalid_token"' in headers.get("WWW-Authenticate", ""),
    )
    status, headers, _ = agent.request(
        "POST",
        ctx.mcp_url.rstrip("/"),
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": MCP_ACCEPT},
    )
    check(
        "1 POST /mcp (no slash, as Claude Code sends it): 401 with the challenge",
        status == 401 and "resource_metadata=" in headers.get("WWW-Authenticate", ""),
    )

    status, headers, text = agent.request("GET", prm_url, headers=oauth.JSON)
    prm = json.loads(text) if status == 200 else {}
    issuer = (prm.get("authorization_servers") or [""])[0]
    check(
        "1 protected-resource metadata",
        status == 200
        and prm.get("resource") == ctx.mcp_url
        and issuer == f"{ctx.base}/plugin/inventree-mcp"
        and prm.get("bearer_methods_supported") == ["header"]
        and "g:read" in prm.get("scopes_supported", []),
        f"resource={prm.get('resource')} issuer={issuer} scopes={len(prm.get('scopes_supported', []))}",
    )
    status, headers, text = agent.request(
        "GET", f"{issuer}/.well-known/oauth-authorization-server", headers=oauth.JSON
    )
    asm = json.loads(text) if status == 200 else {}
    endpoints = (
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "revocation_endpoint",
    )
    check(
        "1 authorization-server metadata",
        status == 200
        and asm.get("issuer") == issuer
        and all(
            str(asm.get(key, "")).startswith(issuer + "/oauth/") for key in endpoints
        )
        and asm.get("code_challenge_methods_supported") == ["S256"]
        and asm.get("grant_types_supported") == ["authorization_code", "refresh_token"]
        and asm.get("client_id_metadata_document_supported") is True,
        f"issuer={asm.get('issuer')}",
    )
    status, _, text = agent.request(
        "GET", f"{issuer}/.well-known/openid-configuration", headers=oauth.JSON
    )
    check(
        "1 openid-configuration alias under the issuer",
        status == 200 and json.loads(text).get("issuer") == issuer,
    )

    # RFC 8414 / RFC 9728 path-insertion URLs (served on 1.5.x through the proxy rewrites of the README)
    parsed = urllib.parse.urlparse(issuer)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    inserted = {
        f"/.well-known/oauth-authorization-server{parsed.path}": "issuer",
        f"/.well-known/openid-configuration{parsed.path}": "issuer",
        "/.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp": "resource",
        "/.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp/": "resource",
    }
    for path, key in inserted.items():
        status, headers, text = agent.request("GET", origin + path, headers=oauth.JSON)
        is_json = (headers.get("Content-Type") or "").startswith("application/json")
        value = json.loads(text).get(key) if status == 200 and is_json else None
        check(
            f"1 path-insertion URL {path}: 200 JSON",
            status == 200 and value in (issuer, ctx.mcp_url),
        )

    # core's /.well-known/<name> redirects (WellKnownMixin, InvenTree > 1.5.x only)
    status, headers, _ = agent.request(
        "GET", f"{origin}/.well-known/oauth-authorization-server"
    )
    location = headers.get("Location", "")
    if status in (301, 302) and "/plugin/inventree-mcp/" in location:
        for name in ("oauth-authorization-server", "oauth-protected-resource"):
            status, headers, _ = agent.request("GET", f"{origin}/.well-known/{name}")
            target = sso.absolute(origin, headers.get("Location", ""))
            followed, _, _ = agent.request("GET", target, headers=oauth.JSON)
            check(
                f"1 /.well-known/{name} redirects to the plugin document",
                status in (301, 302) and followed == 200,
            )
    else:
        skip(
            "1 core /.well-known/<name> redirects",
            f"InvenTree {ctx.version} has no plugin /.well-known/ support (inventree/InvenTree#12698, newer than "
            f"1.5.x): /.well-known/oauth-authorization-server -> {status} {location.split('?')[0] or '-'}; "
            "the proxy rewrites above stand in",
        )

    # CORS through the proxy: metadata (anonymous GET, any origin), preflights of the machine endpoints
    for url in (prm_url, f"{issuer}/.well-known/oauth-authorization-server"):
        name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        _, headers, _ = agent.request("GET", url, headers=oauth.JSON)
        plain = headers.get("Access-Control-Allow-Origin")
        _, headers, _ = agent.request(
            "GET", url, headers={**oauth.JSON, "Origin": TRUSTED_ORIGIN}
        )
        echoed = headers.get("Access-Control-Allow-Origin")
        check(
            f"1 CORS on {name}: Access-Control-Allow-Origin without and with Origin",
            plain == "*" and echoed in ("*", TRUSTED_ORIGIN),
            f"no Origin: {plain}; Origin {TRUSTED_ORIGIN}: {echoed}; Cache-Control: {headers.get('Cache-Control')}",
        )
    for url, method in (
        (prm_url, "GET"),
        (asm["registration_endpoint"], "POST"),
        (asm["token_endpoint"], "POST"),
        (asm["revocation_endpoint"], "POST"),
        (ctx.mcp_url, "POST"),
    ):
        status, headers, _ = agent.request(
            "OPTIONS",
            url,
            headers={
                "Origin": TRUSTED_ORIGIN,
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "content-type, authorization",
            },
        )
        allowed = (headers.get("Access-Control-Allow-Methods") or "").upper()
        check(
            f"1 CORS preflight {method} {urllib.parse.urlparse(url).path} from {TRUSTED_ORIGIN}",
            status in (200, 204)
            and headers.get("Access-Control-Allow-Origin") in ("*", TRUSTED_ORIGIN)
            and method in allowed,
            f"{status}, allow-origin={headers.get('Access-Control-Allow-Origin')}",
        )
    _, headers, _ = agent.request(
        "POST",
        ctx.mcp_url,
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": MCP_ACCEPT, "Origin": TRUSTED_ORIGIN},
    )
    check(
        "1 the 401 of /mcp/ carries CORS headers for a trusted origin",
        headers.get("Access-Control-Allow-Origin") in ("*", TRUSTED_ORIGIN),
    )


def scenario_member(ctx: Context) -> None:
    """2: connect, tool calls with OAuth and API token, refresh with a fresh check, refresh with an aged check."""
    agent, asm, client_id, tokens = membership.connect(ctx.base, "2")
    scopes = tokens.get("scope", "").split()
    check(
        "2 e2e-member connects (DCR, PKCE S256, login bridge, Authentik, consent, code, token)",
        tokens.get("token_type") == "Bearer"
        and "g:read" in scopes
        and tokens.get("refresh_token"),
        f"expires_in={tokens.get('expires_in')} scopes={len(scopes)} access_token=<{len(tokens['access_token'])} chars>",
    )
    status, _, payload = mcp_call(agent, ctx.base, f"Bearer {tokens['access_token']}")
    check(
        "2 tools/list with the access token",
        status == 200 and tool_names(payload),
        f"{len(tool_names(payload))} tools",
    )
    permissions.real_tool_calls(
        agent, ctx.base, f"Bearer {tokens['access_token']}", "2", "OAuth token:"
    )
    check("2 paginated list_parts and get_part with the OAuth token", True)
    key = api_token()
    try:
        permissions.real_tool_calls(agent, ctx.base, f"Token {key}", "2", "API token:")
        check("2 paginated list_parts and get_part with an InvenTree API token", True)
    finally:
        membership.delete_api_tokens()

    before = membership.state()
    status, body = oauth.refresh(
        agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
    )
    after = membership.state()
    check(
        "2 refresh with a fresh check: 200, no upstream call (upstream token unchanged)",
        status == 200
        and after["upstream"] is not None
        and after["upstream"] == before["upstream"],
        f"{status}; last check {after['checked_age_s']} s ago; rotated: {body.get('refresh_token') != tokens['refresh_token']}",
    )
    old_access, tokens = tokens["access_token"], body
    status, _, _ = mcp_call(agent, ctx.base, f"Bearer {tokens['access_token']}")
    old_status, _, _ = mcp_call(agent, ctx.base, f"Bearer {old_access}")
    check(
        "2 the new access token works, the rotated one is refused",
        status == 200 and old_status == 401,
    )

    membership.age_check(2)
    status, body = oauth.refresh(
        agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
    )
    aged = membership.state()
    check(
        "2 refresh with an aged check (2 h): 200, real re-check (upstream token rotated, check stamped)",
        status == 200
        and aged["upstream"] not in (None, after["upstream"])
        and aged["checked_age_s"] < 120
        and aged["error"] == "",
        f"{status}; last check {aged['checked_age_s']} s ago; groups={aged['groups']}",
    )


def scenario_removed(ctx: Context) -> None:
    """3: removed from inventree-mcp in Authentik, blocked; back in the group, an SSO login lifts the block."""
    agent, asm, client_id, tokens = membership.connect(ctx.base, "3")
    try:
        bootstrap.set_user_groups(ctx.authentik, USER, ["inventree-users"])
        groups = bootstrap.user_groups(ctx.authentik, USER)
        check(
            "3 e2e-member removed from inventree-mcp in Authentik",
            groups == ["inventree-users"],
            str(groups),
        )
        membership.age_check(2)
        status, body = oauth.refresh(
            agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
        )
        s = membership.state()
        check(
            "3 aged refresh: 400 invalid_grant",
            status == 400 and body.get("error") == "invalid_grant",
            str(status),
        )
        check(
            "3 the user is blocked, every MCP token revoked, the upstream token deleted",
            s["blocked"]
            and s["reason"] == "not_in_required_groups"
            and s["live_refresh_tokens"] == 0
            and s["upstream"] is None,
            f"reason={s['reason']!r} groups={s['groups']} live MCP refresh tokens={s['live_refresh_tokens']}",
        )
        status, headers, _ = mcp_call(
            agent, ctx.base, f"Bearer {tokens['access_token']}"
        )
        check(
            "3 MCP bearer of the blocked user: 401 with the challenge",
            status == 401
            and "resource_metadata=" in headers.get("WWW-Authenticate", ""),
            str(status),
        )
        browser = sso.sso_login(
            USER
        )  # still outside the group: the login keeps the block
        url, _verifier = oauth.authorize_url(asm, client_id)
        status, headers, page = browser.request("GET", url)
        check(
            "3 authorize after a new SSO login outside the group: 403 'Access revoked' page, no redirect",
            status == 403 and "Access revoked" in page and not headers.get("Location"),
            f"{status} {page_title(page)!r}",
        )

        bootstrap.set_user_groups(ctx.authentik, USER, MEMBER_GROUPS)
        browser = sso.sso_login(USER)
        s = membership.state()
        check(
            "3 back in inventree-mcp: a new SSO login lifts the block",
            not s["blocked"]
            and REQUIRED_GROUP in s["groups"]
            and s["upstream"] is not None,
            f"groups={s['groups']}",
        )
        status, _, page = browser.request("GET", url)
        check(
            "3 authorize shows the consent page again",
            status == 200 and 'name="allow"' in page,
            str(status),
        )
    finally:
        bootstrap.set_user_groups(ctx.authentik, USER, MEMBER_GROUPS)
        if membership.state()["blocked"]:
            membership.unblock()
            print("[3] cleanup: e2e-member unblocked by hand")


def scenario_outage(ctx: Context) -> None:
    """4: Authentik stopped: grace, then invalid_grant, nothing revoked; back up, the same refresh token works."""
    if ctx.skip_outage:
        raise Skipped("--skip-outage")
    agent, asm, client_id, tokens = membership.connect(ctx.base, "4")
    # membership_e2e.outage(): stop authentik-server, refresh within grace (2 h) -> 200, past grace (5 h) ->
    # invalid_grant with nothing revoked, start authentik-server (in a finally), the same refresh token -> 200
    tokens = membership.outage(agent, asm, client_id, tokens, ctx.base)
    check(
        "4 grace period, invalid_grant past it, nothing revoked, recovery with the same refresh token",
        True,
    )
    status, _, _ = mcp_call(agent, ctx.base, f"Bearer {tokens['access_token']}")
    check(
        "4 tools/list with the token issued after the recovery",
        status == 200,
        str(status),
    )


def scenario_non_members(ctx: Context) -> None:
    """5: interactive-only and guest users are refused by the plugin, the non-member by Authentik."""
    for username, expected_groups in (
        ("e2e-interactive", ["Users"]),
        ("e2e-guest", []),
    ):
        url, (kind, landed, status, text) = bridge_attempt(ctx.base, username)
        check(
            f"5 {username}: signs in through Authentik, back on authorize: 403 'No MCP access', no redirect",
            kind == "page"
            and landed == url
            and status == 403
            and "No MCP access" in text,
            f"{kind} {status} {page_title(text)!r}",
        )
        state = sso.inventree_state(username)
        check(
            f"5 {username}: InvenTree user with groups {expected_groups} (interactive roles only / none)",
            state is not None and state["groups"] == expected_groups,
            str(state and state["groups"]),
        )
    _url, (kind, _landed, status, text) = bridge_attempt(ctx.base, "e2e-nonmember")
    check(
        "5 e2e-nonmember: refused by Authentik",
        kind == "authentik-denied",
        f"{kind} {status} {text[:60]!r}",
    )
    check(
        "5 e2e-nonmember: no InvenTree user exists",
        sso.inventree_state("e2e-nonmember") is None,
    )


def scenario_narrowing(ctx: Context) -> None:
    """6: MCP view on parts only: part tools only, stock refused on MCP, stock still readable on /api/."""
    admin = ctx.admin
    group = permissions.group_pk(admin)
    snapshot = mcp_rules(admin, group)
    try:
        admin.api("POST", "rulesets/clear/", {"group": group})
        rows = permissions.rows_by_name(admin, group)
        admin.api("PATCH", f"rulesets/{rows['part']['pk']}/", {"can_view": True})
        granted = sorted(
            name
            for name, row in permissions.rows_by_name(admin, group).items()
            if row["can_view"]
        )
        check(
            f"6 '{REQUIRED_GROUP}' MCP roles set to view on parts only (plugin API)",
            granted == ["part"],
            str(granted),
        )

        agent, _asm, _client_id, tokens = membership.connect(ctx.base, "6")
        check(
            "6 the token carries the MCP scopes only",
            tokens["scope"] == "g:read r:view:part",
            repr(tokens["scope"]),
        )
        bearer = f"Bearer {tokens['access_token']}"
        status, _, payload = mcp_call(agent, ctx.base, bearer)
        names = tool_names(payload)
        check(
            "6 tools/list: part tools, no stock tool",
            status == 200
            and "list_parts" in names
            and not names & permissions.STOCK_TOOLS,
            f"{len(names)} tools",
        )
        result = permissions.call_tool(agent, ctx.base, bearer, "list_stock_items")
        message = (result.get("content") or [{}])[0].get("text", "")
        check(
            "6 tools/call list_stock_items is refused",
            bool(result.get("isError")) and "permission" in message,
        )
        permissions.real_tool_calls(agent, ctx.base, bearer, "6", "OAuth token:")

        key = api_token()
        try:
            status, count = permissions.stock_with_api_token(agent, ctx.base, key)
            check(
                "6 the same user's API token still reads /api/stock/",
                status == 200,
                f"count={count}",
            )
            status, payload = permissions.mcp_with_api_token(agent, ctx.base, key)
            check(
                "6 the API token on /mcp/ sees exactly the OAuth tool list",
                status == 200 and tool_names(payload) == names,
            )
            result = permissions.call_tool(
                agent, ctx.base, f"Token {key}", "list_stock_items"
            )
            check(
                "6 the API token's stock tool call on /mcp/ is refused",
                bool(result.get("isError")),
            )
        finally:
            membership.delete_api_tokens()
    finally:
        restore_mcp_rules(admin, snapshot)
        print(f"[6] cleanup: MCP roles of '{REQUIRED_GROUP}' restored")


def scenario_reduction(ctx: Context) -> None:
    """7: clearing an MCP flag revokes the members' live refresh tokens at once."""
    admin = ctx.admin
    group = permissions.group_pk(admin)
    snapshot = mcp_rules(admin, group)
    try:
        rows = permissions.rows_by_name(admin, group)
        if not rows["part"]["can_view"]:
            admin.api("PATCH", f"rulesets/{rows['part']['pk']}/", {"can_view": True})
        agent, asm, client_id, tokens = membership.connect(ctx.base, "7")
        before = membership.state()
        admin.api("PATCH", f"rulesets/{rows['part']['pk']}/", {"can_view": False})
        after = membership.state()
        check(
            "7 PATCH api/rulesets/<part>/ can_view=false revokes the live MCP refresh tokens at once",
            before["live_refresh_tokens"] >= 1 and after["live_refresh_tokens"] == 0,
            f"{before['live_refresh_tokens']} -> {after['live_refresh_tokens']}",
        )
        status, body = oauth.refresh(
            agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
        )
        check(
            "7 refresh with the revoked token: invalid_grant",
            status == 400 and body.get("error") == "invalid_grant",
        )
        status, _, _ = mcp_call(agent, ctx.base, f"Bearer {tokens['access_token']}")
        check("7 the access token is refused", status == 401, str(status))
        check(
            "7 the user is not blocked (a reduction is not an identity refusal)",
            not after["blocked"],
        )
    finally:
        restore_mcp_rules(admin, snapshot)
        print(f"[7] cleanup: MCP roles of '{REQUIRED_GROUP}' restored")


def create_cimd_row() -> None:
    shell(
        f"""
from oauth2_provider.models import get_application_model
Application = get_application_model()
Application.objects.filter(client_id={CIMD_CLIENT_ID!r}).delete()
Application.objects.create(
    client_id={CIMD_CLIENT_ID!r}, name='inventree-mcp e2e (simulated CIMD)', user=None,
    client_type=Application.CLIENT_PUBLIC, authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
    redirect_uris={CIMD_HTTPS_REDIRECT + " http://127.0.0.1/callback"!r}, client_secret='',
    registration_source=Application.RegistrationSource.CIMD, skip_authorization=False,
    cimd_expires_at=timezone.now() + timedelta(days=1))
print('E2E {{}}')
"""
    )


def delete_cimd_row() -> int:
    return shell(
        f"""
from oauth2_provider.models import get_application_model
deleted, _ = get_application_model().objects.filter(client_id={CIMD_CLIENT_ID!r}).delete()
print('E2E ' + json.dumps({{'deleted': deleted}}))
"""
    )["deleted"]


def scenario_clients(ctx: Context) -> None:
    """8: untrusted client / unregistered redirect -> error page; trusted CIMD: loopback consent, https skip."""
    agent = sso.Browser()
    asm = membership.authorization_server(agent, ctx.base)
    browser = sso.sso_login(USER)

    url, _ = oauth.authorize_url(
        asm, "https://untrusted.example/oauth/client-metadata.json"
    )
    status, headers, page = browser.request("GET", url)
    check(
        "8 untrusted https client_id: error page, never a redirect",
        status in (400, 401, 403)
        and not headers.get("Location")
        and "Unknown client" in page,
        f"{status} {page_title(page)!r}",
    )
    client_id = oauth.register(agent, asm, "dev/e2e/run.py")
    url, _ = oauth.authorize_url(
        asm, client_id, redirect_uri="https://attacker.example/callback"
    )
    status, headers, page = browser.request("GET", url)
    check(
        "8 registered client with an unregistered redirect URI: error page, never a redirect",
        status in (400, 401, 403)
        and not headers.get("Location")
        and "Unknown client" in page,
        f"{status} {page_title(page)!r}",
    )

    create_cimd_row()
    try:
        url, _ = oauth.authorize_url(asm, CIMD_CLIENT_ID)
        status, headers, page = browser.request("GET", url)
        check(
            "8 trusted CIMD client, loopback redirect URI: the consent page is still shown",
            status == 200 and 'name="allow"' in page,
            f"{status}, redirect_uri={oauth.REDIRECT_URI}",
        )
        url, verifier = oauth.authorize_url(
            asm, CIMD_CLIENT_ID, redirect_uri=CIMD_HTTPS_REDIRECT
        )
        status, headers, _ = browser.request("GET", url)
        location = headers.get("Location", "")
        result = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query))
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["state"]
        check(
            "8 trusted CIMD client, https redirect URI: consent skipped, code sent to the redirect URI",
            status == 302
            and location.startswith(CIMD_HTTPS_REDIRECT + "?")
            and "code" in result
            and result.get("state") == state,
            f"{status} to {location.split('?')[0]}",
        )
        status, _, text = agent.request(
            "POST",
            asm["token_endpoint"],
            form={
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": CIMD_HTTPS_REDIRECT,
                "client_id": CIMD_CLIENT_ID,
                "code_verifier": verifier,
            },
            headers=oauth.JSON,
        )
        tokens = json.loads(text) if status == 200 else {}
        mcp_status = (
            mcp_call(agent, ctx.base, f"Bearer {tokens.get('access_token', '')}")[0]
            if tokens
            else 0
        )
        check(
            "8 the trusted client's code exchanges (public client, PKCE) and its token works on /mcp/",
            status == 200 and mcp_status == 200,
            f"token {status}, tools/list {mcp_status}",
        )
    finally:
        deleted = delete_cimd_row()
        print(
            f"[8] cleanup: simulated CIMD row deleted ({deleted} rows incl. grants and tokens)"
        )


def upstream_secrets() -> None:
    """The current upstream (Authentik) tokens of the test users, to be grepped for as well (never printed)."""
    values = membership.shell(
        """
import json
from allauth.socialaccount.models import SocialToken
rows = SocialToken.objects.filter(account__user__username__startswith='e2e-')
print('E2E ' + json.dumps([v for t in rows for v in (t.token, t.token_secret) if v]))
"""
    )
    for value in values:
        remember(value)


CORE_REQUEST_LOG_NOTE = (
    "Not the plugin: InvenTree's gunicorn access log (on by default in the container image) and, at "
    "INVENTREE_LOG_LEVEL=INFO (the dev stack), django_structlog's request lines record the SSO callback's query "
    "string. The code is single use, short-lived and useless without the OIDC client secret. Mitigation in "
    "docs/OPERATIONS.md (logging)."
)


def _around(line: str, marker: str, width: int = 130) -> str:
    """The part of a (masked) log line around *marker*."""
    at = max(line.find(marker), 0)
    return line[max(at - width, 0) : at + width]


def _core_request_log_callback(line: str) -> bool:
    """A core request-log line of the allauth SSO callback: django_structlog's request_started/finished or
    gunicorn's access log. The upstream (Authentik) code is in its query string."""
    if "/login/callback/?" not in line:
        return False
    return "django_structlog" in line or bool(
        re.search(r'"GET /accounts/\S+ HTTP/1\.[01]" \d{3}', line)
    )


def scenario_logs(ctx: Context) -> None:
    """9: no test user address, token or code in the server, worker and proxy logs of this run."""
    upstream_secrets()
    addresses = [f"{username}@example.test" for username in bootstrap.USERS]
    since = ctx.started.strftime("%Y-%m-%dT%H:%M:%SZ")
    lengths = sorted({len(secret) for secret in SECRETS})
    print(
        f"[9] grepping logs since {since} for {len(addresses)} addresses and {len(SECRETS)} secrets "
        f"(lengths {lengths}; not printed)"
    )
    for services in (("inventree-server", "inventree-worker"), ("proxy",)):
        dump = subprocess.run(
            [str(DEV / "logs.sh"), "--dump", "--since", since, *services],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        label = "+".join(services)
        lines = dump.splitlines()
        check(
            f"9 {label}: log of this run is not empty",
            len(lines) > 0,
            f"{len(lines)} lines",
        )
        lowered = dump.lower()
        found = [address for address in addresses if address in lowered]
        check(
            f"9 {label}: no test user e-mail address",
            not found,
            f"found: {found}" if found else "",
        )
        leaked = [secret for secret in SECRETS if secret in dump]
        core_request_log = []  # see CORE_REQUEST_LOG_NOTE
        for secret in leaked:  # where, with every secret of the run masked
            hits = [line for line in lines if secret in line]
            if all(_core_request_log_callback(line) for line in hits):
                core_request_log.append(secret)
            masked = re.sub(r"\x1b\[[0-9;]*m", "", hits[0])  # without terminal colours
            for other in SECRETS:
                masked = masked.replace(
                    other, f"<secret {fingerprint(other)} len={len(other)}>"
                )
            print(
                f"[9] {label}: {fingerprint(secret)} in {len(hits)} line(s): {_around(masked, fingerprint(secret))}"
            )
        others = [secret for secret in leaked if secret not in core_request_log]
        check(
            f"9 {label}: none of the run's tokens and codes (MCP access/refresh tokens, MCP "
            "authorization codes, upstream tokens, API tokens; Authentik codes outside core's request log)",
            not others,
            f"{len(others)} leaked" if others else f"{len(SECRETS)} checked",
        )
        if core_request_log:
            ctx.warnings.append(
                f"9 {label}: {len(core_request_log)} Authentik code(s) in core's request/access log"
            )
            print(
                f"WARN 9 {label}: {len(core_request_log)} Authentik authorization code(s) in InvenTree core's "
                f"request/access log lines of the SSO callback. {CORE_REQUEST_LOG_NOTE}",
                flush=True,
            )
        if services == ("inventree-server", "inventree-worker"):
            plugin_lines = sum("inventree-mcp:" in line for line in lines)
            check(
                "9 the plugin did log during the run (the grep is not vacuous)",
                plugin_lines > 0,
                f"{plugin_lines} lines",
            )
        else:
            callbacks = [
                line for line in lines if "/accounts/authentik/login/callback/" in line
            ]
            with_code = [line for line in callbacks if re.search(r"[?&]code=", line)]
            check(
                "9 proxy: the Authentik callback lines carry no code (Caddy's log filter strips code and state)",
                callbacks and not with_code,
                f"{len(callbacks)} callback lines, {len(with_code)} with a code",
            )


def claude(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["claude", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=REPO,
        check=False,
    )


MANUAL_STEPS = f"""manual steps to finish the connection (needs a browser and an interactive Claude Code):
  1. claude mcp add --transport http {CLAUDE_SERVER} http://localhost:8080/plugin/inventree-mcp/mcp/
  2. start claude in this directory, run /mcp, select {CLAUDE_SERVER}, choose Authenticate
  3. the browser opens InvenTree, goes on to Authentik: sign in as e2e-member / {bootstrap.DEV_PASSWORD}
  4. consent page (always shown: Claude Code receives the code on a loopback port): Approve
  5. /mcp lists {CLAUDE_SERVER} as connected; ask "list three parts from InvenTree" (tool list_parts)
  6. claude mcp remove {CLAUDE_SERVER}"""


def scenario_claude_code(ctx: Context) -> None:
    """10: claude mcp add, claude mcp list reports "Needs authentication", claude mcp remove."""
    if shutil.which("claude") is None:
        print(MANUAL_STEPS)
        raise Skipped("the claude binary is not installed")
    if claude("mcp", "get", CLAUDE_SERVER).returncode == 0:
        print(MANUAL_STEPS)
        raise Skipped(
            f"an MCP server named {CLAUDE_SERVER!r} is already configured; not touching it"
        )
    result = claude("mcp", "add", "--transport", "http", CLAUDE_SERVER, ctx.mcp_url)
    check(
        f"10 claude mcp add --transport http {CLAUDE_SERVER} {ctx.mcp_url}",
        result.returncode == 0,
        (result.stdout or result.stderr).strip().splitlines()[0]
        if (result.stdout or result.stderr)
        else "",
    )
    try:
        result = claude("mcp", "list", timeout=300)
        line = next(
            (
                line
                for line in result.stdout.splitlines()
                if line.startswith(f"{CLAUDE_SERVER}:")
            ),
            "",
        )
        check(
            "10 claude mcp list: the dev server needs authentication",
            "Needs authentication" in line,
            line.strip(),
        )
    finally:
        result = claude("mcp", "remove", CLAUDE_SERVER, "-s", "local")
        print(
            f"[10] cleanup: claude mcp remove {CLAUDE_SERVER} -> exit {result.returncode}"
        )
    print(MANUAL_STEPS)


SCENARIOS: list[tuple[str, str, Callable[[Context], None]]] = [
    ("1", "discovery", scenario_discovery),
    ("2", "member", scenario_member),
    ("3", "removed", scenario_removed),
    ("4", "outage", scenario_outage),
    ("5", "non-members", scenario_non_members),
    ("6", "narrowing", scenario_narrowing),
    ("7", "reduction", scenario_reduction),
    ("8", "clients", scenario_clients),
    ("9", "logs", scenario_logs),
    ("10", "claude-code", scenario_claude_code),
]


# -- set-up and restore ---------------------------------------------------------------------------------------------


def read_settings() -> dict:
    return shell(
        f"print('E2E ' + json.dumps({{key: plugin.get_setting(key) for key in {MANAGED_SETTINGS!r}}}))\n"
    )


def write_settings(values: dict) -> None:
    shell(
        f"for key, value in json.loads({json.dumps(values)!r}).items():\n"
        "    plugin.set_setting(key, value)\n"
        "print('E2E {}')\n"
    )


def delete_registered_clients() -> int:
    ids = sorted(client for client in REGISTERED_CLIENTS if client)
    if not ids:
        return 0
    return membership.shell(
        f"""
import json
from oauth2_provider.models import get_application_model
Application = get_application_model()
deleted, _ = Application.objects.filter(client_id__in={ids!r}, registration_source=Application.RegistrationSource.DCR).delete()
print('E2E ' + json.dumps({{'deleted': deleted}}))
"""
    )["deleted"]


class Restore:
    """Snapshots taken before the scenarios and put back afterwards, whatever happened."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.groups = {
            name: bootstrap.user_groups(ctx.authentik, name) for name in bootstrap.USERS
        }
        self.settings = read_settings()
        before = membership.state()
        self.was_blocked = before["blocked"]
        self.block_reason = before["reason"]
        admin = ctx.admin
        self.rules = mcp_rules(admin, permissions.group_pk(admin))

    def __call__(self) -> None:
        steps = [
            (
                "authentik-server started",
                lambda: membership.compose("start", "authentik-server"),
            ),
            ("Authentik ready", membership.wait_for_authentik),
            ("Authentik groups of the test users", self._groups),
            ("block state of e2e-member", self._block),
            (
                "MCP roles of 'MCP Users'",
                lambda: restore_mcp_rules(self.ctx.admin, self.rules),
            ),
            ("API tokens", membership.delete_api_tokens),
            ("simulated CIMD row", delete_cimd_row),
            ("OAuth clients registered by the run", delete_registered_clients),
            (
                "plugin settings " + ", ".join(MANAGED_SETTINGS),
                lambda: write_settings(self.settings),
            ),
        ]
        for name, action in steps:
            try:
                action()
                print(f"[restore] {name}", flush=True)
            except BaseException as error:  # noqa: BLE001 - restore as much as possible
                print(
                    f"FAIL restore {name}: {type(error).__name__}: {error}", flush=True
                )

    def _groups(self) -> None:
        for name, groups in self.groups.items():
            if bootstrap.user_groups(self.ctx.authentik, name) != groups:
                bootstrap.set_user_groups(self.ctx.authentik, name, groups)

    def _block(self) -> None:
        blocked = membership.state()["blocked"]
        if blocked and not self.was_blocked:
            membership.unblock()
        elif self.was_blocked and not blocked:  # scenario 3's SSO login lifted it
            membership.shell(
                membership.PRELUDE
                + f"""
from inventree_mcp.auth.membership import block_user
block_user(user, {self.block_reason!r})
print('E2E {{}}')
"""
            )


def run_scenario(ctx: Context, number: str, name: str, function) -> str:
    print(
        f"\n== scenario {number}: {name} - {function.__doc__.split(':', 1)[1].strip()}",
        flush=True,
    )
    try:
        function(ctx)
    except Skipped as reason:
        skip(f"scenario {number} {name}", str(reason))
        return "SKIP"
    except ScenarioFailed:
        return "FAIL"
    except (
        SystemExit
    ) as error:  # the imported helpers' expect() / shell() exit on a failed assertion
        detail = (
            error.code if isinstance(error.code, str) else "see the FAILED line above"
        )
        print(f"FAIL {number} helper assertion: {detail}", flush=True)
        return "FAIL"
    except Exception as error:  # noqa: BLE001 - one broken scenario must not stop the others
        print(f"FAIL {number} {type(error).__name__}: {error}", flush=True)
        traceback.print_exc(limit=4, file=sys.stdout)
        return "FAIL"
    return "PASS"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument(
        "--only",
        action="append",
        metavar="N|NAME",
        help="run only this scenario (repeatable)",
    )
    parser.add_argument(
        "--skip-outage",
        action="store_true",
        help="skip scenario 4 (stops authentik-server)",
    )
    parser.add_argument(
        "--list", action="store_true", help="list the scenarios and exit"
    )
    args = parser.parse_args()
    if args.list:
        for number, name, function in SCENARIOS:
            print(f"{number:>2} {name:<12} {function.__doc__.split(':', 1)[1].strip()}")
        return 0
    selected = SCENARIOS
    if args.only:
        wanted = set(args.only)
        selected = [s for s in SCENARIOS if s[0] in wanted or s[1] in wanted]
        unknown = wanted - {s[0] for s in selected} - {s[1] for s in selected}
        if unknown:
            parser.error(
                f"unknown scenario(s): {', '.join(sorted(unknown))} (see --list)"
            )

    ctx = Context(args.base_url.rstrip("/"), args.skip_outage)
    status, _, text = sso.Browser().request(
        "GET", f"{ctx.base}/api/", headers=oauth.JSON
    )
    if status != 200:
        print(f"FAIL setup: {ctx.base}/api/ -> {status} (is dev/up.sh running?)")
        return 1
    ctx.version = json.loads(text).get("version", "?")
    print(
        f"InvenTree {ctx.version} at {ctx.base}; Authentik at {bootstrap.public_url()}; started {ctx.started:%H:%M:%S} UTC"
    )
    if sso.inventree_state(USER) is None:
        sso.sso_login(
            USER
        )  # a fresh stack: the first SSO login creates the InvenTree user the helpers expect
        print(f"[setup] first SSO login of {USER}")

    restore = Restore(ctx)
    results: list[tuple[str, str, str]] = []
    try:
        write_settings(RUN_SETTINGS)
        print(f"[setup] settings for the run: {RUN_SETTINGS} (were {restore.settings})")
        for number, name, function in selected:
            results.append((number, name, run_scenario(ctx, number, name, function)))
    finally:
        print("\n== restore")
        restore()

    print("\n== summary")
    for number, name, outcome in results:
        print(f"{outcome} scenario {number} {name}")
    counts = {
        outcome: sum(r[2] == outcome for r in results)
        for outcome in ("PASS", "FAIL", "SKIP")
    }
    for warning in ctx.warnings:
        print(f"WARN {warning}")
    print(
        f"{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped, "
        f"{len(ctx.warnings)} warning(s)"
    )
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
