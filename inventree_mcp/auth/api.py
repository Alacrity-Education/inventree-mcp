"""DRF API of the MCP permission set (WP3), used by the plugin's admin pages (WP4).

All under ``/plugin/inventree-mcp/api/`` (URL names in auth/urls.py):

- ``api/rulesets/`` (``?group=<pk>``), ``api/rulesets/<pk>/`` (GET, PATCH the
  four flags), ``api/rulesets/copy/``, ``api/rulesets/clear/``,
  ``api/rulesets/revoke/`` (POST ``{"group": pk}``): staff with the admin
  role's change permission;
- ``api/effective/<user_pk>/``: the caller's own document, anybody else's for
  the same staff admins;
- ``api/connections/`` (``?user=<pk>`` for staff admins),
  ``api/connections/<pk>/`` (DELETE revokes), ``api/status/`` (``?user=<pk>``
  for staff admins): the caller's own MCP connections and state.

Authentication is InvenTree's default list (session for the web UI, API
token, Basic, OAuth2). Permissions reuse core's classes: DRF's
IsAuthenticated, core's role check (users.permissions.check_user_role) and
core's OAuth2 scope check (oauth2_bridge's fixed
InvenTreeTokenMatchesOASRequirements). OAuth tokens are refused on these
views by InvenTree's AuthRequiredMiddleware (it knows sessions and API tokens
only, and runs before DRF); the views additionally require "a:staff" on an
OAuth token for the staff endpoints, which MCP tokens never carry. Serializers are explicit (core's RuleSetSerializer for the
rulesets) for schema generation; InvenTree's schema export only covers /api/
and disables plugins anyway.

Revocation on a reduction happens in auth/signals.py (PATCH goes through
MCPRuleSet.save()); copy/clear update in bulk and revoke once.
"""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from InvenTree.mixins import ListAPI, RetrieveUpdateAPI
from InvenTree.permissions import map_scope
from oauth2_provider.models import get_refresh_token_model
from oauth2_provider.settings import oauth2_settings
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from users.oauth2_scopes import DEFAULT_STAFF
from users.permissions import check_user_role
from users.ruleset import RULESET_CHOICES
from users.serializers import RuleSetSerializer, get_user_roles

from ..models import MCPRuleSet
from ..oauth2_bridge import _FixedTokenMatchesOASRequirements
from ..settings import get_plugin_setting
from . import membership, oauth_enabled, policy
from .clients import is_trusted_client, mcp_applications
from .discovery import resource
from .signals import ensure_rulesets, ensure_rulesets_for, revoke_group_members

FLAGS = MCPRuleSet.RULE_OPTIONS
PERMISSIONS = tuple(perm for _flag, perm in policy.FLAG_PERMISSIONS)

#: OAuth2 scopes per method: "a:staff" for the admin endpoints, "g:read" for the caller's own.
STAFF_SCOPES = map_scope(only_read=True, read_name=DEFAULT_STAFF)
USER_SCOPES = map_scope(only_read=True)


# -- permissions ----------------------------------------------------------------


class IsStaffAdmin(permissions.BasePermission):
    """Active staff holding the admin role's change permission (core's role check).

    The same condition core's StaffRolePermissionOrReadOnly applies to writes
    of users.RuleSet, here for reads too: MCP permissions are an admin matter.
    """

    message = "Staff with the admin role's change permission required"

    def has_permission(self, request, view):
        """Staff and core's check_user_role(user, "admin", "change")."""
        user = getattr(request, "user", None)
        return bool(
            user is not None
            and user.is_authenticated
            and user.is_active
            and user.is_staff
            and check_user_role(user, "admin", "change")
        )


USER_PERMISSIONS: list[type] = [
    permissions.IsAuthenticated,
    _FixedTokenMatchesOASRequirements,
]
STAFF_PERMISSIONS: list[type] = [*USER_PERMISSIONS, IsStaffAdmin]


def target_user(request, view, user_pk=None):
    """The user a self-service endpoint is about: the caller, or (staff admins) *user_pk*.

    *user_pk* None reads ``?user=<pk>``. Another user than the caller needs
    IsStaffAdmin; an unknown pk is a 404 (only after the permission check).
    """
    if user_pk is None:
        raw = request.query_params.get("user")
        if raw in (None, ""):
            return request.user
        try:
            user_pk = int(raw)
        except (TypeError, ValueError):
            raise ValidationError({"user": "A user id is required."}) from None
    if user_pk == request.user.pk:
        return request.user
    if not IsStaffAdmin().has_permission(request, view):
        raise PermissionDenied(IsStaffAdmin.message)
    return get_object_or_404(get_user_model(), pk=user_pk)


# -- serializers ------------------------------------------------------------------


class MCPRuleSetSerializer(RuleSetSerializer):
    """Core's RuleSetSerializer on MCPRuleSet: pk, name, label, group and the four flags."""

    class Meta(RuleSetSerializer.Meta):
        """Same fields and read-only fields as core's serializer."""

        model = MCPRuleSet


class GroupActionSerializer(serializers.Serializer):
    """Request body of copy / clear / revoke: ``{"group": <pk>}``."""

    group = serializers.PrimaryKeyRelatedField(queryset=Group.objects.all())


class RevokeResultSerializer(serializers.Serializer):
    """Response of api/rulesets/revoke/."""

    users = serializers.IntegerField(help_text="Members whose MCP tokens were revoked")
    tokens = serializers.IntegerField(
        help_text="Tokens revoked (queued: live tokens at queueing time)"
    )
    queued = serializers.BooleanField(
        help_text="Revocation runs as a background task (large group)"
    )


class RoleFlagsSerializer(serializers.Serializer):
    """One role of a role map: its label and the four permissions."""

    label = serializers.CharField()
    view = serializers.BooleanField()
    add = serializers.BooleanField()
    change = serializers.BooleanField()
    delete = serializers.BooleanField()


class EffectiveSerializer(serializers.Serializer):
    """Response of api/effective/<user_pk>/."""

    user = serializers.IntegerField()
    username = serializers.CharField()
    superuser = serializers.BooleanField()
    roles = serializers.DictField(
        child=RoleFlagsSerializer(), help_text="Interactive roles (core RuleSet)"
    )
    mcp_roles = serializers.DictField(
        child=RoleFlagsSerializer(), help_text="Union of the groups' MCP roles"
    )
    effective = serializers.DictField(
        child=RoleFlagsSerializer(),
        help_text=(
            "Per role: interactive role AND MCP scope. Tables shared by several "
            "roles can be reachable without both on the same role (see note)"
        ),
    )
    scopes = serializers.ListField(child=serializers.CharField())
    may_connect = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)
    note = serializers.CharField(help_text="How to read 'effective'")


class ConnectionSerializer(serializers.Serializer):
    """One MCP connection: a live (unrevoked) façade refresh token."""

    pk = serializers.IntegerField(source="id")
    client_name = serializers.CharField(source="application.name")
    client_host = serializers.SerializerMethodField()
    client_trusted = serializers.SerializerMethodField()
    created = serializers.DateTimeField()
    expires = serializers.SerializerMethodField(
        help_text="When the refresh token expires unless used"
    )
    access_expires = serializers.SerializerMethodField()
    scopes = serializers.SerializerMethodField()

    def get_client_host(self, token) -> str:
        """Host of a CIMD client_id URL, else of the first redirect URI."""
        application = token.application
        url = application.client_id
        if not url.startswith("https://"):
            url = (application.redirect_uris or "").split(" ")[0]
        return urlparse(url).hostname or ""

    def get_client_trusted(self, token) -> bool:
        """clients.is_trusted_client()."""
        return is_trusted_client(token.application)

    def get_expires(self, token) -> str | None:
        """created + DOT's REFRESH_TOKEN_EXPIRE_SECONDS (what DOT's cleanup uses), if set."""
        seconds = oauth2_settings.REFRESH_TOKEN_EXPIRE_SECONDS
        if not seconds:
            return None
        delta = (
            seconds if isinstance(seconds, timedelta) else timedelta(seconds=seconds)
        )
        return serializers.DateTimeField().to_representation(token.created + delta)

    def get_access_expires(self, token) -> str | None:
        """Expiry of the current access token of this connection."""
        access = token.access_token
        if access is None:
            return None
        return serializers.DateTimeField().to_representation(access.expires)

    def get_scopes(self, token) -> list[str]:
        """Scopes of the current access token."""
        access = token.access_token
        return access.scope.split() if access is not None else []


class MembershipStateSerializer(serializers.Serializer):
    """The membership part of api/status/."""

    checked_at = serializers.DateTimeField(allow_null=True)
    blocked = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)


class StatusSerializer(serializers.Serializer):
    """Response of api/status/."""

    oauth_enabled = serializers.BooleanField()
    mcp_url = serializers.CharField()
    membership = MembershipStateSerializer()
    scopes = serializers.ListField(child=serializers.CharField())
    may_connect = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)


# -- rulesets -----------------------------------------------------------------------


def _sorted_rows(queryset) -> list[MCPRuleSet]:
    return sorted(queryset, key=lambda row: (str(row.label), row.group_id))


class MCPRuleSetList(ListAPI):
    """GET api/rulesets/?group=<pk>: the MCP rulesets (rows ensured), sorted by label."""

    queryset = MCPRuleSet.objects.all()
    serializer_class = MCPRuleSetSerializer
    permission_classes: ClassVar[list] = STAFF_PERMISSIONS
    required_alternate_scopes = STAFF_SCOPES
    pagination_class = None  # a plain list (11 rows per group)

    def _group(self):
        raw = self.request.query_params.get("group")
        if raw in (None, ""):
            return None
        try:
            return get_object_or_404(Group, pk=int(raw))
        except (TypeError, ValueError):
            raise ValidationError({"group": "A group id is required."}) from None

    @extend_schema(
        parameters=[
            OpenApiParameter("group", int, description="Only this group's rulesets")
        ]
    )
    def get(self, request, *args, **kwargs):
        """List, creating missing rows first (a role added by a core upgrade appears)."""
        group = self._group()
        # One SELECT and at most one bulk INSERT, for one group or all of them.
        ensure_rulesets_for([group] if group is not None else Group.objects.all())
        queryset = self.get_queryset()
        if group is not None:
            queryset = queryset.filter(group=group)
        return Response(self.get_serializer(_sorted_rows(queryset), many=True).data)


class MCPRuleSetDetail(RetrieveUpdateAPI):
    """GET / PATCH api/rulesets/<pk>/: the four flags (name and group are read-only)."""

    queryset = MCPRuleSet.objects.all()
    serializer_class = MCPRuleSetSerializer
    permission_classes: ClassVar[list] = STAFF_PERMISSIONS
    required_alternate_scopes = STAFF_SCOPES


class _GroupAction(GenericAPIView):
    """POST ``{"group": <pk>}`` for a staff admin."""

    serializer_class = GroupActionSerializer
    permission_classes: ClassVar[list] = STAFF_PERMISSIONS
    required_alternate_scopes = STAFF_SCOPES
    pagination_class = None

    def validated_group(self, request) -> Group:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data["group"]

    def replace_flags(self, group: Group, new_flags) -> Response:
        """Set every row of *group* to ``new_flags(row)``; one revocation when anything shrank."""
        ensure_rulesets(group)
        rows = list(MCPRuleSet.objects.filter(group=group))
        reduced = False
        for row in rows:
            flags = new_flags(row)
            reduced |= any(getattr(row, f) and not flags[f] for f in FLAGS)
            for flag in FLAGS:
                setattr(row, flag, flags[flag])
        # Bulk update: no per-row signals, so the members are revoked once below.
        MCPRuleSet.objects.bulk_update(rows, list(FLAGS))
        if reduced:
            revoke_group_members(group, "MCP permissions of a group replaced")
        return Response(MCPRuleSetSerializer(_sorted_rows(rows), many=True).data)


class MCPRuleSetCopy(_GroupAction):
    """POST api/rulesets/copy/: the group's MCP roles become a copy of its interactive roles."""

    @extend_schema(responses=MCPRuleSetSerializer(many=True))
    def post(self, request, *args, **kwargs):
        """Copy core's RuleSet flags of the group (missing core rows count as all false)."""
        from users.models import RuleSet

        group = self.validated_group(request)
        core = {row.name: row for row in RuleSet.objects.filter(group=group)}
        return self.replace_flags(
            group,
            lambda row: {f: bool(getattr(core.get(row.name), f, False)) for f in FLAGS},
        )


class MCPRuleSetClear(_GroupAction):
    """POST api/rulesets/clear/: clear every MCP flag of the group (revokes its members)."""

    @extend_schema(responses=MCPRuleSetSerializer(many=True))
    def post(self, request, *args, **kwargs):
        """All flags false."""
        group = self.validated_group(request)
        return self.replace_flags(group, lambda _row: dict.fromkeys(FLAGS, False))


class MCPRuleSetRevoke(_GroupAction):
    """POST api/rulesets/revoke/: revoke the MCP tokens of every member of the group."""

    @extend_schema(responses=RevokeResultSerializer)
    def post(self, request, *args, **kwargs):
        """{"users": n, "tokens": m, "queued": bool}; background task above 20 members."""
        from .signals import REVOKE_SYNC_LIMIT

        group = self.validated_group(request)
        users, tokens = revoke_group_members(
            group, "MCP tokens of a group revoked by staff"
        )
        return Response(
            RevokeResultSerializer({
                "users": users,
                "tokens": tokens,
                "queued": users > REVOKE_SYNC_LIMIT,
            }).data
        )


# -- effective permissions -------------------------------------------------------------

#: The effective document's note (F4 of the WP3 review): "effective" is per role,
#: access is per table.
EFFECTIVE_NOTE = (
    "'effective' marks a role when the user holds it interactively and as an MCP "
    "role. Access is checked per table: for a table shared by several roles (for "
    "example parts: Parts and Build Orders; stock items: Stock Items and Build "
    "Orders; companies: the three order roles), any interactive role and any MCP "
    "role that cover the table suffice, even when they differ."
)


def _role_map(flags_of) -> dict[str, dict]:
    """{role: {label, view, add, change, delete}} for every core role, in core's order."""
    return {
        str(name): {"label": str(label)}
        | {perm: bool(flags_of(str(name), perm)) for perm in PERMISSIONS}
        for name, label in RULESET_CHOICES
    }


def effective_document(user) -> dict:
    """Interactive roles, MCP roles, their intersection, the scopes and may_connect."""
    interactive = {
        str(role): set(perms or []) for role, perms in get_user_roles(user).items()
    }
    superuser_full = user.is_superuser and get_plugin_setting(
        "MCP_SUPERUSER_FULL_ACCESS", False
    )
    mcp = policy.mcp_roles(user)
    scopes = policy.narrow(None, user)
    granted = set(scopes)
    ok, reason = policy.may_connect(user)
    return {
        "user": user.pk,
        "username": user.get_username(),
        "superuser": bool(user.is_superuser),
        "roles": _role_map(lambda role, perm: perm in interactive.get(role, ())),
        "mcp_roles": _role_map(
            lambda role, perm: superuser_full or mcp.get(role, {}).get(perm, False)
        ),
        "effective": _role_map(
            lambda role, perm: (
                perm in interactive.get(role, ()) and f"r:{perm}:{role}" in granted
            )
        ),
        "scopes": scopes,
        "may_connect": ok,
        "reason": reason,
        "note": EFFECTIVE_NOTE,
    }


class EffectiveView(GenericAPIView):
    """GET api/effective/<user_pk>/: what MCP can do for that user (self, or staff admins)."""

    serializer_class = EffectiveSerializer
    permission_classes: ClassVar[list] = USER_PERMISSIONS
    required_alternate_scopes = USER_SCOPES

    def get(self, request, user_pk: int, *args, **kwargs):
        """The effective document."""
        user = target_user(request, self, user_pk)
        return Response(EffectiveSerializer(effective_document(user)).data)


# -- connections and status ----------------------------------------------------------------


def live_connections(user):
    """The user's unrevoked façade refresh tokens, newest first."""
    return (
        get_refresh_token_model()
        .objects.filter(
            user=user, revoked__isnull=True, application__in=mcp_applications()
        )
        .select_related("application", "access_token")
        .order_by("-created")
    )


class ConnectionList(GenericAPIView):
    """GET api/connections/ (``?user=<pk>`` for staff admins): live MCP connections."""

    serializer_class = ConnectionSerializer
    permission_classes: ClassVar[list] = USER_PERMISSIONS
    required_alternate_scopes = USER_SCOPES
    pagination_class = None

    @extend_schema(
        parameters=[OpenApiParameter("user", int, description="Staff: another user")],
        responses=ConnectionSerializer(many=True),
    )
    def get(self, request, *args, **kwargs):
        """The list (not paginated)."""
        user = target_user(request, self)
        return Response(ConnectionSerializer(live_connections(user), many=True).data)


class ConnectionDetail(GenericAPIView):
    """DELETE api/connections/<pk>/: revoke a connection (DOT's RefreshToken.revoke())."""

    serializer_class = ConnectionSerializer
    permission_classes: ClassVar[list] = USER_PERMISSIONS
    required_alternate_scopes = USER_SCOPES

    def _token(self, request, pk: int):
        """The live façade refresh token *pk* of the caller (any user's for staff admins), or None."""
        token = (
            get_refresh_token_model()
            .objects.select_related("application", "access_token")
            .filter(pk=pk, revoked__isnull=True, application__in=mcp_applications())
            .first()
        )
        if token is None:
            return None
        if token.user_id != request.user.pk and not IsStaffAdmin().has_permission(
            request, self
        ):
            return None  # somebody else's connection: as if it did not exist
        return token

    def get(self, request, pk: int, *args, **kwargs):
        """One connection."""
        token = self._token(request, pk)
        if token is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(ConnectionSerializer(token).data)

    @extend_schema(responses={204: None})
    def delete(self, request, pk: int, *args, **kwargs):
        """Revoke the refresh token and its access token."""
        token = self._token(request, pk)
        if token is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        token.revoke()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StatusView(GenericAPIView):
    """GET api/status/ (``?user=<pk>`` for staff admins): OAuth on, MCP URL, membership, scopes."""

    serializer_class = StatusSerializer
    permission_classes: ClassVar[list] = USER_PERMISSIONS
    required_alternate_scopes = USER_SCOPES

    @extend_schema(
        parameters=[OpenApiParameter("user", int, description="Staff: another user")]
    )
    def get(self, request, *args, **kwargs):
        """The status document."""
        user = target_user(request, self)
        state = membership._state(user) if membership.models_available() else None
        ok, reason = policy.may_connect(user)
        return Response(
            StatusSerializer({
                "oauth_enabled": oauth_enabled(),
                "mcp_url": resource(request),
                "membership": {
                    "checked_at": state.membership_checked_at if state else None,
                    "blocked": membership.is_blocked(user),
                    "reason": state.revoked_reason if state else "",
                },
                "scopes": policy.narrow(None, user),
                "may_connect": ok,
                "reason": reason,
            }).data
        )
