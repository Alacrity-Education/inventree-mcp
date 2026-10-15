"""Membership re-checks against the SSO provider, and blocking (guide §5, DESIGN §7.1).

The login proved group membership once; three points keep proving it:

- login (auth/signals.py): an SSO login stamps ``membership_checked_at`` and
  lifts a block (core's group sync ran in the same request);
- refresh grant (auth/token.py -> refresh_grant_allowed()): when the last
  check is older than MEMBERSHIP_RECHECK_INTERVAL_MINUTES, recheck() runs
  synchronously before DOT rotates anything;
- MCP requests (mcp_transport.py): blocked users get 401; requests with a
  long-lived credential (ApiToken, Basic) queue a background re-check
  (tasks.recheck_user); the hourly tasks.membership_sweep covers idle users.

recheck() redeems the user's upstream refresh token, which lives in allauth's
own ``SocialToken`` row (``token_secret``; no second copy), at the provider's
token endpoint, stores the rotated tokens on the same row, verifies the new
ID token with allauth's jwtkit, writes the groups claim into
``SocialAccount.extra_data["userinfo"]`` and lets core's
``InvenTree.sso.ensure_sso_groups`` sync the InvenTree groups exactly as a
login does. Whether the user is still a member is then policy.may_connect()
on the synced user, never a comparison of raw claim strings here.

Never logged or returned: tokens, claims, e-mail addresses, names, IdP
response bodies. Logged: user id, provider id, issuer host, reason code.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from enum import Enum
from hashlib import sha256
from urllib.parse import urlparse

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from ..settings import plugin_setting

logger = logging.getLogger("inventree")

#: Seconds for every outbound call (connect and read each).
HTTP_TIMEOUT_SECONDS = 5
DISCOVERY_CACHE_SECONDS = 600
#: The "no upstream token" warning is logged at most once per user and this many seconds.
NO_TOKEN_WARNING_SECONDS = 3600

#: Reason codes stored in MCPUserState.revoked_reason by a failed re-check.
REVOKED_UPSTREAM = "invalid_grant"


class Outcome(str, Enum):
    """Result of a membership re-check."""

    MEMBER = "member"
    NOT_MEMBER = "not_member"
    UNREACHABLE = "unreachable"
    NO_UPSTREAM_TOKEN = "no_upstream_token"
    NOT_DUE = "not_due"
    DISABLED = "disabled"


class _Unreachable(Exception):
    """The provider could not confirm or deny membership; *code* goes to last_recheck_error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _RevokedUpstream(Exception):
    """The provider answered invalid_grant: the upstream grant is gone."""


class _NoUpstreamToken(Exception):
    """Nothing to redeem; *code* names the cause (no_provider, no_token, ...)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# -- state --------------------------------------------------------------------


def models_available() -> bool:
    """MCPUserState needs the plugin's Django app (InvenTree's app integration)."""
    from django.apps import apps

    return apps.is_installed("inventree_mcp")


def enforced() -> bool:
    """MEMBERSHIP_ENFORCED is on and the plugin's model is usable."""
    return bool(plugin_setting("MEMBERSHIP_ENFORCED")) and models_available()


def _state(user):
    """The user's MCPUserState row, or None."""
    from ..models import MCPUserState

    return MCPUserState.objects.filter(user_id=user.pk).first()


def _locked_state(user):
    """The user's MCPUserState row, created if missing, locked until the transaction ends."""
    from ..models import MCPUserState

    state, _created = MCPUserState.objects.select_for_update().get_or_create(user=user)
    return state


def _is_due(state) -> bool:
    if state is None or state.membership_checked_at is None:
        return True
    interval = timedelta(minutes=plugin_setting("MEMBERSHIP_RECHECK_INTERVAL_MINUTES"))
    return state.membership_checked_at <= timezone.now() - interval


def recheck_due(user) -> bool:
    """Cheap read: enforcement is on and the last check is missing or older than the interval."""
    if user is None or not getattr(user, "pk", None) or not enforced():
        return False
    return _is_due(_state(user))


def is_blocked(user) -> bool:
    """Whether *user*'s MCP access has been revoked (one indexed row; False while not enforced)."""
    if user is None or not getattr(user, "pk", None) or not enforced():
        return False
    from ..models import MCPUserState

    return MCPUserState.objects.filter(
        user_id=user.pk, access_revoked_at__isnull=False
    ).exists()


# -- decision functions (called by token.py) -----------------------------------


def token_grant_allowed(user) -> tuple[bool, str]:
    """Gate for the authorization_code grant: (allowed, oauth error code)."""
    if is_blocked(user):
        return False, "invalid_grant"
    return True, ""


def refresh_grant_allowed(user) -> tuple[bool, str]:
    """Gate for the refresh_token grant: re-check when due, then decide (DESIGN §7.1 table).

    A refusal is ``(False, "invalid_grant")``; token.py answers it before DOT
    runs, so nothing is rotated or revoked unless recheck() blocked the user.
    """
    if not enforced():
        return True, ""
    if is_blocked(user):
        return False, "invalid_grant"

    try:
        outcome = recheck(user)
    except Exception as exc:  # noqa: BLE001 - e.g. the lock failed: grace decides
        logger.warning(
            "inventree-mcp: membership re-check for user id %s failed (%s)",
            user.pk,
            type(exc).__name__,
        )
        outcome = Outcome.UNREACHABLE
    if outcome in (Outcome.MEMBER, Outcome.NOT_DUE, Outcome.DISABLED):
        return True, ""
    if outcome is Outcome.NOT_MEMBER:
        return False, "invalid_grant"

    state = _state(user)
    now = timezone.now()

    if outcome is Outcome.UNREACHABLE:
        checked = state.membership_checked_at if state else None
        grace = timedelta(hours=plugin_setting("MEMBERSHIP_GRACE_HOURS"))
        if checked is not None and checked > now - grace:
            return True, ""
        logger.info(
            "inventree-mcp: refresh refused for user id %s: identity provider "
            "unreachable past the grace period",
            user.pk,
        )
        return False, "invalid_grant"

    # NO_UPSTREAM_TOKEN: degraded mode, the last interactive login decides.
    last_login = (state.last_login_at if state else None) or user.last_login
    relogin = timedelta(hours=plugin_setting("RELOGIN_INTERVAL_HOURS"))
    if last_login is not None and last_login > now - relogin:
        return True, ""
    logger.info(
        "inventree-mcp: refresh refused for user id %s: no upstream refresh token "
        "and the last login is older than the re-login interval",
        user.pk,
    )
    return False, "invalid_grant"


# -- the re-check -------------------------------------------------------------------


def recheck(user, *, force: bool = False) -> Outcome:
    """Re-check *user*'s group membership at the SSO provider (see the module docstring).

    Serialised per user by a row lock on MCPUserState, so the upstream refresh
    token is never redeemed twice at once; a re-check that waited for the lock
    returns NOT_DUE when the other one just succeeded.
    """
    if not enforced():
        return Outcome.DISABLED
    if not force and not recheck_due(user):
        return Outcome.NOT_DUE

    with transaction.atomic():
        state = _locked_state(user)
        if state.access_revoked_at is not None:
            return Outcome.NOT_MEMBER  # blocked until the next SSO login
        if not force and not _is_due(state):
            return Outcome.NOT_DUE

        context = {"issuer_host": "unknown"}
        try:
            reason = _check_upstream(user, context)
        except _NoUpstreamToken as exc:
            _record_error(state, exc.code)
            _warn_no_upstream_token(user, exc.code)
            return Outcome.NO_UPSTREAM_TOKEN
        except _RevokedUpstream:
            logger.info(
                "inventree-mcp: identity provider %s revoked the upstream grant of user id %s",
                context["issuer_host"],
                user.pk,
            )
            block_user(user, REVOKED_UPSTREAM)
            return Outcome.NOT_MEMBER
        except Exception as exc:  # _Unreachable, or anything unexpected: inconclusive
            code = exc.code if isinstance(exc, _Unreachable) else "error"
            if code == "error":
                logger.debug("inventree-mcp: re-check failure", exc_info=True)
            _record_error(state, code)
            logger.warning(
                "inventree-mcp: membership re-check for user id %s inconclusive (%s%s), issuer %s",
                user.pk,
                code,
                "" if isinstance(exc, _Unreachable) else f": {type(exc).__name__}",
                context["issuer_host"],
            )
            return Outcome.UNREACHABLE

        if reason:
            block_user(user, reason)
            return Outcome.NOT_MEMBER

        state.membership_checked_at = timezone.now()
        state.last_recheck_error = ""
        state.save(update_fields=["membership_checked_at", "last_recheck_error"])
        logger.info(
            "inventree-mcp: membership re-check for user id %s: member (issuer %s)",
            user.pk,
            context["issuer_host"],
        )
        return Outcome.MEMBER


def _record_error(state, code: str) -> None:
    state.last_recheck_error = code[:255]
    state.save(update_fields=["last_recheck_error"])


def _warn_no_upstream_token(user, code: str) -> None:
    """Log why there is nothing to re-check with, at most once per user per hour."""
    if cache.add(
        f"inventree_mcp:no_upstream_token:{user.pk}", True, NO_TOKEN_WARNING_SECONDS
    ):
        logger.warning(
            "inventree-mcp: no upstream refresh token for user id %s (%s); MCP refresh "
            "grants work until %s hours after the last login. Check that the SSO provider "
            "grants offline_access and the user signed in through SSO.",
            user.pk,
            code,
            plugin_setting("RELOGIN_INTERVAL_HOURS"),
        )


def _check_upstream(user, context: dict) -> str:
    """Redeem, store, verify, sync groups and decide. Returns the refusal reason ("" = member).

    Raises _NoUpstreamToken, _Unreachable or _RevokedUpstream. Sets
    ``context["issuer_host"]`` for the caller's log lines.
    """
    from allauth.socialaccount.adapter import get_adapter
    from allauth.socialaccount.models import SocialAccount
    from allauth.socialaccount.providers.openid_connect.views import (
        OpenIDConnectOAuth2Adapter,
    )
    from django.contrib.auth import get_user_model

    from . import policy
    from .sso import OIDC_PROVIDER, provider_id_of, sso_app

    app = sso_app(None)
    if app is None:
        raise _NoUpstreamToken("no_provider")
    if app.provider != OIDC_PROVIDER:
        raise _NoUpstreamToken("unsupported_provider")
    provider_id = provider_id_of(app)
    context["issuer_host"] = (
        urlparse(app.settings.get("server_url", "")).hostname or "unknown"
    )

    account = SocialAccount.objects.filter(user=user, provider=provider_id).first()
    token = _social_token(account, app) if account else None
    if token is None or not token.token_secret:
        raise _NoUpstreamToken("no_token")

    adapter = OpenIDConnectOAuth2Adapter(None, provider_id)
    with get_adapter().get_requests_session() as session:
        config = _discovery(session, app, adapter)
        data = _redeem(session, app, adapter, token.token_secret)
        # The old upstream refresh token is spent now: store the rotated one
        # first, outside the savepoint below, so no later failure loses it.
        _store_tokens(adapter, token, data)

        with transaction.atomic():
            claims = _verify_id_token(config, app, data)
            subject_field = app.settings.get("uid_field", "sub")
            if claims is not None and str(claims.get(subject_field)) != account.uid:
                raise _Unreachable("subject_mismatch")
            groups = _groups_claim(
                session, adapter, data, claims, subject_field, account
            )
            _sync_groups(account, groups)

            ok, reason = policy.may_connect(get_user_model().objects.get(pk=user.pk))
    return "" if ok else reason


def _social_token(account, app):
    """allauth's SocialToken of *account* for *app* (settings-backed apps store app=NULL)."""
    from allauth.socialaccount.models import SocialToken

    tokens = SocialToken.objects.filter(account=account)
    tokens = tokens.filter(app=app) if app.pk else tokens.filter(app__isnull=True)
    return tokens.first()


def _http(session, method: str, url: str, **kwargs):
    """One outbound call with the fixed timeout; network errors become _Unreachable."""
    import requests

    try:
        return session.request(method, url, timeout=HTTP_TIMEOUT_SECONDS, **kwargs)
    except requests.Timeout as exc:
        raise _Unreachable("timeout") from exc
    except requests.RequestException as exc:
        raise _Unreachable("connection_error") from exc


def _json(response, code: str = "bad_response") -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise _Unreachable(code) from exc
    if not isinstance(data, dict):
        raise _Unreachable(code)
    return data


def _http_error_code(response) -> str:
    return "http_5xx" if response.status_code >= 500 else "http_4xx"


def _discovery(session, app, adapter) -> dict:
    """The provider's discovery document, cached for 10 minutes, handed to allauth's adapter.

    allauth's ``openid_config`` would fetch it on every adapter instance with
    allauth's own timeout; seeding it keeps the adapter's endpoint and
    client-authentication logic (``access_token_url``, ``profile_url``,
    ``basic_auth``) while the fetch uses the fixed timeout and the cache.
    """
    server_url = app.get_provider(None).server_url
    key = f"inventree_mcp:oidc_config:{sha256(server_url.encode()).hexdigest()}"
    config = cache.get(key)
    if config is None:
        response = _http(session, "GET", server_url)
        if response.status_code != 200:
            raise _Unreachable("discovery_" + _http_error_code(response))
        config = _json(response, "bad_discovery")
        if not all(config.get(k) for k in ("issuer", "token_endpoint", "jwks_uri")):
            raise _Unreachable("bad_discovery")
        cache.set(key, config, DISCOVERY_CACHE_SECONDS)
    # allauth's memo attribute, see the docstring
    adapter._openid_config = config
    return config


def _redeem(session, app, adapter, refresh_token: str) -> dict:
    """POST grant_type=refresh_token with the client authentication allauth uses for the login."""
    from requests.auth import HTTPBasicAuth

    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    auth = None
    if adapter.basic_auth:
        auth = HTTPBasicAuth(app.client_id, app.secret)
    else:
        data |= {"client_id": app.client_id, "client_secret": app.secret}

    response = _http(
        session,
        "POST",
        adapter.access_token_url,
        data=data,
        auth=auth,
        headers={"Accept": "application/json"},
    )
    if response.status_code == 400:
        try:
            error = response.json().get("error")
        except (ValueError, AttributeError):
            error = None
        if error == "invalid_grant":
            raise _RevokedUpstream
    if response.status_code != 200:
        raise _Unreachable(_http_error_code(response))
    body = _json(response)
    if not body.get("access_token"):
        raise _Unreachable("bad_response")
    return body


def _store_tokens(adapter, token, data: dict) -> None:
    """Write the redeemed tokens onto the existing SocialToken row, like allauth's _store_token()."""
    parsed = adapter.parse_token(data)
    token.token = parsed.token
    if parsed.token_secret:  # providers may not rotate the refresh token
        token.token_secret = parsed.token_secret
    token.expires_at = parsed.expires_at
    token.save(update_fields=["token", "token_secret", "expires_at"])


def _verify_id_token(config: dict, app, data: dict) -> dict | None:
    """Claims of the new ID token, verified against the JWKS (allauth's jwtkit); None if absent."""
    from allauth.socialaccount.internal import jwtkit

    id_token = data.get("id_token")
    if not id_token:
        return None
    try:
        return jwtkit.verify_and_decode(
            credential=id_token,
            keys_url=config["jwks_uri"],
            issuer=config["issuer"],
            audience=app.client_id,
            lookup_kid=jwtkit.lookup_kid_jwk,
        )
    except Exception as exc:  # OAuth2Error, requests errors, malformed header ...
        import requests

        if isinstance(exc, requests.Timeout):
            raise _Unreachable("timeout") from exc
        raise _Unreachable("bad_id_token") from exc


def _groups_claim(session, adapter, data, claims, subject_field, account) -> list[str]:
    """SSO_GROUP_KEY from the ID token, else from userinfo (same subject); [] when absent.

    Without an ID token the userinfo call, authenticated with the new access
    token over the provider's TLS endpoint, is what confirms the subject.
    """
    from common.settings import get_global_setting

    key = get_global_setting("SSO_GROUP_KEY") or "groups"
    if claims is not None and key in claims:
        return _as_group_list(claims[key])

    try:
        profile_url = adapter.profile_url
    except KeyError as exc:
        if claims is None:
            raise _Unreachable("bad_id_token") from exc
        return []

    response = _http(
        session,
        "GET",
        profile_url,
        headers={
            "Authorization": f"Bearer {data['access_token']}",
            "Accept": "application/json",
        },
    )
    if response.status_code != 200:
        raise _Unreachable("userinfo_" + _http_error_code(response))
    userinfo = _json(response, "bad_userinfo")
    if str(userinfo.get(subject_field)) != account.uid:
        raise _Unreachable("subject_mismatch")
    return _as_group_list(userinfo.get(key))


def _as_group_list(value) -> list[str]:
    """A groups claim as a list of strings (a single string counts as one group)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _sync_groups(account, groups: list[str]) -> None:
    """Put the claim where core reads it, then run core's group sync (SSO_GROUP_MAP, SSO_REMOVE_GROUPS)."""
    from allauth.socialaccount.models import SocialLogin
    from common.settings import get_global_setting
    from InvenTree.sso import ensure_sso_groups

    key = get_global_setting("SSO_GROUP_KEY") or "groups"
    extra = account.extra_data if isinstance(account.extra_data, dict) else {}
    userinfo = extra.get("userinfo")
    if not isinstance(userinfo, dict):
        # allauth's OIDC account data prefers "userinfo"; keep the subject there.
        userinfo = {"sub": account.uid}
    userinfo[key] = groups
    extra["userinfo"] = userinfo
    account.extra_data = extra
    account.save(update_fields=["extra_data"])

    ensure_sso_groups(None, SocialLogin(account=account))


# -- blocking -----------------------------------------------------------------------


def block_user(user, reason: str) -> None:
    """Block *user*: stamp the block, revoke every MCP token, delete the upstream token.

    Lifted by the next SSO login (signals.py) or unblock_user().
    """
    with transaction.atomic():
        state = _locked_state(user)
        state.access_revoked_at = timezone.now()
        state.revoked_reason = (reason or "")[:64]
        state.save(update_fields=["access_revoked_at", "revoked_reason"])
        count = revoke_all_mcp_tokens(user)
        _delete_upstream_token(user)

    logger.warning(
        "inventree-mcp: MCP access blocked for user id %s (%s), %s tokens revoked",
        user.pk,
        reason,
        count,
    )


def _delete_upstream_token(user) -> int:
    """Delete the user's allauth SocialToken(s) for the MCP sign-in provider."""
    from allauth.socialaccount.models import SocialToken

    from .sso import provider_id_of, sso_app

    app = sso_app(None)
    if app is None:
        return 0
    deleted, _ = SocialToken.objects.filter(
        account__user=user, account__provider=provider_id_of(app)
    ).delete()
    return deleted


def unblock_user(user) -> None:
    """Lift a block (an SSO login does this through signals.py)."""
    from ..models import MCPUserState

    lifted = MCPUserState.objects.filter(
        user_id=user.pk, access_revoked_at__isnull=False
    ).update(access_revoked_at=None, revoked_reason="")
    if lifted:
        logger.info("inventree-mcp: MCP block lifted for user id %s", user.pk)


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
