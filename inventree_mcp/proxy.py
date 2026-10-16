"""Permission-safe bridge from MCP tools to InvenTree's existing REST API views.

Every MCP tool must read/write data through call_view() rather than the ORM
directly. Dispatching through the real DRF view class means the exact same
authentication, RolePermission / ModelPermission checks, filtering, and
serialization that the normal REST API enforces also apply here - a user can
never see or change more via MCP than they could via the regular API.

Failures become a ToolError whose text starts like the Go server's
(github.com/syntaxerr66/inventree-mcp, internal/client/client.go):

- 401 "authentication failed (401)"
- 403 "permission denied (403): your MCP roles or your InvenTree permissions do not allow this"
- 404 "not found (404): resource does not exist"
- 400 "validation failed (400): " followed by one "field: message" line per field of
  InvenTree's error body (lists joined with "; ", nested fields as "items[0].quantity",
  non_field_errors and detail without a prefix)
- anything else "API error <status>: <body>" ("API error <status> (empty response)")

Error details are cut at 500 characters, and credential-shaped "Token <value>",
"Bearer <value>" and "Basic <value>" strings (16+ token characters) are redacted from them. The MCP SDK prefixes the text it sends with
"Error executing tool <name>: ". Two more ToolErrors come from here: the read-only gate
(any non-GET call while the MCP_READ_ONLY setting is on, the default) and a view class
that could not be resolved on the running InvenTree version.
"""

from __future__ import annotations

import json
import re
from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.mcpserver.exceptions import ToolError
from rest_framework import exceptions
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSetMixin

from .context import (
    get_current_oauth2_token,
    get_current_request_meta,
    get_current_user,
)
from .oauth2_bridge import authentication_classes_for, scoped_view_class
from .settings import get_plugin_setting

_factory = APIRequestFactory()

#: Longest error detail passed on (the Go server's limit).
MAX_ERROR_DETAIL = 500

#: Error body keys whose messages are not about one field.
_UNPREFIXED_KEYS = frozenset({"non_field_errors", "detail", "__all__"})

#: Credentials that must never reach a tool result.
#: Case-sensitive and credential-shaped (16+ token characters), so ordinary words in
#: validation messages ("Basic units", "token expired") are left alone.
_CREDENTIAL = re.compile(r"\b(Token|Bearer|Basic) [A-Za-z0-9._~+/=-]{16,}")

#: The read-only gate's ToolError text (MCP_READ_ONLY).
READ_ONLY_TEXT = (
    "This MCP server is running in read-only mode; write actions are disabled. "
    "An administrator must disable the 'Read Only' plugin setting to allow them."
)

_STATUS_TEXT = {
    401: "authentication failed (401)",
    403: (
        "permission denied (403): your MCP roles or your InvenTree permissions "
        "do not allow this"
    ),
    404: "not found (404): resource does not exist",
}


def _truncate(detail: str) -> str:
    """*detail* without credentials, cut at MAX_ERROR_DETAIL characters."""
    detail = _CREDENTIAL.sub(r"\1 [REDACTED]", detail.strip())
    if len(detail) > MAX_ERROR_DETAIL:
        return detail[:MAX_ERROR_DETAIL] + "..."
    return detail


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, dict | list)


def _error_lines(body: Any, prefix: str = "") -> list[str]:
    """InvenTree's validation error body as "field: message" lines.

    {"name": ["required"], "items": [{}, {"quantity": ["> 0"]}], "non_field_errors": ["x"]}
    -> ["name: required", "items[1].quantity: > 0", "x"].
    """
    lead = f"{prefix}: " if prefix else ""
    if isinstance(body, dict):
        lines = []
        for key, value in body.items():
            if key in _UNPREFIXED_KEYS:
                child = prefix
            else:
                child = f"{prefix}.{key}" if prefix else str(key)
            lines.extend(_error_lines(value, child))
        return lines
    if isinstance(body, list):
        if all(_is_scalar(item) for item in body):
            messages = [str(item) for item in body if str(item)]
            return [lead + "; ".join(messages)] if messages else []
        lines = []
        for index, item in enumerate(body):
            if _is_scalar(item):
                lines.extend(_error_lines([item], prefix))
            else:
                lines.extend(_error_lines(item, f"{prefix}[{index}]"))
        return lines
    if body is None or str(body) == "":
        return []
    return [lead + str(body)]


def error_message(status: int, body: Any) -> str:
    """The ToolError text for an InvenTree response with HTTP *status* and parsed *body*.

    *body* is the decoded JSON, or the raw text when the response was not JSON.
    """
    if status in _STATUS_TEXT:
        return _STATUS_TEXT[status]
    if status == 400:
        lines = _error_lines(body)
        return "validation failed (400): " + _truncate(
            "\n".join(lines) if lines else _body_text(body)
        )
    detail = _body_text(body)
    if not detail:
        return f"API error {status} (empty response)"
    return f"API error {status}: {_truncate(detail)}"


def _body_text(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, dict | list) and not body:
        return ""
    return json.dumps(body, ensure_ascii=False)


def _parse_body(content: bytes) -> Any:
    """The JSON of *content*, {} when empty, or its text when it is not JSON."""
    if not content:
        return {}
    try:
        return json.loads(content)
    except ValueError:
        return content.decode("utf-8", errors="replace")


# HTTP method -> ViewSet action, for the case where a pk *is* present
# (GET/'retrieve' is the odd one out - see _viewset_actions()).
_DETAIL_ACTIONS = {
    "GET": "retrieve",
    "PUT": "update",
    "PATCH": "partial_update",
    "DELETE": "destroy",
}


def _viewset_actions(
    view_cls: type[APIView], method: str, view_kwargs: dict[str, Any]
) -> dict[str, str] | None:
    """Build the `actions` mapping DRF ViewSet.as_view() requires, or None for a plain view.

    InvenTree core is gradually converting some endpoints (PurchaseOrder so
    far) from separate List/Detail generic views to a single combined
    ViewSet class serving both routes - unlike a plain generic view,
    ViewSet.as_view() can't infer 'list' vs 'retrieve' from the request
    itself and raises TypeError without an explicit method->action mapping
    (see rest_framework.viewsets.ViewSetMixin.as_view). A bare
    view_kwargs['pk'] is what distinguishes a detail call from a list call
    for every tool in tools/*.py, matching how the URL a real router would
    generate carries a pk only for detail routes.
    """
    if not issubclass(view_cls, ViewSetMixin):
        return None

    method = method.upper()
    if "pk" not in view_kwargs and method in ("GET", "POST"):
        # List routes: GET lists, POST creates (the write tools' POSTs).
        action = "list" if method == "GET" else "create"
    else:
        action = _DETAIL_ACTIONS[method]
    return {method.lower(): action}


def ensure_writable(method: str = "POST") -> None:
    """The read-only gate: ToolError(READ_ONLY_TEXT) for a non-GET *method* while
    MCP_READ_ONLY is on (blocking; reads a plugin setting).

    call_view() applies it to every call; tools that do work before their first write
    (the image download in tools/images.py) call it first themselves.
    """
    if method.upper() != "GET" and get_plugin_setting("MCP_READ_ONLY"):
        raise ToolError(READ_ONLY_TEXT)


def _call_view_sync(
    view_cls: type[APIView],
    method: str,
    path: str,
    *,
    query_params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    **view_kwargs: Any,
) -> Any:
    user = get_current_user()
    oauth2_token = get_current_oauth2_token()
    method = method.upper()

    ensure_writable(method)

    factory_method = getattr(_factory, method.lower())
    # The originating request's resolved host, scheme and client address
    # (context._Identity.request_meta) instead of the factory's "testserver":
    # paginated views build absolute "next" / "previous" links from it, and
    # "testserver" fails ALLOWED_HOSTS on a real instance.
    meta = get_current_request_meta()

    if method == "GET":
        request = factory_method(path, data=query_params or {}, **meta)
    elif files:
        # File uploads (the part image): a multipart body with the files next to *data*.
        request = factory_method(
            path, data={**(data or {}), **files}, format="multipart", **meta
        )
    else:
        request = factory_method(path, data=data or {}, format="json", **meta)

    actions = _viewset_actions(view_cls, method, view_kwargs)

    if oauth2_token is not None:
        # The MCP request itself was OAuth2-authenticated: make the proxied
        # view see the *real* token (via a real OAuth2Authentication
        # subclass), so InvenTreeTokenMatchesOASRequirements enforces the
        # token's actual granted scopes - not just the user's role
        # permissions. force_authenticate() can't do this: it always
        # presents as a synthetic ForcedAuthentication, which the scope
        # check doesn't recognize as OAuth2 at all. scoped_view_class() also
        # works around a separate InvenTree core bug - see oauth2_bridge.py.
        scoped_cls = scoped_view_class(view_cls)
        auth_classes = authentication_classes_for(user, oauth2_token)
        view = (
            scoped_cls.as_view(actions, authentication_classes=auth_classes)
            if actions is not None
            else scoped_cls.as_view(authentication_classes=auth_classes)
        )
    else:
        force_authenticate(request, user=user)
        view = view_cls.as_view(actions) if actions is not None else view_cls.as_view()

    response = view(request, **view_kwargs)
    response.render()

    body = _parse_body(response.rendered_content)

    if response.status_code >= 400:
        raise ToolError(error_message(response.status_code, body))

    return body


def _user_has_access_sync(view_cls: type[APIView], method: str) -> bool:
    try:
        user = get_current_user()
    except PermissionError:
        # No bound user (e.g. called outside a real MCP request) - nothing
        # to check permissions *for*. Mirrors tool_visibility.py's own
        # has_current_user() guard, but doesn't rely on every caller
        # remembering to check that first.
        return False

    oauth2_token = get_current_oauth2_token()
    method = method.upper()
    request = _factory.generic(method, "/", **get_current_request_meta())

    if oauth2_token is not None:
        # Same reasoning as _call_view_sync's OAuth2 branch: a synthetic
        # force_authenticate() identity isn't recognized as OAuth2 by the
        # scope-check permission class, so the real (user, token) pair has
        # to be re-presented as a genuine OAuth2Authentication instance.
        view = scoped_view_class(view_cls)()
        view.authentication_classes = authentication_classes_for(user, oauth2_token)
    else:
        force_authenticate(request, user=user)
        view = view_cls()

    if isinstance(view, ViewSetMixin):
        # ViewSetMixin.initialize_request() reads self.action_map (normally
        # set by .as_view()) to derive self.action - without it, instantiating
        # the class directly (above) leaves that attribute unset and this
        # raises AttributeError. The specific action doesn't affect the
        # permission result (RolePermission keys off request.method, not
        # view.action) so 'list' stands in here the same way a GET
        # permission check already stands in for both list and detail tools.
        view.action_map = _viewset_actions(view_cls, method, {})

    view.args = ()
    view.kwargs = {}
    drf_request = view.initialize_request(request)
    view.request = drf_request

    try:
        # initial() (not a hand-picked subset of it) deliberately: some
        # InvenTree permission classes' has_permission() end up calling
        # view.get_queryset() to resolve the model for a permission-codename
        # lookup (DRF's DjangoModelPermissions._queryset()), which needs
        # format_kwarg/content-negotiation state that only initial() sets up
        # - skipping straight to check_permissions() raised a bare
        # AttributeError ('BomList' object has no attribute 'format_kwarg')
        # instead of a clean permission result. check_throttles() runs too,
        # which is correct, not just harmless: a real GET call would be
        # subject to the same throttling.
        view.initial(drf_request)
    except exceptions.APIException:
        return False

    return True


async def user_has_access(view_cls: type[APIView], method: str = "GET") -> bool:
    """Check whether the current MCP user could actually call this view, without calling it.

    Used by tool_visibility.py to decide whether a tool should appear in
    tools/list at all - runs only the real permission check
    (RolePermission/OAuth2 scope, exactly what call_view() itself enforces),
    never the view's business logic, so it doesn't touch the database for
    real data and doesn't need a valid object id for detail views.

    tool_visibility.py passes the method each tool declared with
    tools._common.access() (GET for the read tools, POST / PATCH / DELETE for
    the write tools). Never OPTIONS - see tool_visibility.py's module
    docstring for why a literal HTTP OPTIONS request is the wrong tool for
    this: InvenTree's OAuth2 scope resolver
    (map_scope() in InvenTree/permissions.py) hardcodes OPTIONS to a generic
    "g:read" scope regardless of resource, while GET requires the real
    resource-specific scope (e.g. "r:view:part") - an OPTIONS-based check
    would show a tool as available to a narrowly-scoped OAuth2 token that
    the real GET call would then reject. RolePermission (used by
    token/basic auth) doesn't have this problem (it maps OPTIONS to "view",
    same as GET) - but checking via "GET" is correct for both cases, not
    just the one that would otherwise silently break.

    Args:
        view_cls: the view class to check (e.g. part.api.PartList).
        method: the HTTP method whose permission to check (the tool's
            declared method, see tools._common.ACCESS).

    Returns:
        True if the current user (and OAuth2 token, if applicable) has
        permission to make this request; False otherwise, including when no
        user is bound at all.
    """
    return await sync_to_async(_user_has_access_sync)(view_cls, method)


async def call_view(
    view_cls: type[APIView] | None,
    method: str,
    path: str,
    *,
    query_params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    **view_kwargs: Any,
) -> Any:
    """Invoke an existing InvenTree DRF view class as the current MCP user.

    Args:
        view_cls: the view class as used in the real API urlconf (e.g.
            part.api.PartList), or None if view_resolution.resolve_view()
            couldn't import it (a version mismatch between this plugin and
            the running InvenTree core - see that module's docstring).
        method: HTTP method to simulate ('GET', 'POST', 'PATCH', ...).
        path: the API path being emulated (only used for logging/routing context, not resolved).
        query_params: query string parameters (GET requests).
        data: request body (write requests), sent as JSON.
        files: uploaded files (field name -> django File, e.g. SimpleUploadedFile) for a
            write request; when given, the body is sent as multipart with *data* beside
            the files instead of JSON.
        view_kwargs: extra kwargs the URL pattern would normally supply (e.g. pk=...).

    Returns:
        The parsed JSON response body, exactly as a real API client would receive it.

    Raises:
        ToolError: the underlying API call did not succeed (permission denied,
            not found, validation error, ...), method is not GET while the
            plugin's MCP_READ_ONLY setting is enabled (the default), or
            view_cls is None. The message is safe to surface to the calling
            agent; the texts are listed in the module docstring
            (error_message()).

    Note:
        MCPServer calls tool functions directly in the request's event loop, and
        the actual view dispatch does synchronous Django ORM work - so it must
        be handed off via sync_to_async, or Django raises
        SynchronousOnlyOperation. thread_sensitive=True (the default) matters
        here: it routes the call back onto the thread that's running the
        request (see mcp_transport.py's use of async_to_sync), which keeps it
        on the same DB connection - a plain worker thread would get its own
        connection, silently missing whatever the request's transaction has
        open (breaking Django TestCase's per-test transaction, and able to
        deadlock against it under Postgres).
    """
    if view_cls is None:
        # Checked here rather than left to _call_view_sync (no ORM/IO
        # involved, so no need for the sync_to_async hop) - every tool body
        # reaches this through resolve_view(), so this is the one place a
        # missing endpoint turns into the same clean ToolError a real
        # permission or validation failure would give the caller, instead of
        # each call site needing its own None check.
        raise ToolError(
            "This tool is unavailable: its underlying InvenTree API endpoint "
            "could not be found. This usually means the InvenTree MCP "
            "plugin doesn't match the running InvenTree core version."
        )

    return await sync_to_async(_call_view_sync)(
        view_cls,
        method,
        path,
        query_params=query_params,
        data=data,
        files=files,
        **view_kwargs,
    )
