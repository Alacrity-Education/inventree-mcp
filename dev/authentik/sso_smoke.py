#!/usr/bin/env python3
"""SSO round-trip smoke test against the dev stack (standard library only; needs the stack from dev/up.sh).

Adapted from KINA's kina_authentik_e2e.py (Browser, run_flow). For each scenario it starts the allauth headless
login (POST /api/auth/v1/auth/provider/redirect), follows the redirect into Authentik, drives the identification and
password stages, follows back into InvenTree and finally calls GET /api/user/me/ with the session cookie.

  e2e-member     -> logs in, InvenTree groups Users + MCP Users, a refresh token is stored (socialaccount_socialtoken)
  e2e-interactive-> logs in, group Users only
  e2e-guest      -> logs in, no InvenTree group
  e2e-nonmember  -> refused by Authentik

Usage: python3 dev/authentik/sso_smoke.py [username ...]   (default: all four scenarios). Exit code 1 on failure.

Importable by other scripts (put dev/authentik on sys.path): Browser, browse(..., stop=...), sso_login(username) and
SsoRefused. browse(stop=callable) returns ("stopped", url, status, "") as soon as the next redirect target matches,
so an OAuth client flow can stop at its own redirect_uri instead of fetching it.
"""
from __future__ import annotations

import ast
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

# the shared constants
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootstrap import DEV_PASSWORD, USERS, public_url

HERE = Path(__file__).resolve().parent
DEV = HERE.parent
INVENTREE = os.environ.get("INVENTREE_URL", "http://localhost:8080")
AUTHENTIK = public_url()
PASSWORD = DEV_PASSWORD
PROVIDER = "authentik"
TIMEOUT = 30
FAILED = False


def check(name: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    FAILED = FAILED or not ok
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
    return ok


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Browser:
    """Cookie-keeping HTTP client that never follows redirects on its own."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def request(self, method: str, url: str, form=None, body=None, headers=None):
        data, all_headers = None, {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            all_headers["Content-Type"] = "application/x-www-form-urlencoded"
        if body is not None:
            data = json.dumps(body).encode()
            all_headers["Content-Type"] = "application/json"
        all_headers.update(headers or {})
        request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                return response.status, response.headers, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")

    def cookie(self, name: str):
        return next((c.value for c in self.jar if c.name == name), None)


def absolute(base_url: str, location: str) -> str:
    return urllib.parse.urljoin(base_url, location)


def run_flow(browser: Browser, flow_url: str, username: str, password: str) -> tuple[str, str]:
    """Drives an Authentik flow (/if/flow/<slug>/?...) through the flow executor API.
    Returns ("redirect", url) or ("denied", message)."""
    parsed = urllib.parse.urlparse(flow_url)
    slug = parsed.path.rstrip("/").split("/")[-1]
    executor = f"{AUTHENTIK}/api/v3/flows/executor/{slug}/?" + urllib.parse.urlencode({"query": parsed.query})
    status, headers, text = browser.request("GET", executor, headers={"Accept": "application/json"})
    for _ in range(12):
        if status in (301, 302):
            status, headers, text = browser.request("GET", absolute(executor, headers["Location"]),
                                                    headers={"Accept": "application/json"})
            continue
        challenge = json.loads(text)
        component = challenge.get("component")
        if component == "xak-flow-redirect":
            return "redirect", absolute(AUTHENTIK, challenge["to"])
        if component == "ak-stage-access-denied":
            return "denied", challenge.get("error_message") or "access denied"
        if component == "ak-stage-identification":
            answer = {"component": component, "uid_field": username}
            if challenge.get("password_fields"):
                answer["password"] = password
        elif component == "ak-stage-password":
            answer = {"component": component, "password": password}
        elif component == "ak-stage-consent":
            answer = {"component": component, "token": challenge.get("token", "")}
        else:
            raise RuntimeError(f"unexpected Authentik stage {component}: {text[:300]}")
        csrf = browser.cookie("authentik_csrf")
        status, headers, text = browser.request(
            "POST", executor, body=answer,
            headers={"Accept": "application/json", **({"X-authentik-CSRF": csrf} if csrf else {})})
    raise RuntimeError("Authentik flow did not finish")


def browse(browser: Browser, url: str, username: str, password: str, stop: Callable[[str], bool] | None = None):
    """Follows redirects across InvenTree and Authentik (running Authentik flows) until a non-redirect response.
    Returns (kind, url, status, text); kind is "page", "authentik-denied" or, when stop(url) is true for the next
    redirect target, "stopped" (the target is not fetched; status is that of the redirecting response)."""
    status = 0
    for _ in range(30):
        if stop is not None and stop(url):
            return "stopped", url, status, ""
        if url.startswith(AUTHENTIK) and "/if/flow/" in url:
            outcome, target = run_flow(browser, url, username, password)
            if outcome == "denied":
                return "authentik-denied", url, 403, target
            url, status = target, 302
            continue
        status, headers, text = browser.request("GET", url)
        if status in (301, 302, 303, 307):
            url = absolute(url, headers["Location"])
            continue
        if url.startswith(AUTHENTIK) and "Permission denied" in text:
            # The application's policy binding refused the signed-in user (rendered page, not a flow stage).
            return "authentik-denied", url, status, "Permission denied"
        return "page", url, status, text
    raise RuntimeError("too many redirects")


def start_login(browser: Browser) -> str:
    """allauth headless (browser client): POST the provider redirect with the CSRF cookie; returns the Authentik URL."""
    status, _, _ = browser.request("GET", f"{INVENTREE}/api/auth/v1/config", headers={"Accept": "application/json"})
    if status != 200:
        raise RuntimeError(f"/api/auth/v1/config -> {status}")
    csrf = browser.cookie("csrftoken")
    if not csrf:  # allauth sets the cookie on the session endpoint
        browser.request("GET", f"{INVENTREE}/api/auth/v1/auth/session", headers={"Accept": "application/json"})
        csrf = browser.cookie("csrftoken")
    if not csrf:
        raise RuntimeError("no csrftoken cookie from InvenTree")
    status, headers, text = browser.request(
        "POST", f"{INVENTREE}/api/auth/v1/auth/provider/redirect",
        form={"provider": PROVIDER, "process": "login", "callback_url": f"{INVENTREE}/web/logged-in",
              "csrfmiddlewaretoken": csrf},
        headers={"Referer": INVENTREE + "/", "X-CSRFToken": csrf})
    if status not in (301, 302, 303) or not headers.get("Location"):
        raise RuntimeError(f"provider redirect -> {status}: {text[:300]}")
    return absolute(INVENTREE, headers["Location"])


class SsoRefused(RuntimeError):
    """Authentik refused the user, or the round trip did not end on InvenTree's logged-in page."""


def sso_login(username: str, password: str = PASSWORD, browser: Browser | None = None) -> Browser:
    """Full SSO login through allauth headless and Authentik. Returns the Browser holding the InvenTree session
    cookie; raises SsoRefused when Authentik refuses the user or the final page is not /web/logged-in with 200."""
    browser = browser or Browser()
    authorize = start_login(browser)
    if not authorize.startswith(AUTHENTIK):
        raise SsoRefused(f"{username}: InvenTree did not redirect into Authentik: {authorize[:100]}")
    kind, url, status, text = browse(browser, authorize, username, password)
    if kind == "authentik-denied":
        raise SsoRefused(f"{username}: refused by Authentik ({status}: {text[:200]})")
    if kind != "page" or status != 200 or urllib.parse.urlparse(url).path != "/web/logged-in":
        raise SsoRefused(f"{username}: round trip ended on {kind} {status} {url[:100]}: {text[:300]}")
    return browser


def inventree_state(username: str) -> dict:
    """Groups and stored upstream refresh token of the InvenTree user (read through dev/shell.sh)."""
    code = (
        "import json\n"
        "from django.contrib.auth import get_user_model\n"
        "from allauth.socialaccount.models import SocialToken\n"
        f"u = get_user_model().objects.filter(username={username!r}).first()\n"
        "print('STATE ' + json.dumps(None if u is None else {\n"
        "  'groups': sorted(u.groups.values_list('name', flat=True)),\n"
        "  'refresh_token': any(bool(t.token_secret) for t in SocialToken.objects.filter(account__user=u))}))\n")
    out = subprocess.run([str(DEV / "shell.sh"), "-c", code], capture_output=True, text=True, check=True).stdout
    line = next(line for line in out.splitlines() if line.startswith("STATE "))
    return json.loads(line[6:])


def expected_groups(username: str) -> list[str]:
    """InvenTree groups a user ends up in: bootstrap.USERS (Authentik groups) through SSO_GROUP_MAP of
    configure_inventree.py (read as a literal, that file needs Django and cannot be imported here)."""
    tree = ast.parse((DEV / "configure_inventree.py").read_text())
    mapping = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "SSO_GROUP_MAP" for t in node.targets))
    return sorted(mapping[g] for g in USERS[username] if g in mapping)


def scenario(username: str) -> None:
    try:
        browser = sso_login(username)
    except SsoRefused as e:
        if username == "e2e-nonmember":
            check("e2e-nonmember: refused by Authentik", "refused by Authentik" in str(e), str(e)[:120])
            state = inventree_state(username)
            check("e2e-nonmember: no InvenTree user created", state is None, str(state))
        else:
            check(f"{username}: SSO round trip ends on /web/logged-in with 200", False, str(e))
        return
    except RuntimeError as e:
        check(f"{username}: allauth login start", False, str(e))
        return
    if username == "e2e-nonmember":
        check("e2e-nonmember: refused by Authentik", False, "login succeeded")
        return
    check(f"{username}: SSO round trip ends on /web/logged-in with 200", True)
    status, _, text = browser.request("GET", f"{INVENTREE}/api/user/me/", headers={"Accept": "application/json"})
    me = json.loads(text) if status == 200 else {}
    check(f"{username}: GET /api/user/me/ with the session cookie", status == 200 and me.get("username") == username,
          f"{status} username={me.get('username')}")
    state = inventree_state(username)
    expected = expected_groups(username)
    check(f"{username}: InvenTree groups == {expected}", state is not None and state["groups"] == expected,
          str(state and state["groups"]))
    if username == "e2e-member":
        check("e2e-member: refresh token stored in socialaccount_socialtoken",
              state is not None and state["refresh_token"] is True)


if __name__ == "__main__":
    for name in sys.argv[1:] or list(USERS):
        scenario(name)
    sys.exit(1 if FAILED else 0)
