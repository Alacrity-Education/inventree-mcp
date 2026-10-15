"""End-to-end OAuth flow with Django's test client.

This is the regression test later work packages must keep green: 401 with
challenge -> protected-resource metadata -> authorization-server metadata ->
register -> authorize (session) -> consent -> token -> MCP call -> refresh
(rotation, old access token refused) -> MCP call -> revoke -> MCP call and
refresh both refused at once.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar
from urllib.parse import urlencode, urlparse

from django.test import Client, override_settings
from InvenTree.unit_test import InvenTreeTestCase
from part.models import Part, PartCategory
from users.models import ApiToken

from ..auth import policy
from ..test_mcp import _raw_api_token
from .oauth_helpers import (
    MCP_URL,
    OAuthFacadeTestMixin,
    consent_form_data,
    pkce_pair,
    query,
)

REDIRECT_URI = "http://127.0.0.1:43123/callback"


def _path(absolute_url: str) -> str:
    parsed = urlparse(absolute_url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


@override_settings(PLUGIN_TESTING_SETUP=True)
class OAuthFlowTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """The whole flow, as an MCP client plus a browser would drive it."""

    roles: ClassVar[list[str]] = ["part.view"]

    def test_full_flow(self):
        agent = Client()  # the MCP client (no cookies needed)

        # 1. Unauthenticated MCP call -> 401 with the discovery challenge.
        response = self.mcp_call()
        self.assertEqual(response.status_code, 401)
        prm_url = re.search(
            r'resource_metadata="([^"]+)"', response["WWW-Authenticate"]
        ).group(1)

        # 2. Protected-resource metadata -> authorization server.
        prm = agent.get(_path(prm_url)).json()
        self.assertTrue(prm["resource"].endswith(MCP_URL))
        [issuer] = prm["authorization_servers"]

        # 3. Authorization-server metadata (issuer + /.well-known/openid-configuration
        #    is one of the locations MCP clients probe).
        asm = agent.get(
            _path(f"{issuer}/.well-known/oauth-authorization-server")
        ).json()
        self.assertEqual(asm["issuer"], issuer)

        # 4. Dynamic client registration (Claude Code style: loopback redirect).
        response = agent.post(
            _path(asm["registration_endpoint"]),
            data=json.dumps({
                "client_name": "Claude Code",
                "redirect_uris": ["http://127.0.0.1/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        client_id = response.json()["client_id"]

        # 5. Authorize in the browser: anonymous -> login bridge -> sign in -> back.
        verifier, challenge = pkce_pair()
        authorize = (
            _path(asm["authorization_endpoint"])
            + "?"
            + urlencode({
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "flow-state",
                "scope": " ".join(prm["scopes_supported"]),
                "resource": prm["resource"],
            })
        )
        browser = Client()
        response = browser.get(authorize)
        self.assertEqual(response.status_code, 302)
        bridge = response["Location"]
        self.assertIn("/oauth/login/", bridge)

        browser.force_login(self.user)  # stands in for the SSO round trip
        response = browser.get(bridge)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], authorize)

        # 6. Consent (loopback redirect: always shown), approve.
        page = browser.get(authorize)
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["loopback_redirect"])
        response = browser.post(
            authorize, data=consent_form_data(page) | {"allow": "Authorize"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(REDIRECT_URI))
        params = query(response["Location"])
        self.assertEqual(params["state"], "flow-state")

        # 7. Token exchange with the PKCE verifier.
        response = agent.post(
            _path(asm["token_endpoint"]),
            data={
                "grant_type": "authorization_code",
                "code": params["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": prm["resource"],
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        tokens = response.json()

        # 8. MCP call with the access token.
        response = self.mcp_call(tokens["access_token"])
        self.assertEqual(response.status_code, 200, response.content)
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        self.assertIn("list_parts", names)

        # 9. Refresh, then call again with the new access token.
        response = agent.post(
            _path(asm["token_endpoint"]),
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client_id,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        refreshed = response.json()
        self.assertNotEqual(refreshed["access_token"], tokens["access_token"])
        self.assertNotEqual(refreshed["refresh_token"], tokens["refresh_token"])

        response = self.mcp_call(refreshed["access_token"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(
            "list_parts", {t["name"] for t in response.json()["result"]["tools"]}
        )
        self.assertEqual(self.mcp_call(tokens["access_token"]).status_code, 401)

        # 10. Revoke (RFC 7009): the access token stops working, and the revoked
        #     refresh token is refused at once (no grace-period re-issue).
        response = agent.post(
            _path(asm["revocation_endpoint"]),
            data={
                "token": refreshed["refresh_token"],
                "token_type_hint": "refresh_token",
                "client_id": client_id,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.mcp_call(refreshed["access_token"]).status_code, 401)

        response = agent.post(
            _path(asm["token_endpoint"]),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refreshed["refresh_token"],
                "client_id": client_id,
            },
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")


@override_settings(PLUGIN_TESTING_SETUP=True)
class PermissionSetFlowTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """WP3 test 3: MCP access = interactive roles ∩ MCP roles, through the whole façade.

    Interactive: part view+change, stock view. MCP: part view only.
    """

    roles: ClassVar[list[str]] = ["part.view", "part.change", "stock.view"]
    mcp_roles: ClassVar[tuple[str, ...]] = ("part.view",)

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.part = Part.objects.create(
            name="WP3 flow part",
            description="x",
            category=PartCategory.objects.create(name="WP3 flow"),
        )

    def call_tool(self, access_token: str, name: str) -> dict:
        response = Client().post(
            MCP_URL,
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": name, "arguments": {}},
            }),
            content_type="application/json",
            HTTP_ACCEPT="application/json, text/event-stream",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["result"]

    def test_mcp_roles_narrow_the_token_not_the_interactive_api(self):
        browser = Client()
        browser.force_login(self.user)
        client_id = self.register_public_client()

        # The client asks for everything advertised; the code carries part view.
        code, verifier = self.obtain_code(
            browser, client_id, scope=" ".join(policy.mcp_scopes_available())
        )
        response = self.exchange_code(client_id, code, verifier)
        self.assertEqual(response.status_code, 200, response.content)
        tokens = response.json()
        self.assertEqual(tokens["scope"], "g:read r:view:part")

        response = self.mcp_call(tokens["access_token"])
        self.assertEqual(response.status_code, 200, response.content)
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        self.assertIn("list_parts", names)
        self.assertIn("get_part", names)
        self.assertNotIn("list_stock_items", names)
        self.assertNotIn("get_stock_item", names)

        result = self.call_tool(tokens["access_token"], "list_parts")
        self.assertFalse(result.get("isError"), result)
        result = self.call_tool(tokens["access_token"], "list_stock_items")
        self.assertTrue(result["isError"])
        self.assertIn("permission", result["content"][0]["text"])

        # The interactive API with the same user's API token: roles untouched.
        api_token = _raw_api_token(ApiToken.objects.create(user=self.user, name="wp3"))
        response = Client().patch(
            f"/api/part/{self.part.pk}/",
            data={"description": "changed interactively"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {api_token}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        response = Client().get("/api/stock/", HTTP_AUTHORIZATION=f"Token {api_token}")
        self.assertEqual(response.status_code, 200)
