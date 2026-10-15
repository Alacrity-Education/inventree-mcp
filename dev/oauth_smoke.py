#!/usr/bin/env python3
"""Smoke test of the OAuth façade against the running dev instance (WP1 acceptance 2).

Drives the flow an MCP client and a browser would:

  401 + challenge -> protected-resource metadata -> AS metadata -> register (DCR)
  -> authorize (anonymous: login bridge) -> sign in -> consent approve -> token
  -> tools/list -> approval_prompt=auto still shows consent -> refresh (rotation)
  -> tools/list -> old access token refused -> revoke -> revoked refresh token
  refused at once

Sign-in modes:

  (default)    allauth headless password login (POST /api/auth/v1/auth/login) for --username/--password
               (admin/admin), standing in for the SSO round trip.
  --sso USER   the real path: the login bridge's auto-submitted form (POST provider/redirect with
               callback_url = the authorize URL) -> Authentik (USER, the dev password) -> back to the
               authorize URL with a session. Use e2e-member.

Usage: python3 dev/oauth_smoke.py [--base-url http://localhost:8080] [--username admin] [--password admin]
       python3 dev/oauth_smoke.py --sso e2e-member

Prerequisite: the plugin setting OAUTH_ENABLED is on. Standard library only; the HTTP client and the
Authentik flow driver are dev/authentik/sso_smoke.py's (Browser, browse). Tokens, codes and secrets
are never printed, only their length.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import sys
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

# dev/authentik is not a package: the shared HTTP client and Authentik flow driver
sys.path.insert(0, str(Path(__file__).resolve().parent / "authentik"))
from sso_smoke import PASSWORD, Browser, absolute, browse

CALLBACK_ORIGIN = "http://127.0.0.1:43123"
REDIRECT_URI = f"{CALLBACK_ORIGIN}/callback"
JSON = {"Accept": "application/json"}


class Forms(HTMLParser):
    """The action and <input> fields (hidden ones, plus named buttons ignored) of every <form>."""

    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.forms.append(
                {
                    "id": attrs.get("id"),
                    "action": attrs.get("action") or "",
                    "fields": {},
                }
            )
        elif (
            tag == "input"
            and self.forms
            and attrs.get("type") == "hidden"
            and attrs.get("name")
        ):
            self.forms[-1]["fields"][attrs["name"]] = attrs.get("value") or ""


def parse_forms(html: str) -> list[dict]:
    parser = Forms()
    parser.feed(html)
    return parser.forms


def step(number: str, text: str) -> None:
    print(f"[{number}] {text}", flush=True)


def expect(condition: bool, text: str) -> None:
    if not condition:
        print(f"FAILED: {text}")
        sys.exit(1)


def mcp_tools_list(
    agent: Browser, mcp_url: str, token: str | None
) -> tuple[int, dict, dict]:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, resp_headers, text = agent.request(
        "POST",
        mcp_url,
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers=headers,
    )
    try:
        payload = json.loads(text or "{}")
    except ValueError:
        payload = {}
    return status, resp_headers, payload


def refresh(
    agent: Browser, token_endpoint: str, client_id: str, refresh_token: str
) -> tuple[int, dict]:
    status, _, text = agent.request(
        "POST",
        token_endpoint,
        form={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        headers=JSON,
    )
    return status, json.loads(text or "{}")


def register(agent: Browser, asm: dict, client_name: str = "oauth_smoke.py") -> str:
    """Dynamic client registration of a public loopback client; returns the client_id."""
    status, _, text = agent.request(
        "POST",
        asm["registration_endpoint"],
        body={
            "client_name": client_name,
            "redirect_uris": ["http://127.0.0.1/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        headers=JSON,
    )
    expect(status == 201, f"registration failed: {status} {text[:200]!r}")
    return json.loads(text)["client_id"]


def authorize_url(asm: dict, client_id: str, **extra: str) -> tuple[str, str]:
    """(authorize URL with PKCE S256 and a random state, code verifier); *extra* adds parameters."""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": secrets.token_urlsafe(8),
    } | extra
    return asm["authorization_endpoint"] + "?" + urllib.parse.urlencode(
        params
    ), verifier


def approve_and_exchange(
    agent: Browser,
    browser: Browser,
    asm: dict,
    client_id: str,
    url: str,
    verifier: str,
    page: str,
    **extra: str,
) -> dict:
    """Post the consent *page*'s form back approved, check the state, exchange the code.

    *extra* adds token request parameters (e.g. resource). Returns the token response."""
    consent = next(
        (f for f in parse_forms(page) if f["id"] == "authorizationForm"), None
    )
    expect(consent is not None, f"no consent form: {page[:200]!r}")
    status, headers, _ = browser.request(
        "POST",
        url,
        form=consent["fields"] | {"allow": "Authorize"},
        headers={"X-CSRFToken": browser.cookie("csrftoken") or "", "Referer": url},
    )
    location = headers.get("Location", "")
    result = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query))
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query)).get("state")
    expect(
        status == 302 and "code" in result and result.get("state") == state,
        f"approval failed: {status} to {location.split('?')[0]}",
    )
    status, _, text = agent.request(
        "POST",
        asm["token_endpoint"],
        form={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        | extra,
        headers=JSON,
    )
    expect(status == 200, f"token exchange -> {status}: {text[:200]!r}")
    return json.loads(text)


def password_login(
    browser: Browser, base_url: str, username: str, password: str
) -> None:
    """allauth headless password login (stands in for the SSO round trip)."""
    status, _, text = browser.request(
        "POST",
        f"{base_url}/api/auth/v1/auth/login",
        body={"username": username, "password": password},
        headers={**JSON, "X-CSRFToken": browser.cookie("csrftoken") or ""},
    )
    step("6", f"allauth headless login as {username!r} -> {status}")
    expect(status == 200, f"login failed: {text[:200]!r}")


def sso_login_through_bridge(
    browser: Browser, bridge_url: str, bridge_html: str, username: str
) -> tuple[str, str]:
    """Submit the bridge's SSO form as its script would, then drive Authentik back to the authorize URL.

    Returns (final url, consent page html)."""
    form = next((f for f in parse_forms(bridge_html) if f["id"] == "sso-form"), None)
    expect(
        form is not None, "the login bridge rendered no SSO form (is SSO configured?)"
    )
    fields = form["fields"]
    step(
        "6",
        f"bridge form: POST {form['action']} provider={fields.get('provider')} process={fields.get('process')} "
        f"callback_url={fields.get('callback_url', '').split('?')[0]}",
    )
    status, headers, text = browser.request(
        "POST",
        absolute(bridge_url, form["action"]),
        form=fields,
        headers={
            "Referer": bridge_url,
            "X-CSRFToken": browser.cookie("csrftoken") or "",
        },
    )
    location = absolute(bridge_url, headers.get("Location", ""))
    step("6", f"provider redirect -> {status} to {location.split('?')[0]}")
    expect(
        status in (301, 302, 303) and headers.get("Location"),
        f"provider redirect failed: {text[:200]!r}",
    )

    kind, url, status, text = browse(
        browser,
        location,
        username,
        PASSWORD,
        stop=lambda target: target.startswith(CALLBACK_ORIGIN),
    )
    step(
        "6",
        f"Authentik as {username!r} -> back on {urllib.parse.urlparse(url).path} ({kind}, {status})",
    )
    expect(
        kind == "page" and status == 200,
        f"SSO round trip ended on {kind} {status}: {text[:200]!r}",
    )
    return url, text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument(
        "--username", default="admin", help="password mode: InvenTree user"
    )
    parser.add_argument(
        "--password", default="admin", help="password mode: InvenTree password"
    )
    parser.add_argument(
        "--sso",
        metavar="USER",
        help="sign in through the login bridge and Authentik as USER",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    mcp_url = f"{base_url}/plugin/inventree-mcp/mcp/"
    agent = Browser()  # the MCP client
    browser = Browser()  # the user's browser

    # 1. Unauthenticated MCP call: 401 with the discovery challenge.
    status, headers, _ = mcp_tools_list(agent, mcp_url, None)
    challenge = headers.get("WWW-Authenticate", "")
    step(
        "1",
        f"POST /mcp/ without token -> {status}; WWW-Authenticate: {challenge[:120]}...",
    )
    expect(status == 401, "expected 401")
    match = re.search(r'resource_metadata="([^"]+)"', challenge)
    expect(
        match is not None,
        "no resource_metadata in the challenge (is OAUTH_ENABLED on?)",
    )

    # 2. Protected-resource metadata.
    status, _, text = agent.request("GET", match.group(1), headers=JSON)
    prm = json.loads(text)
    step(
        "2",
        f"PRM -> {status}; resource={prm['resource']} authorization_servers={prm['authorization_servers']}",
    )
    expect(status == 200, "PRM not served")
    issuer = prm["authorization_servers"][0]

    # 3. Authorization-server metadata, at the RFC 8414 path-insertion URL Claude Code uses
    #    (needs the proxy rewrite on InvenTree 1.5.x), and under the issuer.
    parsed = urllib.parse.urlparse(issuer)
    inserted = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server{parsed.path}"
    status, headers, text = agent.request("GET", inserted, headers=JSON)
    step(
        "3",
        f"AS metadata at {urllib.parse.urlparse(inserted).path} -> {status} {headers.get('Content-Type')}",
    )
    expect(
        status == 200 and json.loads(text).get("issuer") == issuer,
        "path-insertion URL not served (proxy rewrite?)",
    )
    status, _, text = agent.request(
        "GET", f"{issuer}/.well-known/oauth-authorization-server", headers=JSON
    )
    asm = json.loads(text)
    step(
        "3",
        f"AS metadata -> {status}; issuer={asm['issuer']} token_endpoint={asm['token_endpoint']}",
    )
    expect(status == 200 and asm["issuer"] == issuer, "AS metadata issuer mismatch")

    # 4. Dynamic client registration.
    client_id = register(agent, asm)
    step("4", f"register -> 201; client_id=<{len(client_id)} chars>")

    # 5. Authorize: the anonymous browser is sent to the login bridge.
    scope = " ".join(prm["scopes_supported"])
    url, verifier = authorize_url(asm, client_id, scope=scope, resource=prm["resource"])
    status, headers, _ = browser.request("GET", url)
    bridge = absolute(url, headers.get("Location", ""))
    step(
        "5",
        f"authorize (anonymous) -> {status} to {urllib.parse.urlparse(bridge).path}",
    )
    expect(
        status == 302 and "/oauth/login/" in bridge,
        "expected a redirect to the login bridge",
    )

    status, _, bridge_html = browser.request("GET", bridge)
    mode = "sso" if 'id="sso-form"' in bridge_html else "manual"
    step(
        "5",
        f"login bridge -> {status} ({mode} mode), csrftoken cookie set: {bool(browser.cookie('csrftoken'))}",
    )

    # 6. Sign in, 7. consent (loopback redirect: always shown).
    if args.sso:
        expect(
            mode == "sso",
            "the login bridge is in manual mode; --sso needs SSO configured",
        )
        landed, page = sso_login_through_bridge(browser, bridge, bridge_html, args.sso)
        expect(landed == url, f"SSO did not land on the authorize URL: {landed[:120]}")
        status = 200
    else:
        password_login(browser, base_url, args.username, args.password)
        status, _, page = browser.request("GET", url)
    consent_shown = 'name="allow"' in page
    step("7", f"authorize (signed in) -> {status}, consent page shown: {consent_shown}")
    expect(
        status == 200 and consent_shown, f"expected the consent page: {page[:300]!r}"
    )

    # 7-8. Approve (the state must come back), then the token exchange.
    tokens = approve_and_exchange(
        agent, browser, asm, client_id, url, verifier, page, resource=prm["resource"]
    )
    step(
        "8",
        f"approved, code exchanged; token_type={tokens.get('token_type')} expires_in={tokens.get('expires_in')} "
        f"access_token=<{len(tokens.get('access_token', ''))} chars> "
        f"refresh_token=<{len(tokens.get('refresh_token', ''))} chars> scopes={len(tokens.get('scope', '').split())}",
    )

    # 9. tools/list with the access token.
    status, _, payload = mcp_tools_list(agent, mcp_url, tokens["access_token"])
    tools = payload.get("result", {}).get("tools", [])
    step(
        "9",
        f"tools/list -> {status}; {len(tools)} tools, e.g. {[t['name'] for t in tools[:4]]}",
    )
    expect(status == 200 and tools, "tools/list failed")

    # 9b. With a live token, approval_prompt=auto must still show the consent page.
    auto_url, _verifier = authorize_url(
        asm, client_id, scope=scope, resource=prm["resource"], approval_prompt="auto"
    )
    status, headers, _ = browser.request("GET", auto_url)
    step(
        "9b",
        f"authorize with approval_prompt=auto -> {status} (consent shown again: {status == 200})",
    )
    expect(status == 200, f"consent skipped: {headers.get('Location', '')[:40]}")

    # 10. Refresh: rotation, then call again.
    status, refreshed = refresh(
        agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
    )
    rotated = refreshed.get("refresh_token") not in (None, tokens["refresh_token"])
    step(
        "10",
        f"refresh -> {status}; expires_in={refreshed.get('expires_in')} refresh token rotated: {rotated}",
    )
    expect(status == 200 and rotated, "refresh failed")

    status, _, payload = mcp_tools_list(agent, mcp_url, refreshed["access_token"])
    tools = payload.get("result", {}).get("tools", [])
    step("11", f"tools/list with refreshed token -> {status}; {len(tools)} tools")
    expect(status == 200 and tools, "tools/list after refresh failed")

    status, _, _ = mcp_tools_list(agent, mcp_url, tokens["access_token"])
    step(
        "12",
        f"tools/list with the pre-refresh access token -> {status} (revoked by rotation)",
    )
    expect(status == 401, "old access token still works")

    # 13. Revoke the current refresh token: access stops, and the revoked token is refused at once.
    status, _, _ = agent.request(
        "POST",
        asm["revocation_endpoint"],
        form={
            "token": refreshed["refresh_token"],
            "token_type_hint": "refresh_token",
            "client_id": client_id,
        },
    )
    step("13", f"revoke refresh token -> {status}")
    expect(status == 200, "revocation failed")
    status, _, _ = mcp_tools_list(agent, mcp_url, refreshed["access_token"])
    step("13", f"tools/list with the revoked pair's access token -> {status}")
    expect(status == 401, "access token still works after revocation")
    status, body = refresh(
        agent, asm["token_endpoint"], client_id, refreshed["refresh_token"]
    )
    step(
        "14",
        f"refresh with the just-revoked refresh token -> {status} {body.get('error')}",
    )
    expect(
        status == 400 and body.get("error") == "invalid_grant",
        "revoked refresh token was accepted",
    )

    print(
        f"OK: OAuth façade flow completed ({'SSO as ' + args.sso if args.sso else 'password login'})"
    )


if __name__ == "__main__":
    main()
