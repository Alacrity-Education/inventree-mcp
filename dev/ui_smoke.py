#!/usr/bin/env python3
"""Live check of the plugin pages in a headless browser (needs playwright, see dev/README.md).

    dev/ui_smoke.py [--base-url http://localhost:8080] [--user admin --password admin]

Logs in, opens /web/plugin/inventree-mcp/groups, selects the "MCP Users" group, toggles one MCP flag, saves, checks
through the plugin API that it persisted, restores it, then opens /web/plugin/inventree-mcp/connections and checks the
status card. Screenshots go to docs/plans/screenshots/. Needs an InvenTree with plugin routes (newer than 1.5.7).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

GROUP = "MCP Users"
ROLE_LABEL = "Parts"  # MCP role whose "Delete" flag the check toggles
SHOTS = Path(__file__).resolve().parent.parent / "docs" / "plans" / "screenshots"


class Api:
    """API-token client for the verification reads (independent of the browser session)."""

    def __init__(self, base: str, user: str, password: str):
        self.base = base.rstrip("/")
        basic = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        request = urllib.request.Request(
            self.base + f"/api/user/token/?name=ui-smoke-{int(time.time())}", headers={"Authorization": basic}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            self.auth = "Token " + json.load(response)["token"]

    def get(self, path: str):
        request = urllib.request.Request(
            self.base + path, headers={"Authorization": self.auth}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def post(self, path: str, data: dict):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode(),
            headers={"Authorization": self.auth, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def group_pk(self) -> int:
        groups = self.get("/api/user/group/")
        groups = groups.get("results", groups) if isinstance(groups, dict) else groups
        for group in groups:
            if group["name"] == GROUP:
                return group["pk"]
        return self.post("/api/user/group/", {"name": GROUP})["pk"]

    def flag(self, group: int) -> bool:
        rows = self.get(f"/plugin/inventree-mcp/api/rulesets/?group={group}")
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        return next(r for r in rows if r["label"] == ROLE_LABEL)["can_delete"]


def login(page, base: str, user: str, password: str):
    page.goto(f"{base}/web/login")
    page.locator("input[data-path=username]").fill(user)
    page.locator("input[data-path=password]").fill(password)
    page.get_by_role("button", name="Log In").click()
    page.wait_for_function(
        "!location.pathname.includes('/login')", timeout=60_000
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    SHOTS.mkdir(parents=True, exist_ok=True)

    api = Api(base, args.user, args.password)
    pk = api.group_pk()
    before = api.flag(pk)
    print(f"group {GROUP!r} pk={pk}: {ROLE_LABEL} delete = {before}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        login(page, base, args.user, args.password)

        page.goto(f"{base}/web/plugin/inventree-mcp/groups")
        page.get_by_text(GROUP, exact=True).first.click()
        page.get_by_text("MCP Roles").first.wait_for(timeout=30_000)
        page.screenshot(path=str(SHOTS / "groups-1-selected.png"))

        # the second grid ("MCP Roles") is the plugin one: open its accordion, toggle the flag, save
        grid = page.locator("table").filter(has_text=ROLE_LABEL)
        mcp_grid = grid.last
        row = mcp_grid.locator("tr").filter(has_text=ROLE_LABEL).first
        row.get_by_role("checkbox").nth(3).click()  # View, Change, Add, Delete
        page.screenshot(path=str(SHOTS / "groups-2-edited.png"))
        page.get_by_role("button", name="Save").last.click()
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "groups-3-saved.png"))

        after = api.flag(pk)
        print(f"after save: {ROLE_LABEL} delete = {after}")
        assert after != before, "the toggled flag was not persisted"

        # restore (through the UI's own API call, then confirm)
        rows = api.get(f"/plugin/inventree-mcp/api/rulesets/?group={pk}")
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        row_pk = next(r for r in rows if r["label"] == ROLE_LABEL)["pk"]
        request = urllib.request.Request(
            f"{base}/plugin/inventree-mcp/api/rulesets/{row_pk}/",
            data=json.dumps({"can_delete": before}).encode(),
            method="PATCH",
            headers={"Authorization": api.auth, "Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=30).read()
        assert api.flag(pk) == before, "restore failed"
        print("restored")

        page.goto(f"{base}/web/plugin/inventree-mcp/connections")
        expect(page.get_by_text("MCP OAuth")).to_be_visible(timeout=30_000)
        expect(page.get_by_text("You can connect")).to_be_visible()
        page.screenshot(path=str(SHOTS / "connections.png"), full_page=True)
        print("connections page: status card rendered")
        browser.close()
    print("ui_smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
