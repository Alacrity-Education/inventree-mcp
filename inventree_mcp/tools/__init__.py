"""MCP tools of InvenTree MCP: the tool set of github.com/syntaxerr66/inventree-mcp.

One module per resource (parts, categories, stock, locations) plus images (part image
search). Each tool is a thin async wrapper around proxy.call_view() built from the helpers in
_common.py (tool(), access(), listing(), compact(), clean(), text()), and each module ends with
the access() declarations tool_visibility.py reads. The modules register their tools as a
side effect of being imported by mcp_server.py - see that module for why they must stay
import-safe (InvenTree view classes are resolved lazily, through view_resolution).
"""
