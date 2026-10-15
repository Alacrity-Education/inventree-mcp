"""auth.settings_inject: DOT settings follow the plugin settings at runtime."""

from __future__ import annotations

from django.conf import settings
from django.test import override_settings
from InvenTree.unit_test import InvenTreeTestCase
from oauth2_provider.settings import oauth2_settings

from ..auth import settings_inject
from ..auth.token import MCPTokenView
from .oauth_helpers import OAuthFacadeTestMixin


@override_settings(PLUGIN_TESTING_SETUP=True)
class SettingsInjectTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """apply() / ensure_applied()."""

    def test_apply_injects_the_plugin_settings(self):
        self.set_setting("ACCESS_TOKEN_LIFETIME_SECONDS", 1234)
        self.set_setting("REFRESH_TOKEN_LIFETIME_DAYS", 7)
        self.set_setting("TRUSTED_CLIENT_HOSTS", "claude.ai, .anthropic.com")

        values = settings_inject.apply()

        self.assertIs(oauth2_settings.CIMD_ENABLED, True)
        self.assertEqual(oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS, 1234)
        self.assertEqual(oauth2_settings.REFRESH_TOKEN_EXPIRE_SECONDS, 7 * 86400)
        self.assertEqual(oauth2_settings.AUTHORIZATION_CODE_EXPIRE_SECONDS, 600)
        self.assertEqual(
            oauth2_settings.CIMD_ALLOWED_HOSTS, ["claude.ai", ".anthropic.com"]
        )
        self.assertIs(oauth2_settings.REFRESH_TOKEN_REUSE_PROTECTION, True)
        self.assertEqual(oauth2_settings.REFRESH_TOKEN_GRACE_PERIOD_SECONDS, 120)
        self.assertIs(oauth2_settings.ALLOW_LOCALHOST_LOOPBACK, True)
        self.assertNotIn("DCR_ENABLED", values)
        self.assertNotIn("OIDC_ISS_ENDPOINT", values)
        self.assertIs(oauth2_settings.DCR_ENABLED, False)
        # InvenTree's own keys are left alone.
        self.assertIn("SCOPES", settings.OAUTH2_PROVIDER)

    def test_ensure_applied_follows_setting_changes(self):
        self.set_setting("ACCESS_TOKEN_LIFETIME_SECONDS", 1234)
        settings_inject.ensure_applied()
        self.assertEqual(oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS, 1234)

        MCPTokenView.get_oauthlib_core()  # cache a core built with the old lifetime
        self.set_setting("ACCESS_TOKEN_LIFETIME_SECONDS", 4321)
        settings_inject.ensure_applied()

        self.assertEqual(oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS, 4321)
        self.assertNotIn("_oauthlib_core", MCPTokenView.__dict__)

    def test_disabling_restores_what_inventree_configured(self):
        settings_inject.ensure_applied()
        self.assertIs(oauth2_settings.CIMD_ENABLED, True)

        self.set_setting("OAUTH_ENABLED", False)
        settings_inject.ensure_applied()

        self.assertIs(oauth2_settings.CIMD_ENABLED, False)
        self.assertNotIn("CIMD_ENABLED", settings.OAUTH2_PROVIDER)
        self.assertEqual(oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS, 36000)
