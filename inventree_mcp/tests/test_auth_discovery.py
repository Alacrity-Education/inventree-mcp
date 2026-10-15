"""Discovery documents, well-known redirects and the MCP endpoint's 401 challenge."""

from __future__ import annotations

from django.test import Client, RequestFactory, override_settings
from django.urls import NoReverseMatch, reverse
from InvenTree.unit_test import InvenTreeTestCase
from oauth2_provider.settings import oauth2_settings

from ..auth import policy
from .oauth_helpers import MCP_URL, OAuthFacadeTestMixin, url

PRM_PATH = "/plugin/inventree-mcp/.well-known/oauth-protected-resource"
ASM_PATH = "/plugin/inventree-mcp/.well-known/oauth-authorization-server"
OIDC_PATH = "/plugin/inventree-mcp/.well-known/openid-configuration"


@override_settings(PLUGIN_TESTING_SETUP=True)
class DiscoveryDocumentTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """Protected-resource and authorization-server metadata."""

    def test_url_names_match_the_overview(self):
        self.assertEqual(url("oauth-protected-resource"), PRM_PATH)
        self.assertEqual(url("oauth-authorization-server"), ASM_PATH)
        self.assertEqual(url("openid-configuration"), OIDC_PATH)

    def test_protected_resource_metadata(self):
        response = Client().get(PRM_PATH)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertEqual(response["Cache-Control"], "max-age=300")
        doc = response.json()
        self.assertEqual(doc["resource"], f"http://testserver{MCP_URL}")
        self.assertEqual(
            doc["authorization_servers"], ["http://testserver/plugin/inventree-mcp"]
        )
        self.assertEqual(doc["scopes_supported"], policy.mcp_scopes_available())
        self.assertEqual(doc["bearer_methods_supported"], ["header"])
        self.assertEqual(doc["resource_name"], "InvenTree MCP")
        self.assertIn("resource_documentation", doc)

    def test_authorization_server_metadata(self):
        response = Client().get(ASM_PATH)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertEqual(response["Cache-Control"], "max-age=300")
        doc = response.json()
        base = "http://testserver/plugin/inventree-mcp"
        self.assertEqual(doc["issuer"], base)
        self.assertEqual(doc["authorization_endpoint"], f"{base}/oauth/authorize/")
        self.assertEqual(doc["token_endpoint"], f"{base}/oauth/token/")
        self.assertEqual(doc["registration_endpoint"], f"{base}/oauth/register/")
        self.assertEqual(doc["revocation_endpoint"], f"{base}/oauth/revoke/")
        self.assertEqual(doc["response_types_supported"], ["code"])
        self.assertEqual(doc["response_modes_supported"], ["query"])
        self.assertEqual(
            doc["grant_types_supported"], ["authorization_code", "refresh_token"]
        )
        self.assertEqual(doc["code_challenge_methods_supported"], ["S256"])
        methods = ["none", "client_secret_basic", "client_secret_post"]
        self.assertEqual(doc["token_endpoint_auth_methods_supported"], methods)
        self.assertEqual(doc["revocation_endpoint_auth_methods_supported"], methods)
        self.assertEqual(doc["scopes_supported"], policy.mcp_scopes_available())
        self.assertIs(doc["client_id_metadata_document_supported"], True)
        self.assertIn("service_documentation", doc)

    def test_openid_configuration_alias_serves_the_same_document(self):
        self.assertEqual(Client().get(OIDC_PATH).json(), Client().get(ASM_PATH).json())

    def test_documents_are_404_when_disabled(self):
        self.set_setting("OAUTH_ENABLED", False)

        for path in (PRM_PATH, ASM_PATH, OIDC_PATH):
            with self.subTest(path=path):
                self.assertEqual(Client().get(path).status_code, 404)

    @override_settings(
        USE_X_FORWARDED_HOST=True,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        ALLOWED_HOSTS=["*"],
    )
    def test_urls_honour_forwarded_proto_and_host(self):
        headers = {
            "HTTP_X_FORWARDED_PROTO": "https",
            "HTTP_X_FORWARDED_HOST": "inventree.example.com",
        }
        prm = Client().get(PRM_PATH, **headers).json()
        asm = Client().get(ASM_PATH, **headers).json()

        origin = "https://inventree.example.com"
        self.assertEqual(prm["resource"], f"{origin}{MCP_URL}")
        self.assertEqual(
            prm["authorization_servers"], [f"{origin}/plugin/inventree-mcp"]
        )
        self.assertEqual(asm["issuer"], f"{origin}/plugin/inventree-mcp")
        self.assertEqual(
            asm["token_endpoint"], f"{origin}/plugin/inventree-mcp/oauth/token/"
        )

    def test_scopes_follow_mcp_read_only(self):
        self.set_setting("MCP_READ_ONLY", True)
        read_only = policy.mcp_scopes_available()
        self.assertEqual(read_only[0], "g:read")
        self.assertIn("r:view:part", read_only)
        self.assertFalse([s for s in read_only if s.startswith(("r:add", "r:change"))])

        self.set_setting("MCP_READ_ONLY", False)
        writable = policy.mcp_scopes_available()
        self.assertIn("r:add:part", writable)
        self.assertIn("r:delete:stock", writable)
        self.assertEqual(writable[: len(read_only)], read_only)


@override_settings(
    PLUGIN_TESTING_SETUP=True,
    # Core's site-URL middleware answers 500 when the visited host differs from SITE_URL
    SITE_URL="http://testserver",
    CSRF_TRUSTED_ORIGINS=["http://testserver"],
)
class WellKnownRedirectTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """Core's /.well-known/<name>[/<anything>] redirects (InvenTree with WellKnownMixin only)."""

    def setUp(self):
        super().setUp()
        try:
            reverse("well-known:index")
        except NoReverseMatch:
            self.skipTest("this InvenTree version has no /.well-known/ plugin index")

    def test_protected_resource_path_insertion_redirects_to_the_plugin(self):
        response = Client().get(
            "/.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp/"
        )
        self.assertIn(response.status_code, (301, 302))
        self.assertTrue(response["Location"].endswith(PRM_PATH))

        response = Client().get("/.well-known/oauth-protected-resource/anything")
        self.assertTrue(response["Location"].endswith(PRM_PATH))

    def test_authorization_server_path_insertion_redirects_to_the_plugin(self):
        response = Client().get(
            "/.well-known/oauth-authorization-server/plugin/inventree-mcp"
        )
        self.assertIn(response.status_code, (301, 302))
        self.assertTrue(response["Location"].endswith(ASM_PATH))

    def test_index_lists_the_oauth_documents(self):
        data = Client().get(reverse("well-known:index")).json()["well_known_urls"]
        self.assertTrue(data["oauth-protected-resource"].endswith(PRM_PATH))
        self.assertTrue(data["oauth-authorization-server"].endswith(ASM_PATH))


@override_settings(PLUGIN_TESTING_SETUP=True)
class ChallengeHeaderTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """WWW-Authenticate on the MCP endpoint's 401 responses."""

    def test_no_credential_gets_the_discovery_challenge(self):
        response = self.mcp_call()

        self.assertEqual(response.status_code, 401)
        self.assertIn("error", response.json())  # JSON-RPC body kept
        header = response["WWW-Authenticate"]
        self.assertTrue(header.startswith('Bearer realm="InvenTree"'))
        self.assertIn(f'resource_metadata="http://testserver{PRM_PATH}"', header)
        self.assertIn(f'scope="{" ".join(policy.mcp_scopes_available())}"', header)
        self.assertNotIn("invalid_token", header)

    def test_url_without_trailing_slash_also_gets_the_challenge(self):
        """Claude Code drops the trailing slash of the configured MCP URL."""
        response = self.mcp_call(path=MCP_URL.rstrip("/"))
        self.assertEqual(response.status_code, 401)
        self.assertIn("resource_metadata=", response["WWW-Authenticate"])

    def test_presented_credential_adds_invalid_token(self):
        for credential in ("Bearer not-a-token", "Token not-a-token"):
            with self.subTest(credential=credential):
                response = self.mcp_call(HTTP_AUTHORIZATION=credential)
                self.assertEqual(response.status_code, 401)
                header = response["WWW-Authenticate"]
                self.assertIn('error="invalid_token"', header)
                self.assertIn("resource_metadata=", header)

    def test_disabled_facade_challenge_has_no_discovery(self):
        self.set_setting("OAUTH_ENABLED", False)

        response = self.mcp_call()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], 'Bearer realm="InvenTree"')

    def test_disabled_facade_restores_dot_settings_through_the_challenge(self):
        """ensure_applied() runs before the OAUTH_ENABLED check (review F3)."""
        self.mcp_call()
        self.assertIs(oauth2_settings.CIMD_ENABLED, True)

        self.set_setting("OAUTH_ENABLED", False)
        response = self.mcp_call(HTTP_AUTHORIZATION="Bearer not-a-token")

        self.assertEqual(
            response["WWW-Authenticate"],
            'Bearer realm="InvenTree", error="invalid_token"',
        )
        self.assertIs(oauth2_settings.CIMD_ENABLED, False)
        self.assertEqual(oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS, 36000)


@override_settings(PLUGIN_TESTING_SETUP=True)
class WellKnownAdvertisementTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """InvenTreeMCP.get_well_known_urls() (review F15)."""

    def _names(self, request):
        return [name for name, _url in self.plugin.get_well_known_urls(request)]

    def test_redirect_patterns_always_cover_the_oauth_documents(self):
        self.set_setting("OAUTH_ENABLED", False)
        self.assertEqual(
            self._names(None),
            [
                "mcp-server-card",
                "oauth-protected-resource",
                "oauth-authorization-server",
            ],
        )

    def test_index_lists_oauth_documents_only_while_served(self):
        request = RequestFactory().get("/.well-known/")
        self.assertIn("oauth-authorization-server", self._names(request))

        self.set_setting("OAUTH_ENABLED", False)
        self.assertEqual(self._names(request), ["mcp-server-card"])
