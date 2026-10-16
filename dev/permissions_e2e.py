#!/usr/bin/env python3
"""End-to-end check of the MCP permission set against the running dev instance (WP3 acceptance).

  0  as admin (session, through the plugin API): "MCP Users" gets MCP view on parts only
     (clear, then PATCH the part row); the effective document of e2e-member
  A  e2e-member connects through the façade (login bridge -> Authentik -> consent -> token):
     the token carries g:read r:view:part; tools/list lists the part tools and no stock tool;
     a stock tool call (get_stock) fails with the permission error; list_parts {"limit": 1}
     succeeds with a count and a row, and get_part on that row succeeds
  B  e2e-member's InvenTree API token: /api/stock/ still answers 200 (interactive roles untouched),
     on /mcp it sees exactly the OAuth tool list, and list_parts / get_part succeed as in A
  C  the plugin API in the server process: rulesets (all groups), effective, connections (?user=),
     status all answer 200 (no query goes through a reverse relation from a core model)
  D  admin clears the flag: the live refresh token is revoked at once (refresh -> invalid_grant,
     the access token -> 401), the API token gets 403 "No MCP permission" on /mcp, /api/stock/ still 200

Usage: python3 dev/permissions_e2e.py [--base-url http://localhost:8080]

Switches MCP_FILTER_TOOL_LIST on for the run. Restores that setting, the MCP rulesets of "MCP Users" and deletes the API token at the end. Reuses dev/oauth_smoke.py's
and dev/membership_e2e.py's helpers. Tokens are never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEV = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV))
sys.path.insert(0, str(DEV / "authentik"))
from membership_e2e import (
    PRELUDE,
    USER,
    connect,
    create_api_token,
    delete_api_tokens,
    shell,
    state,
)
from oauth_smoke import JSON, expect, mcp_tools_list, password_login, refresh, step
from sso_smoke import Browser

GROUP = "MCP Users"
FLAGS = ("can_view", "can_add", "can_change", "can_delete")
STOCK_TOOLS = {
    "get_stock",
    "get_stock_item",
    "search_stock_locations",
    "list_stock_locations",
    "get_stock_location",
}


class Admin:
    """The admin's browser session against the plugin API (session auth with CSRF, as the web UI)."""

    def __init__(self, base_url: str):
        self.base = base_url
        self.browser = Browser()
        self.browser.request(
            "GET", f"{base_url}/api/auth/v1/config"
        )  # csrftoken cookie
        password_login(self.browser, base_url, "admin", "admin")

    def call(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, object]:
        headers = {**JSON, "X-CSRFToken": self.browser.cookie("csrftoken") or ""}
        if method != "GET":
            headers["Referer"] = f"{self.base}/web/"
        status, _, text = self.browser.request(
            method, f"{self.base}{path}", body=body, headers=headers
        )
        try:
            return status, json.loads(text or "null")
        except ValueError:
            return status, text[:200]

    def api(self, method: str, path: str, body: dict | None = None) -> object:
        status, data = self.call(method, f"/plugin/inventree-mcp/api/{path}", body)
        expect(status in (200, 204), f"{method} api/{path} -> {status} {data!r}")
        return data


def group_pk(admin: Admin) -> int:
    _status, groups = admin.call("GET", "/api/user/group/")
    groups = groups["results"] if isinstance(groups, dict) else groups
    match = next((g["pk"] for g in groups if g["name"] == GROUP), None)
    expect(match is not None, f"group {GROUP!r} not found")
    return match


def member_pk(admin: Admin, group: int) -> int:
    _status, detail = admin.call("GET", f"/api/user/group/{group}/?user_detail=true")
    match = next(
        (u["pk"] for u in detail.get("users", []) if u["username"] == USER), None
    )
    expect(
        match is not None,
        f"{USER} is not in {GROUP!r} (run dev/membership_e2e.py's restore?)",
    )
    return match


def mcp_with_api_token(
    agent: Browser, base_url: str, api_token: str
) -> tuple[int, dict]:
    status, _, text = agent.request(
        "POST",
        f"{base_url}/plugin/inventree-mcp/mcp/",
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Token {api_token}",
        },
    )
    try:
        return status, json.loads(text or "{}")
    except ValueError:
        return status, {}


def tool_names(payload: dict) -> set[str]:
    return {tool["name"] for tool in payload.get("result", {}).get("tools", [])}


def call_tool(
    agent: Browser,
    base_url: str,
    authorization: str,
    name: str,
    arguments: dict | None = None,
) -> dict:
    """tools/call with *authorization* ("Bearer ..." / "Token ..."); the JSON-RPC result."""
    _status, _, text = agent.request(
        "POST",
        f"{base_url}/plugin/inventree-mcp/mcp/",
        body={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": {"limit": 1} if arguments is None else arguments,
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": authorization,
        },
    )
    return json.loads(text or "{}").get("result", {})


def tool_body(result: dict) -> dict:
    """A tool result's structured content (or its JSON text)."""
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    try:
        return json.loads((result.get("content") or [{}])[0].get("text", "{}"))
    except ValueError:
        return {}


def real_tool_calls(
    agent: Browser, base_url: str, authorization: str, label: str, who: str
) -> None:
    """list_parts {"limit": 1} and get_part on its row must succeed (list + detail call)."""
    result = call_tool(agent, base_url, authorization, "list_parts", {"limit": 1})
    message = (result.get("content") or [{}])[0].get("text", "")
    body = tool_body(result)
    rows = body.get("results") or []
    step(
        label,
        f"{who} tools/call list_parts {{'limit': 1}} -> isError={bool(result.get('isError'))}, "
        f"count={body.get('count')}, rows={len(rows)}",
    )
    expect(
        not result.get("isError") and len(rows) >= 1,
        f"list_parts failed: {message[:200]!r}",
    )
    pk = rows[0]["pk"]
    result = call_tool(agent, base_url, authorization, "get_part", {"id": pk})
    body = tool_body(result)
    step(
        label,
        f"{who} tools/call get_part {{'id': {pk}}} -> isError={bool(result.get('isError'))}, "
        f"pk={body.get('pk')}",
    )
    expect(
        not result.get("isError") and body.get("pk") == pk,
        f"get_part failed: {(result.get('content') or [{}])[0].get('text', '')[:200]!r}",
    )


def stock_with_api_token(
    agent: Browser, base_url: str, api_token: str
) -> tuple[int, int | None]:
    status, _, text = agent.request(
        "GET",
        f"{base_url}/api/stock/?limit=1",
        headers={**JSON, "Authorization": f"Token {api_token}"},
    )
    try:
        return status, json.loads(text).get("count")
    except (ValueError, AttributeError):
        return status, None


def set_tool_filter(value: bool) -> bool:
    """MCP_FILTER_TOOL_LIST (off by default since 0.4.2; the tool list checks need it on); returns the old value."""
    return shell(
        PRELUDE
        + f"""
previous = plugin.get_setting('MCP_FILTER_TOOL_LIST')
plugin.set_setting('MCP_FILTER_TOOL_LIST', {value})
print('E2E ' + json.dumps(previous))
"""
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    admin = Admin(base_url)
    group = group_pk(admin)
    rows = admin.api("GET", f"rulesets/?group={group}")
    snapshot = {row["pk"]: {flag: row[flag] for flag in FLAGS} for row in rows}
    step(
        "0",
        f"{GROUP!r} MCP roles before: {sorted(r['name'] for r in rows if r['can_view'])}",
    )
    filter_before = set_tool_filter(True)
    step("0", f"MCP_FILTER_TOOL_LIST on (was {filter_before})")
    try:
        run(admin, base_url, group)
    finally:
        set_tool_filter(filter_before)
        for pk, flags in snapshot.items():
            admin.api("PATCH", f"rulesets/{pk}/", flags)
        delete_api_tokens()
        step("-", f"restored the MCP roles of {GROUP!r} and deleted the API token")
    print("OK: MCP permission set end to end")


def run(admin: Admin, base_url: str, group: int) -> None:
    # 0: MCP view on parts only, through the plugin API as the web UI would.
    admin.api("POST", "rulesets/clear/", {"group": group})
    rows = {row["name"]: row for row in admin.api("GET", f"rulesets/?group={group}")}
    admin.api("PATCH", f"rulesets/{rows['part']['pk']}/", {"can_view": True})
    rows = admin.api("GET", f"rulesets/?group={group}")
    granted = [
        (r["name"], [f[4:] for f in FLAGS if r[f]]) for r in rows if r["can_view"]
    ]
    step("0", f"{GROUP!r} MCP roles now: {granted}")
    expect(granted == [("part", ["view"])], "expected MCP view on parts only")

    member = member_pk(admin, group)
    doc = admin.api("GET", f"effective/{member}/")
    interactive = sorted(name for name, f in doc["roles"].items() if f["view"])
    effective = sorted(name for name, f in doc["effective"].items() if f["view"])
    step(
        "0",
        f"effective document of {USER}: interactive view on {len(interactive)} roles, "
        f"effective {effective}, scopes={doc['scopes']}, may_connect={doc['may_connect']}",
    )
    expect(
        doc["scopes"] == ["g:read", "r:view:part"] and "stock" in interactive,
        "unexpected document",
    )

    # A: connect through the façade as e2e-member.
    agent, asm, client_id, tokens = connect(base_url, "A")
    step("A", f"token scope: {tokens['scope']!r}")
    expect(
        tokens["scope"] == "g:read r:view:part",
        "token scope not narrowed to the MCP roles",
    )
    status, _, payload = mcp_tools_list(
        agent, f"{base_url}/plugin/inventree-mcp/mcp/", tokens["access_token"]
    )
    oauth_tools = tool_names(payload)
    part_tools = sorted(name for name in oauth_tools if "part" in name)
    step(
        "A",
        f"tools/list -> {status}; {len(oauth_tools)} tools; part tools {part_tools}; "
        f"stock tools {sorted(oauth_tools & STOCK_TOOLS)}",
    )
    expect(
        status == 200 and "list_parts" in oauth_tools and not oauth_tools & STOCK_TOOLS,
        "tools/list does not reflect MCP view on parts only",
    )
    bearer = f"Bearer {tokens['access_token']}"
    result = call_tool(agent, base_url, bearer, "get_stock")
    message = (result.get("content") or [{}])[0].get("text", "")
    step(
        "A",
        f"tools/call get_stock -> isError={result.get('isError')} {message!r}",
    )
    expect(
        result.get("isError") and "permission" in message, "stock tool call not refused"
    )
    # A list call and a detail call.
    real_tool_calls(agent, base_url, bearer, "A", "OAuth token:")

    # B: the interactive API is untouched; the API token on /mcp is narrowed the same way.
    api_token = create_api_token()
    status, count = stock_with_api_token(agent, base_url, api_token)
    step("B", f"{USER}'s API token on /api/stock/ -> {status} (count={count})")
    expect(status == 200, "the interactive API lost stock access")
    status, payload = mcp_with_api_token(agent, base_url, api_token)
    token_tools = tool_names(payload)
    step(
        "B",
        f"{USER}'s API token on /mcp tools/list -> {status}; {len(token_tools)} tools; "
        f"identical to the OAuth list: {token_tools == oauth_tools}",
    )
    expect(
        status == 200 and token_tools == oauth_tools,
        "API token not narrowed like OAuth",
    )
    real_tool_calls(agent, base_url, f"Token {api_token}", "B", "API token:")

    # C: every plugin API endpoint answers from the server process.
    checks = [
        ("rulesets/", "GET"),
        (f"rulesets/?group={group}", "GET"),
        (f"effective/{member}/", "GET"),
        (f"connections/?user={member}", "GET"),
        (f"status/?user={member}", "GET"),
    ]
    results = []
    for path, method in checks:
        code, _data = admin.call(method, f"/plugin/inventree-mcp/api/{path}")
        results.append(f"{path.split('?')[0]} {code}")
        expect(code == 200, f"api/{path} -> {code}")
    _code, connections = admin.call(
        "GET", f"/plugin/inventree-mcp/api/connections/?user={member}"
    )
    step(
        "C",
        f"plugin API in the server process: {', '.join(results)}; live connections of {USER}: {len(connections)}",
    )

    # D: clear the flag: revoked at once.
    before = state()
    admin.api(
        "PATCH",
        f"rulesets/{rows_by_name(admin, group)['part']['pk']}/",
        {"can_view": False},
    )
    after = state()
    step(
        "D",
        f"cleared MCP view on parts: live MCP refresh tokens of {USER} {before['live_refresh_tokens']} -> "
        f"{after['live_refresh_tokens']}",
    )
    expect(
        before["live_refresh_tokens"] >= 1 and after["live_refresh_tokens"] == 0,
        "tokens not revoked",
    )
    status, body = refresh(
        agent, asm["token_endpoint"], client_id, tokens["refresh_token"]
    )
    step("D", f"refresh with the live refresh token -> {status} {body.get('error')}")
    expect(
        status == 400 and body.get("error") == "invalid_grant",
        "refresh token still works",
    )
    status, _, _ = mcp_tools_list(
        agent, f"{base_url}/plugin/inventree-mcp/mcp/", tokens["access_token"]
    )
    step("D", f"tools/list with the access token -> {status}")
    expect(status == 401, "access token still works")
    status, payload = mcp_with_api_token(agent, base_url, api_token)
    message = payload.get("error", {}).get("message")
    step("D", f"API token on /mcp -> {status} {message!r}")
    expect(status == 403 and message == "No MCP permission", "API token not refused")
    status, _count = stock_with_api_token(agent, base_url, api_token)
    step("D", f"API token on /api/stock/ -> {status}")
    expect(status == 200, "the interactive API was affected")
    doc = admin.api("GET", f"effective/{member}/")
    step(
        "D",
        f"effective document: may_connect={doc['may_connect']} reason={doc['reason']!r}",
    )


def rows_by_name(admin: Admin, group: int) -> dict[str, dict]:
    return {row["name"]: row for row in admin.api("GET", f"rulesets/?group={group}")}


if __name__ == "__main__":
    main()
