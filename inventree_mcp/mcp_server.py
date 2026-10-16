"""The MCPServer instance and tool registry for InvenTree MCP.

InvenTree's plugin registry walks every submodule of a plugin package at
discovery time (plugin.helpers.get_modules) and may pass arbitrary
module-level objects through inspect.getmembers() while looking for
InvenTreePlugin subclasses. Doing that to a live MCPServer instance can touch
lazily-computed properties (e.g. session_manager, which raises RuntimeError
if accessed before streamable_http_app() has ever been called) before the
server has ever been run. Setting __all__ = [] here keeps this instance out
of that scan without changing how it's imported elsewhere (`from .mcp_server
import mcp` still works - __all__ only affects `from module import *` and
the registry's own name-filtering).

stateless_http/json_response are no longer MCPServer constructor arguments
(mcp 2.0's breaking rewrite moved them onto streamable_http_app() /
run_streamable_http_async()) - they're passed directly to the
StreamableHTTPSessionManager mcp_transport.py constructs instead.
"""

from __future__ import annotations

__all__: list[str] = []

from mcp.server.mcpserver import MCPServer

from . import PLUGIN_VERSION

INSTRUCTIONS = """\
MCP server for InvenTree inventory management: parts, part categories, stock items and
stock locations. Every tool runs through InvenTree's own API as the signed-in user, so the
user's InvenTree permissions and MCP roles apply. `id` arguments are database primary keys
(pk) returned by the search, list and get tools.

Workflow:
- Search before you create: use search_parts (and search_part_categories /
  search_stock_locations) to find existing records before creating new ones, and reuse them.
- Categories: list the hierarchy with list_part_categories and read the pathstring fields.
  Categories can be nested many levels deep (e.g. Electronic Components/Resistors/Through
  Hole/1/8 Watt): always pick the deepest, most specific category that fits. If none fits,
  create one with create_part_category under the most appropriate parent; do not put a part
  in an unrelated category just because it exists, and do not create top-level categories
  when the part belongs under an existing parent. Parts should always have a category.
- Part descriptions from part numbers: when the user gives only a part number (e.g. LM7805,
  ESP32-S3-WROOM-1), look up or infer what the part is and write a short descriptive
  description. Never leave the description blank or just repeat the part number.
- Stock: add_stock creates a new stock item for a part; stock_add_quantity,
  stock_remove_quantity and stock_transfer take stock item pks (not part ids), which get_stock
  lists.
- Images: search_part_images finds product image URLs (needs the plugin settings
  GOOGLE_API_KEY and GOOGLE_CSE_ID); pass a URL to create_part / update_part (image_url) or
  set_part_image. InvenTree downloads it server-side when its "download from URL" setting is
  enabled.

Write tools (create, update, delete, stock adjustments) work only when an administrator has
turned off the plugin's "Read Only" setting and the user holds the matching MCP write roles
(add, change, delete) as well as the InvenTree permissions; otherwise they answer with an
error. Delete tools cannot be undone: confirm with the user first.
"""

mcp = MCPServer(name="InvenTree MCP", instructions=INSTRUCTIONS, version=PLUGIN_VERSION)

# Import the tool modules for their side effect of registering their tools
# (tools._common.tool()) and their ACCESS entries; tool_visibility.apply()
# reads ACCESS, so it runs after these imports.
from . import tool_logging, tool_visibility
from .tools import categories, images, locations, parts, stock  # noqa: F401

tool_visibility.apply()
tool_logging.apply()
