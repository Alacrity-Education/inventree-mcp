"""Dynamic client registration through the façade, client helpers, stale-client cleanup."""

from __future__ import annotations

import datetime

from django.test import Client, override_settings
from django.utils import timezone
from InvenTree.unit_test import InvenTreeTestCase
from oauth2_provider.models import get_application_model

from ..auth import clients
from ..auth.tasks import cleanup_stale_clients
from .oauth_helpers import REDIRECT_URI, OAuthFacadeTestMixin, make_token_pair, url

Application = get_application_model()


@override_settings(PLUGIN_TESTING_SETUP=True)
class RegistrationTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """POST /plugin/inventree-mcp/oauth/register/."""

    def test_dot_helpers_present(self):
        """The façade reuses DOT's private DCR helpers; a DOT upgrade must not drop them."""
        from oauth2_provider.views import dynamic_client_registration as dcr

        for name in (
            "_parse_metadata",
            "_build_application_kwargs",
            "_issue_registration_token",
            "_application_to_response",
            "_error_response",
            "_validation_error_description",
        ):
            with self.subTest(helper=name):
                self.assertTrue(callable(getattr(dcr, name, None)), name)

    def test_public_client_registration(self):
        response = self.register()

        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertIn("client_id", data)
        self.assertNotIn("client_secret", data)
        self.assertEqual(data["client_secret_expires_at"], 0)
        self.assertIsInstance(data["client_id_issued_at"], int)
        self.assertEqual(data["redirect_uris"], [REDIRECT_URI])
        self.assertEqual(data["token_endpoint_auth_method"], "none")
        self.assertEqual(data["grant_types"], ["authorization_code", "refresh_token"])
        self.assertEqual(data["client_name"], "Test MCP client")
        self.assertIn("registration_access_token", data)

        app = Application.objects.get(client_id=data["client_id"])
        self.assertEqual(app.registration_source, Application.RegistrationSource.DCR)
        self.assertEqual(app.client_type, Application.CLIENT_PUBLIC)
        self.assertIsNone(app.user)
        self.assertFalse(app.skip_authorization)
        self.assertEqual(app.allowed_origins, "https://claude.ai https://claude.com")

    def test_missing_client_name_gets_a_default(self):
        response = self.register(client_name="")
        app = Application.objects.get(client_id=response.json()["client_id"])
        self.assertEqual(app.name, "MCP client")

    def test_confidential_client_gets_a_secret(self):
        response = self.register(token_endpoint_auth_method="client_secret_post")

        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertTrue(data["client_secret"])
        app = Application.objects.get(client_id=data["client_id"])
        self.assertEqual(app.client_type, Application.CLIENT_CONFIDENTIAL)

    def test_invalid_redirect_uri_is_rejected(self):
        response = self.register(redirect_uris=["ftp://bad.example/cb"])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_client_metadata")

    def test_only_the_authorization_code_grant_can_register(self):
        response = self.register(grant_types=["client_credentials"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_client_metadata")

    def test_malformed_body_is_rejected_like_dot(self):
        response = Client().post(
            url("oauth-register"), data="not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_client_metadata")

    def test_rate_limit(self):
        self.set_setting("REGISTER_RATE_LIMIT_PER_MINUTE", 2)

        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.register().status_code, 201)
        response = self.register()

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"error": "too_many_requests"})
        self.assertTrue(1 <= int(response["Retry-After"]) <= 60)

    def test_rate_limit_zero_disables_it(self):
        self.set_setting("REGISTER_RATE_LIMIT_PER_MINUTE", 0)
        for _ in range(3):
            self.assertEqual(self.register().status_code, 201)

    def test_anonymous_registration_needs_no_session_or_csrf(self):
        response = self.register(client=Client(enforce_csrf_checks=True))
        self.assertEqual(response.status_code, 201)

    def test_disabled_facade_answers_404(self):
        self.set_setting("OAUTH_ENABLED", False)
        self.assertEqual(self.register().status_code, 404)


@override_settings(PLUGIN_TESTING_SETUP=True)
class ClientHelperTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """is_trusted_client(), is_loopback_redirect(), mcp_applications()."""

    def test_is_loopback_redirect(self):
        for uri in (
            "http://localhost:3000/cb",
            "http://127.0.0.1:5555/callback",
            "http://[::1]:8080/x",
        ):
            self.assertTrue(clients.is_loopback_redirect(uri), uri)
        for uri in (
            "https://localhost/cb",
            "https://claude.ai/api/mcp/auth_callback",
            "http://example.com/cb",
            "",
        ):
            self.assertFalse(clients.is_loopback_redirect(uri), uri)

    def test_is_trusted_client(self):
        trusted = self.create_cimd_application()
        subdomain = self.create_cimd_application(
            client_id="https://console.anthropic.com/client.json"
        )
        untrusted = self.create_cimd_application(
            client_id="https://evil.example/client.json"
        )
        dcr = Application.objects.create(
            name="dcr",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris=REDIRECT_URI,
            registration_source=Application.RegistrationSource.DCR,
        )

        self.assertTrue(clients.is_trusted_client(trusted))
        self.assertTrue(clients.is_trusted_client(subdomain))
        self.assertFalse(clients.is_trusted_client(untrusted))
        self.assertFalse(clients.is_trusted_client(dcr))

        self.set_setting("TRUSTED_CLIENT_HOSTS", "claude.ai")
        self.assertFalse(clients.is_trusted_client(subdomain))

    def test_mcp_applications_excludes_manual_ones(self):
        cimd = self.create_cimd_application()
        manual = Application.objects.create(
            name="manual",
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
        )

        ids = set(clients.mcp_applications().values_list("pk", flat=True))
        self.assertIn(cimd.pk, ids)
        self.assertNotIn(manual.pk, ids)


@override_settings(PLUGIN_TESTING_SETUP=True)
class CleanupStaleClientsTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """auth.tasks.cleanup_stale_clients()."""

    def _dcr_app(self, name: str, *, old: bool = True):
        response = self.register(client_name=name)
        app = Application.objects.get(client_id=response.json()["client_id"])
        if old:
            Application.objects.filter(pk=app.pk).update(
                updated=timezone.now() - datetime.timedelta(days=120)
            )
        return app

    def test_deletes_only_unused_old_dcr_clients(self):
        unused = self._dcr_app("unused")  # only DOT's registration token
        expired = self._dcr_app("expired-token")
        make_token_pair(expired, self.user, expired=True, refresh=False)
        live_access = self._dcr_app("live-access")
        make_token_pair(live_access, self.user, refresh=False)
        live_refresh = self._dcr_app("live-refresh")
        make_token_pair(live_refresh, self.user, expired=True)
        recent = self._dcr_app("recent", old=False)

        cimd = self.create_cimd_application()
        manual = Application.objects.create(
            name="manual",
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
        )
        Application.objects.filter(pk__in=[cimd.pk, manual.pk]).update(
            updated=timezone.now() - datetime.timedelta(days=400)
        )

        deleted = cleanup_stale_clients()

        self.assertEqual(deleted, 2)
        remaining = set(Application.objects.values_list("pk", flat=True))
        self.assertNotIn(unused.pk, remaining)
        self.assertNotIn(expired.pk, remaining)
        for kept in (live_access, live_refresh, recent, cimd, manual):
            self.assertIn(kept.pk, remaining)

    def test_retention_setting_is_respected(self):
        app = self._dcr_app("unused")
        self.set_setting("UNUSED_CLIENT_RETENTION_DAYS", 365)

        self.assertEqual(cleanup_stale_clients(), 0)
        self.assertTrue(Application.objects.filter(pk=app.pk).exists())
