"""Inject the façade's DOT settings into the running process.

InvenTree hard-codes ``settings.OAUTH2_PROVIDER`` (only SCOPES, OIDC and PKCE),
with no config-file override, so CIMD, token lifetimes and refresh-token reuse
protection are merged into that dict at runtime and DOT's settings object is
reloaded. Upstream fix proposed in the master plan (WP6, PR 1); delete this
module once core can be configured instead.

Values come from plugin settings so admins can tune them without a restart:
every façade view and the 401 challenge call ensure_applied(), which re-applies
only when the desired values changed (per process). While OAUTH_ENABLED is off
nothing is injected, and values injected earlier are restored to what
InvenTree configured, so installing the plugin does not change DOT's
instance-wide lifetimes for other OAuth applications.

Also injected, beyond the overview's list: DCR_REGISTRATION_TOKEN_EXPIRE_SECONDS
(see desired_values()). Never injected: DCR_ENABLED (the façade does its own registration, DOT's
/o/register/ stays off) and OIDC_ISS_ENDPOINT (DOT's /o/ issuer stays as is).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from django.conf import settings as django_settings

logger = logging.getLogger("inventree")

_MISSING = object()

#: What OAUTH2_PROVIDER held for each key before the first injection.
_originals: dict[str, Any] = {}
#: The values injected by the last apply() in this process ({} = none).
_last_applied: dict[str, Any] | None = None
_lock = threading.Lock()


def _split_hosts(value: str) -> list[str]:
    return [host.strip() for host in value.split(",") if host.strip()]


def desired_values() -> dict[str, Any]:
    """The DOT settings the façade needs, from the plugin settings ({} when disabled)."""
    from ..settings import get_plugin_setting
    from . import oauth_enabled

    if not oauth_enabled():
        return {}

    return {
        "CIMD_ENABLED": True,
        "CIMD_REGISTRATION_PERMISSION_CLASSES": (
            "oauth2_provider.cimd.HostAllowlistCIMDPermission",
        ),
        "CIMD_ALLOWED_HOSTS": _split_hosts(
            get_plugin_setting(
                "TRUSTED_CLIENT_HOSTS", "claude.ai,claude.com,.anthropic.com"
            )
        ),
        "ALLOW_LOCALHOST_LOOPBACK": get_plugin_setting(
            "ALLOW_LOCALHOST_LOOPBACK", True
        ),
        "ACCESS_TOKEN_EXPIRE_SECONDS": get_plugin_setting(
            "ACCESS_TOKEN_LIFETIME_SECONDS", 3600
        ),
        "REFRESH_TOKEN_EXPIRE_SECONDS": get_plugin_setting(
            "REFRESH_TOKEN_LIFETIME_DAYS", 30
        )
        * 86400,
        "AUTHORIZATION_CODE_EXPIRE_SECONDS": get_plugin_setting(
            "AUTH_CODE_LIFETIME_SECONDS", 600
        ),
        "REFRESH_TOKEN_REUSE_PROTECTION": True,
        "REFRESH_TOKEN_GRACE_PERIOD_SECONDS": 120,
        # DOT's default (None) dates the registration access token issued with
        # every DCR client to year 9999, which overflows PostgreSQL as soon as
        # the connection time zone is east of UTC (any query or cascade delete
        # reading the row fails). The token is useless here anyway: the RFC 7592
        # management endpoint is DOT's /o/register/<id>/, which stays disabled.
        "DCR_REGISTRATION_TOKEN_EXPIRE_SECONDS": 86400,
    }


def _reset_oauthlib_cores() -> None:
    """Drop every cached oauthlib server so new lifetimes reach token issuance.

    DOT caches one OAuthLibCore per view class (``_oauthlib_core``), built with
    ``server_kwargs`` (``token_expires_in`` etc.) at first use; ``reload()``
    alone does not reach it.
    """
    from oauth2_provider.views.mixins import OAuthLibMixin

    pending = [OAuthLibMixin]
    while pending:
        cls = pending.pop()
        if "_oauthlib_core" in cls.__dict__:
            delattr(cls, "_oauthlib_core")
        pending.extend(cls.__subclasses__())


def _write(values: dict[str, Any]) -> None:
    """Merge *values* into OAUTH2_PROVIDER (restoring originals for keys not given)."""
    from oauth2_provider.settings import oauth2_settings

    provider = django_settings.OAUTH2_PROVIDER

    for key in values:
        _originals.setdefault(key, provider.get(key, _MISSING))

    for key, original in _originals.items():
        if key in values:
            provider[key] = values[key]
        elif original is _MISSING:
            provider.pop(key, None)
        else:
            provider[key] = original

    oauth2_settings.reload()
    _reset_oauthlib_cores()


def apply() -> dict[str, Any]:
    """Inject the desired DOT settings now; returns the values injected."""
    return _apply(desired_values())


def _apply(values: dict[str, Any]) -> dict[str, Any]:
    global _last_applied

    with _lock:
        _write(values)
        _last_applied = values

    if values:
        logger.info(
            "inventree-mcp: DOT settings injected: %s",
            ", ".join(f"{key}={value!r}" for key, value in sorted(values.items())),
        )
    else:
        logger.info("inventree-mcp: OAuth façade disabled, no DOT settings injected")
    return values


def ensure_applied() -> None:
    """Re-apply when the plugin settings changed since the last apply() in this process."""
    values = desired_values()
    if values != _last_applied:
        _apply(values)
