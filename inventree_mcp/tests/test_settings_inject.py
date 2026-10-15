"""auth.settings_inject: DOT settings follow the plugin settings at runtime."""

from __future__ import annotations

from unittest.mock import patch

from django.conf import settings
from django.test import Client, override_settings
from django_q.signals import pre_execute
from InvenTree.unit_test import InvenTreeTestCase
from oauth2_provider.settings import oauth2_settings
from plugin import registry
from plugin.models import PluginSetting

from ..auth import settings_inject
from ..auth.token import MCPTokenView
from ..core import InvenTreeMCP
from ..settings import get_plugin_setting
from .oauth_helpers import OAuthFacadeTestMixin, url


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

    def test_disabled_facade_view_restores_dot_settings(self):
        """FacadeViewMixin syncs before its 404, so switching off takes effect (review F3)."""
        settings_inject.ensure_applied()
        self.assertIs(oauth2_settings.CIMD_ENABLED, True)

        self.set_setting("OAUTH_ENABLED", False)
        response = Client().get(url("oauth-authorization-server"))

        self.assertEqual(response.status_code, 404)
        self.assertIs(oauth2_settings.CIMD_ENABLED, False)
        self.assertEqual(oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS, 36000)

    def test_desired_values_are_memoised_between_saves(self):
        """F16: no setting reads per request; a save in this process invalidates."""
        self.set_setting(
            "ACCESS_TOKEN_LIFETIME_SECONDS", 1000
        )  # post_save: invalidated
        settings_inject.ensure_applied()
        self.assertEqual(oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS, 1000)

        # update() sends no signal, like a change made by another process.
        PluginSetting.objects.filter(
            plugin=self.plugin.plugin_config(), key="ACCESS_TOKEN_LIFETIME_SECONDS"
        ).update(value="777")

        with patch.object(settings_inject, "desired_values") as desired:
            settings_inject.ensure_applied()
            desired.assert_not_called()

        later = settings_inject.time.monotonic() + 60
        with patch.object(settings_inject.time, "monotonic", return_value=later):
            settings_inject.ensure_applied()
        self.assertEqual(oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS, 777)


def _forget_injection():
    """A process that has not injected anything yet: DOT as InvenTree configured it."""
    settings_inject._apply({})
    settings_inject._last_applied = None
    settings_inject.invalidate()


@override_settings(PLUGIN_TESTING_SETUP=True)
class FreshProcessTest(OAuthFacadeTestMixin, InvenTreeTestCase):
    """Injection in processes that never served a façade request (review F3)."""

    def setUp(self):
        super().setUp()
        self.set_setting("REFRESH_TOKEN_LIFETIME_DAYS", 7)
        _forget_injection()
        self.assertIsNone(oauth2_settings.REFRESH_TOKEN_EXPIRE_SECONDS)

    def assertFacadeValues(self):
        self.assertIs(oauth2_settings.CIMD_ENABLED, True)
        self.assertEqual(oauth2_settings.REFRESH_TOKEN_EXPIRE_SECONDS, 7 * 86400)
        self.assertEqual(oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS, 3600)

    def test_plugin_init_injects_while_the_registry_is_loading(self):
        """At start-up the registry holds no instance yet; settings come from the DB."""
        with patch.dict(registry.plugins):
            del registry.plugins["inventree-mcp"]
            self.assertIs(get_plugin_setting("OAUTH_ENABLED", False), True)
            self.assertEqual(get_plugin_setting("REFRESH_TOKEN_LIFETIME_DAYS", 30), 7)

            InvenTreeMCP()

        self.assertFacadeValues()

    def test_inactive_plugin_injects_nothing(self):
        # (patched: the dev stack lists the plugin in PLUGINS_MANDATORY)
        with (
            patch("plugin.models.PluginConfig.is_active", return_value=False),
            patch.dict(registry.plugins),
        ):
            del registry.plugins["inventree-mcp"]
            self.assertIs(get_plugin_setting("OAUTH_ENABLED", False), False)
            self.assertEqual(settings_inject.apply(), {})

    def test_scheduled_task_sees_the_facade_values(self):
        """django-q sends pre_execute in the worker process before running a task.

        This is what InvenTree's daily run_oauth_maintenance (DOT clear_expired,
        which enforces REFRESH_TOKEN_EXPIRE_SECONDS) relies on.
        """
        settings_inject.connect_signals()  # idempotent; also done by the plugin

        pre_execute.send(
            sender="django_q",
            func="InvenTree.tasks.run_oauth_maintenance",
            task={"name": "run_oauth_maintenance"},
        )

        self.assertFacadeValues()
