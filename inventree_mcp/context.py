"""Propagate the authenticated Django user (and OAuth2 auth, if any) into MCP tool execution.

Tool functions are invoked by the MCP SDK's dispatcher, which does not know
about Django requests. The transport view sets the current identity via a
contextvar before handing the request off to the MCP session manager, so
tools (and the proxy layer) can recover *who* is calling without threading a
request object through every tool signature.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.contrib.auth.models import AbstractUser

_NO_META: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class _Identity:
    user: AbstractUser
    # The oauth2_provider AccessToken whose scopes bound this request: the
    # real token of an OAuth2-authenticated MCP request, or for API-token /
    # Basic requests an unsaved one carrying the user's MCP scopes (see
    # mcp_transport.scoped_token_for). None only when nobody is authenticated.
    # Propagated so proxy.call_view() makes the proxied view see these scopes,
    # not just the user's role permissions. See proxy.py.
    oauth2_token: Any | None = None
    # WSGI environ entries of the originating MCP request that proxied
    # requests must carry instead of APIRequestFactory's defaults: the
    # resolved HTTP_HOST and wsgi.url_scheme (absolute URLs such as a
    # paginated list's "next" are built from them, and "testserver" fails
    # ALLOWED_HOSTS), REMOTE_ADDR, and HTTP_ACCEPT_LANGUAGE when sent.
    request_meta: Mapping[str, str] = field(default=_NO_META)


_current_identity: ContextVar[_Identity | None] = ContextVar(
    "inventree_mcp_current_identity", default=None
)


def set_current_user(
    user: AbstractUser | None,
    oauth2_token: Any | None = None,
    request_meta: Mapping[str, str] | None = None,
) -> Token:
    """Bind *user*, the token whose scopes bound the request, and the request's meta.

    *request_meta* (see _Identity) is optional; without it proxied requests
    keep APIRequestFactory's defaults.
    """
    identity = (
        _Identity(user, oauth2_token, MappingProxyType(dict(request_meta or {})))
        if user is not None
        else None
    )
    return _current_identity.set(identity)


def reset_current_user(token: Token) -> None:
    """Undo a prior set_current_user call."""
    _current_identity.reset(token)


def has_bound_identity() -> bool:
    """Return whether set_current_user() has been called in this context at all.

    True for *any* real MCP request - including one that resolved to an
    unauthenticated (AnonymousUser) identity, e.g. REQUIRE_AUTH disabled and
    no credentials were sent. False only outside a real request (e.g. static
    introspection - see has_current_user()'s docstring). Distinct from
    has_current_user(): that answers "is there someone to check permissions
    for", this answers "did a request happen at all" - callers that need to
    tell "no request" (safe to skip filtering) apart from "a request with no
    authenticated user" (should filter down to nothing, not everything) need
    this one, not has_current_user().
    """
    return _current_identity.get() is not None


def has_current_user() -> bool:
    """Return whether an *authenticated* user is currently bound.

    False both outside a real MCP request (see has_bound_identity() to tell
    that case apart) and for a real request that resolved to an
    unauthenticated (AnonymousUser) identity - callers that only need "is
    there someone to check permissions for" (e.g. tool_logging.py's caller
    label) can treat both the same way; callers that need to react
    differently to the two (e.g. tool_visibility.py, which must not show
    every tool just because this request happened to be unauthenticated)
    should check has_bound_identity() first.
    """
    identity = _current_identity.get()
    return identity is not None and identity.user.is_authenticated


def get_current_user() -> AbstractUser:
    """Return the user bound to the current MCP request.

    Raises:
        PermissionError: if no user is bound (e.g. a tool is invoked outside
            of a real MCP request, or authentication was not enforced).
    """
    identity = _current_identity.get()

    if identity is None or not identity.user.is_authenticated:
        raise PermissionError("No authenticated user for this MCP request")

    return identity.user


def get_current_oauth2_token() -> Any | None:
    """Return the real OAuth2 access token for the current MCP request, if any."""
    identity = _current_identity.get()
    return identity.oauth2_token if identity else None


def get_current_request_meta() -> dict[str, str]:
    """WSGI environ entries of the originating MCP request (empty outside a request)."""
    identity = _current_identity.get()
    return dict(identity.request_meta) if identity else {}
