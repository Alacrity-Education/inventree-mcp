"""WP3: the MCP permission set (policy, transport narrowing, revocation on reduction).

Spec tests 1, 2, 4 and 5 (3 is in test_auth_flow.py, 6 in test_api.py).
"""

from __future__ import annotations

import base64
import json
from typing import ClassVar
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, override_settings
from InvenTree.unit_test import InvenTreeTestCase
from oauth2_provider.contrib.rest_framework import TokenMatchesOASRequirements
from oauth2_provider.models import AccessToken, RefreshToken
from part.models import Part, PartCategory
from users.models import ApiToken

from .. import context, proxy, tool_visibility
from ..auth import policy, signals
from ..mcp_server import mcp
from ..mcp_transport import scoped_token_for
from ..models import MCPRuleSet
from ..oauth2_bridge import scoped_view_class
from ..test_mcp import _raw_api_token
from .oauth_helpers import (
    MCP_URL,
    OAuthFacadeTestMixin,
    grant_mcp,
    make_token_pair,
    pkce_pair,
)

User = get_user_model()

ALL_VIEW = {
    "g:read",
    "r:view:admin",
    "r:view:part_category",
    "r:view:part",
    "r:view:bom",
    "r:view:stock_location",
    "r:view:stock",
    "r:view:build",
    "r:view:purchase_order",
    "r:view:sales_order",
    "r:view:return_order",
    "r:view:transfer_order",
}


def api_token_for(user) -> str:
    """A fresh InvenTree API token's raw value."""
    return _raw_api_token(ApiToken.objects.create(user=user, name="wp3-test"))


class PermissionSetTestCase(OAuthFacadeTestMixin, InvenTreeTestCase):
    """The test user: interactive part view+change and stock view; no MCP roles yet."""

    roles: ClassVar[list[str]] = ["part.view", "part.change", "stock.view"]
    mcp_roles: ClassVar[tuple[str, ...]] = ()

    def set_mcp(self, group, **flags) -> MCPRuleSet:
        """Set flags of one MCP role row through the model: set_mcp(group, part="view")."""
        [(name, perms)] = flags.items()
        row = MCPRuleSet.objects.get(group=group, name=name)
        for perm in ("view", "add", "change", "delete"):
            setattr(row, f"can_{perm}", perm in perms.split(","))
        row.save()
        return row


class EffectiveScopesTest(PermissionSetTestCase):
    """Test 1: effective_scopes()."""

    def test_new_group_gets_rows_for_every_role(self):
        from users.ruleset import RULESET_NAMES

        group = Group.objects.create(name="Fresh")
        self.assertEqual(
            set(MCPRuleSet.objects.filter(group=group).values_list("name", flat=True)),
            {str(name) for name in RULESET_NAMES},
        )
        self.assertFalse(MCPRuleSet.objects.filter(group=group, can_view=True).exists())

    def test_no_rulesets_means_no_scopes(self):
        self.assertEqual(policy.effective_scopes(self.user), set())
        self.assertEqual(policy.may_connect(self.user), (False, "no_mcp_permission"))
        self.assertEqual(policy.narrow(None, self.user), [])

    def test_view_on_part(self):
        grant_mcp(self.group, "part.view")
        self.assertEqual(policy.effective_scopes(self.user), {"g:read", "r:view:part"})
        self.assertEqual(policy.may_connect(self.user), (True, ""))
        self.assertEqual(policy.narrow(None, self.user), ["g:read", "r:view:part"])

    def test_write_scopes_only_without_read_only(self):
        grant_mcp(self.group, "part.change")  # implies view, as core's RuleSet
        self.assertEqual(policy.effective_scopes(self.user), {"g:read", "r:view:part"})

        self.set_setting("MCP_READ_ONLY", False)
        self.assertEqual(
            policy.effective_scopes(self.user),
            {"g:read", "r:view:part", "r:change:part"},
        )
        grant_mcp(self.group, "stock.delete")  # implies change and view
        self.assertEqual(
            policy.effective_scopes(self.user),
            {
                "g:read",
                "r:view:part",
                "r:change:part",
                "r:view:stock",
                "r:change:stock",
                "r:delete:stock",
            },
        )

    def test_union_across_groups(self):
        grant_mcp(self.group, "part.view")
        other = Group.objects.create(name="Stock MCP")
        grant_mcp(other, "stock.view")
        self.user.groups.add(other)
        self.assertEqual(
            policy.effective_scopes(self.user),
            {"g:read", "r:view:part", "r:view:stock"},
        )
        # A group the user is not in counts for nothing.
        grant_mcp(Group.objects.create(name="Elsewhere"), "all")
        self.assertNotIn("r:view:build", policy.effective_scopes(self.user))

    def test_superuser(self):
        self.user.is_superuser = True
        self.user.save()
        self.assertEqual(policy.effective_scopes(self.user), set())
        grant_mcp(self.group, "bom.view")
        self.assertEqual(policy.effective_scopes(self.user), {"g:read", "r:view:bom"})

        self.set_setting("MCP_SUPERUSER_FULL_ACCESS", True)
        self.assertEqual(
            policy.effective_scopes(self.user), set(policy.mcp_scopes_available())
        )
        self.assertEqual(policy.effective_scopes(self.user), ALL_VIEW)

    def test_inactive_and_outside_required_groups(self):
        grant_mcp(self.group, "all")
        self.set_setting("MCP_REQUIRED_GROUPS", "Somebody else's group")
        self.assertEqual(policy.effective_scopes(self.user), set())
        self.set_setting("MCP_REQUIRED_GROUPS", self.group.name)
        self.assertEqual(policy.effective_scopes(self.user), ALL_VIEW)

        self.user.is_active = False
        self.user.save()
        self.assertEqual(policy.effective_scopes(self.user), set())
        self.assertEqual(policy.may_connect(self.user), (False, "inactive"))

    def test_one_query_for_the_rulesets(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        grant_mcp(self.group, "part.view")
        with CaptureQueriesContext(connection) as queries:
            policy.ruleset_scopes(self.user)
        selects = [q["sql"] for q in queries if q["sql"].startswith("SELECT")]
        self.assertEqual(len(selects), 1, selects)  # plus the savepoint pair
        self.assertIn("inventree_mcp_mcpruleset", selects[0])


class NoMcpPermissionTest(PermissionSetTestCase):
    """Test 2: no_mcp_permission at authorize and at refresh."""

    def test_authorize_renders_the_403_page(self):
        browser = Client()
        browser.force_login(self.user)
        client_id = self.register_public_client()
        _verifier, challenge = pkce_pair()
        page = browser.get(self.authorize_url(client_id, challenge))
        self.assertEqual(page.status_code, 403)
        self.assertEqual(page.context["reason"], "no_mcp_permission")
        self.assertContains(page, "No MCP access", status_code=403)

    def test_refresh_after_the_group_lost_every_flag(self):
        grant_mcp(self.group, "part.view")
        browser = Client()
        browser.force_login(self.user)
        client_id = self.register_public_client()
        code, verifier = self.obtain_code(browser, client_id)
        tokens = self.exchange_code(client_id, code, verifier).json()
        self.assertEqual(tokens["scope"], "g:read r:view:part")

        # Bypasses the reduction signal (update() sends none): the refresh's
        # own narrowing must refuse, revoke the presented token, and say why.
        MCPRuleSet.objects.filter(group=self.group).update(can_view=False)
        self.set_setting("MEMBERSHIP_ENFORCED", False)
        response = self.refresh(client_id, tokens["refresh_token"])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertIsNotNone(
            RefreshToken.objects.get(token=tokens["refresh_token"]).revoked
        )


@override_settings(PLUGIN_TESTING_SETUP=True)
class TransportNarrowingTest(PermissionSetTestCase):
    """Test 4: API tokens and Basic on /mcp get the OAuth narrowing."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        category = PartCategory.objects.create(name="WP3")
        Part.objects.create(name="WP3 part", description="x", category=category)
        # A second part, so list_parts {"limit": 1} has a "next" link.
        Part.objects.create(name="WP3 part 2", description="x", category=category)

    def setUp(self):
        super().setUp()
        grant_mcp(self.group, "part.view")
        self.api_token = api_token_for(self.user)
        # No background membership re-check is queued for the API token here.
        self.set_setting("MEMBERSHIP_ENFORCED", False)

    def tools(self, **headers) -> set[str]:
        response = self.mcp_call(**headers)
        self.assertEqual(response.status_code, 200, response.content)
        return {tool["name"] for tool in response.json()["result"]["tools"]}

    def call_tool(self, name: str, arguments: dict | None = None, **headers):
        return (
            Client()
            .post(
                MCP_URL,
                data=json.dumps({
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                }),
                content_type="application/json",
                HTTP_ACCEPT="application/json, text/event-stream",
                **headers,
            )
            .json()
        )

    def test_api_token_sees_what_an_oauth_token_sees(self):
        application = self.create_cimd_application()
        access, _refresh = make_token_pair(
            application, self.user, scope=" ".join(policy.narrow(None, self.user))
        )
        oauth_tools = self.tools(HTTP_AUTHORIZATION=f"Bearer {access.token}")
        access_rows = list(AccessToken.objects.values_list("pk", "scope", "expires"))
        token_tools = self.tools(HTTP_AUTHORIZATION=f"Token {self.api_token}")

        self.assertEqual(token_tools, oauth_tools)
        self.assertIn("list_parts", token_tools)
        # Interactive stock view, but no MCP stock role.
        self.assertNotIn("list_stock_items", token_tools)
        self.assertNotIn("list_locations", token_tools)

        result = self.call_tool(
            "list_stock_items", HTTP_AUTHORIZATION=f"Token {self.api_token}"
        )["result"]
        self.assertTrue(result["isError"])
        result = self.call_tool(
            "list_parts", HTTP_AUTHORIZATION=f"Token {self.api_token}"
        )["result"]
        self.assertFalse(result.get("isError"), result)
        # Nothing persisted through the whole proxy path (the scoped token is unsaved).
        self.assertEqual(
            list(AccessToken.objects.values_list("pk", "scope", "expires")),
            access_rows,
        )

        # The interactive API is untouched: the same API token reads stock.
        response = Client().get(
            "/api/stock/", HTTP_AUTHORIZATION=f"Token {self.api_token}"
        )
        self.assertEqual(response.status_code, 200)

    def non_facade_token(self, scope: str) -> AccessToken:
        """A token of an application registered by hand (core's /o/ issues such tokens)."""
        from oauth2_provider.models import get_application_model

        Application = get_application_model()
        application = Application.objects.create(
            name="Admin-registered client",
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://client.example/callback",
            registration_source=Application.RegistrationSource.MANUAL,
            user=None,
        )
        access, _refresh = make_token_pair(application, self.user, scope=scope)
        return access

    def test_non_facade_oauth_token_is_cut_to_the_mcp_roles(self):
        """F1: interactive stock view and a stock scope, but MCP grants parts only."""
        access = self.non_facade_token("g:read r:view:stock r:view:part")
        bearer = {"HTTP_AUTHORIZATION": f"Bearer {access.token}"}

        tools = self.tools(**bearer)
        self.assertIn("list_parts", tools)
        self.assertNotIn("list_stock_items", tools)
        result = self.call_tool("list_stock_items", **bearer)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("permission", result["content"][0]["text"])
        result = self.call_tool("list_parts", {"limit": 1}, **bearer)["result"]
        self.assertFalse(result.get("isError"), result)
        # The presented token itself is unchanged.
        access.refresh_from_db()
        self.assertEqual(access.scope, "g:read r:view:stock r:view:part")

    def test_oauth_token_without_any_mcp_scope_gets_403(self):
        access = self.non_facade_token("r:view:stock r:change:part")
        response = self.mcp_call(access.token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["message"], "No MCP permission")

    def test_leaving_the_group_takes_effect_at_the_next_request(self):
        """F3: no revocation needed, the transport cuts to the current MCP scopes."""
        MCPRuleSet.objects.filter(group=self.group).update(can_view=False)
        mcp_group = Group.objects.create(name="MCP parts")
        grant_mcp(mcp_group, "part.view")
        self.user.groups.add(mcp_group)
        application = self.create_cimd_application()
        access, refresh = make_token_pair(
            application, self.user, scope="g:read r:view:part"
        )
        self.assertEqual(self.mcp_call(access.token).status_code, 200)

        self.user.groups.remove(mcp_group)
        response = self.mcp_call(access.token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["message"], "No MCP permission")
        refresh.refresh_from_db()
        self.assertIsNone(refresh.revoked)  # nothing revoked, nothing needed

    @override_settings(ALLOWED_HOSTS=["mcp.example"])
    def test_paginated_list_uses_the_request_host(self):
        """F2: proxied requests carry the MCP request's host, not "testserver"."""
        for authorization in (
            f"Token {self.api_token}",
            "Bearer "
            + make_token_pair(
                self.create_cimd_application(), self.user, scope="g:read r:view:part"
            )[0].token,
        ):
            result = self.call_tool(
                "list_parts",
                {"limit": 1},
                HTTP_AUTHORIZATION=authorization,
                HTTP_HOST="mcp.example",
            )["result"]
            self.assertFalse(result.get("isError"), result)
            body = result.get("structuredContent") or json.loads(
                result["content"][0]["text"]
            )
            self.assertEqual(len(body["results"]), 1)
            self.assertTrue(
                body["next"].startswith("http://mcp.example/"), body["next"]
            )

    def test_request_meta_reaches_the_proxied_request(self):
        from django.test import RequestFactory

        from ..mcp_transport import request_meta

        request = RequestFactory().post(
            "/plugin/inventree-mcp/mcp/",
            HTTP_HOST="testserver",
            REMOTE_ADDR="192.0.2.7",
            HTTP_ACCEPT_LANGUAGE="de",
            HTTP_AUTHORIZATION="Token secret",
            secure=True,
        )
        meta = request_meta(request)
        self.assertEqual(
            meta,
            {
                "HTTP_HOST": "testserver",
                "wsgi.url_scheme": "https",
                "REMOTE_ADDR": "192.0.2.7",
                "HTTP_ACCEPT_LANGUAGE": "de",
            },
        )
        token = context.set_current_user(self.user, None, meta)
        self.addCleanup(context.reset_current_user, token)
        self.assertEqual(context.get_current_request_meta(), meta)

    def test_empty_effective_scopes_get_403(self):
        MCPRuleSet.objects.filter(group=self.group).update(can_view=False)
        response = self.mcp_call(HTTP_AUTHORIZATION=f"Token {self.api_token}")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["message"], "No MCP permission")
        self.assertNotIn("WWW-Authenticate", response)

    def test_basic_auth_only_when_allowed(self):
        self.user.set_password("wp3-password")
        self.user.save()
        basic = base64.b64encode(f"{self.user.username}:wp3-password".encode()).decode()

        response = self.mcp_call(HTTP_AUTHORIZATION=f"Basic {basic}")
        self.assertEqual(response.status_code, 401)

        self.set_setting("MCP_ALLOW_BASIC_AUTH", True)
        tools = self.tools(HTTP_AUTHORIZATION=f"Basic {basic}")
        self.assertIn("list_parts", tools)
        self.assertNotIn("list_stock_items", tools)

    def test_scoped_token_is_unsaved_and_carries_the_effective_scopes(self):
        token = scoped_token_for(self.user)
        self.assertIsNone(token.pk)
        self.assertEqual(token.scope, "g:read r:view:part")
        self.assertTrue(token.is_valid(["r:view:part"]))
        self.assertFalse(token.is_valid(["r:view:stock"]))
        self.assertFalse(token.is_expired())
        self.assertEqual(AccessToken.objects.filter(user=self.user).count(), 0)

    def test_dot_permission_needs_only_is_valid_on_the_token(self):
        """DOT's TokenMatchesOASRequirements calls nothing but is_valid() (-> allow_scopes/is_expired)."""

        class Probe:
            scope = "r:view:part"
            calls: ClassVar[list] = []

            def is_valid(self, scopes=None):
                self.calls.append(list(scopes))
                return set(scopes) <= {"r:view:part"}

            def __getattr__(self, name):
                raise AssertionError(f"unexpected token attribute {name}")

        class Request:
            auth = Probe()
            method = "GET"

        class View:
            required_alternate_scopes: ClassVar[dict] = {
                "GET": [["r:view:stock"], ["r:view:part"]]
            }

        self.assertTrue(TokenMatchesOASRequirements().has_permission(Request(), View()))
        self.assertEqual(Probe.calls, [["r:view:stock"], ["r:view:part"]])

    def test_multi_role_tables_accept_any_role_scope(self):
        """PartList's table is in the part and build rulesets: either scope suffices (bridge bug 3)."""
        from part.api import PartList

        required = scoped_view_class(PartList).required_alternate_scopes["GET"]
        self.assertIn(["r:view:part"], required)
        self.assertIn(["r:view:build"], required)

    async def test_tool_visibility_reflects_the_narrowed_set(self):
        """visible_tool_names() needs no change: the transport binds the scoped token."""
        from asgiref.sync import sync_to_async

        token = await sync_to_async(scoped_token_for)(self.user)
        context.set_current_user(self.user, token)
        self.addCleanup(context.set_current_user, None)

        names = await tool_visibility.visible_tool_names(
            tool.name for tool in await mcp.list_tools()
        )
        self.assertIn("list_parts", names)
        self.assertNotIn("list_stock_items", names)

        from stock.api import StockList

        self.assertFalse(await proxy.user_has_access(StockList, "GET"))


class ReductionRevokesTest(PermissionSetTestCase):
    """Test 5: a reduction revokes the members' façade tokens, nothing else."""

    def setUp(self):
        super().setUp()
        grant_mcp(self.group, "part.view")
        self.application = self.create_cimd_application()
        self.member_access, self.member_refresh = make_token_pair(
            self.application, self.user, scope="g:read r:view:part"
        )
        self.outsider = User.objects.create_user(username="wp3-outsider", password="x")
        other = Group.objects.create(name="Other MCP")
        grant_mcp(other, "part.view")
        self.outsider.groups.add(other)
        self.outsider_access, self.outsider_refresh = make_token_pair(
            self.application, self.outsider
        )
        self.api_token = ApiToken.objects.create(user=self.user, name="wp3")

    def assert_member_revoked(self, revoked: bool):
        self.member_refresh.refresh_from_db()
        self.assertEqual(self.member_refresh.revoked is not None, revoked)
        self.assertEqual(
            AccessToken.objects.filter(pk=self.member_access.pk).exists(), not revoked
        )
        # Never the other group's member, never API tokens.
        self.outsider_refresh.refresh_from_db()
        self.assertIsNone(self.outsider_refresh.revoked)
        self.assertTrue(AccessToken.objects.filter(pk=self.outsider_access.pk).exists())
        self.api_token.refresh_from_db()
        self.assertFalse(self.api_token.revoked)

    def test_adding_a_flag_revokes_nothing(self):
        grant_mcp(self.group, "stock.view")
        self.set_mcp(self.group, part="view")  # saved unchanged
        self.assert_member_revoked(False)

    def test_clearing_view_revokes_the_members(self):
        with self.assertLogs("inventree", level="INFO") as logs:
            self.set_mcp(self.group, part="")
        self.assert_member_revoked(True)
        self.assertFalse(
            any(self.user.username in line for line in logs.output), logs.output
        )

    def test_reducing_a_write_flag_revokes(self):
        self.set_setting("MCP_READ_ONLY", False)
        grant_mcp(self.group, "part.change")
        self.assert_member_revoked(False)
        self.set_mcp(self.group, part="view")  # change -> off
        self.assert_member_revoked(True)

    def test_deleting_a_granting_row_revokes(self):
        MCPRuleSet.objects.get(group=self.group, name="part").delete()
        self.assert_member_revoked(True)

    def test_deleting_the_group_revokes(self):
        self.group.delete()
        self.assert_member_revoked(True)

    def test_deleting_a_group_without_mcp_access_revokes_nothing(self):
        empty = Group.objects.create(name="Nothing")
        self.user.groups.add(empty)
        empty.delete()
        self.assert_member_revoked(False)

    def test_more_than_twenty_members_are_offloaded(self):
        for index in range(signals.REVOKE_SYNC_LIMIT):
            User.objects.create_user(username=f"wp3-member-{index}").groups.add(
                self.group
            )
        with patch("InvenTree.tasks.offload_task") as offload:
            self.set_mcp(self.group, part="")
        offload.assert_called_once()
        task, user_ids = offload.call_args.args
        self.assertEqual(task, "inventree_mcp.auth.tasks.revoke_mcp_tokens")
        self.assertEqual(len(user_ids), signals.REVOKE_SYNC_LIMIT + 1)
        self.assert_member_revoked(False)  # queued, not yet run

        from ..auth.tasks import revoke_mcp_tokens

        self.assertEqual(revoke_mcp_tokens(user_ids), 1)
        self.assert_member_revoked(True)

    def test_mcp_endpoint_refuses_a_revoked_member_at_once(self):
        self.assertEqual(self.mcp_call(self.member_access.token).status_code, 200)
        self.set_mcp(self.group, part="")
        self.assertEqual(self.mcp_call(self.member_access.token).status_code, 401)
