"""The façade's authorization endpoint: DOT's AuthorizationView plus the MCP policy.

What this adds on top of DOT (which keeps doing client and redirect URI
validation, PKCE, code issuance and the consent form round trip):

- anonymous browsers go to the login bridge (``login_url``), which sends them
  through InvenTree's SSO and back to the same authorize URL;
- policy.may_connect() refuses blocked / inactive users with an error page;
- scopes are narrowed to what the user may hold through MCP (policy.narrow),
  both when the request is validated and when the consent form comes back;
- PKCE with S256 is required, regardless of the instance-wide PKCE_REQUIRED;
- consent is skipped for trusted CIMD clients (claude.ai, ...) unless the
  redirect URI is a loopback one, which any local program could listen on;
  DOT's ``approval_prompt=auto`` shortcut (no consent when the user holds a
  live token for the client) is disabled, it would skip consent for any
  client, including loopback and self-registered ones;
- RFC 8707 ``resource`` values must name this server's MCP endpoint;
- fatal client errors (unknown client, unregistered redirect URI) render the
  plugin's error page and never redirect.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from django.shortcuts import render
from django.urls import reverse_lazy
from oauth2_provider.cimd import is_cimd_client_id
from oauth2_provider.exceptions import FatalClientError, OAuthToolkitError
from oauth2_provider.models import get_application_model
from oauth2_provider.oauth2_validators import is_valid_resource_uri
from oauth2_provider.settings import oauth2_settings
from oauth2_provider.views import AuthorizationView
from oauthlib.oauth2.rfc6749.errors import CustomOAuth2Error, InvalidRequestError

from ..settings import get_plugin_setting
from . import FacadeViewMixin, policy
from .clients import is_loopback_redirect, is_trusted_client
from .discovery import resource as mcp_resource

logger = logging.getLogger("inventree")

ERROR_TEMPLATE = "inventree_mcp/error.html"
PKCE_METHOD = "S256"

#: reason code -> (title, sentence) shown on the error page.
REASONS = {
    "anonymous": (
        "Sign-in required",
        "Sign in to InvenTree, then connect your MCP client again.",
    ),
    "inactive": (
        "Account inactive",
        "Your InvenTree account is inactive. Ask an administrator.",
    ),
    "blocked": (
        "Access revoked",
        "Your access has been revoked. Sign in to InvenTree again.",
    ),
    "no_mcp_permission": (
        "No MCP access",
        "None of your groups grant MCP access. Ask an administrator.",
    ),
    "client": (
        "Unknown client",
        "This client is not registered or not trusted.",
    ),
    "sso_unavailable": (
        "Single sign-on unavailable",
        "Single sign-on is not available on this InvenTree server. Ask an administrator.",
    ),
    "invalid_request": (
        "Invalid request",
        "This link is not valid. Start the connection from your MCP client again.",
    ),
}


def render_error(request, reason: str, status: int, detail: str | None = None):
    """Render the plugin's error page for *reason* (one of REASONS)."""
    title, sentence = REASONS.get(reason, REASONS["invalid_request"])
    return render(
        request,
        ERROR_TEMPLATE,
        {"title": title, "sentence": sentence, "reason": reason, "detail": detail},
        status=status,
    )


def human_duration(seconds: int | None) -> str:
    """'1 hour', '30 days', ... for the consent page; None means no expiry."""
    if not seconds:
        return "until revoked"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds % size == 0:
            count = seconds // size
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return f"{seconds} seconds"


def _allowed_resources(request) -> set[str]:
    """RFC 8707 resource indicators this server issues tokens for: the MCP endpoint, with or without slash."""
    uri = mcp_resource(request)
    return {uri, uri.rstrip("/")}


def _uses_s256(data: dict) -> bool:
    return bool(data.get("code_challenge")) and (
        data.get("code_challenge_method") == PKCE_METHOD
    )


class MCPAuthorizationView(FacadeViewMixin, AuthorizationView):
    """Authorization endpoint of the façade (``plugin:inventree-mcp:oauth-authorize``)."""

    template_name = "inventree_mcp/consent.html"
    login_url = reverse_lazy("plugin:inventree-mcp:oauth-login")

    def validate_authorization_request(self, request):
        """DOT's validation, with the requested scopes narrowed by policy.narrow().

        Narrowing here (rather than only in get()) means DOT's own get() also
        renders the consent form, and its hidden scope field, with the
        narrowed scopes.
        """
        scopes, credentials = super().validate_authorization_request(request)
        return policy.narrow(scopes, request.user), credentials

    def get(self, request, *args, **kwargs):
        """Apply the MCP policy, then either skip consent or let DOT render it."""
        ok, reason = policy.may_connect(request.user)
        if not ok:
            return self.render_denied(reason)

        try:
            scopes, credentials = self.validate_authorization_request(request)
        except OAuthToolkitError as error:
            return self.error_response(error, application=None)

        application = get_application_model().objects.get(
            client_id=credentials["client_id"]
        )

        if not _uses_s256(credentials):
            return self.error_response(self._pkce_error(credentials), application)

        if not scopes:
            return self.render_denied("no_mcp_permission")

        foreign = self._foreign_resources(request.GET.getlist("resource"))
        if foreign:
            return self.error_response(
                self._invalid_target_error(credentials, foreign[0]), application
            )

        # DOT's approval_prompt=auto (from the query or REQUEST_APPROVAL_PROMPT)
        # skips consent for any client the user already holds a live token
        # for; skipping consent is _may_skip_consent()'s decision alone.
        query = request.GET.copy()
        query["approval_prompt"] = "force"
        request.GET = query

        if self._may_skip_consent(request, application, credentials["redirect_uri"]):
            # RFC 8707 resource indicators, as AuthorizationView.get() handles them
            # (accepted duplication). Invalid values fall through to DOT's get(),
            # which answers them with its invalid_target error.
            resources = request.GET.getlist("resource")
            if all(is_valid_resource_uri(uri) for uri in resources):
                if resources:
                    credentials["resource"] = resources
                try:
                    uri, _headers, _body, _status = self.create_authorization_response(
                        request=request,
                        scopes=" ".join(scopes),
                        credentials=credentials,
                        allow=True,
                    )
                except OAuthToolkitError as error:
                    return self.error_response(error, application)
                logger.info(
                    "inventree-mcp: consent skipped for trusted client %s, user id %s",
                    application.pk,
                    request.user.pk,
                )
                return self.redirect(uri, application)

        return super().get(request, *args, **kwargs)

    def _may_skip_consent(self, request, application, redirect_uri: str) -> bool:
        return (
            get_plugin_setting("AUTO_APPROVE_TRUSTED_CLIENTS", True)
            and is_trusted_client(application)
            and not is_loopback_redirect(redirect_uri)
            # prompt=login/create/... need DOT's handling in get()
            and not request.GET.get("prompt")
        )

    def _foreign_resources(self, resources) -> list[str]:
        allowed = _allowed_resources(self.request)
        return [uri for uri in resources if uri not in allowed]

    @staticmethod
    def _invalid_target_error(credentials: dict, uri: str) -> OAuthToolkitError:
        return OAuthToolkitError(
            error=CustomOAuth2Error(
                error="invalid_target",
                description=f"The resource '{uri}' is not served by this authorization server.",
                state=credentials.get("state"),
            ),
            redirect_uri=credentials["redirect_uri"],
        )

    @staticmethod
    def _pkce_error(credentials: dict) -> OAuthToolkitError:
        return OAuthToolkitError(
            error=InvalidRequestError(
                description="PKCE with code_challenge_method=S256 is required.",
                state=credentials.get("state"),
            ),
            redirect_uri=credentials["redirect_uri"],
        )

    def form_valid(self, form):
        """Re-apply the policy to the posted consent form before DOT issues the code.

        The hidden fields come back from the browser and can be tampered with.
        A denial goes straight to DOT, which redirects with access_denied.
        """
        if form.cleaned_data.get("allow"):
            ok, reason = policy.may_connect(self.request.user)
            if not ok:
                return self.render_denied(reason)

            if not _uses_s256(form.cleaned_data):
                return render_error(self.request, "invalid_request", 400)

            # The hidden resource field can be tampered with; the redirect URI
            # posted alongside is not validated yet, so no error redirect.
            if self._foreign_resources(form.cleaned_data.get("resource", "").split()):
                return render_error(self.request, "invalid_request", 400)

            scopes = policy.narrow(
                form.cleaned_data.get("scope", "").split(), self.request.user
            )
            if not scopes:
                return self.render_denied("no_mcp_permission")
            form.cleaned_data["scope"] = " ".join(scopes)

        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        """DOT's consent context plus what consent.html shows about client and lifetimes."""
        context = super().get_context_data(**kwargs)

        application = kwargs.get("application")
        redirect_uri = kwargs.get("redirect_uri") or ""
        all_scopes = oauth2_settings.SCOPES
        client_id = application.client_id if application else ""

        context.update({
            "scope_labels": [
                all_scopes.get(scope, scope) for scope in kwargs.get("scopes", [])
            ],
            "client_host": (
                urlparse(client_id).hostname if is_cimd_client_id(client_id) else None
            ),
            "client_trusted": bool(application) and is_trusted_client(application),
            "client_registered_dynamically": bool(application)
            and application.registration_source == application.RegistrationSource.DCR,
            "loopback_redirect": is_loopback_redirect(redirect_uri),
            "access_token_lifetime": human_duration(
                oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS
            ),
            "refresh_token_lifetime": human_duration(
                oauth2_settings.REFRESH_TOKEN_EXPIRE_SECONDS
            ),
            "inventree_user": self.request.user.get_username(),
        })
        return context

    def error_response(self, error, application, **kwargs):
        """Render the plugin's error page where DOT would render its own template.

        DOT redirects every non-fatal error back to the (validated) redirect
        URI; that behaviour is kept. Fatal client errors (unknown client,
        unregistered or mismatching redirect URI, ...) are never redirected.
        """
        if not isinstance(error, FatalClientError):
            return super().error_response(error, application, **kwargs)

        oauthlib_error = error.oauthlib_error
        client_id = self.request.GET.get("client_id", "")
        # A client_id URL names a public document (claude.ai, ...): logging it
        # is safe and tells the operator which client was refused. DOT logs
        # why a document was refused at INFO ("CIMD resolution failed").
        logger.warning(
            "inventree-mcp: authorization refused (%s) for user id %s, client %s",
            oauthlib_error.error,
            self.request.user.pk,
            client_id if is_cimd_client_id(client_id) else "(registered id)",
        )
        return render_error(
            self.request,
            "client",
            status=oauthlib_error.status_code,
            detail=oauthlib_error.description,
        )

    def render_denied(self, reason: str):
        """403 error page for a policy refusal; never a redirect."""
        logger.info(
            "inventree-mcp: authorization denied (%s) for user id %s",
            reason,
            self.request.user.pk,
        )
        return render_error(self.request, reason, status=403)
