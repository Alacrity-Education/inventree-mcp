"""The façade's token and revocation endpoints: DOT's views plus the MCP policy hooks.

DOT/oauthlib keep doing client authentication, code and PKCE verification,
token issuance and refresh-token rotation. MCPTokenView only decides, before
delegating, whether the grant may proceed:

- authorization_code: membership.token_grant_allowed(grant.user);
- refresh_token: membership.refresh_grant_allowed(rt.user) (WP2 runs the
  upstream membership re-check there), then the scope is re-narrowed with
  policy.narrow() (WP3: a reduced MCP ruleset takes effect at the next refresh).

A refusal is ``400 invalid_grant``, the error Claude treats as "sign in again".
authorization_code grants are rate limited per client IP
(TOKEN_RATE_LIMIT_PER_MINUTE, ratelimit.py: 429 with Retry-After); refresh
grants are not.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from types import SimpleNamespace

from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone
from oauth2_provider.models import (
    get_access_token_model,
    get_grant_model,
    get_refresh_token_model,
)
from oauth2_provider.settings import oauth2_settings
from oauth2_provider.views import RevokeTokenView, TokenView
from oauthlib.oauth2.rfc6749.utils import scope_to_list

from . import FacadeViewMixin, membership, policy, ratelimit
from .clients import mcp_applications
from .cors import TrustedOriginCORSMixin

logger = logging.getLogger("inventree")

SUPPORTED_GRANT_TYPES = ("authorization_code", "refresh_token")


def _oauth_error(error: str, description: str, status: int = 400) -> JsonResponse:
    response = JsonResponse(
        {"error": error, "error_description": description}, status=status
    )
    response["Cache-Control"] = "no-store"
    return response


def _scope_list(scope) -> list[str]:
    """oauthlib's scope_to_list() without the empty strings it yields for ''."""
    return [item for item in scope_to_list(scope) or [] if item]


def _is_mcp_application(application_id) -> bool:
    return mcp_applications().filter(pk=application_id).exists()


def _refused(reason: str, user_id, description: str) -> JsonResponse:
    logger.info("inventree-mcp: refresh refused (%s) for user id %s", reason, user_id)
    return _oauth_error("invalid_grant", description)


class MCPTokenView(FacadeViewMixin, TrustedOriginCORSMixin, TokenView):
    """Token endpoint of the façade (``plugin:inventree-mcp:oauth-token``).

    The policy hooks only apply to grants of façade applications
    (clients.mcp_applications()); anything else is DOT's business alone, so
    the hooks never reveal or change state of other OAuth applications.
    """

    def post(self, request, *args, **kwargs):
        """Run the grant-type specific policy hook, then delegate to DOT."""
        grant_type = request.POST.get("grant_type")

        if grant_type == "authorization_code":
            # Code guessing: per client IP, like registration. Refresh grants
            # are not limited (a refresh token is a long random secret).
            wait = ratelimit.retry_after(
                request, "token", "TOKEN_RATE_LIMIT_PER_MINUTE"
            )
            if wait:
                return ratelimit.too_many_requests(wait)
            refusal = self._check_code_grant(request)
        elif grant_type == "refresh_token":
            refusal = self._check_refresh_grant(request)
        else:
            refusal = _oauth_error(
                "unsupported_grant_type",
                f"Supported grant types: {', '.join(SUPPORTED_GRANT_TYPES)}.",
            )

        if refusal is not None:
            return refusal
        return super().post(request, *args, **kwargs)

    def _check_code_grant(self, request) -> JsonResponse | None:
        """Membership gate for the code exchange; the grant itself is left for DOT."""
        grants = get_grant_model().objects.filter(code=request.POST.get("code", ""))
        if client_id := request.POST.get("client_id"):
            grants = grants.filter(application__client_id=client_id)
        grant = grants.select_related("user").first()

        if grant is None or not _is_mcp_application(grant.application_id):
            return None  # DOT answers (invalid_grant for an unknown code)

        ok, code = membership.token_grant_allowed(grant.user)
        if not ok:
            logger.info(
                "inventree-mcp: code exchange refused (%s) for user id %s",
                code,
                grant.user_id,
            )
            return _oauth_error(
                code or "invalid_grant", "Access was revoked. Sign in again."
            )
        return None

    def _check_refresh_grant(self, request) -> JsonResponse | None:
        """Membership gate and scope re-narrowing for the refresh grant.

        Also guards a gap in DOT: within REFRESH_TOKEN_GRACE_PERIOD_SECONDS DOT
        accepts a revoked refresh token, and when it was revoked rather than
        rotated (no successor access token) it mints a brand-new, unscoped
        token pair. So a revoked row goes to DOT only when it was rotated (DOT
        then re-serves the pair it already issued, after the membership gate)
        or when the grace period is over (DOT refuses it and applies reuse
        protection to the family). Revoked rows are sorted out before the
        membership gate, so a dead token never triggers an upstream re-check.
        """
        raw_token = request.POST.get("refresh_token", "")
        if not raw_token:
            return None  # DOT answers invalid_request

        # Same lookup as OAuth2Validator.validate_refresh_token: rows may share
        # a checksum, prefer the unrevoked one.
        checksum = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        refresh_token = (
            get_refresh_token_model()
            .objects.filter(token_checksum=checksum)
            .select_related("access_token", "user")
            .order_by(F("revoked").desc(nulls_first=True))
            .first()
        )
        if refresh_token is None or not _is_mcp_application(
            refresh_token.application_id
        ):
            return None  # DOT answers invalid_grant, or it is not an MCP client

        user = refresh_token.user
        rotated_in_grace = False
        if refresh_token.revoked is not None:
            # A dead token never reaches the membership gate (and so never
            # triggers an upstream call): only a rotation inside the grace
            # window, which DOT answers by re-serving the pair it already
            # issued, goes on to the gate.
            if not self._revoked_within_grace(refresh_token):
                return None  # DOT refuses it and applies reuse protection
            if not self._has_successor(refresh_token):
                self._revoke_family(refresh_token)
                return _refused(
                    "revoked", user.pk, "The refresh token was revoked. Sign in again."
                )
            rotated_in_grace = True

        ok, code = membership.refresh_grant_allowed(user)
        if not ok:
            logger.info(
                "inventree-mcp: refresh refused (%s) for user id %s", code, user.pk
            )
            return _oauth_error(
                code or "invalid_grant", "Access was revoked. Sign in again."
            )

        if rotated_in_grace:
            return None  # DOT re-serves the existing pair

        requested = _scope_list(request.POST.get("scope")) or self._original_scopes(
            raw_token, refresh_token
        )
        if not requested:
            return _refused(
                "unknown_scope",
                user.pk,
                "The original scope is unknown. Sign in again.",
            )

        scopes = policy.narrow(requested, user)
        if not scopes:
            refresh_token.revoke()
            return _refused(
                "no_mcp_permission",
                user.pk,
                "No MCP permissions are left. Sign in again.",
            )

        # oauthlib accepts a subset of the original scopes on refresh.
        post = request.POST.copy()
        post["scope"] = " ".join(scopes)
        request.POST = post
        return None

    @staticmethod
    def _has_successor(refresh_token) -> bool:
        """Whether a revoked row was rotated: its successor access token still exists.

        Exactly DOT's re-serve condition in _save_bearer_token. A revocation
        (RefreshToken.revoke(), /oauth/revoke/) or the successor's own rotation
        deletes that access token.
        """
        return (
            get_access_token_model()
            .objects.filter(source_refresh_token=refresh_token)
            .exists()
        )

    @staticmethod
    def _revoke_family(refresh_token) -> None:
        """DOT's reuse protection, applied inside the grace window too.

        A revoked token without a successor is either plainly revoked (the rest
        of its family is revoked already, so this is a no-op) or a stale
        rotated token replayed after its successor was itself rotated, which a
        legitimate client cannot do: revoke the family, as DOT does past the
        window (OAuth2Validator.validate_refresh_token).
        """
        if not (
            oauth2_settings.REFRESH_TOKEN_REUSE_PROTECTION
            and refresh_token.token_family
        ):
            return
        family = get_refresh_token_model().objects.filter(
            token_family=refresh_token.token_family, revoked__isnull=True
        )
        for related in family:
            related.revoke()
        logger.info(
            "inventree-mcp: revoked token reused, token family of user id %s revoked",
            refresh_token.user_id,
        )

    @staticmethod
    def _revoked_within_grace(refresh_token) -> bool:
        """DOT's own test in validate_refresh_token: still accepted after revocation."""
        grace = timedelta(seconds=oauth2_settings.REFRESH_TOKEN_GRACE_PERIOD_SECONDS)
        return refresh_token.revoked > timezone.now() - grace

    def _original_scopes(self, raw_token: str, refresh_token) -> list[str]:
        """The scopes DOT would carry over, via the validator's own get_original_scopes()."""
        validator = self.get_validator_class()()
        scopes = validator.get_original_scopes(
            raw_token, SimpleNamespace(refresh_token_instance=refresh_token)
        )
        return _scope_list(scopes)


class MCPRevokeView(FacadeViewMixin, TrustedOriginCORSMixin, RevokeTokenView):
    """RFC 7009 revocation endpoint of the façade (``plugin:inventree-mcp:oauth-revoke``)."""
