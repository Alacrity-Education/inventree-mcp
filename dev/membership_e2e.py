#!/usr/bin/env python3
"""End-to-end membership re-check against the real dev Authentik (WP2 acceptance).

Scenarios (adapted from KINA's kina_authentik_e2e.py), all as e2e-member:

  A  connect through the façade (login bridge -> Authentik -> consent -> token) and call a tool
  B  age membership_checked_at, refresh: a real re-check redeems the upstream refresh token
     (rotated on the SocialToken row) and the refresh succeeds
  C  stop authentik-server: within the grace period the refresh still succeeds; past it the refresh
     gets invalid_grant, nothing is revoked; start Authentik again and the same refresh token works
  D  remove e2e-member from inventree-mcp in Authentik, age, refresh: invalid_grant, the user is
     blocked (not_in_required_groups), the upstream token is deleted, an API token on /mcp gets 401
     "Access revoked"
  E  a new SSO login (still outside the group) keeps the block (identity checks fail) but stamps the
     check; after an admin unblock the API token gets 403 "No MCP permission" and authorize refuses
  F  add the user back to inventree-mcp, connect again: works, and so does the API token
  G  long-lived credentials: an API token is refused with 401 once the last re-check found the
     provider unreachable past the grace period (/api/ still works), and works again after the
     background re-check it queued

Usage: python3 dev/membership_e2e.py [--base-url http://localhost:8080] [--skip-outage]

The script sets the plugin settings it needs (OAUTH_ENABLED, MEMBERSHIP_ENFORCED,
MCP_REQUIRED_GROUPS="MCP Users") through dev/shell.sh and restores MCP_REQUIRED_GROUPS, the
Authentik groups and Authentik itself at the end. Reuses dev/oauth_smoke.py's flow helpers and
dev/authentik's Browser, sso_login() and set_user_groups(). Tokens are never printed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEV = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV))
sys.path.insert(0, str(DEV / "authentik"))
from bootstrap import api_from_env, public_url, set_user_groups, user_groups
from oauth_smoke import (
    JSON,
    approve_and_exchange,
    authorize_url,
    expect,
    mcp_tools_list,
    refresh,
    register,
    sso_login_through_bridge,
    step,
)
from sso_smoke import Browser, absolute, sso_login

USER = "e2e-member"
MEMBER_GROUPS = ["inventree-users", "inventree-mcp"]
REQUIRED_GROUP = "MCP Users"


# -- Django state through dev/shell.sh ------------------------------------------------------------


def shell(code: str) -> dict:
    """Run *code* in the server's Django shell; it prints one line 'E2E <json>'."""
    out = subprocess.run(
        [str(DEV / "shell.sh"), "-c", code], capture_output=True, text=True, check=True
    ).stdout
    line = next((line for line in out.splitlines() if line.startswith("E2E ")), None)
    if line is None:
        sys.exit(f"FAILED: dev/shell.sh printed no result:\n{out[-2000:]}")
    return json.loads(line[4:])


PRELUDE = f"""
import json, hashlib
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.utils import timezone
from allauth.socialaccount.models import SocialToken
from oauth2_provider.models import RefreshToken
from plugin import registry
from inventree_mcp.models import MCPUserState
user = get_user_model().objects.get(username={USER!r})
plugin = registry.get_plugin('inventree-mcp')
"""


def configure() -> dict:
    """Switch on what the scenarios need; returns the previous MCP_REQUIRED_GROUPS."""
    return shell(
        PRELUDE
        + f"""
previous = plugin.get_setting('MCP_REQUIRED_GROUPS')
plugin.set_setting('OAUTH_ENABLED', True)
plugin.set_setting('MEMBERSHIP_ENFORCED', True)
plugin.set_setting('MCP_REQUIRED_GROUPS', {REQUIRED_GROUP!r})
print('E2E ' + json.dumps({{'previous_required_groups': previous,
  'interval_min': plugin.get_setting('MEMBERSHIP_RECHECK_INTERVAL_MINUTES'),
  'grace_h': plugin.get_setting('MEMBERSHIP_GRACE_HOURS')}}))
"""
    )


def restore_required_groups(previous: str) -> None:
    shell(
        PRELUDE
        + f"plugin.set_setting('MCP_REQUIRED_GROUPS', {previous!r})\nprint('E2E {{}}')\n"
    )


def state() -> dict:
    """MCPUserState, the upstream token's fingerprint (compared, never printed), groups."""
    return shell(
        PRELUDE
        + """
s = MCPUserState.objects.filter(user=user).first()
t = SocialToken.objects.filter(account__user=user, account__provider='authentik').first()
now = timezone.now()
print('E2E ' + json.dumps({
  'checked_age_s': None if not s or not s.membership_checked_at
                   else round((now - s.membership_checked_at).total_seconds()),
  'blocked': bool(s and s.access_revoked_at),
  'reason': s.revoked_reason if s else '',
  'error': s.last_recheck_error if s else '',
  'upstream': hashlib.sha256(t.token_secret.encode()).hexdigest() if t and t.token_secret else None,
  'groups': sorted(user.groups.values_list('name', flat=True)),
  'live_refresh_tokens': RefreshToken.objects.filter(user=user, revoked__isnull=True).count(),
}))
"""
    )


def age_check(hours: float) -> None:
    """Pretend the last membership check happened *hours* ago."""
    shell(
        PRELUDE
        + f"""
MCPUserState.objects.filter(user=user).update(
    membership_checked_at=timezone.now() - timedelta(hours={hours}))
print('E2E {{}}')
"""
    )


def create_api_token() -> str:
    return shell(
        PRELUDE
        + """
from users.models import ApiToken
token = ApiToken.objects.create(user=user, name='membership-e2e')
print('E2E ' + json.dumps({'key': token.key}))
"""
    )["key"]


def unblock() -> None:
    """The admin's manual unblock (membership.unblock_user)."""
    shell(
        PRELUDE
        + """
from inventree_mcp.auth.membership import unblock_user
unblock_user(user)
print('E2E {}')
"""
    )


def record_unreachable(hours: float) -> None:
    """Pretend the last re-check found the provider unreachable, the last success *hours* ago."""
    shell(
        PRELUDE
        + f"""
MCPUserState.objects.filter(user=user).update(
    membership_checked_at=timezone.now() - timedelta(hours={hours}),
    last_recheck_error='connection_error')
print('E2E {{}}')
"""
    )


def delete_api_tokens() -> None:
    shell(
        PRELUDE
        + """
from users.models import ApiToken
ApiToken.objects.filter(user=user, name='membership-e2e').delete()
print('E2E {}')
"""
    )


def show(label: str, s: dict) -> None:
    step(
        label,
        f"state: checked {s['checked_age_s']} s ago, blocked={s['blocked']} reason={s['reason']!r} "
        f"last_error={s['error']!r} upstream token={'present' if s['upstream'] else 'none'} "
        f"groups={s['groups']} live MCP refresh tokens={s['live_refresh_tokens']}",
    )


# -- OAuth client ---------------------------------------------------------------------------------


def authorization_server(agent: Browser, base_url: str) -> dict:
    status, _, text = agent.request(
        "GET",
        f"{base_url}/plugin/inventree-mcp/.well-known/oauth-authorization-server",
        headers=JSON,
    )
    expect(status == 200, f"AS metadata -> {status} (is OAUTH_ENABLED on?)")
    return json.loads(text)


def connect(base_url: str, label: str) -> tuple[Browser, dict, str, dict]:
    """The façade flow through the login bridge and Authentik; returns (agent, asm, client_id, tokens)."""
    agent, browser = Browser(), Browser()
    asm = authorization_server(agent, base_url)
    client_id = register(agent, asm, "membership_e2e.py")
    url, verifier = authorize_url(asm, client_id)
    status, headers, _ = browser.request("GET", url)
    bridge = absolute(url, headers.get("Location", ""))
    expect(
        status == 302 and "/oauth/login/" in bridge,
        f"expected the login bridge, got {status}",
    )
    _status, _, bridge_html = browser.request("GET", bridge)
    landed, page = sso_login_through_bridge(browser, bridge, bridge_html, USER)
    expect(landed == url, "SSO did not land on the authorize URL")
    tokens = approve_and_exchange(agent, browser, asm, client_id, url, verifier, page)
    step(
        label,
        f"connected through the façade: expires_in={tokens['expires_in']} scopes={len(tokens['scope'].split())}",
    )
    return agent, asm, client_id, tokens


def tools_list(agent: Browser, base_url: str, access_token: str) -> int:
    status, _, _ = mcp_tools_list(
        agent, f"{base_url}/plugin/inventree-mcp/mcp/", access_token
    )
    return status


def tools_list_api_token(
    agent: Browser, base_url: str, api_token: str
) -> tuple[int, str | None, str]:
    """tools/list with an InvenTree API token: (status, JSON-RPC error message, WWW-Authenticate)."""
    status, headers, text = agent.request(
        "POST",
        f"{base_url}/plugin/inventree-mcp/mcp/",
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Token {api_token}",
        },
    )
    try:
        message = json.loads(text or "{}").get("error", {}).get("message")
    except ValueError:
        message = None
    return status, message, headers.get("WWW-Authenticate", "")


# -- Authentik outage -----------------------------------------------------------------------------


def compose(*args: str) -> None:
    subprocess.run([str(DEV / "compose.sh"), *args], check=True, capture_output=True)


def wait_for_authentik(seconds: int = 240) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"{public_url()}/-/health/ready/", timeout=5
            ) as response:
                if response.status in (200, 204):
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    sys.exit("FAILED: Authentik did not come back")


def outage(agent, asm, client_id, tokens, base_url) -> dict:
    step("C", "stopping authentik-server")
    compose("stop", "authentik-server")
    try:
        age_check(2)
        status, body = refresh(
            agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
        )
        s = state()
        step(
            "C",
            f"Authentik down, last check 2 h ago (grace 4 h): refresh -> {status}; last_error={s['error']!r}",
        )
        expect(
            status == 200 and s["error"] and not s["blocked"],
            "refresh within the grace period failed",
        )
        tokens = body

        age_check(5)
        status, body = refresh(
            agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
        )
        s = state()
        step(
            "C",
            f"Authentik down, last check 5 h ago: refresh -> {status} {body.get('error')}; "
            f"blocked={s['blocked']} live MCP refresh tokens={s['live_refresh_tokens']}",
        )
        expect(
            status == 400 and body.get("error") == "invalid_grant",
            "expected invalid_grant past the grace period",
        )
        expect(
            not s["blocked"] and s["live_refresh_tokens"] >= 1 and s["upstream"],
            "something was revoked",
        )
        status = tools_list(agent, base_url, tokens["access_token"])
        step(
            "C",
            f"tools/list with the current access token -> {status} (nothing revoked)",
        )
        expect(status == 200, "access token stopped working")
    finally:
        step("C", "starting authentik-server")
        compose("start", "authentik-server")
        wait_for_authentik()

    for attempt in range(1, 13):
        status, body = refresh(
            agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
        )
        if status == 200:
            break
        time.sleep(5)
    s = state()
    step(
        "C",
        f"Authentik back: the same refresh token -> {status} (attempt {attempt}); checked {s['checked_age_s']} s ago",
    )
    expect(
        status == 200 and s["checked_age_s"] is not None and s["checked_age_s"] < 120,
        "refresh did not recover",
    )
    return body


# -- the scenarios --------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument(
        "--skip-outage", action="store_true", help="skip scenario C (stops Authentik)"
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    api = api_from_env()
    set_user_groups(api, USER, MEMBER_GROUPS)
    settings = configure()
    step(
        "0",
        f"settings: MCP_REQUIRED_GROUPS={REQUIRED_GROUP!r}, re-check interval {settings['interval_min']} min, "
        f"grace {settings['grace_h']} h; Authentik groups of {USER}: {user_groups(api, USER)}",
    )
    try:
        run(api, base_url, skip_outage=args.skip_outage)
    finally:
        set_user_groups(api, USER, MEMBER_GROUPS)
        delete_api_tokens()
        restore_required_groups(settings["previous_required_groups"] or "")
    print("OK: membership re-check end to end")


def run(api, base_url: str, *, skip_outage: bool) -> None:
    # A: connect and call a tool.
    agent, asm, client_id, tokens = connect(base_url, "A")
    status = tools_list(agent, base_url, tokens["access_token"])
    step("A", f"tools/list -> {status}")
    expect(status == 200, "tools/list failed")
    before = state()
    show("A", before)
    expect(
        before["upstream"] and before["checked_age_s"] is not None,
        "the SSO login did not stamp the state",
    )

    # B: a due re-check at refresh time redeems the upstream token for real.
    age_check(2)
    status, body = refresh(
        agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
    )
    after = state()
    step(
        "B",
        f"last check aged to 2 h, refresh -> {status}; upstream refresh token rotated: "
        f"{after['upstream'] not in (None, before['upstream'])}",
    )
    show("B", after)
    expect(
        status == 200 and after["upstream"] not in (None, before["upstream"]),
        "no real re-check happened",
    )
    expect(
        after["checked_age_s"] < 120 and after["error"] == "",
        "membership_checked_at not refreshed",
    )
    tokens = body
    status, body = refresh(
        agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
    )
    step(
        "B",
        f"refresh again within the interval -> {status}; upstream token unchanged: "
        f"{state()['upstream'] == after['upstream']}",
    )
    expect(status == 200, "second refresh failed")
    tokens = body

    # C: provider outage.
    if skip_outage:
        step("C", "skipped (--skip-outage)")
    else:
        tokens = outage(agent, asm, client_id, tokens, base_url)

    # D: removed from the group in Authentik.
    set_user_groups(api, USER, ["inventree-users"])
    step("D", f"Authentik groups of {USER}: {user_groups(api, USER)}")
    api_token = create_api_token()
    age_check(2)
    status, body = refresh(
        agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
    )
    s = state()
    step("D", f"refresh -> {status} {body.get('error')}")
    show("D", s)
    expect(
        status == 400 and body.get("error") == "invalid_grant", "expected invalid_grant"
    )
    expect(s["blocked"] and s["reason"] == "not_in_required_groups", "user not blocked")
    expect(
        s["upstream"] is None and s["live_refresh_tokens"] == 0, "tokens not revoked"
    )
    status = tools_list(agent, base_url, tokens["access_token"])
    step("D", f"tools/list with the last access token -> {status}")
    expect(status == 401, "access token still works")
    status, message, _ = tools_list_api_token(agent, base_url, api_token)
    step(
        "D",
        f"tools/list with an InvenTree API token of the blocked user -> {status} {message!r}",
    )
    expect(
        status == 401 and "Access revoked" in (message or ""),
        "blocked user not refused on /mcp",
    )

    # E: a new SSO login while outside the group keeps the block (identity checks fail).
    age_check(2)
    browser = sso_login(USER)
    s = state()
    show("E", s)
    expect(
        s["blocked"] and s["checked_age_s"] < 120,
        "the SSO login lifted the block or did not stamp",
    )
    url, _verifier = authorize_url(asm, client_id)
    status, _, page = browser.request("GET", url)
    step(
        "E",
        f"SSO login while outside the group: authorize -> {status} ('Access revoked' shown: {'Access revoked' in page})",
    )
    expect(status == 403 and "Access revoked" in page, "authorize did not refuse")
    # An admin lifts the block by hand: the policy still refuses, now with 403 (F1).
    unblock()
    status, message, _ = tools_list_api_token(agent, base_url, api_token)
    step(
        "E",
        f"after an admin unblock: tools/list with the API token -> {status} {message!r}",
    )
    expect(
        status == 403 and message == "No MCP permission",
        "API token outside the required group not refused",
    )
    status, _, page = browser.request("GET", url)
    step(
        "E",
        f"after an admin unblock: authorize -> {status} ('No MCP access' shown: {'No MCP access' in page})",
    )
    expect(status == 403 and "No MCP access" in page, "authorize did not refuse")

    # F: back in the group: a new connection works.
    set_user_groups(api, USER, MEMBER_GROUPS)
    step("F", f"Authentik groups of {USER}: {user_groups(api, USER)}")
    agent, asm, client_id, tokens = connect(base_url, "F")
    status = tools_list(agent, base_url, tokens["access_token"])
    s = state()
    step("F", f"tools/list -> {status}")
    show("F", s)
    expect(
        status == 200 and not s["blocked"] and REQUIRED_GROUP in s["groups"],
        "reconnect failed",
    )
    status, message, _ = tools_list_api_token(agent, base_url, api_token)
    step("F", f"tools/list with the API token -> {status}")
    expect(status == 200, "API token of a member refused")

    # G: long-lived credentials follow the grace window (F7).
    record_unreachable(5)
    status, message, challenge = tools_list_api_token(agent, base_url, api_token)
    step(
        "G",
        f"last re-check unreachable, last success 5 h ago (grace 4 h): API token on /mcp -> {status} "
        f"{message!r}; WWW-Authenticate: {challenge}",
    )
    expect(
        status == 401 and challenge == 'Bearer realm="InvenTree"',
        "API token not refused past grace",
    )
    status, _, _ = agent.request(
        "GET",
        f"{base_url}/api/user/me/",
        headers={**JSON, "Authorization": f"Token {api_token}"},
    )
    step("G", f"the same API token on /api/user/me/ -> {status}")
    expect(status == 200, "the interactive API was affected")
    for attempt in range(1, 21):
        time.sleep(3)
        status, _, _ = tools_list_api_token(agent, base_url, api_token)
        if status == 200:
            break
    s = state()
    step(
        "G",
        f"after the queued background re-check: API token on /mcp -> {status} (attempt {attempt}); "
        f"checked {s['checked_age_s']} s ago, last_error={s['error']!r}",
    )
    expect(
        status == 200 and s["error"] == "",
        "the background re-check did not restore access",
    )


if __name__ == "__main__":
    main()
