"""Filter tools/list results down to what the current caller can actually use.

Every tool is always *registered* - but a caller without the underlying API
endpoint's permission would only ever get a ToolError from actually calling
it. With the MCP_FILTER_TOOL_LIST setting on, a real tools/list request
reflects that up front: a tool is only listed if the current user (and, for
OAuth2 requests, their token's scope) can really reach the view it wraps,
with the HTTP method it uses - determined by running the *real* permission
check via proxy.user_has_access() (RolePermission / OAuth2 scope, the same
InvenTree.permissions machinery call_view() itself relies on), not a
hand-rolled guess. Which view and method decide is declared by each tool
module with tools._common.access() (ACCESS): GET on the list / detail view
for the read tools, POST / PATCH / DELETE for the write tools, so a user
without e.g. the r:delete:part scope does not see delete_part. A tool with
no ACCESS entry (search_part_images: no InvenTree call) is always listed.

This is a discovery-time convenience, not a new security boundary:
proxy.call_view() remains the only real enforcement point, and still runs in
full for every actual tool call regardless of what tools/list showed - a
client that calls a "hidden" tool by name gets exactly the same ToolError it
always did (see MCPServer.call_tool()'s own tool lookup, which never
consults this module). Don't rely on this module to prevent access to
anything; it only prevents *advertising* access that doesn't exist.

Why the tool's real method rather than a literal HTTP OPTIONS request (the
obvious-looking shortcut): InvenTree's OAuth2 scope resolver (map_scope() in
InvenTree/permissions.py) hardcodes OPTIONS to a generic "g:read" scope for
every view regardless of resource, while GET requires the real
resource-specific scope (e.g. "r:view:part"). An OPTIONS-based check would
show a tool as available to an OAuth2 token scoped down to "g:read" only,
even though the real call that tool makes would then be rejected - silently
defeating the exact "narrow a token below the user's role" scenario this
plugin exists to support correctly (see proxy.user_has_access()'s docstring).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from mcp.types import ListToolsResult, PaginatedRequestParams

from . import proxy
from .context import has_bound_identity
from .mcp_server import mcp
from .settings import get_plugin_setting
from .tools._common import ACCESS

if TYPE_CHECKING:
    from mcp.server.context import ServerRequestContext


async def visible_tool_names(names: Iterable[str]) -> set[str]:
    """Return the subset of *names* the current bound user can actually call.

    A tool with no ACCESS entry (search_part_images) is always included - it
    has no underlying view to check a permission against, so this holds even
    for an unauthenticated caller (see below). If there's no request at all
    (e.g. static introspection outside a real MCP request, such as a test
    calling mcp.list_tools() directly without binding an identity), every
    name is returned unfiltered, since there's no caller to filter *by* -
    this is not a real request path, so there's nothing unsafe about it.

    A real request that resolved to an unauthenticated identity (e.g.
    REQUIRE_AUTH disabled and no credentials were sent) is deliberately
    *not* treated the same as "no request" above - has_bound_identity()
    (not has_current_user()) gates the unfiltered fallback, so this case
    falls through into the per-tool loop below instead. Every gated tool
    then correctly resolves to "not visible" (proxy.user_has_access()
    catches get_current_user()'s PermissionError for an unauthenticated
    caller and returns False).

    Each (view, method) pair is checked once per call: list and detail
    tools of a resource with the same method share the result.
    """
    if not has_bound_identity():
        return set(names)

    visible: set[str] = set()
    checked: dict[tuple[type, str], bool] = {}

    for name in names:
        entry = ACCESS.get(name)
        if entry is None:
            visible.add(name)
            continue

        view_loader, method = entry
        view_cls = view_loader()
        if view_cls is None:
            continue
        key = (view_cls, method)
        if key not in checked:
            checked[key] = await proxy.user_has_access(view_cls, method)
        if checked[key]:
            visible.add(name)

    return visible


def apply() -> None:
    """Make the real (low-level, transport-facing) tools/list handler permission-aware.

    MCPServer.list_tools() is left untouched as an attribute - it's reassigned
    *around*, not in place, specifically so tests that call
    mcp.list_tools() directly to introspect the full, unfiltered registry
    keep working unchanged. What
    this actually overrides is the low-level mcp.server.lowlevel.Server's
    registered "tools/list" request handler - the thing a real client's
    tools/list request reaches over the wire (see mcp_transport.py) - by
    re-registering it via add_request_handler(), the same "no public API for
    this, reach into internals" approach tool_logging.apply() uses for
    tools/call. mcp 2.0's rewrite replaced the
    lowlevel Server's decorator-based `.list_tools()` registration with
    add_request_handler(method, params_type, handler) - the handler itself
    now takes (ctx, params) and must return a ListToolsResult, rather than a
    bare list[Tool].
    """
    unfiltered_list_tools = mcp.list_tools

    async def filtered_list_tools(
        ctx: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        tools = await unfiltered_list_tools()
        # Off by default: every tool is listed and a call the user may not
        # make fails with a permission error (call_view() enforces either way).
        if not await sync_to_async(get_plugin_setting)("MCP_FILTER_TOOL_LIST", False):
            return ListToolsResult(tools=list(tools))
        names = await visible_tool_names(tool.name for tool in tools)
        return ListToolsResult(tools=[tool for tool in tools if tool.name in names])

    mcp._lowlevel_server.add_request_handler(
        "tools/list", PaginatedRequestParams, filtered_list_tools
    )
