"""The façade's token and revocation endpoints, and membership.revoke_all_mcp_tokens()."""

from __future__ import annotations

import datetime
import hashlib
from typing import ClassVar
from unittest.mock import patch

from django.test import Client, RequestFactory, override_settings
from django.utils import timezone
from InvenTree.unit_test import InvenTreeTestCase
from oauth2_provider.models import (
    AccessToken,
    RefreshToken,
    get_application_model,
)

from ..auth import membership, policy
from ..auth.token import MCPTokenView
from .oauth_helpers import OAuthFacadeTestMixin, make_token_pair, url

Application = get_application_model()


@override_settings(PLUGIN_TESTING_SETUP=True)
class TokenEndpointTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """POST /plugin/inventree-mcp/oauth/token/ and /oauth/revoke/."""

    roles: ClassVar[list[str]] = ["part.view", "part_category.view"]

    def setUp(self):
        super().setUp()
        self.client_id = self.register_public_client()
        self.browser = Client()
        self.browser.force_login(self.user)

    def _tokens(self, scope: str | None = None) -> dict:
        code, verifier = self.obtain_code(self.browser, self.client_id, scope=scope)
        response = self.exchange_code(self.client_id, code, verifier)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_code_exchange_issues_tokens_with_configured_lifetime(self):
        self.set_setting("ACCESS_TOKEN_LIFETIME_SECONDS", 1800)

        tokens = self._tokens()

        self.assertEqual(tokens["token_type"], "Bearer")
        self.assertEqual(tokens["expires_in"], 1800)
        self.assertTrue(tokens["access_token"])
        self.assertTrue(tokens["refresh_token"])
        self.assertEqual(
            set(tokens["scope"].split()), set(policy.mcp_scopes_available())
        )

    def test_access_token_works_on_mcp_and_the_rest_api(self):
        tokens = self._tokens()

        response = self.mcp_call(tokens["access_token"])
        self.assertEqual(response.status_code, 200, response.content)
        tools = response.json()["result"]["tools"]
        self.assertTrue(tools)
        self.assertIn("list_parts", {tool["name"] for tool in tools})

        response = Client().get(
            "/api/part/", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}"
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_wrong_verifier_is_rejected(self):
        code, _verifier = self.obtain_code(self.browser, self.client_id)
        response = self.exchange_code(self.client_id, code, "x" * 50)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_blocked_user_cannot_exchange_a_code(self):
        code, verifier = self.obtain_code(self.browser, self.client_id)
        with patch(
            "inventree_mcp.auth.membership.token_grant_allowed",
            return_value=(False, "invalid_grant"),
        ):
            response = self.exchange_code(self.client_id, code, verifier)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_refresh_rotates_and_old_token_is_rejected(self):
        tokens = self._tokens()

        response = self.refresh(self.client_id, tokens["refresh_token"])
        self.assertEqual(response.status_code, 200, response.content)
        rotated = response.json()
        self.assertNotEqual(rotated["refresh_token"], tokens["refresh_token"])
        self.assertNotEqual(rotated["access_token"], tokens["access_token"])
        self.assertEqual(self.mcp_call(rotated["access_token"]).status_code, 200)
        self.assertEqual(self.mcp_call(tokens["access_token"]).status_code, 401)

        # Within DOT's grace period a rotated token is re-served the same pair
        # (a client retrying after a lost response), nothing new is minted.
        response = self.refresh(self.client_id, tokens["refresh_token"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["access_token"], rotated["access_token"])
        self.assertEqual(response.json()["refresh_token"], rotated["refresh_token"])

        # Past DOT's grace period (REFRESH_TOKEN_GRACE_PERIOD_SECONDS=120) the old
        # refresh token is rejected, and reuse protection revokes the whole family.
        self._expire_grace_period()
        response = self.refresh(self.client_id, tokens["refresh_token"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

        self._expire_grace_period()
        self.assertEqual(
            self.refresh(self.client_id, rotated["refresh_token"]).status_code, 400
        )

    @staticmethod
    def _expire_grace_period():
        RefreshToken.objects.filter(revoked__isnull=False).update(
            revoked=timezone.now() - datetime.timedelta(minutes=5)
        )

    def test_refresh_re_applies_narrowed_scope(self):
        tokens = self._tokens(scope="g:read r:view:part r:view:stock")

        with patch(
            "inventree_mcp.auth.policy.effective_scopes",
            return_value={"g:read", "r:view:part"},
        ):
            response = self.refresh(self.client_id, tokens["refresh_token"])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["scope"], "g:read r:view:part")
        checksum = hashlib.sha256(response.json()["access_token"].encode()).hexdigest()
        token = AccessToken.objects.get(token_checksum=checksum)
        self.assertEqual(token.scope, "g:read r:view:part")

    def test_refresh_without_any_mcp_scope_left_revokes(self):
        tokens = self._tokens()

        with patch("inventree_mcp.auth.policy.effective_scopes", return_value=set()):
            response = self.refresh(self.client_id, tokens["refresh_token"])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertEqual(self.mcp_call(tokens["access_token"]).status_code, 401)

    def test_refresh_refused_by_membership(self):
        tokens = self._tokens()

        with patch(
            "inventree_mcp.auth.membership.refresh_grant_allowed",
            return_value=(False, "invalid_grant"),
        ):
            response = self.refresh(self.client_id, tokens["refresh_token"])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")
        # Refused before DOT ran: nothing was rotated.
        self.assertEqual(self.mcp_call(tokens["access_token"]).status_code, 200)

    def test_unsupported_grant_type(self):
        response = Client().post(
            url("oauth-token"),
            data={"grant_type": "client_credentials", "client_id": self.client_id},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "unsupported_grant_type")

    def test_revoke_endpoint_revokes(self):
        tokens = self._tokens()

        response = Client().post(
            url("oauth-revoke"),
            data={
                "token": tokens["refresh_token"],
                "token_type_hint": "refresh_token",
                "client_id": self.client_id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mcp_call(tokens["access_token"]).status_code, 401)
        # Refused immediately: DOT's grace period would otherwise mint a new,
        # unscoped pair for a token that was revoked rather than rotated.
        response = self.refresh(self.client_id, tokens["refresh_token"])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertFalse(
            AccessToken.objects.filter(
                user=self.user, application__client_id=self.client_id
            ).exists()
        )

    def test_refresh_after_revoke_all_mcp_tokens_is_refused(self):
        """The path WP2's block_user() relies on."""
        tokens = self._tokens()

        membership.revoke_all_mcp_tokens(self.user)

        response = self.refresh(self.client_id, tokens["refresh_token"])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_membership_gate_runs_for_rotated_tokens_too(self):
        tokens = self._tokens()
        self.assertEqual(
            self.refresh(self.client_id, tokens["refresh_token"]).status_code, 200
        )

        with patch(
            "inventree_mcp.auth.membership.refresh_grant_allowed",
            return_value=(False, "invalid_grant"),
        ) as gate:
            response = self.refresh(self.client_id, tokens["refresh_token"])

        gate.assert_called_once_with(self.user)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_stale_token_replayed_in_grace_revokes_the_family(self):
        """R1: RT1 -> RT2 -> RT3, then RT1 again inside the grace window."""
        tokens = self._tokens()
        second = self.refresh(self.client_id, tokens["refresh_token"]).json()
        third = self.refresh(self.client_id, second["refresh_token"]).json()
        self.assertEqual(self.mcp_call(third["access_token"]).status_code, 200)

        response = self.refresh(self.client_id, tokens["refresh_token"])

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")
        checksum = hashlib.sha256(third["refresh_token"].encode()).hexdigest()
        self.assertIsNotNone(RefreshToken.objects.get(token_checksum=checksum).revoked)
        self.assertEqual(self.mcp_call(third["access_token"]).status_code, 401)
        self.assertEqual(
            self.refresh(self.client_id, third["refresh_token"]).status_code, 400
        )

    def test_dead_tokens_never_reach_the_membership_gate(self):
        """A revoked token that is not a grace-window rotation triggers no re-check."""
        revoked = self._tokens()
        self.assertEqual(
            Client()
            .post(
                url("oauth-revoke"),
                data={"token": revoked["refresh_token"], "client_id": self.client_id},
            )
            .status_code,
            200,
        )
        rotated_away = self._tokens()
        self.refresh(self.client_id, rotated_away["refresh_token"])

        with (
            patch(
                "inventree_mcp.auth.membership.refresh_grant_allowed",
                wraps=membership.refresh_grant_allowed,
            ) as gate,
            patch("inventree_mcp.auth.membership.recheck") as recheck,
        ):
            # revoked inside the grace window (no successor)
            response = self.refresh(self.client_id, revoked["refresh_token"])
            self.assertEqual(response.status_code, 400, response.content)
            # rotated, past the grace window (DOT refuses it)
            self._expire_grace_period()
            response = self.refresh(self.client_id, rotated_away["refresh_token"])
            self.assertEqual(response.status_code, 400, response.content)
            self.assertEqual(response.json()["error"], "invalid_grant")

        gate.assert_not_called()
        recheck.assert_not_called()

    def test_unknown_original_scope_is_refused(self):
        tokens = self._tokens()
        checksum = hashlib.sha256(tokens["access_token"].encode()).hexdigest()
        AccessToken.objects.filter(token_checksum=checksum).delete()

        response = self.refresh(self.client_id, tokens["refresh_token"])

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_empty_scope_parameter_keeps_the_original_scope(self):
        tokens = self._tokens(scope="g:read r:view:part")

        response = self.refresh(self.client_id, tokens["refresh_token"], scope="")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["scope"], "g:read r:view:part")

    def test_other_applications_are_left_to_dot(self):
        """No policy hook (and no revocation) for tokens of non-MCP applications."""
        manual = Application.objects.create(
            name="manual",
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://client.example/callback",
        )
        _access, refresh_token = make_token_pair(manual, self.user, scope="a:staff")

        with patch("inventree_mcp.auth.policy.effective_scopes", return_value=set()):
            response = self.refresh(manual.client_id, refresh_token.token)

        self.assertEqual(response.status_code, 401)  # DOT: invalid_client, no secret
        refresh_token.refresh_from_db()
        self.assertIsNone(refresh_token.revoked)

    def test_cors_for_trusted_origin_only(self):
        """TrustedOriginCORSMixin, called on the view directly.

        Through the full middleware stack, InvenTree's CORS middleware
        (django-cors-headers, which covers /plugin/) answers real browser
        preflights itself and rewrites the CORS headers of responses for the
        origins it is configured with; the mixin covers TRUSTED_CLIENT_ORIGINS
        independently of that configuration.
        """
        view = MCPTokenView.as_view()
        factory = RequestFactory()

        trusted = view(factory.options("/", HTTP_ORIGIN="https://claude.ai"))
        self.assertEqual(trusted.status_code, 204)
        self.assertEqual(trusted["Access-Control-Allow-Origin"], "https://claude.ai")
        self.assertIn("POST", trusted["Access-Control-Allow-Methods"])
        self.assertEqual(
            trusted["Access-Control-Allow-Headers"], "Authorization, Content-Type"
        )
        self.assertEqual(trusted["Access-Control-Max-Age"], "3600")

        untrusted = view(factory.options("/", HTTP_ORIGIN="https://evil.example"))
        self.assertNotIn("Access-Control-Allow-Origin", untrusted)

        code, verifier = self.obtain_code(self.browser, self.client_id)
        request = factory.post(
            "/",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://client.example/callback",
                "client_id": self.client_id,
                "code_verifier": verifier,
            },
            HTTP_ORIGIN="https://claude.ai",
        )
        response = view(request)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://claude.ai")

        response = view(
            factory.post(
                "/",
                data={"grant_type": "refresh_token", "refresh_token": "unknown"},
                HTTP_ORIGIN="https://evil.example",
            )
        )
        self.assertEqual(response.status_code, 401)  # invalid_client: no client_id
        self.assertNotIn("Access-Control-Allow-Origin", response)

    def test_disabled_facade_answers_404(self):
        self.set_setting("OAUTH_ENABLED", False)
        for name in ("oauth-token", "oauth-revoke"):
            with self.subTest(name=name):
                self.assertEqual(Client().post(url(name)).status_code, 404)


@override_settings(PLUGIN_TESTING_SETUP=True)
class RevokeAllMcpTokensTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """membership.revoke_all_mcp_tokens() touches only façade applications."""

    def test_only_facade_tokens_are_revoked(self):
        dcr = Application.objects.create(
            name="dcr",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://client.example/callback",
            registration_source=Application.RegistrationSource.DCR,
        )
        cimd = self.create_cimd_application()
        manual = Application.objects.create(
            name="manual",
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://client.example/callback",
        )
        dcr_access, dcr_refresh = make_token_pair(dcr, self.user)
        cimd_access, cimd_refresh = make_token_pair(cimd, self.user)
        manual_access, manual_refresh = make_token_pair(manual, self.user)
        lone_access, _ = make_token_pair(dcr, self.user, refresh=False)

        count = membership.revoke_all_mcp_tokens(self.user)

        self.assertEqual(
            count, 3
        )  # two refresh tokens (+ their access tokens), one lone access token
        for refresh in (dcr_refresh, cimd_refresh):
            refresh.refresh_from_db()
            self.assertIsNotNone(refresh.revoked)
        for access in (dcr_access, cimd_access, lone_access):
            self.assertFalse(AccessToken.objects.filter(pk=access.pk).exists())

        manual_refresh.refresh_from_db()
        self.assertIsNone(manual_refresh.revoked)
        self.assertTrue(AccessToken.objects.filter(pk=manual_access.pk).exists())
