#!/usr/bin/env python3
"""Smoke test of the OAuth façade against the running dev instance (WP1 acceptance 2).

Drives the flow an MCP client and a browser would:

  401 + challenge -> protected-resource metadata -> AS metadata -> register (DCR)
  -> authorize (InvenTree session) -> consent approve -> token -> tools/list
  -> refresh -> tools/list

The browser session is obtained with allauth's headless password login
(POST /api/auth/v1/auth/login) for the admin account, standing in for the SSO
round trip. Prerequisite: the plugin setting OAUTH_ENABLED is on.

Usage: dev/oauth_smoke.py [--base-url http://localhost:8080] [--username admin] [--password admin]

Standard library only. Tokens are never printed, only their length.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

REDIRECT_URI = "http://127.0.0.1:43123/callback"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses instead of following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HiddenInputs(HTMLParser):
    """Collect the hidden <input> fields of the consent form."""

    def __init__(self):
        super().__init__()
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") == "hidden" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value") or ""


class Session:
    """A cookie-keeping HTTP client that does not follow redirects."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies), NoRedirect()
        )

    def request(self, method, url, *, data=None, json_body=None, headers=None):
        """Return (status, headers, body bytes); *url* may be relative to base_url."""
        if url.startswith("/"):
            url = self.base_url + url
        headers = dict(headers or {})
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def cookie(self, name: str) -> str | None:
        return next((c.value for c in self.cookies if c.name == name), None)


def step(number: int, text: str) -> None:
    print(f"[{number}] {text}")


def fail(text: str) -> None:
    print(f"FAILED: {text}")
    sys.exit(1)


def expect(condition: bool, text: str) -> None:
    if not condition:
        fail(text)


def mcp_tools_list(
    agent: Session, mcp_url: str, token: str | None
) -> tuple[int, dict, dict]:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, resp_headers, body = agent.request(
        "POST",
        mcp_url,
        json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers=headers,
    )
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        payload = {}
    return status, resp_headers, payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    args = parser.parse_args()

    mcp_url = f"{args.base_url}/plugin/inventree-mcp/mcp/"
    agent = Session(args.base_url)  # the MCP client
    browser = Session(args.base_url)  # the user's browser

    # 1. Unauthenticated MCP call: 401 with the discovery challenge.
    status, headers, _ = mcp_tools_list(agent, mcp_url, None)
    challenge = headers.get("WWW-Authenticate", "")
    step(
        1,
        f"POST /mcp/ without token -> {status}; WWW-Authenticate: {challenge[:120]}...",
    )
    expect(status == 401, "expected 401")
    match = re.search(r'resource_metadata="([^"]+)"', challenge)
    expect(
        match is not None,
        "no resource_metadata in the challenge (is OAUTH_ENABLED on?)",
    )

    # 2. Protected-resource metadata.
    status, _, body = agent.request("GET", match.group(1))
    prm = json.loads(body)
    step(
        2,
        f"PRM -> {status}; resource={prm['resource']} authorization_servers={prm['authorization_servers']}",
    )
    expect(status == 200, "PRM not served")
    issuer = prm["authorization_servers"][0]

    # 3. Authorization-server metadata.
    status, _, body = agent.request(
        "GET", f"{issuer}/.well-known/oauth-authorization-server"
    )
    asm = json.loads(body)
    step(
        3,
        f"AS metadata -> {status}; issuer={asm['issuer']} token_endpoint={asm['token_endpoint']}",
    )
    expect(status == 200 and asm["issuer"] == issuer, "AS metadata issuer mismatch")

    # 4. Dynamic client registration.
    status, _, body = agent.request(
        "POST",
        asm["registration_endpoint"],
        json_body={
            "client_name": "oauth_smoke.py",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    registration = json.loads(body)
    step(4, f"register -> {status}; client_id={registration.get('client_id')}")
    expect(status == 201, f"registration failed: {body[:200]!r}")
    client_id = registration["client_id"]

    # 5. Authorize: anonymous browser is sent to the login bridge.
    verifier = secrets.token_urlsafe(48)
    challenge_value = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(8)
    authorize_url = (
        asm["authorization_endpoint"]
        + "?"
        + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge_value,
            "code_challenge_method": "S256",
            "state": state,
            "scope": " ".join(prm["scopes_supported"]),
            "resource": prm["resource"],
        })
    )
    status, headers, _ = browser.request("GET", authorize_url)
    bridge = headers.get("Location", "")
    step(5, f"authorize (anonymous) -> {status} to {bridge.split('?')[0]}")
    expect(
        status == 302 and "/oauth/login/" in bridge,
        "expected a redirect to the login bridge",
    )

    status, _, body = browser.request("GET", bridge)
    mode = "sso" if b'id="sso-form"' in body else "manual"
    step(
        5,
        f"login bridge -> {status} ({mode} mode), csrftoken cookie set: {bool(browser.cookie('csrftoken'))}",
    )

    # 6. Sign in through allauth headless (stands in for the SSO round trip).
    status, _, body = browser.request(
        "POST",
        "/api/auth/v1/auth/login",
        json_body={"username": args.username, "password": args.password},
        headers={"X-CSRFToken": browser.cookie("csrftoken") or ""},
    )
    step(6, f"allauth headless login as {args.username!r} -> {status}")
    expect(status == 200, f"login failed: {body[:200]!r}")

    # 7. Authorize again with the session: consent page (loopback redirect), approve.
    status, _, body = browser.request("GET", authorize_url)
    consent_shown = b'name="allow"' in body
    step(7, f"authorize (signed in) -> {status}, consent page shown: {consent_shown}")
    expect(status == 200, f"expected the consent page: {body[:300]!r}")
    parser_ = HiddenInputs()
    parser_.feed(body.decode())
    form = parser_.fields | {"allow": "Authorize"}
    status, headers, _ = browser.request(
        "POST",
        authorize_url,
        data=form,
        headers={
            "X-CSRFToken": browser.cookie("csrftoken") or "",
            "Referer": authorize_url,
        },
    )
    location = headers.get("Location", "")
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query))
    step(
        7,
        f"approve -> {status} to {location.split('?')[0]} (code: {'code' in params}, state ok: {params.get('state') == state})",
    )
    expect(
        status == 302 and "code" in params and params.get("state") == state,
        "approval failed",
    )

    # 8. Token exchange.
    status, _, body = agent.request(
        "POST",
        asm["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": prm["resource"],
        },
    )
    tokens = json.loads(body)
    step(
        8,
        f"token -> {status}; token_type={tokens.get('token_type')} expires_in={tokens.get('expires_in')} "
        f"access_token=<{len(tokens.get('access_token', ''))} chars> refresh_token=<{len(tokens.get('refresh_token', ''))} chars> "
        f"scopes={len(tokens.get('scope', '').split())}",
    )
    expect(status == 200, f"token exchange failed: {body[:200]!r}")

    # 9. tools/list with the access token.
    status, _, payload = mcp_tools_list(agent, mcp_url, tokens["access_token"])
    tools = payload.get("result", {}).get("tools", [])
    step(
        9,
        f"tools/list -> {status}; {len(tools)} tools, e.g. {[t['name'] for t in tools[:4]]}",
    )
    expect(status == 200 and tools, "tools/list failed")

    # 10. Refresh and call again.
    status, _, body = agent.request(
        "POST",
        asm["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        },
    )
    refreshed = json.loads(body)
    rotated = refreshed.get("refresh_token") not in (None, tokens["refresh_token"])
    step(
        10,
        f"refresh -> {status}; expires_in={refreshed.get('expires_in')} refresh token rotated: {rotated}",
    )
    expect(status == 200 and rotated, f"refresh failed: {body[:200]!r}")

    status, _, payload = mcp_tools_list(agent, mcp_url, refreshed["access_token"])
    tools = payload.get("result", {}).get("tools", [])
    step(11, f"tools/list with refreshed token -> {status}; {len(tools)} tools")
    expect(status == 200 and tools, "tools/list after refresh failed")

    status, _, _ = mcp_tools_list(agent, mcp_url, tokens["access_token"])
    step(
        12,
        f"tools/list with the pre-refresh access token -> {status} (revoked by rotation)",
    )
    expect(status == 401, "old access token still works")

    print("OK: OAuth façade flow completed")


if __name__ == "__main__":
    main()
