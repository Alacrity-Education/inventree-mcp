"""Membership re-checks against the SSO provider, and blocking.

WP1 ships stubs for the policy functions (nobody is ever blocked, re-checks
are disabled) and the real revoke_all_mcp_tokens(). WP2 replaces the stub
bodies; the call sites (authorize.py via policy.may_connect, token.py) stay.
"""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger("inventree")


class Outcome(str, Enum):
    """Result of a membership re-check."""

    MEMBER = "member"
    NOT_MEMBER = "not_member"
    UNREACHABLE = "unreachable"
    NO_UPSTREAM_TOKEN = "no_upstream_token"
    NOT_DUE = "not_due"
    DISABLED = "disabled"


def is_blocked(user) -> bool:
    """Whether *user*'s MCP access has been revoked. Stub: never."""
    return False


def token_grant_allowed(user) -> tuple[bool, str]:
    """Gate for the authorization_code grant: (allowed, oauth error code)."""
    if is_blocked(user):
        return False, "invalid_grant"
    return True, ""


def refresh_grant_allowed(user) -> tuple[bool, str]:
    """Gate for the refresh_token grant; WP2 runs recheck() here when due. Stub: allow."""
    return True, ""


def recheck(user, *, force: bool = False) -> Outcome:
    """Re-check *user*'s group membership upstream. Stub: disabled."""
    return Outcome.DISABLED


def block_user(user, reason: str) -> None:
    """Block *user* and revoke their MCP tokens (WP2)."""
    raise NotImplementedError("block_user() is implemented in WP2")


def unblock_user(user) -> None:
    """Lift a block. Stub: nothing is ever blocked, so nothing to do."""


def revoke_all_mcp_tokens(user) -> int:
    """Revoke every live token *user* holds for an MCP (DCR/CIMD) application.

    Refresh tokens go through DOT's RefreshToken.revoke(), which also removes
    the paired access token. Access tokens without a live refresh token are
    revoked with AccessToken.revoke(). Returns the number of tokens revoked.
    """
    from oauth2_provider.models import get_access_token_model, get_refresh_token_model

    from .clients import mcp_applications

    applications = mcp_applications()
    count = 0

    for refresh_token in get_refresh_token_model().objects.filter(
        user=user, revoked__isnull=True, application__in=applications
    ):
        refresh_token.revoke()
        count += 1

    for access_token in get_access_token_model().objects.filter(
        user=user, application__in=applications
    ):
        access_token.revoke()
        count += 1

    logger.info("Revoked %s MCP tokens for user id %s", count, user.pk)
    return count
