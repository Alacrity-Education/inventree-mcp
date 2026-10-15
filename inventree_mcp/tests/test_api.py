"""WP3 test 6: the MCP permission API (auth/api.py) and the ruleset rows."""

from __future__ import annotations

import importlib
from typing import ClassVar

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from InvenTree.unit_test import InvenTreeTestCase
from oauth2_provider.models import RefreshToken
from users.models import RuleSet
from users.ruleset import RULESET_NAMES

from ..models import MCPRuleSet
from .oauth_helpers import OAuthFacadeTestMixin, grant_mcp, make_token_pair, url

User = get_user_model()


class ApiTestCase(OAuthFacadeTestMixin, InvenTreeTestCase):
    """self.user: staff with admin.change (the "staff admin"); self.plain: not staff, own group."""

    roles: ClassVar[list[str]] = ["admin.change", "part.view", "stock.view"]
    mcp_roles: ClassVar[tuple[str, ...]] = ("part.view",)

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.plain = User.objects.create_user(username="wp3-plain", password="pw")
        cls.plain_group = Group.objects.create(name="WP3 plain")
        grant_mcp(cls.plain_group, "part.view")
        cls.plain.groups.add(cls.plain_group)

    def setUp(self):
        super().setUp()
        self.plain_client = Client()
        self.plain_client.force_login(self.plain)
        self.application = self.create_cimd_application()

    def rows(self, group=None) -> dict[str, MCPRuleSet]:
        return {
            row.name: row
            for row in MCPRuleSet.objects.filter(group=group or self.group)
        }


class RuleSetApiTest(ApiTestCase):
    def test_permissions(self):
        row = self.rows()["part"]
        for method, name, kwargs, data in [
            ("get", "api-ruleset-list", {}, None),
            ("get", "api-ruleset-detail", {"pk": row.pk}, None),
            ("patch", "api-ruleset-detail", {"pk": row.pk}, {"can_view": False}),
            ("post", "api-ruleset-copy", {}, {"group": self.group.pk}),
            ("post", "api-ruleset-clear", {}, {"group": self.group.pk}),
            ("post", "api-ruleset-revoke", {}, {"group": self.group.pk}),
        ]:
            with self.subTest(name=name, method=method):
                call = getattr(self.plain_client, method)
                response = call(
                    url(name, **kwargs), data=data, content_type="application/json"
                )
                self.assertEqual(response.status_code, 403, response.content)
        self.assertTrue(self.rows()["part"].can_view)  # nothing changed

        # Staff without the admin role's change permission: refused as well.
        self.plain.is_staff = True
        self.plain.save()
        response = self.plain_client.get(url("api-ruleset-list"))
        self.assertEqual(response.status_code, 403)

        # Anonymous: refused by InvenTree before the view.
        self.assertIn(Client().get(url("api-ruleset-list")).status_code, (401, 403))

    def test_list_sorted_by_label_and_rows_ensured(self):
        MCPRuleSet.objects.filter(group=self.group, name="bom").delete()
        response = self.client.get(url("api-ruleset-list"), {"group": self.group.pk})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(
            [row["label"] for row in data], sorted(row["label"] for row in data)
        )
        self.assertIn("bom", {row["name"] for row in data})  # re-created
        part = next(row for row in data if row["name"] == "part")
        self.assertEqual(
            set(part),
            {
                "pk",
                "name",
                "label",
                "group",
                "can_view",
                "can_add",
                "can_change",
                "can_delete",
            },
        )
        self.assertEqual(part["group"], self.group.pk)
        self.assertTrue(part["can_view"])
        self.assertEqual(part["label"], "Parts")

        self.assertEqual(
            self.client.get(url("api-ruleset-list"), {"group": "x"}).status_code, 400
        )

    def test_patch_flags_only(self):
        row = self.rows()["stock"]
        response = self.client.patch(
            url("api-ruleset-detail", pk=row.pk),
            data={"can_view": True, "name": "admin", "group": 999},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        self.assertTrue(row.can_view)
        self.assertEqual((row.name, row.group_id), ("stock", self.group.pk))

    def test_copy_and_clear(self):
        RuleSet.objects.filter(group=self.group, name="build").update(
            can_view=True, can_change=True
        )
        _access, refresh = make_token_pair(self.application, self.user)

        response = self.client.post(
            url("api-ruleset-copy"),
            data={"group": self.group.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        core = {r.name: r for r in RuleSet.objects.filter(group=self.group)}
        for name, row in self.rows().items():
            for flag in MCPRuleSet.RULE_OPTIONS:
                self.assertEqual(
                    getattr(row, flag), getattr(core[name], flag), (name, flag)
                )
        refresh.refresh_from_db()
        self.assertIsNone(refresh.revoked)  # only additions: part view stays

        response = self.client.post(
            url("api-ruleset-clear"),
            data={"group": self.group.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(
            any(
                getattr(row, flag)
                for row in self.rows().values()
                for flag in MCPRuleSet.RULE_OPTIONS
            )
        )
        refresh.refresh_from_db()
        self.assertIsNotNone(refresh.revoked)  # a reduction: members revoked

        response = self.client.post(
            url("api-ruleset-copy"), data={}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_revoke_group(self):
        make_token_pair(self.application, self.user)
        make_token_pair(self.application, self.plain)
        response = self.client.post(
            url("api-ruleset-revoke"),
            data={"group": self.group.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"users": 1, "tokens": 1, "queued": False})
        self.assertFalse(
            RefreshToken.objects.filter(user=self.user, revoked__isnull=True).exists()
        )
        # Another group's member keeps the connection.
        self.assertTrue(
            RefreshToken.objects.filter(user=self.plain, revoked__isnull=True).exists()
        )

    def test_mcp_token_cannot_change_mcp_permissions(self):
        """Bearer tokens never reach the plugin API: 401 from InvenTree's middleware.

        AuthRequiredMiddleware runs before DRF and knows only sessions and API
        tokens, so the views' own a:staff requirement (next test) is defence
        in depth.
        """
        access, _refresh = make_token_pair(
            self.application, self.user, scope="g:read r:view:admin a:staff"
        )
        row = self.rows()["part"]
        response = Client().patch(
            url("api-ruleset-detail", pk=row.pk),
            data={"can_view": False},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access.token}",
        )
        self.assertEqual(response.status_code, 401)
        self.assertTrue(self.rows()["part"].can_view)

    def test_staff_endpoints_require_a_staff_on_an_oauth_token(self):
        """Called directly (past the middleware): the scope check itself refuses."""
        from rest_framework.test import APIRequestFactory

        from ..auth.api import MCPRuleSetDetail
        from ..oauth2_bridge import authentication_classes_for

        row = self.rows()["part"]

        def patch(scope: str):
            access, _refresh = make_token_pair(self.application, self.user, scope=scope)
            view = MCPRuleSetDetail.as_view(
                authentication_classes=authentication_classes_for(self.user, access)
            )
            request = APIRequestFactory().patch(
                f"/rulesets/{row.pk}/", {"can_view": False}, format="json"
            )
            return view(request, pk=row.pk)

        response = patch("g:read r:view:admin r:change:admin")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertTrue(self.rows()["part"].can_view)

        response = patch("a:staff")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(self.rows()["part"].can_view)


class RowsTest(ApiTestCase):
    def test_new_group_gets_rows(self):
        group = Group.objects.create(name="WP3 new group")
        self.assertEqual(len(self.rows(group)), len(RULESET_NAMES))

    def test_migration_creates_rows_for_existing_groups(self):
        group = Group.objects.create(name="WP3 pre-existing")
        MCPRuleSet.objects.filter(group=group).delete()
        migration = importlib.import_module("inventree_mcp.migrations.0002_mcpruleset")
        migration.create_rows_for_existing_groups(apps, None)
        rows = self.rows(group)
        self.assertEqual(len(rows), len(RULESET_NAMES))
        self.assertFalse(any(row.can_view for row in rows.values()))
        # Existing rows are kept as they are.
        self.assertTrue(self.rows()["part"].can_view)


class EffectiveApiTest(ApiTestCase):
    def test_self_and_others(self):
        response = self.plain_client.get(url("api-effective", user_pk=self.plain.pk))
        self.assertEqual(response.status_code, 200, response.content)
        response = self.plain_client.get(url("api-effective", user_pk=self.user.pk))
        self.assertEqual(response.status_code, 403)
        response = self.client.get(url("api-effective", user_pk=self.plain.pk))
        self.assertEqual(response.status_code, 200)
        response = self.client.get(url("api-effective", user_pk=999999))
        self.assertEqual(response.status_code, 404)

    def test_document(self):
        grant_mcp(self.group, "stock.view", "build.view")
        doc = self.client.get(url("api-effective", user_pk=self.user.pk)).json()

        self.assertEqual(doc["user"], self.user.pk)
        self.assertEqual(doc["username"], self.user.username)
        self.assertFalse(doc["superuser"])
        self.assertTrue(doc["may_connect"])
        self.assertEqual(doc["reason"], "")
        self.assertEqual(
            doc["scopes"], ["g:read", "r:view:part", "r:view:stock", "r:view:build"]
        )
        for key in ("roles", "mcp_roles", "effective"):
            self.assertEqual(doc[key]["part"]["label"], "Parts")
            self.assertEqual(
                set(doc[key]["part"]), {"label", "view", "add", "change", "delete"}
            )
        self.assertTrue(doc["roles"]["admin"]["change"])
        self.assertFalse(doc["mcp_roles"]["admin"]["view"])
        self.assertFalse(doc["effective"]["admin"]["view"])  # interactive only
        self.assertTrue(doc["mcp_roles"]["build"]["view"])
        self.assertFalse(doc["roles"]["build"]["view"])
        self.assertFalse(doc["effective"]["build"]["view"])  # MCP only
        self.assertTrue(doc["effective"]["part"]["view"])  # both
        self.assertTrue(doc["effective"]["stock"]["view"])
        self.assertIn("any interactive role and any MCP role", doc["note"])

    def test_no_permission_document(self):
        MCPRuleSet.objects.filter(group=self.plain_group).update(can_view=False)
        doc = self.plain_client.get(url("api-effective", user_pk=self.plain.pk)).json()
        self.assertFalse(doc["may_connect"])
        self.assertEqual(doc["reason"], "no_mcp_permission")
        self.assertEqual(doc["scopes"], [])


class ConnectionsAndStatusApiTest(ApiTestCase):
    def test_list_own_and_staff_user_parameter(self):
        _access, mine = make_token_pair(self.application, self.plain, scope="g:read")
        make_token_pair(self.application, self.user)

        data = self.plain_client.get(url("api-connection-list")).json()
        self.assertEqual([c["pk"] for c in data], [mine.pk])
        self.assertEqual(
            set(data[0]),
            {
                "pk",
                "client_name",
                "client_host",
                "client_trusted",
                "created",
                "expires",
                "access_expires",
                "scopes",
            },
        )
        self.assertEqual(data[0]["client_host"], "claude.ai")
        self.assertTrue(data[0]["client_trusted"])
        self.assertEqual(data[0]["scopes"], ["g:read"])
        self.assertIsNotNone(data[0]["expires"])

        response = self.plain_client.get(
            url("api-connection-list"), {"user": self.user.pk}
        )
        self.assertEqual(response.status_code, 403)
        data = self.client.get(
            url("api-connection-list"), {"user": self.plain.pk}
        ).json()
        self.assertEqual([c["pk"] for c in data], [mine.pk])

    def test_revoke_own_only(self):
        _access, mine = make_token_pair(self.application, self.plain)
        _access, theirs = make_token_pair(self.application, self.user)

        response = self.plain_client.delete(url("api-connection-detail", pk=theirs.pk))
        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.revoked)

        response = self.plain_client.delete(url("api-connection-detail", pk=mine.pk))
        self.assertEqual(response.status_code, 204)
        mine.refresh_from_db()
        self.assertIsNotNone(mine.revoked)
        self.assertEqual(self.plain_client.get(url("api-connection-list")).json(), [])

        # Staff admins may revoke somebody else's connection.
        response = self.client.delete(url("api-connection-detail", pk=theirs.pk))
        self.assertEqual(response.status_code, 204)

    def test_status(self):
        data = self.plain_client.get(url("api-status")).json()
        self.assertTrue(data["oauth_enabled"])
        self.assertTrue(data["mcp_url"].endswith("/plugin/inventree-mcp/mcp/"))
        self.assertEqual(
            data["membership"], {"checked_at": None, "blocked": False, "reason": ""}
        )
        self.assertEqual(data["scopes"], ["g:read", "r:view:part"])
        self.assertTrue(data["may_connect"])
        self.assertEqual(data["reason"], "")

        response = self.plain_client.get(url("api-status"), {"user": self.user.pk})
        self.assertEqual(response.status_code, 403)
        response = self.client.get(url("api-status"), {"user": self.plain.pk})
        self.assertEqual(response.status_code, 200)
