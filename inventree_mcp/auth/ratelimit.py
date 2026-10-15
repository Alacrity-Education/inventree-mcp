"""Fixed-window rate limits per client IP for the façade's anonymous endpoints.

One implementation for the registration endpoint (REGISTER_RATE_LIMIT_PER_MINUTE,
clients.py) and the token endpoint's authorization_code grant
(TOKEN_RATE_LIMIT_PER_MINUTE, token.py). Counters live in Django's cache, one
per endpoint and client IP, in one-minute windows aligned to the clock.
``REMOTE_ADDR`` already reflects InvenTree's proxy settings.
"""

from __future__ import annotations

import math
import time

from django.core.cache import cache
from django.http import HttpRequest, JsonResponse

from ..settings import plugin_setting

WINDOW_SECONDS = 60


def cache_key(scope: str, request: HttpRequest) -> str:
    """The counter of *scope* ("dcr", "token") for the request's client IP."""
    return f"inventree_mcp:{scope}:{request.META.get('REMOTE_ADDR', 'unknown')}"


def retry_after(request: HttpRequest, scope: str, setting: str) -> int:
    """Count this request; the seconds to wait when over the limit, else 0.

    *setting* names the plugin setting holding the limit per minute; 0
    disables the limit (nothing is counted).
    """
    limit = plugin_setting(setting)
    if limit <= 0:
        return 0

    key = cache_key(scope, request)
    remaining = math.ceil(WINDOW_SECONDS - (time.time() % WINDOW_SECONDS))

    cache.add(key, 0, timeout=remaining)
    try:
        count = cache.incr(key)
    except ValueError:  # expired between add() and incr()
        cache.set(key, 1, timeout=remaining)
        count = 1

    return remaining if count > limit else 0


def too_many_requests(retry_after_seconds: int) -> JsonResponse:
    """The 429 answer, with Retry-After."""
    response = JsonResponse({"error": "too_many_requests"}, status=429)
    response["Retry-After"] = str(retry_after_seconds)
    response["Cache-Control"] = "no-store"
    return response
