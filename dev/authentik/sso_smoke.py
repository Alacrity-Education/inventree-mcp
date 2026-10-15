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
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEV = HERE.parent
INVENTREE = os.environ.get("INVENTREE_URL", "http://localhost:8080")
AUTHENTIK = os.environ.get("AUTHENTIK_PUBLIC_URL", "http://authentik.localtest.me:19000")
PASSWORD = "dev-password-123"
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


def browse(browser: Browser, url: str, username: str, password: str):
    """Follows redirects across InvenTree and Authentik (running Authentik flows) until a non-redirect response or the
    final callback target. Returns (kind, url, status, text)."""
    for _ in range(30):
        if url.startswith(AUTHENTIK) and "/if/flow/" in url:
            outcome, target = run_flow(browser, url, username, password)
            if outcome == "denied":
                return "authentik-denied", url, 403, target
            url = target
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


def inventree_state(username: str) -> dict:
    """Groups and stored upstream refresh token of the InvenTree user (read through the Django shell)."""
    code = (
        "import json\n"
        "from django.contrib.auth import get_user_model\n"
        "from allauth.socialaccount.models import SocialToken\n"
        f"u = get_user_model().objects.filter(username={username!r}).first()\n"
        "print('STATE ' + json.dumps(None if u is None else {\n"
        "  'groups': sorted(u.groups.values_list('name', flat=True)),\n"
        "  'refresh_token': any(bool(t.token_secret) for t in SocialToken.objects.filter(account__user=u))}))\n")
    out = subprocess.run(
        ["docker", "compose", "-f", str(DEV / "compose.yaml"), "--project-directory", str(DEV), "exec", "-T",
         "inventree-server", "python", "src/backend/InvenTree/manage.py", "shell"],
        input=code, capture_output=True, text=True, check=True,
        env={**os.environ, "PG_PASS": "unset", "AUTHENTIK_SECRET_KEY": "unset"}).stdout
    line = next(line for line in out.splitlines() if line.startswith("STATE "))
    return json.loads(line[6:])


def scenario(username: str) -> None:
    browser = Browser()
    try:
        authorize = start_login(browser)
    except RuntimeError as e:
        check(f"{username}: allauth login start", False, str(e))
        return
    check(f"{username}: InvenTree redirects into Authentik", authorize.startswith(AUTHENTIK), authorize[:80])
    kind, url, status, text = browse(browser, authorize, username, PASSWORD)
    if username == "e2e-nonmember":
        check("e2e-nonmember: refused by Authentik", kind == "authentik-denied", f"{kind} {status}")
        state = inventree_state(username)
        check("e2e-nonmember: no InvenTree user created", state is None, str(state))
        return
    if not check(f"{username}: round trip ends back on InvenTree", kind == "page" and url.startswith(INVENTREE),
                 f"{kind} {status} {url[:100]}"):
        print(text[:500])
        return
    status, _, text = browser.request("GET", f"{INVENTREE}/api/user/me/", headers={"Accept": "application/json"})
    me = json.loads(text) if status == 200 else {}
    check(f"{username}: GET /api/user/me/ with the session cookie", status == 200 and me.get("username") == username,
          f"{status} username={me.get('username')}")
    state = inventree_state(username)
    expected = {"e2e-member": ["MCP Users", "Users"], "e2e-interactive": ["Users"], "e2e-guest": []}[username]
    check(f"{username}: InvenTree groups == {expected}", state is not None and state["groups"] == expected,
          str(state and state["groups"]))
    if username == "e2e-member":
        check("e2e-member: refresh token stored in socialaccount_socialtoken",
              state is not None and state["refresh_token"] is True)


if __name__ == "__main__":
    for name in sys.argv[1:] or ["e2e-member", "e2e-interactive", "e2e-guest", "e2e-nonmember"]:
        scenario(name)
    sys.exit(1 if FAILED else 0)
