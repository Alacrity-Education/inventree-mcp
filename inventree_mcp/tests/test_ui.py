"""WP4: the plugin's UI features (routes, navigation, group panel) and the UI feature API."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from common.settings import set_global_setting
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from django.urls import reverse
from InvenTree.unit_test import InvenTreeAPITestCase
from plugin.registry import registry

from ..core import UI_ROUTES_SUPPORTED

User = get_user_model()

STATIC = Path(__file__).resolve().parent.parent / "static"


class UiTestCase(InvenTreeAPITestCase):
    # The plugin's staff API needs staff plus the admin role's change permission.
    roles = ("admin.change",)
    superuser = False
    is_staff = True

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # load and activate the plugin, as test_mcp.py does
        registry.reload_plugins(full_reload=True, collect=True)
        registry.set_plugin_state("inventree-mcp", True)
        cls.plain = User.objects.create_user(username="ui-plain", password="pw")
        # Staff without the admin role: every plugin staff endpoint answers 403.
        cls.staff_only = User.objects.create_user(
            username="ui-staff", password="pw", is_staff=True
        )

    def setUp(self):
        super().setUp()
        self.plugin = registry.get_plugin("inventree-mcp")
        self.assertIsNotNone(self.plugin)
        self.factory = RequestFactory()

    def request_as(self, user):
        request = self.factory.get("/")
        request.user = user
        return request

    def names(self, items, key="key"):
        return [item[key] for item in items]


class UiFeatureMethodsTest(UiTestCase):
    def test_bundles_are_built(self):
        for name in ("Groups.js", "Connections.js", "Panel.js"):
            self.assertTrue(
                (STATIC / name).is_file(), f"{name}: run the frontend build"
            )

    def test_routes_staff(self):
        routes = self.plugin.get_ui_routes(self.request_as(self.user), {})
        self.assertEqual(
            {r["options"]["path"] for r in routes}, {"groups", "connections"}
        )
        sources = {r["options"]["path"]: r["source"] for r in routes}
        self.assertIn("Groups", sources["groups"])
        self.assertTrue(sources["groups"].endswith(":renderGroupsPage"))
        self.assertTrue(sources["connections"].endswith(":renderConnectionsPage"))

    def test_routes_non_staff(self):
        routes = self.plugin.get_ui_routes(self.request_as(self.plain), {})
        self.assertEqual([r["options"]["path"] for r in routes], ["connections"])

    @mock.patch("inventree_mcp.core.UI_ROUTES_SUPPORTED", True)
    def test_staff_without_admin_role_sees_no_staff_entries(self):
        """Finding 8: the UI entries follow IsStaffAdmin, not is_staff."""
        request = self.request_as(self.staff_only)
        self.assertEqual(
            [r["options"]["path"] for r in self.plugin.get_ui_routes(request, {})],
            ["connections"],
        )
        self.assertEqual(
            [i["title"] for i in self.plugin.get_ui_navigation_items(request, {})],
            ["MCP"],
        )
        self.assertEqual(
            self.plugin.get_ui_panels(request, {"target_model": "group"}), []
        )

    def test_routes_anonymous(self):
        from django.contrib.auth.models import AnonymousUser

        request = self.request_as(AnonymousUser())
        self.assertEqual(self.plugin.get_ui_routes(request, {}), [])
        self.assertEqual(self.plugin.get_ui_navigation_items(request, {}), [])

    def test_navigation_without_route_support(self):
        with mock.patch("inventree_mcp.core.UI_ROUTES_SUPPORTED", False):
            self.assertEqual(
                self.plugin.get_ui_navigation_items(self.request_as(self.user), {}), []
            )

    def test_no_route_support_is_logged_once_per_process(self):
        """Finding 6: the web UI asks for navigation items on every page load."""
        with (
            mock.patch("inventree_mcp.core.UI_ROUTES_SUPPORTED", False),
            mock.patch("inventree_mcp.core._logged_once", set()),
            self.assertLogs("inventree", level="INFO") as logs,
        ):
            for _page_load in range(3):
                self.plugin.get_ui_navigation_items(self.request_as(self.user), {})
        lines = [line for line in logs.output if "no plugin routes" in line]
        self.assertEqual(len(lines), 1, logs.output)

    @mock.patch("inventree_mcp.core.UI_ROUTES_SUPPORTED", True)
    def test_navigation(self):
        staff = self.plugin.get_ui_navigation_items(self.request_as(self.user), {})
        self.assertEqual([i["title"] for i in staff], ["MCP Access", "MCP"])
        self.assertEqual(staff[0]["icon"], "ti:plug-connected")
        self.assertEqual(
            [i["options"]["url"] for i in staff],
            ["plugin/inventree-mcp/groups", "plugin/inventree-mcp/connections"],
        )
        plain = self.plugin.get_ui_navigation_items(self.request_as(self.plain), {})
        self.assertEqual([i["title"] for i in plain], ["MCP"])

    def test_panels(self):
        staff = self.request_as(self.user)
        panels = self.plugin.get_ui_panels(staff, {"target_model": "group"})
        self.assertEqual([p["title"] for p in panels], ["MCP Roles"])
        self.assertTrue(panels[0]["source"].endswith(":renderMCPRolesPanel"))
        self.assertEqual(self.plugin.get_ui_panels(staff, {"target_model": "part"}), [])
        self.assertEqual(self.plugin.get_ui_panels(staff, {}), [])
        self.assertEqual(self.plugin.get_ui_panels(staff, None), [])
        self.assertEqual(
            self.plugin.get_ui_panels(
                self.request_as(self.plain), {"target_model": "group"}
            ),
            [],
        )

    def test_missing_build_does_not_raise(self):
        with (
            mock.patch("pathlib.Path.is_file", return_value=False),
            mock.patch("inventree_mcp.core._logged_once", set()),
            self.assertLogs("inventree", level="WARNING") as logs,
        ):
            for _page_load in range(2):
                self.assertEqual(
                    self.plugin.get_ui_routes(self.request_as(self.user), {}), []
                )
                self.assertEqual(
                    self.plugin.get_ui_panels(
                        self.request_as(self.user), {"target_model": "group"}
                    ),
                    [],
                )
        # One warning per missing bundle and process (finding 6).
        missing = sorted(line for line in logs.output if "is missing" in line)
        self.assertEqual(len(missing), 3, logs.output)
        for name, line in zip(
            ("Connections.js", "Groups.js", "Panel.js"), missing, strict=True
        ):
            self.assertIn(name, line)
        # navigation does not depend on the build
        with mock.patch("inventree_mcp.core.UI_ROUTES_SUPPORTED", True):
            self.assertEqual(
                len(
                    self.plugin.get_ui_navigation_items(self.request_as(self.user), {})
                ),
                2,
            )


class UiFeatureApiTest(UiTestCase):
    def setUp(self):
        super().setUp()
        # the UI feature API serves nothing unless the plugin interface is enabled
        set_global_setting("ENABLE_PLUGINS_INTERFACE", True)

    def features(self, feature, **params):
        url = reverse("api-plugin-ui-feature-list", kwargs={"feature": feature})
        return self.get(url, params, expected_code=200).data

    def mine(self, features):
        return [f for f in features if f["plugin_name"] == "inventree-mcp"]

    def test_staff_session(self):
        routes = self.mine(self.features("route"))
        nav = self.mine(self.features("navigation"))
        if UI_ROUTES_SUPPORTED:
            self.assertEqual(
                {r["options"]["path"] for r in routes}, {"groups", "connections"}
            )
            self.assertEqual({n["title"] for n in nav}, {"MCP Access", "MCP"})
        else:  # InvenTree 1.5.x: no plugin routes, so no navigation links to them
            self.assertEqual((routes, nav), ([], []))
        panels = self.mine(self.features("panel", target_model="group", target_id=1))
        self.assertEqual([p["title"] for p in panels], ["MCP Roles"])
        self.assertEqual(self.mine(self.features("panel", target_model="part")), [])

    def test_staff_without_admin_role_session(self):
        self.client.logout()
        self.client.force_login(self.staff_only)
        routes = self.mine(self.features("route"))
        if UI_ROUTES_SUPPORTED:
            self.assertEqual([r["options"]["path"] for r in routes], ["connections"])
        self.assertEqual(self.mine(self.features("panel", target_model="group")), [])

    def test_non_staff_session(self):
        self.client.logout()
        self.client.force_login(self.plain)
        routes = self.mine(self.features("route"))
        nav = self.mine(self.features("navigation"))
        if UI_ROUTES_SUPPORTED:
            self.assertEqual([r["options"]["path"] for r in routes], ["connections"])
            self.assertEqual([n["title"] for n in nav], ["MCP"])
        else:
            self.assertEqual((routes, nav), ([], []))
        self.assertEqual(self.mine(self.features("panel", target_model="group")), [])
