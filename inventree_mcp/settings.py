"""Helpers for reading InvenTreeMCP's own plugin settings.

One place that resolves the plugin instance, coerces the stored value to the
type of the caller's default and fails safe to that default. Used by
mcp_transport.py (REQUIRE_AUTH), proxy.py (MCP_READ_ONLY) and the auth package
(OAUTH_ENABLED, lifetimes, trusted hosts, ...).
"""

from __future__ import annotations

from typing import Any, TypeVar

T = TypeVar("T", bool, int, str)


def _get_plugin_instance() -> Any:
    from plugin import registry

    return registry.get_plugin("inventree-mcp")


def _coerce(value: Any, default: T) -> T:
    """Coerce *value* to ``type(default)``; raises on values that do not fit."""
    if isinstance(default, bool):
        if isinstance(value, str):
            from InvenTree.helpers import str2bool

            return str2bool(value)
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, str):
        return str(value)
    return value


def get_plugin_setting(key: str, default: T = True) -> T:
    """Read one of this plugin's own settings, coerced to ``type(default)``.

    Fails safe to *default* if the plugin instance can't be resolved (e.g.
    outside of a real request), the setting lookup raises, the value is unset
    or it cannot be coerced. Security-relevant settings (REQUIRE_AUTH,
    MCP_READ_ONLY, OAUTH_ENABLED) are read with their restrictive value as
    default, so "can't tell" and "restricted" behave the same.
    """
    plugin = _get_plugin_instance()

    if plugin is None:
        return default

    try:
        value = plugin.get_setting(key)
        if value is None:
            return default
        return _coerce(value, default)
    except Exception:  # noqa: BLE001 - fail safe to the default, see docstring
        return default
