"""Shared fixtures for the OAuth façade tests (not collected: no test_ prefix)."""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import (
    get_access_token_model,
    get_application_model,
    get_refresh_token_model,
)
from plugin import registry

from ..auth import settings_inject

NS = "plugin:inventree-mcp"
MCP_URL = "/plugin/inventree-mcp/mcp/"
REDIRECT_URI = "https://client.example/callback"
TRUSTED_CIMD_CLIENT_ID = "https://claude.ai/oauth/mcp-test-client-metadata"
TRUSTED_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK_REDIRECT_URI = "http://127.0.0.1/callback"


def url(name: str, **kwargs) -> str:
    """Reverse a façade URL name."""
    return reverse(f"{NS}:{name}", kwargs=kwargs or None)


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def query(location: str) -> dict[str, str]:
    """Single-valued query parameters of a redirect Location."""
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


def mcp_body(method: str, request_id: int = 1, params: dict | None = None) -> str:
    """A JSON-RPC request body for the MCP endpoint."""
    return json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    })


def consent_form_data(page) -> dict[str, str]:
    """The consent form's hidden fields, as the rendered page would post them back."""
    return {k: v for k, v in page.context["form"].initial.items() if v is not None}


def grant_mcp(group, *roles: str) -> None:
    """Set *group*'s MCP rulesets, core's assignRole() syntax: "part.view", "stock.change", "all".

    "all" grants every flag of every role; "<role>" alone means "<role>.view".
    Rows not named are left as they are. Saves through the model (signals run).
    """
    from ..auth.signals import ensure_rulesets
    from ..models import MCPRuleSet

    ensure_rulesets(group)
    rows = {row.name: row for row in MCPRuleSet.objects.filter(group=group)}
    for role in roles:
        if role == "all":
            for row in rows.values():
                for flag in MCPRuleSet.RULE_OPTIONS:
                    setattr(row, flag, True)
            continue
        name, _, permission = role.partition(".")
        setattr(rows[name], f"can_{permission or 'view'}", True)
    for row in rows.values():
        row.save()


def make_token_pair(
    application, user, *, expired: bool = False, refresh: bool = True, scope="g:read"
):
    """An AccessToken (live or expired) and, unless refresh=False, its RefreshToken.

    Returns (access_token, refresh_token or None). Token values are random.
    """
    access = get_access_token_model().objects.create(
        user=user,
        application=application,
        token=f"at-{secrets.token_urlsafe(16)}",
        expires=timezone.now() + datetime.timedelta(hours=-1 if expired else 1),
        scope=scope,
    )
    if not refresh:
        return access, None
    refresh_token = get_refresh_token_model().objects.create(
        user=user,
        application=application,
        token=f"rt-{secrets.token_urlsafe(16)}",
        access_token=access,
    )
    return access, refresh_token


class OAuthFacadeTestMixin:
    """Activates the plugin with OAUTH_ENABLED on; restores DOT's settings afterwards.

    The test user's group gets every MCP role (``mcp_roles``, "all" by
    default), so the façade tests see every advertised scope as before the MCP
    permission set; tests of the permission set narrow it with grant_mcp().
    """

    mcp_roles: tuple[str, ...] = ("all",)

    @classmethod
    def setUpTestData(cls):
        """Load and activate the plugin, as test_mcp.py does; grant the MCP roles."""
        super().setUpTestData()
        registry.reload_plugins(full_reload=True, collect=True)
        registry.set_plugin_state("inventree-mcp", True)
        if getattr(cls, "group", None) is not None:
            grant_mcp(cls.group, *cls.mcp_roles)

    @classmethod
    def tearDownClass(cls):
        """Leave DOT as InvenTree configured it, for the other test suites."""
        settings_inject._apply({})
        super().tearDownClass()

    def setUp(self):
        """Enable the façade and reset the registration rate-limit counter."""
        super().setUp()
        self.plugin = registry.get_plugin("inventree-mcp")
        self.set_setting("OAUTH_ENABLED", True)
        settings_inject.invalidate()  # the previous test's rollback sends no signal
        cache.delete("inventree_mcp:dcr:127.0.0.1")

    def set_setting(self, key: str, value: Any) -> None:
        """Set one of the plugin's settings."""
        self.plugin.set_setting(key, value)

    # -- client helpers -------------------------------------------------------

    def register(self, client: Client | None = None, **metadata) -> Any:
        """POST /oauth/register/ with JSON *metadata*; returns the response."""
        body = {
            "client_name": "Test MCP client",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
        } | metadata
        return (client or Client()).post(
            url("oauth-register"),
            data=json.dumps(body),
            content_type="application/json",
        )

    def register_public_client(self) -> str:
        """Register a public client and return its client_id."""
        response = self.register()
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["client_id"]

    @staticmethod
    def create_cimd_application(
        client_id: str = TRUSTED_CIMD_CLIENT_ID,
        redirect_uris: str = f"{TRUSTED_REDIRECT_URI} {LOOPBACK_REDIRECT_URI}",
    ):
        """A CIMD Application as DOT would persist it after fetching the document."""
        Application = get_application_model()
        return Application.objects.create(
            client_id=client_id,
            name="Claude",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris=redirect_uris,
            registration_source=Application.RegistrationSource.CIMD,
            user=None,
        )

    @staticmethod
    def authorize_url(
        client_id: str,
        challenge: str,
        redirect_uri: str = REDIRECT_URI,
        scope: str | None = None,
        state: str = "xyz-state",
        **extra,
    ) -> str:
        """The authorize URL a client would open, with PKCE S256."""
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if scope is not None:
            params["scope"] = scope
        params.update(extra)
        return f"{url('oauth-authorize')}?{urlencode(params)}"

    def approve(self, client: Client, authorize_url: str, **overrides) -> Any:
        """Load the consent page and post it back approved; returns the POST response."""
        page = client.get(authorize_url)
        self.assertEqual(page.status_code, 200, page.content)
        data = consent_form_data(page) | {"allow": "Authorize"} | overrides
        return client.post(authorize_url, data=data)

    def obtain_code(
        self, client: Client, client_id: str, scope: str | None = None
    ) -> tuple[str, str]:
        """Run authorize + consent as the logged-in *client*; returns (code, verifier)."""
        verifier, challenge = pkce_pair()
        response = self.approve(
            client, self.authorize_url(client_id, challenge, scope=scope)
        )
        self.assertEqual(response.status_code, 302, response.content)
        params = query(response["Location"])
        self.assertEqual(params.get("state"), "xyz-state")
        return params["code"], verifier

    def exchange_code(self, client_id: str, code: str, verifier: str, **headers) -> Any:
        """POST the authorization_code grant to the façade's token endpoint."""
        return Client().post(
            url("oauth-token"),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": verifier,
            },
            **headers,
        )

    def refresh(
        self, client_id: str, refresh_token: str, headers: dict | None = None, **data
    ) -> Any:
        """POST the refresh_token grant to the façade's token endpoint."""
        return Client().post(
            url("oauth-token"),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            }
            | data,
            **(headers or {}),
        )

    @staticmethod
    def mcp_call(
        access_token: str | None = None,
        method: str = "tools/list",
        path: str = MCP_URL,
        **headers,
    ) -> Any:
        """Call the MCP endpoint, with a bearer token when given (or raw *headers*)."""
        if access_token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"
        return Client(enforce_csrf_checks=True).post(
            path,
            data=mcp_body(method),
            content_type="application/json",
            HTTP_ACCEPT="application/json, text/event-stream",
            **headers,
        )
