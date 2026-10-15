"""Helpers for reading InvenTreeMCP's own plugin settings.

One place that resolves the plugin instance, coerces the stored value to the
type of the caller's default and fails safe to that default. Used by
mcp_transport.py (REQUIRE_AUTH), proxy.py (MCP_READ_ONLY) and the auth package
(OAUTH_ENABLED, lifetimes, trusted hosts, ...).
"""

from __future__ import annotations

from typing import Any, TypeVar

T = TypeVar("T", bool, int, str)

PLUGIN_SLUG = "inventree-mcp"


def _get_plugin_instance() -> Any:
    from plugin import registry

    return registry.get_plugin(PLUGIN_SLUG)


def _registry_knows_plugin() -> bool:
    """Whether the plugin registry has loaded this plugin (active or not) in this process."""
    from plugin import registry

    return PLUGIN_SLUG in registry.plugins


def _stored_value(key: str) -> Any:
    """The value stored for *key*, read straight from the database; None when unset.

    Used before the registry holds a plugin instance: while the registry is
    still loading (the plugin's own __init__) or in a process that has not
    loaded plugins. Only an active plugin's settings count, as with the
    registry.
    """
    from plugin.models import PluginConfig, PluginSetting

    config = PluginConfig.objects.filter(key=PLUGIN_SLUG).first()
    if config is None or not config.is_active():
        return None
    return (
        PluginSetting.objects
        .filter(plugin=config, key=key)
        .values_list("value", flat=True)
        .first()
    )


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

    Fails safe to *default* if the plugin is inactive, the setting lookup
    raises, the value is unset or it cannot be coerced. While the registry has
    not loaded the plugin yet (start-up, see _stored_value()) the stored value
    is read from the database directly. Security-relevant settings
    (REQUIRE_AUTH, MCP_READ_ONLY, OAUTH_ENABLED) are read with their
    restrictive value as default, so "can't tell" and "restricted" behave the
    same.
    """
    try:
        if not _registry_knows_plugin():
            value = _stored_value(key)
        else:
            plugin = _get_plugin_instance()
            if plugin is None:  # loaded, but inactive
                return default
            value = plugin.get_setting(key)
        if value is None:
            return default
        return _coerce(value, default)
    except Exception:  # noqa: BLE001 - fail safe to the default, see docstring
        return default


def setting_default(key: str) -> Any:
    """The default declared for *key* in InvenTreeMCP.SETTINGS (the single source of defaults)."""
    from .core import InvenTreeMCP

    return InvenTreeMCP.SETTINGS[key]["default"]


def plugin_setting(key: str) -> Any:
    """get_plugin_setting() with the default (and so the type) declared in InvenTreeMCP.SETTINGS."""
    return get_plugin_setting(key, setting_default(key))


def plugin_setting_list(key: str) -> list[str]:
    """A comma separated plugin setting as a list of stripped, non-empty items."""
    return [item.strip() for item in plugin_setting(key).split(",") if item.strip()]
