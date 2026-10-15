#!/usr/bin/env python3
"""Live check of the plugin pages in a headless browser (needs playwright, see dev/README.md).

    dev/ui_smoke.py [--base-url http://localhost:8080] [--user admin --password admin] [--screenshots]

Creates a scratch group ("ui-smoke-<timestamp>", no members), so no real group, role or member token is touched:

1. through the plugin API: the scratch group has MCP rulesets and the "Parts: Delete" flag is off;
2. in the browser: logs in, opens /web/plugin/inventree-mcp/groups, selects the scratch group, toggles the flag in the
   "MCP Roles" grid and saves; then reads it back through the plugin API;
3. opens /web/plugin/inventree-mcp/connections and checks the status card.

The scratch group (with its rulesets) and the admin API token the script created are removed in a ``finally`` block.
The token is revoked through DELETE /api/user/tokens/<id>/ (InvenTree keeps the row, flagged revoked).
Screenshots are written to docs/plans/screenshots/ only with --screenshots (they overwrite the committed ones).

Step 2 and 3 need an InvenTree with plugin routes (1.6 or later, e.g. INVENTREE_TAG=latest). On 1.5.x the script
runs step 1, reports that the pages are not offered and exits with status 3.

membership_e2e.create_api_token / delete_api_tokens are not used: they create and delete the *e2e-member* token named
"membership-e2e" (not an admin), and deleting it would break a concurrent e2e run.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

ROLE_LABEL = "Parts"  # MCP role whose "Delete" flag the check toggles
SHOTS = Path(__file__).resolve().parent.parent / "docs" / "plans" / "screenshots"
SKIPPED = 3


class Api:
    """Admin API-token client for the setup, verification reads and cleanup (independent of the browser session)."""

    def __init__(self, base: str, user: str, password: str):
        self.base = base.rstrip("/")
        basic = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        # the token endpoint is the one place HTTP basic auth works; the plugin API needs the token
        self.name = f"ui-smoke-{int(time.time())}"
        request = urllib.request.Request(
            self.base + f"/api/user/token/?name={self.name}", headers={"Authorization": basic}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            self.auth = "Token " + json.load(response)["token"]
        self.token_id: int | None = None

    def call(self, method: str, path: str, data: dict | None = None):
        request = urllib.request.Request(
            self.base + path,
            data=None if data is None else json.dumps(data).encode(),
            method=method,
            headers={"Authorization": self.auth, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
        return json.loads(body) if body else None

    def find_token_id(self) -> None:
        """Remember this run's token (listed by name) so that it can be revoked at the end."""
        for token in self.call("GET", "/api/user/tokens/"):
            if token["name"] == self.name:
                self.token_id = token["id"]

    def create_group(self, name: str) -> int:
        return self.call("POST", "/api/user/group/", {"name": name})["pk"]

    def flag(self, group: int) -> bool:
        rows = self.call("GET", f"/plugin/inventree-mcp/api/rulesets/?group={group}")
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        return next(r for r in rows if r["label"] == ROLE_LABEL)["can_delete"]

    def pages_offered(self) -> bool:
        """True when the host offers the plugin's navigation items (InvenTree with plugin routes)."""
        return "inventree-mcp" in json.dumps(self.call("GET", "/api/plugins/ui/features/navigation/"))


def login(page, base: str, user: str, password: str):
    page.goto(f"{base}/web/login")
    page.locator("input[data-path=username]").fill(user)
    page.locator("input[data-path=password]").fill(password)
    page.get_by_role("button", name="Log In").click()
    page.wait_for_function("!location.pathname.includes('/login')", timeout=60_000)


def browser_steps(base: str, args, group_name: str, pk: int, api: Api, before: bool) -> None:
    from playwright.sync_api import expect, sync_playwright

    def shot(page, name: str, **kw) -> None:
        if args.screenshots:
            SHOTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOTS / name), **kw)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            login(page, base, args.user, args.password)

            page.goto(f"{base}/web/plugin/inventree-mcp/groups")
            page.get_by_text(group_name, exact=True).first.click()
            page.get_by_text("MCP Roles").first.wait_for(timeout=30_000)
            shot(page, "groups-1-selected.png")

            # the last table that lists the role is the "MCP Roles" grid (the first one is the interactive grid)
            mcp_grid = page.locator("table").filter(has_text=ROLE_LABEL).last
            row = mcp_grid.locator("tr").filter(has_text=ROLE_LABEL).first
            row.get_by_role("checkbox").nth(3).click()  # columns: View, Change, Add, Delete
            shot(page, "groups-2-edited.png")
            page.get_by_role("button", name="Save").last.click()
            page.wait_for_timeout(1500)
            shot(page, "groups-3-saved.png")

            after = api.flag(pk)
            print(f"after save in the browser: {ROLE_LABEL} delete = {after}")
            assert after != before, "the toggled flag was not persisted"

            page.goto(f"{base}/web/plugin/inventree-mcp/connections")
            expect(page.get_by_text("MCP OAuth")).to_be_visible(timeout=30_000)
            expect(page.get_by_text("You can connect")).to_be_visible()
            shot(page, "connections.png", full_page=True)
            print("connections page: status card rendered")
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument(
        "--screenshots", action="store_true", help="write docs/plans/screenshots/*.png (overwrites the committed ones)"
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    api = Api(base, args.user, args.password)
    pk: int | None = None
    try:
        api.find_token_id()
        group_name = f"ui-smoke-{int(time.time())}"
        pk = api.create_group(group_name)
        before = api.flag(pk)
        print(f"scratch group {group_name!r} pk={pk}: {ROLE_LABEL} delete = {before}")

        if not api.pages_offered():
            print("SKIPPED browser steps: this InvenTree does not offer the plugin pages (needs 1.6 or later)")
            return SKIPPED
        browser_steps(base, args, group_name, pk, api, before)
        print("ui_smoke: ok")
        return 0
    finally:
        if pk is not None:
            try:
                api.call("DELETE", f"/api/user/group/{pk}/")
                print(f"deleted scratch group pk={pk}")
            except OSError as error:  # report, but still revoke the token
                print(f"WARNING: could not delete scratch group pk={pk}: {error}", file=sys.stderr)
        if api.token_id is not None:
            try:
                api.call("DELETE", f"/api/user/tokens/{api.token_id}/")
                print("revoked the script's API token")
            except OSError as error:
                print(f"WARNING: could not revoke API token {api.token_id}: {error}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
