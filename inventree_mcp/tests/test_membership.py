"""Membership re-checks against the SSO provider (WP2): auth/membership.py, signals, transport, sweep.

The identity provider is faked at the HTTP boundary: requests.Session.request
is replaced, so membership.py's own calls and allauth's jwtkit (JWKS fetch)
both reach FakeIdP. ID tokens are signed with a test RSA key whose JWKS the
fake serves. The SSO app is settings-backed (SOCIALACCOUNT_PROVIDERS), as in
the dev stack; allauth then stores SocialToken rows with app=NULL.
"""

from __future__ import annotations

import datetime
import json
import time
import unittest
from typing import ClassVar
from unittest.mock import patch

import jwt
import requests
from allauth.account.signals import user_logged_in
from allauth.socialaccount.models import SocialAccount, SocialLogin, SocialToken
from common.settings import set_global_setting
from cryptography.hazmat.primitives.asymmetric import rsa
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone
from InvenTree.unit_test import InvenTreeTestCase
from oauth2_provider.models import AccessToken, RefreshToken
from users.models import ApiToken

from ..auth import membership, policy, tasks
from ..auth.membership import Outcome
from ..models import MCPUserState
from .oauth_helpers import OAuthFacadeTestMixin, make_token_pair, pkce_pair

User = get_user_model()

SUB = "upstream-subject-1"
CLIENT_ID = "inventree-client"
SERVER_URL = "https://idp.example/application/o/inventree"
ISSUER = f"{SERVER_URL}/"
DISCOVERY_URL = f"{SERVER_URL}/.well-known/openid-configuration"
TOKEN_URL = "https://idp.example/application/o/token/"
USERINFO_URL = "https://idp.example/application/o/userinfo/"
JWKS_URL = f"{SERVER_URL}/jwks/"
KID = "test-key"

PROVIDERS = {
    "openid_connect": {
        "APPS": [
            {
                "provider_id": "authentik",
                "name": "Authentik",
                "client_id": CLIENT_ID,
                "secret": "not-a-secret",
                "settings": {"server_url": SERVER_URL},
            }
        ]
    }
}
GROUP_MAP = {"idp-mcp": "MCP Users", "idp-users": "Users"}

OIDC_INSTALLED = apps.is_installed("allauth.socialaccount.providers.openid_connect")


def _response(status: int, body) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    if isinstance(body, (dict, list)):
        response._content = json.dumps(body).encode()
        response.headers["Content-Type"] = "application/json"
    else:
        response._content = str(body).encode()
        response.headers["Content-Type"] = "text/plain"
    return response


class FakeIdP:
    """Discovery, JWKS, token and userinfo endpoints of an OIDC provider.

    ``mode`` selects the token endpoint's answer: ok, invalid_grant, 503,
    timeout, connection_error, malformed, bad_signature, wrong_subject,
    no_id_token.
    """

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.groups: list[str] | str | None = ["idp-mcp", "idp-users"]
        self.groups_in_id_token = True
        self.mode = "ok"
        self.calls: list[tuple[str, str, object]] = []
        self.redeemed: list[str] = []
        self.issued = 0

    def __call__(self, session, method, url, **kwargs):
        """Stands in for requests.Session.request (bound: *session* is the Session)."""
        self.calls.append((method, url, kwargs.get("timeout")))
        if url == DISCOVERY_URL:
            return _response(
                200,
                {
                    "issuer": ISSUER,
                    "token_endpoint": TOKEN_URL,
                    "userinfo_endpoint": USERINFO_URL,
                    "jwks_uri": JWKS_URL,
                    "authorization_endpoint": "https://idp.example/authorize/",
                    "token_endpoint_auth_methods_supported": [
                        "client_secret_post",
                        "client_secret_basic",
                    ],
                },
            )
        if url == JWKS_URL:
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
                self.key.public_key(), as_dict=True
            )
            return _response(200, {"keys": [jwk | {"kid": KID, "alg": "RS256"}]})
        if url == TOKEN_URL:
            return self._token(kwargs.get("data") or {})
        if url == USERINFO_URL:
            body = {"sub": SUB}
            if self.groups is not None:
                body["groups"] = self.groups
            return _response(200, body)
        raise AssertionError(f"unexpected outbound call to {url}")

    @property
    def token_calls(self) -> int:
        return sum(1 for _method, url, _timeout in self.calls if url == TOKEN_URL)

    def _token(self, data: dict):
        if self.mode == "timeout":
            raise requests.Timeout("read timed out")
        if self.mode == "connection_error":
            raise requests.ConnectionError("refused")
        self.redeemed.append(data.get("refresh_token"))
        if self.mode == "invalid_grant":
            return _response(400, {"error": "invalid_grant"})
        if self.mode == "503":
            return _response(503, "Service Unavailable")
        if self.mode == "malformed":
            return _response(200, "<html>not json</html>")

        self.issued += 1
        body = {
            "access_token": f"upstream-at-{self.issued}",
            "refresh_token": f"upstream-rt-{self.issued}",
            "token_type": "Bearer",
            "expires_in": 600,
        }
        if self.mode != "no_id_token":
            body["id_token"] = self.id_token()
        return _response(200, body)

    def id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": "someone-else" if self.mode == "wrong_subject" else SUB,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 600,
        }
        if self.groups_in_id_token and self.groups is not None:
            claims["groups"] = self.groups
        key = self.key
        if self.mode == "bad_signature":
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


@unittest.skipUnless(
    OIDC_INSTALLED, "allauth's openid_connect provider is not installed"
)
@override_settings(PLUGIN_TESTING_SETUP=True, SOCIALACCOUNT_PROVIDERS=PROVIDERS)
class MembershipTestCase(OAuthFacadeTestMixin, InvenTreeTestCase):
    """A user signed in through the fake SSO provider, holding an upstream refresh token."""

    roles: ClassVar[list[str]] = ["part.view"]

    def setUp(self):
        super().setUp()
        self.client_id = self.register_public_client()
        self.browser = Client()
        self.browser.force_login(self.user)
        cache.clear()  # LocMemCache in tests: discovery document, warning markers
        set_global_setting("LOGIN_ENABLE_SSO", True)
        set_global_setting("LOGIN_ENABLE_SSO_GROUP_SYNC", True)
        set_global_setting("SSO_GROUP_KEY", "groups")
        set_global_setting("SSO_GROUP_MAP", json.dumps(GROUP_MAP))
        set_global_setting("SSO_REMOVE_GROUPS", True)
        self.set_setting("MEMBERSHIP_ENFORCED", True)
        self.set_setting("MCP_REQUIRED_GROUPS", "MCP Users")

        # Creating the account runs core's group sync (post_save), as a first login does.
        self.account = SocialAccount.objects.create(
            user=self.user,
            provider="authentik",
            uid=SUB,
            extra_data={"userinfo": {"sub": SUB, "groups": ["idp-mcp"]}},
        )
        self.upstream = SocialToken.objects.create(
            account=self.account,
            app=None,
            token="upstream-at-0",
            token_secret="upstream-rt-0",
        )
        self.assertTrue(self.user.groups.filter(name="MCP Users").exists())

        self.idp = FakeIdP()
        idp = self.idp

        def request(session, method, url, **kwargs):
            return idp(session, method, url, **kwargs)

        patcher = patch.object(requests.Session, "request", new=request)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers --------------------------------------------------------------

    def state(self) -> MCPUserState:
        return MCPUserState.objects.get(user=self.user)

    def set_checked(self, hours_ago: float | None, **fields) -> None:
        """Set membership_checked_at to *hours_ago* (None: never checked)."""
        checked = (
            None
            if hours_ago is None
            else timezone.now() - datetime.timedelta(hours=hours_ago)
        )
        MCPUserState.objects.update_or_create(
            user=self.user, defaults={"membership_checked_at": checked, **fields}
        )

    def group_names(self) -> set[str]:
        return set(self.user.groups.values_list("name", flat=True))

    def upstream_token(self) -> SocialToken | None:
        return SocialToken.objects.filter(account=self.account).first()

    def assert_blocked(self, reason: str) -> None:
        state = self.state()
        self.assertIsNotNone(state.access_revoked_at)
        self.assertEqual(state.revoked_reason, reason)
        self.assertTrue(membership.is_blocked(self.user))
        self.assertIsNone(self.upstream_token())
        self.assertFalse(
            RefreshToken.objects.filter(user=self.user, revoked__isnull=True).exists()
        )
        self.assertFalse(AccessToken.objects.filter(user=self.user).exists())

    def login_request(self):
        """A request as allauth's other login receivers (usersessions) expect it."""
        request = RequestFactory().get("/")
        SessionMiddleware(lambda _request: None).process_request(request)
        request.session.save()
        request.user = self.user
        return request

    def sso_login(self, provider: str = "authentik") -> None:
        """What allauth sends after an SSO login (core's group sync has run by then)."""
        account = SocialAccount(user=self.user, provider=provider, uid=SUB)
        user_logged_in.send(
            sender=User,
            request=self.login_request(),
            response=None,
            user=self.user,
            sociallogin=SocialLogin(account=account),
        )

    def _tokens(self) -> dict:
        code, verifier = self.obtain_code(self.browser, self.client_id)
        response = self.exchange_code(self.client_id, code, verifier)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def authorize_page(self):
        _verifier, challenge = pkce_pair()
        return self.browser.get(self.authorize_url(self.client_id, challenge))


class MemberRecheckTest(MembershipTestCase):
    """Test 1: a member's re-check, and test 11: serialisation."""

    def test_member_recheck_rotates_and_syncs_groups(self):
        stale = Group.objects.create(name="Stale")
        self.user.groups.add(stale)
        self.set_checked(hours_ago=2)

        outcome = membership.recheck(self.user)

        self.assertEqual(outcome, Outcome.MEMBER)
        state = self.state()
        self.assertGreater(
            state.membership_checked_at, timezone.now() - datetime.timedelta(minutes=1)
        )
        self.assertEqual(state.last_recheck_error, "")
        self.assertIsNone(state.access_revoked_at)

        # The upstream refresh token was redeemed once and the rotated pair stored.
        self.assertEqual(self.idp.redeemed, ["upstream-rt-0"])
        token = self.upstream_token()
        self.assertEqual(token.pk, self.upstream.pk)
        self.assertEqual(token.token, "upstream-at-1")
        self.assertEqual(token.token_secret, "upstream-rt-1")
        self.assertIsNotNone(token.expires_at)

        # Core's group sync: "idp-users" mapped and added, unmapped "Stale" removed.
        self.assertEqual(self.group_names(), {"MCP Users", "Users"})
        self.account.refresh_from_db()
        self.assertEqual(
            self.account.extra_data["userinfo"]["groups"], ["idp-mcp", "idp-users"]
        )

        # Every outbound call carried the 5 s timeout (jwtkit's via allauth's default).
        self.assertTrue(self.idp.calls)
        self.assertEqual({timeout for _m, _u, timeout in self.idp.calls}, {5})

    def test_refresh_grant_runs_the_recheck_when_due(self):
        tokens = self._tokens()
        self.set_checked(hours_ago=2)

        response = self.refresh(self.client_id, tokens["refresh_token"])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.idp.token_calls, 1)
        self.assertEqual(self.upstream_token().token_secret, "upstream-rt-1")

    def test_groups_from_userinfo_when_the_id_token_lacks_them(self):
        self.idp.groups_in_id_token = False
        self.idp.groups = "idp-mcp"  # a single string counts as one group
        self.set_checked(hours_ago=2)

        self.assertEqual(membership.recheck(self.user), Outcome.MEMBER)
        self.assertIn(("GET", USERINFO_URL, 5), self.idp.calls)
        self.assertEqual(self.group_names(), {"MCP Users"})

    def test_second_recheck_in_one_interval_makes_no_call(self):
        """Test 11: serialised by the row lock, re-read inside it."""
        self.set_checked(hours_ago=2)

        self.assertEqual(membership.recheck(self.user), Outcome.MEMBER)
        self.assertEqual(membership.recheck(self.user), Outcome.NOT_DUE)
        self.assertEqual(self.idp.token_calls, 1)

        # A worker that read "due" before another one finished re-reads under
        # the lock and does not redeem the (already rotated) token again.
        with patch.object(membership, "recheck_due", return_value=True):
            self.assertEqual(membership.recheck(self.user), Outcome.NOT_DUE)
        self.assertEqual(self.idp.token_calls, 1)


class NotMemberTest(MembershipTestCase):
    """Tests 2 and 3: removed from the group, or the grant revoked upstream."""

    def test_removed_from_group_is_blocked_until_the_next_sso_login(self):
        tokens = self._tokens()
        self.set_checked(hours_ago=2)
        self.idp.groups = ["idp-users"]

        response = self.refresh(self.client_id, tokens["refresh_token"])

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertEqual(self.group_names(), {"Users"})
        self.assert_blocked("no_mcp_permission")

        # The next refresh is refused without asking the provider again.
        calls = len(self.idp.calls)
        response = self.refresh(self.client_id, tokens["refresh_token"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertEqual(len(self.idp.calls), calls)

        # Any bearer token of the blocked user gets 401 on MCP, with the challenge.
        application = RefreshToken.objects.filter(user=self.user).first().application
        access, _refresh = make_token_pair(application, self.user)
        response = self.mcp_call(access.token)
        self.assertEqual(response.status_code, 401)
        self.assertIn("Access revoked", response.json()["error"]["message"])
        self.assertIn("Bearer", response["WWW-Authenticate"])

        # Authorize shows the "blocked" page.
        page = self.authorize_page()
        self.assertEqual(page.status_code, 403)
        self.assertEqual(page.context["reason"], "blocked")

        # The user is put back into the group; the next SSO login lifts the block.
        self.user.groups.add(Group.objects.get(name="MCP Users"))
        self.sso_login()
        self.assertFalse(membership.is_blocked(self.user))
        self.assertEqual(self.state().revoked_reason, "")
        self.assertEqual(self.authorize_page().status_code, 200)
        self.assertEqual(self.mcp_call(access.token).status_code, 200)

    def test_upstream_invalid_grant_blocks(self):
        tokens = self._tokens()
        self.set_checked(hours_ago=2)
        self.idp.mode = "invalid_grant"

        response = self.refresh(self.client_id, tokens["refresh_token"])

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assert_blocked("invalid_grant")
        self.assertEqual(self.mcp_call(tokens["access_token"]).status_code, 401)
        self.assertEqual(self.authorize_page().context["reason"], "blocked")

    def test_inactive_user_is_blocked(self):
        self.set_checked(hours_ago=2)
        User.objects.filter(pk=self.user.pk).update(is_active=False)

        self.assertEqual(membership.recheck(self.user), Outcome.NOT_MEMBER)
        self.assertEqual(self.state().revoked_reason, "inactive")


class UnreachableTest(MembershipTestCase):
    """Test 4: provider unreachable or unverifiable: grace, then refusal without revocation."""

    FAILURES: ClassVar[dict[str, tuple[str, bool]]] = {
        # mode: (last_recheck_error, upstream token rotated before the failure)
        "timeout": ("timeout", False),
        "connection_error": ("connection_error", False),
        "503": ("http_5xx", False),
        "malformed": ("bad_response", False),
        "bad_signature": ("bad_id_token", True),
        "wrong_subject": ("subject_mismatch", True),
    }

    def test_within_grace_refresh_succeeds(self):
        for mode, (code, rotated) in self.FAILURES.items():
            with self.subTest(mode=mode):
                tokens = self._tokens()
                secret_before = self.upstream_token().token_secret
                self.set_checked(hours_ago=2)  # grace is 4 hours
                self.idp.mode = mode

                response = self.refresh(self.client_id, tokens["refresh_token"])

                self.assertEqual(response.status_code, 200, response.content)
                state = self.state()
                self.assertEqual(state.last_recheck_error, code)
                self.assertIsNone(state.access_revoked_at)
                secret_after = self.upstream_token().token_secret
                if rotated:  # redeemed: the rotated token is kept, never lost
                    self.assertNotEqual(secret_after, secret_before)
                else:
                    self.assertEqual(secret_after, secret_before)

    def test_past_grace_refused_without_revoking_then_works_again(self):
        for mode in self.FAILURES:
            with self.subTest(mode=mode):
                self.idp.mode = "ok"
                tokens = self._tokens()
                self.set_checked(hours_ago=5)
                self.idp.mode = mode

                response = self.refresh(self.client_id, tokens["refresh_token"])

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json()["error"], "invalid_grant")
                self.assertFalse(membership.is_blocked(self.user))
                self.assertIsNotNone(self.upstream_token())
                # Nothing rotated or revoked: the pair still works ...
                self.assertEqual(self.mcp_call(tokens["access_token"]).status_code, 200)

                # ... and the same refresh token works once the provider is back.
                self.idp.mode = "ok"
                response = self.refresh(self.client_id, tokens["refresh_token"])
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(self.state().last_recheck_error, "")

    def test_without_id_token_userinfo_confirms_the_subject(self):
        self.idp.mode = "no_id_token"
        self.set_checked(hours_ago=2)

        self.assertEqual(membership.recheck(self.user), Outcome.MEMBER)
        self.assertIn(("GET", USERINFO_URL, 5), self.idp.calls)


class NoUpstreamTokenTest(MembershipTestCase):
    """Test 5: degraded mode without an upstream refresh token."""

    def test_allowed_within_the_relogin_interval_then_refused(self):
        tokens = self._tokens()
        self.upstream.delete()
        an_hour_ago = timezone.now() - datetime.timedelta(hours=1)
        self.set_checked(hours_ago=None, last_login_at=an_hour_ago)

        response = self.refresh(self.client_id, tokens["refresh_token"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.state().last_recheck_error, "no_token")
        self.assertEqual(self.idp.calls, [])

        long_ago = timezone.now() - datetime.timedelta(hours=25)
        MCPUserState.objects.filter(user=self.user).update(last_login_at=long_ago)
        User.objects.filter(pk=self.user.pk).update(last_login=long_ago)
        response = self.refresh(self.client_id, response.json()["refresh_token"])
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertFalse(membership.is_blocked(self.user))

    def test_falls_back_to_the_user_last_login(self):
        self.upstream.delete()
        self.set_checked(hours_ago=None, last_login_at=None)
        User.objects.filter(pk=self.user.pk).update(last_login=timezone.now())
        self.user.refresh_from_db()

        self.assertEqual(membership.refresh_grant_allowed(self.user), (True, ""))

    def test_warning_is_logged_once_per_user(self):
        self.upstream.token_secret = ""  # e.g. offline_access was not granted
        self.upstream.save()
        self.set_checked(hours_ago=2)

        with self.assertLogs("inventree", level="WARNING") as logs:
            for _attempt in range(3):
                outcome = membership.recheck(self.user, force=True)
                self.assertEqual(outcome, Outcome.NO_UPSTREAM_TOKEN)

        warnings = [line for line in logs.output if "no upstream refresh token" in line]
        self.assertEqual(len(warnings), 1, logs.output)
        self.assertIn(f"user id {self.user.pk}", warnings[0])
        self.assertIn("no_token", warnings[0])


class DueAndEnforcementTest(MembershipTestCase):
    """Tests 6 and 7: not due, forced, enforcement off, MCP_REQUIRED_GROUPS."""

    def test_not_due_makes_no_call_and_force_does(self):
        self.set_checked(hours_ago=0.5)  # interval is 60 minutes

        self.assertFalse(membership.recheck_due(self.user))
        self.assertEqual(membership.recheck(self.user), Outcome.NOT_DUE)
        self.assertEqual(self.idp.calls, [])

        self.assertEqual(membership.recheck(self.user, force=True), Outcome.MEMBER)
        self.assertEqual(self.idp.token_calls, 1)

    def test_never_checked_is_due(self):
        self.assertTrue(membership.recheck_due(self.user))
        self.set_checked(hours_ago=None)
        self.assertTrue(membership.recheck_due(self.user))

    def test_enforcement_off(self):
        tokens = self._tokens()
        self.set_checked(hours_ago=24, access_revoked_at=timezone.now())
        self.set_setting("MEMBERSHIP_ENFORCED", False)

        self.assertEqual(membership.recheck(self.user, force=True), Outcome.DISABLED)
        self.assertFalse(membership.is_blocked(self.user))
        self.assertFalse(membership.recheck_due(self.user))
        response = self.refresh(self.client_id, tokens["refresh_token"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.idp.calls, [])

    def test_required_groups(self):
        self.assertEqual(policy.may_connect(self.user), (True, ""))

        self.set_setting("MCP_REQUIRED_GROUPS", "Other, mcp users")  # case-insensitive
        self.assertTrue(policy.in_required_groups(self.user))

        self.set_setting("MCP_REQUIRED_GROUPS", "Other")
        self.assertFalse(policy.in_required_groups(self.user))
        self.assertEqual(policy.may_connect(self.user), (False, "no_mcp_permission"))
        self.assertEqual(self.authorize_page().context["reason"], "no_mcp_permission")

        self.set_setting("MCP_REQUIRED_GROUPS", "")
        self.user.groups.clear()
        self.assertTrue(policy.in_required_groups(self.user))
        self.assertEqual(policy.may_connect(self.user), (True, ""))

    def test_required_groups_apply_with_enforcement_off(self):
        self.set_setting("MEMBERSHIP_ENFORCED", False)
        self.set_setting("MCP_REQUIRED_GROUPS", "Other")
        self.assertEqual(policy.may_connect(self.user), (False, "no_mcp_permission"))


class LoginSignalTest(MembershipTestCase):
    """Test 8: login stamps."""

    def test_sso_login_stamps_and_unblocks(self):
        membership.block_user(self.user, "no_mcp_permission")
        self.assertTrue(membership.is_blocked(self.user))

        self.sso_login()

        state = self.state()
        now = timezone.now()
        self.assertIsNone(state.access_revoked_at)
        self.assertEqual(state.revoked_reason, "")
        self.assertGreater(state.last_login_at, now - datetime.timedelta(minutes=1))
        self.assertGreater(
            state.membership_checked_at, now - datetime.timedelta(minutes=1)
        )

    def test_password_login_only_records_the_login(self):
        membership.block_user(self.user, "no_mcp_permission")

        user_logged_in.send(
            sender=User, request=self.login_request(), response=None, user=self.user
        )
        self.sso_login(provider="some-other-provider")

        state = self.state()
        self.assertIsNotNone(state.last_login_at)
        self.assertIsNone(state.membership_checked_at)
        self.assertIsNotNone(state.access_revoked_at)
        self.assertTrue(membership.is_blocked(self.user))


class TransportTest(MembershipTestCase):
    """Test 9: MCP requests with long-lived credentials, and blocked users."""

    def setUp(self):
        super().setUp()
        self.api_token = ApiToken.objects.create(user=self.user, name="mcp test")

    def call_with_api_token(self):
        return self.mcp_call(HTTP_AUTHORIZATION=f"Token {self.api_token.key}")

    def test_stale_check_queues_exactly_one_background_recheck(self):
        self.set_checked(hours_ago=2)

        with patch("InvenTree.tasks.offload_task") as offload:
            self.assertEqual(self.call_with_api_token().status_code, 200)
            self.assertEqual(self.call_with_api_token().status_code, 200)

        offload.assert_called_once_with(
            "inventree_mcp.auth.tasks.recheck_user",
            self.user.pk,
            force_async=True,
            check_duplicates=True,
        )
        self.assertEqual(self.idp.calls, [])  # the request itself never waits

    def test_fresh_check_or_oauth_token_queues_nothing(self):
        self.set_checked(hours_ago=0.1)
        with patch("InvenTree.tasks.offload_task") as offload:
            self.assertEqual(self.call_with_api_token().status_code, 200)
        offload.assert_not_called()

        self.set_checked(hours_ago=2)
        tokens = self._tokens()
        with patch("InvenTree.tasks.offload_task") as offload:
            self.assertEqual(self.mcp_call(tokens["access_token"]).status_code, 200)
        offload.assert_not_called()

    def test_background_task_runs_the_recheck(self):
        self.set_checked(hours_ago=2)
        self.assertEqual(tasks.recheck_user(self.user.pk), "member")
        self.assertEqual(tasks.recheck_user(10**9), "missing")

    def test_blocked_user_gets_401(self):
        membership.block_user(self.user, "no_mcp_permission")

        with patch("InvenTree.tasks.offload_task") as offload:
            response = self.call_with_api_token()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["error"]["message"],
            "Access revoked, sign in to InvenTree again.",
        )
        self.assertIn('error="invalid_token"', response["WWW-Authenticate"])
        offload.assert_not_called()


class SweepTest(MembershipTestCase):
    """Test 10: the hourly sweep picks only due users who hold live MCP credentials."""

    def test_sweep_picks_only_due_users_with_live_tokens(self):
        application = self.create_cimd_application()
        old = timezone.now() - datetime.timedelta(hours=2)
        fresh = timezone.now()

        def user(name, checked, *, refresh=False, api_token=None, blocked=False):
            account = User.objects.create_user(username=name, password="x")
            MCPUserState.objects.create(
                user=account,
                membership_checked_at=checked,
                access_revoked_at=fresh if blocked else None,
            )
            if refresh:
                make_token_pair(application, account)
            if api_token is not None:
                ApiToken.objects.create(user=account, name="t", revoked=not api_token)
            return account

        due_refresh = user("due-refresh", old, refresh=True)
        due_api = user("due-api", None, api_token=True)
        user("fresh-refresh", fresh, refresh=True)
        user("due-no-tokens", old)
        user("due-blocked", old, refresh=True, blocked=True)
        user("due-revoked-api", old, api_token=False)
        self.set_checked(hours_ago=0.1)  # self.user holds no MCP token anyway

        seen = []

        def fake_recheck(account, *, force=False):
            seen.append(account.pk)
            return Outcome.MEMBER

        with (
            patch.object(membership, "recheck", side_effect=fake_recheck),
            self.assertLogs("inventree", level="INFO") as logs,
        ):
            counts = tasks.membership_sweep()

        self.assertEqual(sorted(seen), sorted([due_refresh.pk, due_api.pk]))
        self.assertEqual(counts, {"member": 2, "not_due": 1})
        self.assertTrue(
            any("membership sweep: member=2" in line for line in logs.output)
        )

    def test_sweep_does_nothing_when_not_enforced(self):
        self.set_setting("MEMBERSHIP_ENFORCED", False)
        with patch.object(membership, "recheck") as recheck:
            self.assertEqual(tasks.membership_sweep(), {})
        recheck.assert_not_called()


class ModelPermissionTest(MembershipTestCase):
    """MCPUserState.check_user_permission: staff, or the own row for reading."""

    def test_check_user_permission(self):
        owner = User.objects.create_user(username="owner", password="x")
        other = User.objects.create_user(username="other", password="x")
        staff = User.objects.create_user(username="staff", password="x", is_staff=True)
        state = MCPUserState.objects.create(user=owner)

        self.assertTrue(MCPUserState.check_user_permission(staff, "change"))
        self.assertTrue(MCPUserState.check_user_permission(owner, "view", state))
        self.assertFalse(MCPUserState.check_user_permission(owner, "change", state))
        self.assertFalse(MCPUserState.check_user_permission(other, "view", state))
        self.assertFalse(MCPUserState.check_user_permission(owner, "view"))
