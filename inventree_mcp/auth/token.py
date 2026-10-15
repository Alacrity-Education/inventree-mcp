"""The façade's token and revocation endpoints: DOT's views plus the MCP policy hooks.

DOT/oauthlib keep doing client authentication, code and PKCE verification,
token issuance and refresh-token rotation. MCPTokenView only decides, before
delegating, whether the grant may proceed:

- authorization_code: membership.token_grant_allowed(grant.user);
- refresh_token: membership.refresh_grant_allowed(rt.user) (WP2 runs the
  upstream membership re-check there), then the scope is re-narrowed with
  policy.narrow() (WP3: a reduced MCP ruleset takes effect at the next refresh).

A refusal is ``400 invalid_grant``, the error Claude treats as "sign in again".
"""

from __future__ import annotations

import hashlib
import logging
from types import SimpleNamespace

from django.db.models import F
from django.http import JsonResponse
from oauth2_provider.models import get_grant_model, get_refresh_token_model
from oauth2_provider.views import RevokeTokenView, TokenView
from oauthlib.oauth2.rfc6749.utils import scope_to_list

from . import FacadeViewMixin, membership, policy
from .cors import TrustedOriginCORSMixin

logger = logging.getLogger("inventree")

SUPPORTED_GRANT_TYPES = ("authorization_code", "refresh_token")


def _oauth_error(error: str, description: str, status: int = 400) -> JsonResponse:
    response = JsonResponse(
        {"error": error, "error_description": description}, status=status
    )
    response["Cache-Control"] = "no-store"
    return response


class MCPTokenView(FacadeViewMixin, TrustedOriginCORSMixin, TokenView):
    """Token endpoint of the façade (``plugin:inventree-mcp:oauth-token``)."""

    def post(self, request, *args, **kwargs):
        """Run the grant-type specific policy hook, then delegate to DOT."""
        grant_type = request.POST.get("grant_type")

        if grant_type == "authorization_code":
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

        if grant is None:
            return None  # DOT answers invalid_grant

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
        """Membership gate and scope re-narrowing for the refresh grant."""
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
        if refresh_token is None or refresh_token.revoked is not None:
            # Unknown, or already rotated: DOT applies the grace period and
            # reuse protection and answers invalid_grant.
            return None

        user = refresh_token.user
        ok, code = membership.refresh_grant_allowed(user)
        if not ok:
            logger.info(
                "inventree-mcp: refresh refused (%s) for user id %s", code, user.pk
            )
            return _oauth_error(
                code or "invalid_grant", "Access was revoked. Sign in again."
            )

        requested = scope_to_list(request.POST.get("scope")) or self._original_scopes(
            raw_token, refresh_token
        )
        if not requested:
            return None  # nothing to narrow; DOT reuses the original scopes

        scopes = policy.narrow(requested, user)
        if not scopes:
            refresh_token.revoke()
            logger.info(
                "inventree-mcp: refresh refused (no_mcp_permission) for user id %s",
                user.pk,
            )
            return _oauth_error(
                "invalid_grant", "No MCP permissions are left. Sign in again."
            )

        # oauthlib accepts a subset of the original scopes on refresh.
        post = request.POST.copy()
        post["scope"] = " ".join(scopes)
        request.POST = post
        return None

    def _original_scopes(self, raw_token: str, refresh_token) -> list[str]:
        """The scopes DOT would carry over, via the validator's own get_original_scopes()."""
        validator = self.get_validator_class()()
        scopes = validator.get_original_scopes(
            raw_token, SimpleNamespace(refresh_token_instance=refresh_token)
        )
        return scope_to_list(scopes) or []


class MCPRevokeView(FacadeViewMixin, TrustedOriginCORSMixin, RevokeTokenView):
    """RFC 7009 revocation endpoint of the façade (``plugin:inventree-mcp:oauth-revoke``)."""
