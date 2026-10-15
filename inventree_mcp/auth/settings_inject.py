"""Inject the façade's DOT settings into the running process.

InvenTree hard-codes ``settings.OAUTH2_PROVIDER`` (only SCOPES, OIDC and PKCE),
with no config-file override, so CIMD, token lifetimes and refresh-token reuse
protection are merged into that dict at runtime and DOT's settings object is
reloaded. Upstream fix proposed in the master plan (WP6, PR 1); delete this
module once core can be configured instead.

The values are per process, so every kind of process applies them:

- at start-up, from the plugin's ``__init__`` (apply()); the plugin settings
  are then read from the database, the registry holds no instance yet;
- in web processes, before every request (``request_started``, memoised) and
  at the top of every façade view and of the 401 challenge (ensure_applied(),
  also while the façade is off, so switching it off restores DOT's values);
- in django-q workers, before every task (connect_signals(): a ``pre_execute``
  receiver), so the façade's scheduled tasks and InvenTree's daily
  ``run_oauth_maintenance`` (DOT's ``clear_expired``, the only place DOT
  enforces the refresh-token lifetime) see the façade's lifetimes.

ensure_applied() re-applies only when the desired values changed. They are
memoised for DESIRED_VALUES_TTL_SECONDS per process and dropped whenever a
plugin setting is saved in this process, so admins can tune lifetimes without
a restart. While OAUTH_ENABLED is off nothing is injected, and values injected
earlier are restored to what InvenTree configured, so installing the plugin
does not change DOT's instance-wide lifetimes for other OAuth applications.

Also injected, beyond the overview's list: DCR_REGISTRATION_TOKEN_EXPIRE_SECONDS
(see desired_values()). Never injected: DCR_ENABLED (the façade does its own registration, DOT's
/o/register/ stays off) and OIDC_ISS_ENDPOINT (DOT's /o/ issuer stays as is).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from django.conf import settings as django_settings

logger = logging.getLogger("inventree")

_MISSING = object()
DESIRED_VALUES_TTL_SECONDS = 5.0

#: What OAUTH2_PROVIDER held for each key before the first injection.
_originals: dict[str, Any] = {}
#: The values injected by the last apply() in this process ({} = none).
_last_applied: dict[str, Any] | None = None
#: (time.monotonic(), desired_values()) memoised by ensure_applied().
_desired_cache: tuple[float, dict[str, Any]] | None = None
_lock = threading.Lock()


def desired_values() -> dict[str, Any]:
    """The DOT settings the façade needs, from the plugin settings ({} when disabled)."""
    from ..settings import plugin_setting
    from . import oauth_enabled

    if not oauth_enabled():
        return {}

    from .clients import trusted_client_hosts

    return {
        "CIMD_ENABLED": True,
        "CIMD_REGISTRATION_PERMISSION_CLASSES": (
            "oauth2_provider.cimd.HostAllowlistCIMDPermission",
        ),
        "CIMD_ALLOWED_HOSTS": trusted_client_hosts(),
        "ALLOW_LOCALHOST_LOOPBACK": plugin_setting("ALLOW_LOCALHOST_LOOPBACK"),
        "ACCESS_TOKEN_EXPIRE_SECONDS": plugin_setting("ACCESS_TOKEN_LIFETIME_SECONDS"),
        "REFRESH_TOKEN_EXPIRE_SECONDS": plugin_setting("REFRESH_TOKEN_LIFETIME_DAYS")
        * 86400,
        "AUTHORIZATION_CODE_EXPIRE_SECONDS": plugin_setting(
            "AUTH_CODE_LIFETIME_SECONDS"
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
            del cls._oauthlib_core
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
    global _last_applied, _desired_cache

    with _lock:
        _write(values)
        _last_applied = values
        _desired_cache = (time.monotonic(), values)

    if values:
        logger.info(
            "inventree-mcp: DOT settings injected: %s",
            ", ".join(f"{key}={value!r}" for key, value in sorted(values.items())),
        )
    else:
        logger.info("inventree-mcp: OAuth façade disabled, no DOT settings injected")
    return values


def invalidate(*args, **kwargs) -> None:
    """Forget the memoised desired values (signal receiver: a plugin setting was saved)."""
    global _desired_cache

    _desired_cache = None


def _memoised_desired_values() -> dict[str, Any]:
    global _desired_cache

    cached = _desired_cache
    now = time.monotonic()
    if cached is not None and now - cached[0] < DESIRED_VALUES_TTL_SECONDS:
        return cached[1]

    values = desired_values()
    _desired_cache = (now, values)
    return values


def ensure_applied() -> None:
    """Re-apply when the plugin settings changed since the last apply() in this process."""
    values = _memoised_desired_values()
    if values != _last_applied:
        _apply(values)


def _before_task(sender=None, **kwargs) -> None:
    """django-q ``pre_execute`` receiver: workers run tasks with the façade's DOT settings."""
    try:
        ensure_applied()
    except Exception:  # never break a task over this; logged for debugging
        logger.debug(
            "inventree-mcp: DOT settings not synced before task", exc_info=True
        )


def _before_request(sender=None, **kwargs) -> None:
    """Django ``request_started`` receiver: idle web workers pick up setting changes too.

    Every request of the process (not only façade requests) re-syncs, at most
    one settings read per DESIRED_VALUES_TTL_SECONDS thanks to the memo, so a
    worker that serves only /o/ or /api/ also follows an admin's change, and a
    deactivated plugin's values are restored (the receiver stays connected).
    """
    try:
        ensure_applied()
    except Exception:  # never break a request over this; logged for debugging
        logger.debug(
            "inventree-mcp: DOT settings not synced before request", exc_info=True
        )


def connect_signals() -> None:
    """Connect the receivers that keep every process in sync (idempotent).

    - ``post_save`` of PluginSetting / PluginConfig drops the memoised values,
      so a change made in this process applies at the next ensure_applied();
    - Django's ``request_started`` runs ensure_applied() before every request
      of a web process (memoised, see _before_request());
    - django-q's ``pre_execute`` runs ensure_applied() in the worker process
      before each task.
    """
    from django.core.signals import request_started
    from django.db.models.signals import post_save

    request_started.connect(
        _before_request, dispatch_uid="inventree_mcp.settings_inject.request_started"
    )

    for model in ("plugin.PluginSetting", "plugin.PluginConfig"):
        post_save.connect(
            invalidate,
            sender=model,
            dispatch_uid=f"inventree_mcp.settings_inject.invalidate.{model}",
        )

    try:
        from django_q.signals import pre_execute
    except ImportError:  # pragma: no cover - django-q is an InvenTree dependency
        return

    pre_execute.connect(
        _before_task, dispatch_uid="inventree_mcp.settings_inject.pre_execute"
    )
