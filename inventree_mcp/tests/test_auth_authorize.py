"""The façade's authorize endpoint and login bridge."""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import quote, urlparse

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, RequestFactory, override_settings
from InvenTree.unit_test import InvenTreeTestCase
from oauth2_provider.models import get_grant_model

from ..auth.authorize import MCPAuthorizationView
from .oauth_helpers import (
    LOOPBACK_REDIRECT_URI,
    REDIRECT_URI,
    TRUSTED_CIMD_CLIENT_ID,
    TRUSTED_REDIRECT_URI,
    OAuthFacadeTestMixin,
    pkce_pair,
    query,
    url,
)

Grant = get_grant_model()


@override_settings(PLUGIN_TESTING_SETUP=True)
class AuthorizeTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """GET/POST /plugin/inventree-mcp/oauth/authorize/."""

    def setUp(self):
        super().setUp()
        self.client_id = self.register_public_client()
        self.verifier, self.challenge = pkce_pair()
        self.browser = Client()
        self.browser.force_login(self.user)

    def _authorize(self, **kwargs) -> str:
        kwargs.setdefault("client_id", self.client_id)
        kwargs.setdefault("challenge", self.challenge)
        return self.authorize_url(**kwargs)

    def assertErrorPage(self, response, reason: str, status: int):  # noqa: N802
        self.assertEqual(response.status_code, status, response.content)
        self.assertTemplateUsed(response, "inventree_mcp/error.html")
        self.assertEqual(response.context["reason"], reason)

    def test_anonymous_goes_to_the_login_bridge_with_next(self):
        authorize = self._authorize()

        response = Client().get(authorize)

        self.assertEqual(response.status_code, 302)
        location = urlparse(response["Location"])
        self.assertEqual(location.path, url("oauth-login"))
        self.assertEqual(query(response["Location"])["next"], authorize)

    def test_signed_in_user_sees_consent_with_narrowed_scopes(self):
        response = self.browser.get(
            self._authorize(scope="g:read r:view:part a:superuser")
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTemplateUsed(response, "inventree_mcp/consent.html")
        self.assertEqual(
            response.context["scope_labels"],
            ["General Read scope", "GET for Role Parts"],
        )
        self.assertEqual(
            response.context["form"].initial["scope"], "g:read r:view:part"
        )
        self.assertEqual(response.context["inventree_user"], self.user.username)
        self.assertFalse(response.context["loopback_redirect"])
        self.assertEqual(response.context["access_token_lifetime"], "1 hour")
        self.assertEqual(response.context["refresh_token_lifetime"], "30 days")
        self.assertNotContains(response, "User Role Superuser")
        self.assertContains(response, 'name="allow"')

    def test_approve_redirects_with_code_and_state(self):
        response = self.approve(
            self.browser, self._authorize(scope="g:read r:view:part")
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(REDIRECT_URI))
        params = query(response["Location"])
        self.assertEqual(params["state"], "xyz-state")
        grant = Grant.objects.get(code=params["code"])
        self.assertEqual(grant.user, self.user)
        self.assertEqual(grant.scope, "g:read r:view:part")
        self.assertEqual(grant.code_challenge_method, "S256")

    def test_deny_redirects_with_access_denied(self):
        page = self.browser.get(self._authorize())
        data = {k: v for k, v in page.context["form"].initial.items() if v is not None}

        response = self.browser.post(self._authorize(), data=data)

        self.assertEqual(response.status_code, 302)
        params = query(response["Location"])
        self.assertEqual(params["error"], "access_denied")
        self.assertEqual(params["state"], "xyz-state")

    def test_tampered_scope_is_narrowed(self):
        response = self.approve(
            self.browser,
            self._authorize(scope="g:read"),
            scope="g:read a:superuser a:staff r:view:part",
        )

        self.assertEqual(response.status_code, 302)
        grant = Grant.objects.get(code=query(response["Location"])["code"])
        self.assertEqual(grant.scope, "g:read r:view:part")

    def test_tampered_pkce_method_is_refused(self):
        response = self.approve(
            self.browser, self._authorize(), code_challenge_method="plain"
        )
        self.assertErrorPage(response, "invalid_request", 400)

    def test_unknown_client_renders_error_page(self):
        response = self.browser.get(self._authorize(client_id="no-such-client"))
        self.assertErrorPage(response, "client", 400)

    def test_unregistered_redirect_uri_renders_error_page(self):
        response = self.browser.get(
            self._authorize(redirect_uri="https://attacker.example/cb")
        )
        self.assertErrorPage(response, "client", 400)

    def test_untrusted_cimd_client_renders_error_page(self):
        """CIMD is restricted to TRUSTED_CLIENT_HOSTS: no fetch, no redirect."""
        response = self.browser.get(
            self._authorize(client_id="https://evil.example/client.json")
        )
        self.assertErrorPage(response, "client", 400)

    def test_missing_pkce_redirects_with_invalid_request(self):
        url_without_pkce = (
            f"{url('oauth-authorize')}?response_type=code&client_id={self.client_id}"
            f"&redirect_uri={quote(REDIRECT_URI, safe='')}&state=s1"
        )
        response = self.browser.get(url_without_pkce)

        self.assertEqual(response.status_code, 302)
        params = query(response["Location"])
        self.assertEqual(params["error"], "invalid_request")
        self.assertEqual(params["state"], "s1")

    def test_trusted_cimd_client_with_https_redirect_skips_consent(self):
        self.create_cimd_application()

        response = self.browser.get(
            self._authorize(
                client_id=TRUSTED_CIMD_CLIENT_ID, redirect_uri=TRUSTED_REDIRECT_URI
            )
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(TRUSTED_REDIRECT_URI))
        self.assertIn("code", query(response["Location"]))

    def test_trusted_cimd_client_with_loopback_redirect_shows_consent(self):
        self.create_cimd_application()

        response = self.browser.get(
            self._authorize(
                client_id=TRUSTED_CIMD_CLIENT_ID,
                redirect_uri=LOOPBACK_REDIRECT_URI.replace(
                    "127.0.0.1", "127.0.0.1:54321"
                ),
            )
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.context["loopback_redirect"])
        self.assertTrue(response.context["client_trusted"])
        self.assertEqual(response.context["client_host"], "claude.ai")

    def test_auto_approve_can_be_turned_off(self):
        self.create_cimd_application()
        self.set_setting("AUTO_APPROVE_TRUSTED_CLIENTS", False)

        response = self.browser.get(
            self._authorize(
                client_id=TRUSTED_CIMD_CLIENT_ID, redirect_uri=TRUSTED_REDIRECT_URI
            )
        )
        self.assertEqual(response.status_code, 200)

    def test_inactive_user_gets_403(self):
        """Django's session middleware already treats inactive users as anonymous,
        so the policy gate is exercised directly on the view.
        """
        inactive = get_user_model().objects.create_user(
            username="inactive", password="password", is_active=False
        )
        request = RequestFactory().get(self._authorize())
        request.user = inactive
        request.session = SessionStore()

        response = MCPAuthorizationView.as_view()(request)

        self.assertEqual(response.status_code, 403)
        self.assertIn(b"inactive", response.content)

    def test_blocked_user_gets_403_never_a_redirect(self):
        with patch("inventree_mcp.auth.membership.is_blocked", return_value=True):
            response = self.browser.get(self._authorize())
        self.assertErrorPage(response, "blocked", 403)

    def test_no_mcp_scopes_gets_403(self):
        with patch("inventree_mcp.auth.policy.effective_scopes", return_value=set()):
            response = self.browser.get(self._authorize())
        self.assertErrorPage(response, "no_mcp_permission", 403)

    def test_disabled_facade_answers_404(self):
        self.set_setting("OAUTH_ENABLED", False)
        self.assertEqual(self.browser.get(self._authorize()).status_code, 404)


@override_settings(PLUGIN_TESTING_SETUP=True)
class LoginBridgeTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """GET /plugin/inventree-mcp/oauth/login/?next=..."""

    def setUp(self):
        super().setUp()
        self.next = f"{url('oauth-authorize')}?response_type=code&client_id=abc&state=s"

    def _get(self, next_url: str, client: Client | None = None):
        return (client or Client()).get(url("oauth-login"), {"next": next_url})

    def test_sso_form_posts_to_allauth_provider_redirect(self):
        with patch(
            "inventree_mcp.auth.login_bridge.sso_provider_id", return_value="authentik"
        ):
            response = self._get(self.next)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "inventree_mcp/login_bridge.html")
        self.assertEqual(response.context["mode"], "sso")
        self.assertEqual(
            response.context["redirect_url"], "/api/auth/v1/auth/provider/redirect"
        )
        self.assertEqual(
            response.context["callback_url"], f"http://testserver{self.next}"
        )
        self.assertContains(response, 'name="provider" value="authentik"')
        self.assertContains(response, 'name="process" value="login"')
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertIn("csrftoken", response.cookies)

    def test_manual_mode_without_sso(self):
        with patch(
            "inventree_mcp.auth.login_bridge.sso_provider_id", return_value=None
        ):
            response = self._get(self.next)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["mode"], "manual")
        self.assertContains(response, "connect again")

    def test_force_sso_off_uses_manual_mode(self):
        self.set_setting("OAUTH_FORCE_SSO", False)
        with patch(
            "inventree_mcp.auth.login_bridge.sso_provider_id", return_value="authentik"
        ):
            response = self._get(self.next)
        self.assertEqual(response.context["mode"], "manual")

    def test_invalid_next_is_rejected(self):
        for bad in (
            "",
            "https://evil.example/plugin/inventree-mcp/oauth/authorize/",
            "/plugin/inventree-mcp/mcp/",
            "//evil.example/plugin/inventree-mcp/oauth/authorize/",
        ):
            with self.subTest(next=bad):
                response = self._get(bad)
                self.assertEqual(response.status_code, 400)
                self.assertTemplateUsed(response, "inventree_mcp/error.html")

    def test_signed_in_user_is_sent_to_next(self):
        browser = Client()
        browser.force_login(self.user)

        response = self._get(self.next, client=browser)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.next)
