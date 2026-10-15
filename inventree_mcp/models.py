"""Plugin models (WP2: MCPUserState, WP3: MCPRuleSet)."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models
from django.utils.translation import gettext_lazy as _
from users.ruleset import RULESET_CHOICES


class MCPUserState(models.Model):
    """Per-user MCP access state: the membership re-check stamp and the block.

    One row per user, created on first use (login stamp, re-check, block).
    ``access_revoked_at`` set means the user is blocked from MCP until the next
    SSO login (auth/membership.py, auth/signals.py). ``recheck_lease_until`` is
    the re-check lease (membership.recheck()): one re-check per user calls the
    provider at a time, without holding a row lock across the network.
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
    recheck_lease_until = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Re-check lease until"),
        help_text=_(
            "Set while a membership re-check calls the identity provider; "
            "other re-checks of the user are skipped until it ends or expires"
        ),
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


class MCPRuleSet(models.Model):
    """What members of a group may do through MCP: core's RuleSet shape, one row per role.

    The second permission set. MCP access is the user's interactive roles
    (core ``RuleSet``) intersected with the MCP roles of the user's groups,
    which become OAuth2 scopes ``r:<view|add|change|delete>:<role>`` on MCP
    tokens (auth/policy.py); core enforces roles and scopes on every proxied
    call. Rows are created for every group (migration 0002, the Group
    ``post_save`` receiver and ``auth.signals.ensure_rulesets``).

    Query from this model (``MCPRuleSet.objects.filter(group__in=...)``),
    never through ``Group.mcp_rule_sets`` in a filter: the reverse relation is
    missing in running processes (plugin apps load after core's relations).
    """

    RULE_OPTIONS = ("can_view", "can_add", "can_change", "can_delete")

    group = models.ForeignKey(
        Group,
        related_name="mcp_rule_sets",
        on_delete=models.CASCADE,
        verbose_name=_("Group"),
        help_text=_("Group"),
    )
    # No ``choices``: a role added by a core upgrade would otherwise make
    # Django report a (no-SQL) AlterField for this app. The rows are only
    # created from users.ruleset.RULESET_NAMES (ensure_rulesets, migration
    # 0002) and the API serves ``name`` read-only.
    name = models.CharField(
        max_length=50,
        verbose_name=_("Name"),
        help_text=_("Permission set"),
    )
    can_view = models.BooleanField(
        default=False, verbose_name=_("View"), help_text=_("MCP may view items")
    )
    can_add = models.BooleanField(
        default=False, verbose_name=_("Add"), help_text=_("MCP may add items")
    )
    can_change = models.BooleanField(
        default=False, verbose_name=_("Change"), help_text=_("MCP may edit items")
    )
    can_delete = models.BooleanField(
        default=False, verbose_name=_("Delete"), help_text=_("MCP may delete items")
    )

    class Meta:
        """Model options."""

        app_label = "inventree_mcp"
        unique_together = (("group", "name"),)
        verbose_name = _("MCP rule set")
        verbose_name_plural = _("MCP rule sets")

    def __str__(self) -> str:
        """Role name only (as core's RuleSet)."""
        return self.name

    @property
    def label(self) -> str:
        """The translated role label (as core's RuleSet.label)."""
        return dict(RULESET_CHOICES).get(self.name, self.name)

    def save(self, *args, **kwargs):
        """Imply view for any write flag and change for add/delete, as core's RuleSet.save() does."""
        if self.can_add or self.can_change or self.can_delete:
            self.can_view = True
        if self.can_add or self.can_delete:
            self.can_change = True
        super().save(*args, **kwargs)

    @classmethod
    def check_user_permission(cls, user, permission: str, obj=None) -> bool:
        """Only active staff (InvenTree calls this for plugin models without a RuleSet)."""
        return bool(user is not None and user.is_active and user.is_staff)
