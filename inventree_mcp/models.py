"""Plugin models (WP2: MCPUserState, WP3: MCPRuleSet)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class MCPUserState(models.Model):
    """Per-user MCP access state: the membership re-check stamp and the block.

    One row per user, created on first use (login stamp, re-check, block).
    ``access_revoked_at`` set means the user is blocked from MCP until the next
    SSO login (auth/membership.py, auth/signals.py).
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="mcp_state",
        on_delete=models.CASCADE,
        verbose_name=_("User"),
    )
    membership_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Membership checked at"),
        help_text=_("Last time group membership was confirmed (login or re-check)"),
    )
    access_revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Access revoked at"),
        help_text=_("Set while the user is blocked from MCP; cleared by an SSO login"),
    )
    revoked_reason = models.CharField(
        max_length=64,
        blank=True,
        verbose_name=_("Revoked reason"),
        help_text=_("Reason code of the block"),
    )
    last_recheck_error = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Last re-check error"),
        help_text=_("Short code of the last failed re-check (e.g. timeout, no_token)"),
    )
    last_login_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Last login at"),
        help_text=_("Last interactive login to InvenTree"),
    )

    class Meta:
        """Model options."""

        app_label = "inventree_mcp"
        verbose_name = _("MCP user state")
        verbose_name_plural = _("MCP user states")

    def __str__(self) -> str:
        """Identify the row by user id only (no names in logs)."""
        return f"MCP state of user {self.user_id}"

    @classmethod
    def check_user_permission(cls, user, permission: str, obj=None) -> bool:
        """Staff may do anything; a user may read their own row (with *obj* given).

        InvenTree's users.permissions.check_user_permission() calls this
        without *obj* for plugin models, so without a row only staff pass.
        """
        if user is None or not user.is_active:
            return False
        if user.is_staff:
            return True
        return permission == "view" and obj is not None and obj.user_id == user.pk
