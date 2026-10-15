"""Django view adapter bridging the MCP Streamable HTTP transport onto InvenTree.

This view is marked auth_exempt so it can run its own, full DRF
authentication (Token / Basic / OAuth2 - see authentication_classes below).
That's necessary, not just a style choice:
InvenTree.middleware.AuthRequiredMiddleware only recognizes session cookies
and InvenTree's own ApiToken, and gates on a fixed path allowlist that
doesn't include plugin URLs - an OAuth2 bearer token would otherwise never
even reach this view (verified: /api/part/ accepts it, this endpoint 401s
before dispatch() runs at all). We gate on REQUIRE_AUTH ourselves, then bind
the authenticated identity into the MCP request context (see context.py) so
tools can act as that user - and under the user's current MCP scopes
(policy.connect_scopes()), not just the user's role permissions - via
proxy.call_view(): an OAuth2 token is bound cut to its granted scopes ∩ those
scopes (narrowed_oauth_token()), an API token or Basic credential as an
unsaved token carrying them (scoped_token_for()). The request's resolved host,
scheme and client address go along (request_meta()) for the proxied requests. This is what makes per-tool permission enforcement
possible, instead of trusting "authenticated = allowed to do anything" as a
single plugin-wide bypass.

authentication_classes deliberately excludes SessionAuthentication (DRF's
default authenticator list includes it, for browser/UI convenience). MCP
clients are machine-to-machine and never send a CSRF token, and including it
actively breaks OAuth2 here: oauth2_provider's own OAuth2TokenMiddleware
(already in InvenTree's MIDDLEWARE, running after AuthRequiredMiddleware)
resolves the Bearer token and sets request.user at the middleware level for
every request. SessionAuthentication then sees an already-active user and
treats it as session auth, calling enforce_csrf() - which fails with "CSRF
Failed: CSRF cookie not set." for every OAuth2 request. Verified via a direct
trace: request.user was already the correct user by the time
SessionAuthentication.authenticate() ran, purely from that middleware.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import path
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from InvenTree.permissions import auth_exempt
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from oauth2_provider.contrib.rest_framework.authentication import OAuth2Authentication
from oauth2_provider.models import get_access_token_model
from rest_framework import exceptions
from rest_framework.authentication import BasicAuthentication
from rest_framework.views import APIView
from users.authentication import ApiTokenAuthentication, ExtendedOAuth2Authentication

from .auth import membership, policy
from .auth.discovery import REALM, challenge_header
from .context import reset_current_user, set_current_user
from .mcp_server import mcp
from .settings import get_plugin_setting

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from starlette.types import Scope

logger = logging.getLogger("inventree")

_REQUEST_TIMEOUT_SECONDS = 60.0

#: Lifetime of the synthetic scoped token of an API-token / Basic request. It is
#: never saved and lives only for the one request; the value only has to
#: outlast it (DOT's is_expired() is part of the scope check).
_SCOPED_TOKEN_LIFETIME = timedelta(days=1)

# Hop-by-hop headers (RFC 7230 6.1) that must never be forwarded from the
# ASGI response onto the Django HttpResponse - WSGI servers (e.g. the stdlib
# wsgiref-based `runserver`) reject them outright, since only the server
# itself (not the application) is allowed to control connection handling.
_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})


def _new_session_manager() -> StreamableHTTPSessionManager:
    """StreamableHTTPSessionManager.run() can only be called once per instance."""
    return StreamableHTTPSessionManager(
        app=mcp._lowlevel_server, json_response=True, stateless=True
    )


def _build_asgi_scope(request: HttpRequest) -> Scope:
    """Convert a Django HttpRequest into a minimal ASGI 'http' scope."""
    body = request.body
    headers: list[tuple[bytes, bytes]] = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in request.headers.items()
        if key.lower() != "content-length"
    ]
    headers.append((b"content-length", str(len(body)).encode("latin-1")))

    return {
        "type": "http",
        "http_version": "1.1",
        "method": request.method,
        "headers": headers,
        "path": request.path,
        "raw_path": request.get_full_path().encode("utf-8"),
        "query_string": request.META.get("QUERY_STRING", "").encode("latin-1"),
        "scheme": "https" if request.is_secure() else "http",
        "client": (request.META.get("REMOTE_ADDR", "127.0.0.1"), 0),
        "server": (request.get_host(), int(request.META.get("SERVER_PORT", 80))),
    }


async def _handle_mcp_request(request: HttpRequest) -> HttpResponse:
    """Dispatch a single Django request through the MCP session manager via ASGI."""
    session_manager = _new_session_manager()
    body = request.body
    scope = _build_asgi_scope(request)

    response_started: dict[str, Any] = {}
    response_body = bytearray()
    consumed = False

    async def receive() -> dict[str, Any]:
        nonlocal consumed
        if not consumed:
            consumed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response_started["status"] = message["status"]
            response_started["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            response_body.extend(message.get("body", b""))

    async with session_manager.run():
        await session_manager.handle_request(scope, receive, send)

    response = HttpResponse(
        bytes(response_body), status=response_started.get("status", 500)
    )
    for key, value in response_started.get("headers", []):
        name = key.decode("latin-1") if isinstance(key, bytes) else key
        if name.lower() in _HOP_BY_HOP_HEADERS:
            continue
        val = value.decode("latin-1") if isinstance(value, bytes) else value
        response[name] = val

    return response


async def _handle_mcp_request_with_timeout(request: HttpRequest) -> HttpResponse:
    return await asyncio.wait_for(
        _handle_mcp_request(request), timeout=_REQUEST_TIMEOUT_SECONDS
    )


def _error_response(status: int, message: str) -> JsonResponse:
    return JsonResponse(
        {"jsonrpc": "2.0", "error": {"code": -32000, "message": message}, "id": None},
        status=status,
    )


def _auth_error_response(
    request: HttpRequest, status: int, message: str
) -> JsonResponse:
    """_error_response() plus, for a 401, the RFC 6750 / RFC 9728 Bearer challenge.

    The challenge's resource_metadata is how an MCP client discovers the OAuth
    façade (see auth/discovery.py); error="invalid_token" marks a request that
    presented a credential.
    """
    response = _error_response(status, message)
    if status == 401:
        response["WWW-Authenticate"] = challenge_header(
            request, invalid_token=bool(request.META.get("HTTP_AUTHORIZATION"))
        )
    return response


#: Messages of the long-lived credential refusals (membership.long_lived_credential_allowed()).
_LONG_LIVED_REFUSALS = {
    "unreachable_past_grace": (
        "MCP access suspended: group membership could not be confirmed with the "
        "identity provider for too long. Try again later or sign in to InvenTree again."
    ),
    "relogin_required": (
        "MCP access suspended: sign in to InvenTree again to keep using this credential."
    ),
}


def _long_lived_refusal(reason: str) -> JsonResponse:
    """401 for an API token / Basic credential past the grace or re-login window.

    The challenge is the bare realm: the credential itself is valid (no
    error="invalid_token") and an OAuth flow is not what fixes it.
    """
    response = _error_response(
        401, _LONG_LIVED_REFUSALS.get(reason, "MCP access suspended.")
    )
    response["WWW-Authenticate"] = f'Bearer realm="{REALM}"'
    return response


def _queue_membership_recheck(user) -> None:
    """Queue a background membership re-check for a long-lived credential (ApiToken, Basic).

    OAuth access tokens are short-lived and re-checked at refresh; ApiTokens
    and passwords are not, so their requests queue tasks.recheck_user when the
    last check is stale. Never makes the request wait; a short cache marker
    keeps a burst of requests from queueing (and de-duplicating) repeatedly.
    After a NO_UPSTREAM_TOKEN outcome membership.recheck() holds the same
    marker for the whole re-check interval. Without InvenTree's global cache
    (Redis) the marker is per process.
    """
    try:
        if not membership.recheck_due(user):
            return
        if not cache.add(
            membership.recheck_queued_key(user.pk),
            True,
            membership.RECHECK_QUEUED_SECONDS,
        ):
            return
        from InvenTree.tasks import offload_task

        offload_task(
            "inventree_mcp.auth.tasks.recheck_user",
            user.pk,
            force_async=True,
            check_duplicates=True,
        )
    except Exception:  # a re-check must never fail an MCP request (logged below)
        logger.warning(
            "inventree-mcp: could not queue a membership re-check for user id %s",
            user.pk,
            exc_info=True,
        )


def scoped_token_for(user, scopes=None):
    """An unsaved DOT AccessToken carrying *user*'s MCP scopes (policy.narrow(None, user)).

    Bound as the identity's oauth2_token for API-token and Basic requests, so
    proxied views enforce the MCP rulesets exactly as for an OAuth token.
    Never saved: no token value, no application, nothing to revoke or leak.
    *scopes* (policy.connect_scopes()'s list) saves recomputing them.
    """
    if scopes is None:
        scopes = policy.narrow(None, user)
    return get_access_token_model()(
        user=user,
        scope=" ".join(scopes),
        expires=timezone.now() + _SCOPED_TOKEN_LIFETIME,
    )


def narrowed_oauth_token(token, user, scopes):
    """An unsaved AccessToken: the presented OAuth *token*'s scopes ∩ *scopes*, same expiry.

    Every OAuth token on /mcp is cut to the user's current MCP scopes, not
    only the façade's (narrowed at authorize and refresh): core's /o/
    endpoints issue tokens of any application with any scope, and a group
    membership or ruleset change since issue takes effect at the next
    request. An empty intersection leaves an empty scope (the caller answers
    403). Like scoped_token_for(), never saved.
    """
    allowed = set(scopes)
    return get_access_token_model()(
        user=user,
        scope=" ".join(s for s in (token.scope or "").split() if s in allowed),
        expires=token.expires,
    )


def request_meta(request: HttpRequest) -> dict[str, str]:
    """The WSGI environ entries proxied requests inherit (context._Identity.request_meta).

    Resolved values: get_host() has validated the host against ALLOWED_HOSTS
    and applied USE_X_FORWARDED_HOST, is_secure() SECURE_PROXY_SSL_HEADER;
    the raw X-Forwarded-* headers are not passed on. No credentials, cookies
    or CSRF headers (the bridge supplies authentication).
    """
    meta = {
        "HTTP_HOST": request.get_host(),
        "wsgi.url_scheme": "https" if request.is_secure() else "http",
        "REMOTE_ADDR": request.META.get("REMOTE_ADDR", "127.0.0.1"),
    }
    if request.META.get("HTTP_ACCEPT_LANGUAGE"):
        meta["HTTP_ACCEPT_LANGUAGE"] = request.META["HTTP_ACCEPT_LANGUAGE"]
    return meta


class MCPView(APIView):
    """DRF view handling MCP Streamable HTTP transport requests.

    Runs its own authentication (Token / Basic / OAuth2 - see module
    docstring for why SessionAuthentication is deliberately excluded) rather
    than relying on permission_classes - REQUIRE_AUTH is admin-configurable,
    and per-tool authorization happens downstream in proxy.call_view(), not
    here.
    """

    authentication_classes: ClassVar[list] = [
        ApiTokenAuthentication,
        BasicAuthentication,
        ExtendedOAuth2Authentication,
    ]
    permission_classes: ClassVar[list] = []

    def get_authenticators(self):
        """The authenticators above; BasicAuthentication only with MCP_ALLOW_BASIC_AUTH."""
        allow_basic = get_plugin_setting("MCP_ALLOW_BASIC_AUTH", False)
        return [
            auth()
            for auth in self.authentication_classes
            if allow_basic or auth is not BasicAuthentication
        ]

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        # Force Django to cache the raw body now, before initialize_request()
        # wraps it in a DRF Request - DRF's own body/stream handling during
        # authentication otherwise leaves the raw request unable to satisfy a
        # later `request.body` read (_handle_mcp_request needs it for the
        # ASGI scope), raising RawPostDataException.
        _ = request.body

        self.args = args
        self.kwargs = kwargs
        drf_request = self.initialize_request(request, *args, **kwargs)

        try:
            self.perform_authentication(drf_request)
        except exceptions.APIException as exc:
            return _auth_error_response(
                request, getattr(exc, "status_code", 401), str(exc)
            )

        user = drf_request.user

        if get_plugin_setting("REQUIRE_AUTH") and not (user and user.is_authenticated):
            return _auth_error_response(
                request,
                401,
                "Authentication required. Provide a valid Token, Bearer, or Basic credential.",
            )

        is_oauth2 = isinstance(
            drf_request.successful_authenticator, OAuth2Authentication
        )

        oauth2_token = None
        if user is not None and user.is_authenticated:
            # may_connect() and the user's MCP scopes in one pass.
            ok, reason, scopes = policy.connect_scopes(user)
            if not ok:
                if reason == "blocked":
                    return _auth_error_response(
                        request, 401, "Access revoked, sign in to InvenTree again."
                    )
                # inactive / no_mcp_permission: the credential itself is valid.
                return _error_response(403, "No MCP permission")
            if is_oauth2:
                oauth2_token = narrowed_oauth_token(drf_request.auth, user, scopes)
            else:
                # Queued before the window check: once the provider is back, the
                # background re-check is what lets this credential in again.
                _queue_membership_recheck(user)
                allowed, why = membership.long_lived_credential_allowed(user)
                if not allowed:
                    logger.info(
                        "inventree-mcp: long-lived credential refused (%s) for user id %s",
                        why,
                        user.pk,
                    )
                    return _long_lived_refusal(why)
                # API token / Basic: narrowed like an OAuth token (module docstring).
                oauth2_token = scoped_token_for(user, scopes)
            if not oauth2_token.scope:
                # An OAuth token holding none of the user's MCP scopes.
                return _error_response(403, "No MCP permission")

        token = set_current_user(user, oauth2_token, request_meta(request))
        try:
            # async_to_sync (rather than a hand-rolled event loop) is what lets
            # proxy.call_view()'s sync_to_async(thread_sensitive=True) route
            # back onto *this* thread - see the note in proxy.py.
            return async_to_sync(_handle_mcp_request_with_timeout)(request)
        except TimeoutError:
            return _error_response(
                504, f"Request timed out after {_REQUEST_TIMEOUT_SECONDS:.0f}s"
            )
        except Exception:  # noqa: BLE001 - last-resort guard so a tool bug returns JSON-RPC, not an HTML 500
            return _error_response(500, "Internal server error")
        finally:
            reset_current_user(token)


_mcp_view = csrf_exempt(auth_exempt(MCPView.as_view()))

urlpatterns = [
    path("mcp/", _mcp_view, name="mcp-endpoint"),
    # Some MCP clients (Claude Code) drop the trailing slash of the configured
    # URL; without this alias their POST misses the view (no APPEND_SLASH
    # redirect for POST) and gets InvenTree's bare 401 without the OAuth challenge.
    path("mcp", _mcp_view, name="mcp-endpoint-noslash"),
]
