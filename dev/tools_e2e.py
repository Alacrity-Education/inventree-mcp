#!/usr/bin/env python3
"""Live round trip of the MCP write tools against the running dev stack (T4 acceptance).

Setup (through dev/shell.sh, undone in a `finally`): the plugin setting MCP_READ_ONLY goes off and the MCP roles of
admin's groups get every flag. Then, with an InvenTree API token of admin (admin/admin) on the MCP endpoint:

  search_part_categories -> create_part_category (under an existing one) -> create_part -> add_stock
  -> stock_add_quantity -> stock_remove_quantity -> create_stock_location -> stock_transfer -> get_stock (verify)
  -> delete_stock_item (every item of the part) -> delete_part -> delete_part_category -> delete_stock_location

one PASS line per step. Last, with MCP_READ_ONLY back on, a write answers the read-only text. Everything the run
created is deleted again (also after a failure); exit 1 on any failure.

Usage: python3 dev/tools_e2e.py [--base-url http://localhost:8080]

Standard library only (host python). The token is never printed. Prerequisite: dev/up.sh.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEV = Path(__file__).resolve().parent
TOKEN_NAME = "tools-e2e"
READ_ONLY_TEXT = "read-only mode"
STEPS = 0


class Failure(Exception):
    """A step of the run failed."""


def step(text: str) -> None:
    global STEPS
    STEPS += 1
    print(f"[{STEPS:02d}] PASS {text}", flush=True)


def check(condition: object, text: str) -> None:
    if not condition:
        raise Failure(text)


# -- Django state through dev/shell.sh ------------------------------------------------------------


def shell(code: str) -> dict:
    """Run *code* in the server's Django shell; it prints one line 'E2E <json>'."""
    result = subprocess.run(
        [str(DEV / "shell.sh"), "-c", code], capture_output=True, text=True, check=False
    )
    line = next(
        (ln for ln in result.stdout.splitlines() if ln.startswith("E2E ")), None
    )
    if result.returncode or line is None:
        raise Failure(f"dev/shell.sh failed:\n{(result.stdout + result.stderr)[-1500:]}")
    return json.loads(line[4:])


PRELUDE = """
import json
from django.contrib.auth import get_user_model
from plugin import registry
from inventree_mcp.models import MCPRuleSet
plugin = registry.get_plugin('inventree-mcp')
admin = get_user_model().objects.get(username='admin')
FLAGS = ('can_view', 'can_add', 'can_change', 'can_delete')
"""


def grant_write() -> dict:
    """MCP_READ_ONLY off, every MCP flag on for admin's groups; returns what to restore."""
    return shell(
        PRELUDE
        + """
previous = {
    'read_only': plugin.get_setting('MCP_READ_ONLY'),
    'rows': {str(r.pk): [getattr(r, f) for f in FLAGS]
             for r in MCPRuleSet.objects.filter(group__in=admin.groups.all())},
}
MCPRuleSet.objects.filter(group__in=admin.groups.all()).update(
    can_view=True, can_add=True, can_change=True, can_delete=True)
plugin.set_setting('MCP_READ_ONLY', False)
print('E2E ' + json.dumps(previous))
"""
    )


def restore(previous: dict) -> None:
    shell(
        PRELUDE
        + f"""
previous = json.loads({json.dumps(json.dumps(previous))})
for pk, flags in previous['rows'].items():
    MCPRuleSet.objects.filter(pk=int(pk)).update(**dict(zip(FLAGS, flags)))
plugin.set_setting('MCP_READ_ONLY', previous['read_only'])
print('E2E {{}}')
"""
    )


def set_read_only(value: bool) -> None:
    shell(PRELUDE + f"plugin.set_setting('MCP_READ_ONLY', {value})\nprint('E2E {{}}')\n")


def delete_tokens() -> None:
    shell(
        PRELUDE
        + f"""
from users.models import ApiToken
ApiToken.objects.filter(user=admin, name={TOKEN_NAME!r}).delete()
print('E2E {{}}')
"""
    )


# -- the MCP client ---------------------------------------------------------------------------------


def http(method: str, url: str, headers: dict, body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


class Client:
    """tools/call with an InvenTree API token."""

    def __init__(self, base_url: str):
        self.url = f"{base_url}/plugin/inventree-mcp/mcp/"
        basic = base64.b64encode(b"admin:admin").decode()
        status, text = http(
            "GET",
            f"{base_url}/api/user/token/?name={TOKEN_NAME}",
            {"Authorization": f"Basic {basic}", "Accept": "application/json"},
        )
        check(status == 200, f"API token request answered {status}")
        self.token = json.loads(text)["token"]
        self.counter = 0

    def request(self, method: str, params: dict) -> dict:
        self.counter += 1
        status, text = http(
            "POST",
            self.url,
            {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Token {self.token}",
            },
            {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params},
        )
        check(status == 200, f"{method} answered HTTP {status}: {text[:200]}")
        body = json.loads(text)
        check("error" not in body, f"{method} failed: {body.get('error')}")
        return body["result"]

    def call(self, name: str, arguments: dict | None = None) -> tuple[bool, object]:
        """(is_error, result): the parsed JSON for object results, the plain text otherwise."""
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        content = (result.get("content") or [{}])[0].get("text", "")
        if result.get("isError"):
            return True, content
        try:
            return False, json.loads(content)
        except ValueError:
            return False, content

    def ok(self, name: str, arguments: dict | None = None) -> object:
        """Call a tool that must succeed; settings changes reach the web process within a few seconds."""
        for _ in range(10):
            is_error, result = self.call(name, arguments)
            if is_error and READ_ONLY_TEXT in str(result):
                time.sleep(1.5)
                continue
            check(not is_error, f"{name} failed: {str(result)[:300]}")
            return result
        raise Failure(f"{name}: still read-only after the setting was switched off")

    def refused(self, name: str, arguments: dict | None = None) -> str:
        is_error, result = self.call(name, arguments)
        check(is_error, f"{name} should have failed, got {str(result)[:200]}")
        return str(result)


# -- the run ----------------------------------------------------------------------------------------


def run(client: Client, created: dict[str, list[int]]) -> None:
    suffix = time.strftime("%H%M%S")
    names = client.request("tools/list", {})["tools"]
    check(len(names) == 26, f"tools/list lists {len(names)} tools, expected 26")
    step("tools/list lists 26 tools")

    found = client.ok("search_part_categories", {"search": "e", "limit": 5})
    check(found["count"] >= 1, "no part category found to create under")
    parent = found["results"][0]["pk"]
    step(f"search_part_categories -> {found['count']} found, parent category {parent}")

    category = client.ok(
        "create_part_category",
        {"name": f"e2e-category-{suffix}", "description": "tools_e2e.py", "parent": parent},
    )
    created["category"].append(category["pk"])
    check(category["parent"] == parent, "the category has the wrong parent")
    step(f"create_part_category -> pk {category['pk']} under {parent}")

    part = client.ok(
        "create_part",
        {
            "name": f"e2e-part-{suffix}",
            "description": "tools_e2e.py",
            "category": category["pk"],
            "IPN": f"E2E-{suffix}",
            "minimum_stock": 1,
        },
    )
    created["part"].append(part["pk"])
    check(part["category"] == category["pk"], "the part is in the wrong category")
    step(f"create_part -> pk {part['pk']} in category {category['pk']}")

    item = client.ok("add_stock", {"part": part["pk"], "quantity": 10, "batch": "E2E", "notes": "tools_e2e.py"})
    created["item"].append(item["pk"])
    check(float(item["quantity"]) == 10, f"add_stock quantity {item['quantity']}")
    step(f"add_stock -> stock item {item['pk']} quantity 10")

    text = client.ok("stock_add_quantity", {"items": [{"pk": item["pk"], "quantity": 5}], "notes": "tools_e2e.py"})
    got = client.ok("get_stock_item", {"id": item["pk"]})
    check(float(got["quantity"]) == 15, f"quantity after add: {got['quantity']}")
    step(f"stock_add_quantity -> {text!r}; quantity 15")

    text = client.ok("stock_remove_quantity", {"items": [{"pk": item["pk"], "quantity": 3}]})
    got = client.ok("get_stock_item", {"id": item["pk"]})
    check(float(got["quantity"]) == 12, f"quantity after remove: {got['quantity']}")
    step(f"stock_remove_quantity -> {text!r}; quantity 12")

    location = client.ok(
        "create_stock_location", {"name": f"e2e-location-{suffix}", "description": "tools_e2e.py"}
    )
    created["location"].append(location["pk"])
    step(f"create_stock_location -> pk {location['pk']}")

    text = client.ok(
        "stock_transfer",
        {"items": [{"pk": item["pk"], "quantity": 4}], "location": location["pk"], "notes": "tools_e2e.py"},
    )
    step(f"stock_transfer -> {text!r}")

    stock = client.ok("get_stock", {"part": part["pk"]})
    for row in stock["results"]:
        if row["pk"] not in created["item"]:
            created["item"].append(row["pk"])
    total = sum(float(row["quantity"]) for row in stock["results"])
    moved = client.ok("get_stock", {"part": part["pk"], "location": location["pk"]})
    check(total == 12, f"part stock is {total}, expected 12")
    check(
        moved["count"] == 1 and float(moved["results"][0]["quantity"]) == 4,
        f"stock in the new location: {moved}",
    )
    step(
        f"get_stock -> {stock['count']} items totalling {total:g}; "
        f"4 in location {location['pk']}"
    )

    for pk in list(created["item"]):
        text = client.ok("delete_stock_item", {"id": pk})
        created["item"].remove(pk)
        step(f"delete_stock_item {pk} -> {text!r}")
    check(client.ok("get_stock", {"part": part["pk"]})["count"] == 0, "stock is left")

    text = client.ok("delete_part", {"id": part["pk"]})
    created["part"].remove(part["pk"])
    step(f"delete_part -> {text!r}")

    text = client.ok("delete_part_category", {"id": category["pk"]})
    created["category"].remove(category["pk"])
    step(f"delete_part_category -> {text!r}")

    text = client.ok("delete_stock_location", {"id": location["pk"]})
    created["location"].remove(location["pk"])
    step(f"delete_stock_location -> {text!r}")


def cleanup(client: Client, created: dict[str, list[int]]) -> None:
    """Best effort, in dependency order, for what a failed run left behind."""
    for key, tool in (
        ("item", "delete_stock_item"),
        ("part", "delete_part"),
        ("category", "delete_part_category"),
        ("location", "delete_stock_location"),
    ):
        for pk in created[key]:
            is_error, result = client.call(tool, {"id": pk})
            print(f"cleanup {tool} {pk}: {'FAILED ' + str(result)[:100] if is_error else 'ok'}")


def read_only_refusal(client: Client) -> None:
    set_read_only(True)
    for _ in range(10):
        is_error, result = client.call("create_part_category", {"name": "e2e-must-not-exist"})
        if is_error and READ_ONLY_TEXT in str(result):
            step(f"MCP_READ_ONLY on: create_part_category answers {str(result)[:90]!r}")
            return
        if not is_error:  # the setting is not live yet; undo and try again
            client.call("delete_part_category", {"id": result["pk"]})
        time.sleep(1.5)
    raise Failure("with MCP_READ_ONLY on a write did not answer the read-only text")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    args = parser.parse_args()

    created: dict[str, list[int]] = {"item": [], "part": [], "category": [], "location": []}
    previous = None
    client = None
    restore_failed = False
    try:
        previous = grant_write()
        step(f"setup: MCP_READ_ONLY off (was {previous['read_only']}), MCP roles of admin's groups granted")
        client = Client(args.base_url)
        step("API token of admin obtained")
        run(client, created)
        read_only_refusal(client)
    except Failure as failure:
        print(f"FAILED: {failure}")
        return 1
    finally:
        if client is not None and any(created.values()):
            try:
                cleanup(client, created)
            except Failure as failure:
                print(f"cleanup failed: {failure}")
        try:
            if previous is not None:
                restore(previous)
                print("restored: MCP_READ_ONLY and the MCP roles of admin's groups")
            delete_tokens()
        except Failure as failure:
            print(f"RESTORE FAILED: {failure}")
            restore_failed = True
    if restore_failed:
        return 1
    print(f"OK: {STEPS} steps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
