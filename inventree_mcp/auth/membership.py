"""Membership re-checks against the SSO provider, and blocking (guide §5, DESIGN §7.1).

The login proved group membership once; three points keep proving it:

- login (auth/signals.py): an SSO login stamps ``membership_checked_at`` and
  lifts a block when the identity checks pass (core's group sync ran in the
  same request);
- refresh grant (auth/token.py -> refresh_grant_allowed()): when the last
  check is older than MEMBERSHIP_RECHECK_INTERVAL_MINUTES, recheck() runs
  synchronously before DOT rotates anything;
- MCP requests (mcp_transport.py): policy.may_connect() for every credential;
  requests with a long-lived credential (ApiToken, Basic) follow the same
  grace and re-login windows as refresh grants (long_lived_credential_allowed())
  and queue a background re-check (tasks.recheck_user); the hourly
  tasks.membership_sweep covers idle users.

recheck() redeems the user's upstream refresh token, which lives in allauth's
own ``SocialToken`` row (``token_secret``; no second copy), at the provider's
token endpoint, stores the rotated tokens on the same row, verifies the new
ID token with allauth's jwtkit, writes the groups claim into
``SocialAccount.extra_data["userinfo"]`` and lets core's
``InvenTree.sso.ensure_sso_groups`` sync the InvenTree groups exactly as a
login does. The synced user is then classified, never by comparing raw claim
strings here:

- identity refusal (inactive, not in MCP_REQUIRED_GROUPS, or the provider
  answered invalid_grant): block_user(), Outcome.NOT_MEMBER;
- authorisation refusal (policy.may_connect() says no_mcp_permission for any
  other reason, e.g. no MCP ruleset): the MCP tokens are revoked, nothing is
  blocked and the upstream token is kept, Outcome.NO_MCP_PERMISSION;
- otherwise Outcome.MEMBER.

One re-check per user calls out at a time: a lease on MCPUserState
(``recheck_lease_until``) is taken and committed under a short row lock, the
outbound calls run without a transaction, and the outcome is applied in a
second short transaction (recheck()).

``MEMBERSHIP_ENFORCED`` off means no re-checks and no new blocks; existing
blocks are honoured regardless (is_blocked()).

Never logged or returned: tokens, claims, e-mail addresses, names, IdP
response bodies. Logged: user id, provider id, issuer host, reason code.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from enum import Enum
from hashlib import sha256
from urllib.parse import urlparse

from django.core.cache import cache
from django.db import DatabaseError, transaction
from django.utils import timezone

from ..settings import plugin_setting

logger = logging.getLogger("inventree")

#: Seconds for every outbound call (connect and read each).
HTTP_TIMEOUT_SECONDS = 5
#: Wall-clock limit of one outbound call, body included: HTTP_TIMEOUT_SECONDS
#: bounds each socket operation, not a response that trickles in.
HTTP_DEADLINE_SECONDS = 15
DISCOVERY_CACHE_SECONDS = 600
#: The "no upstream token" warning is logged at most once per user and this many seconds.
NO_TOKEN_WARNING_SECONDS = 3600

#: After a timeout, connection error or 5xx, further re-checks against the same
#: provider do not call out for this many seconds (F3: no pile-up during an outage).
PROVIDER_BACKOFF_SECONDS = 60
#: The transport's per-user "re-check queued" marker after an ordinary queueing.
RECHECK_QUEUED_SECONDS = 60
#: Lifetime of a re-check's lease (MCPUserState.recheck_lease_until). The lease is
#: re-asserted right before the redeem (the discovery call before it takes at most
#: HTTP_DEADLINE_SECONDS), so the redeem and the store always finish well inside
#: it; a lease left behind by a killed process expires after this.
RECHECK_LEASE_SECONDS = 120

#: Reason codes stored in MCPUserState.revoked_reason by a failed re-check.
REVOKED_UPSTREAM = "invalid_grant"
NOT_IN_REQUIRED_GROUPS = "not_in_required_groups"

#: last_recheck_error codes of a NO_UPSTREAM_TOKEN outcome; any other non-empty
#: code was recorded by an UNREACHABLE outcome.
NO_TOKEN_CODES = frozenset({"no_provider", "unsupported_provider", "no_token"})


class Outcome(str, Enum):
    """Result of a membership re-check."""

    MEMBER = "member"
    NOT_MEMBER = "not_member"
    NO_MCP_PERMISSION = "no_mcp_permission"
    UNREACHABLE = "unreachable"
    NO_UPSTREAM_TOKEN = "no_upstream_token"
    NOT_DUE = "not_due"
    DISABLED = "disabled"


#: Private result of _take_lease(): another process holds a live lease (its
#: re-check is in flight). recheck() reports it as Outcome.NOT_DUE;
#: refresh_grant_allowed() applies the grace / re-login windows to it.
_LEASE_HELD = object()


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


_unenforceable_logged = False


def warn_if_unenforceable() -> None:
    """Log a WARNING when MEMBERSHIP_ENFORCED is on but the plugin's app will not be installed.

    Called once at plugin load (core.py). The app (and so MCPUserState) comes
    from InvenTree's app integration: core's AppMixin installs it only with
    ENABLE_PLUGINS_APP (or in plugin tests). Without it every re-check and
    block is silently skipped. Logged once per process (the registry
    re-creates the plugin on every reload).
    """
    global _unenforceable_logged  # one warning per process
    if _unenforceable_logged:
        return
    try:
        from common.settings import get_global_setting
        from django.conf import settings

        if not plugin_setting("MEMBERSHIP_ENFORCED"):
            return
        if getattr(settings, "PLUGIN_TESTING", False) or get_global_setting(
            "ENABLE_PLUGINS_APP", create=False
        ):
            return
    except Exception:  # noqa: BLE001 - the database may not be ready at load
        return
    _unenforceable_logged = True
    logger.warning(
        "inventree-mcp: MEMBERSHIP_ENFORCED is on, but app integration "
        "(ENABLE_PLUGINS_APP) is off: membership re-checks and blocks are disabled"
    )


_db_error_logged = False


def _read(query, default):
    """Run the row read *query* on the request path; a DatabaseError gives *default*.

    The read runs in its own savepoint so PostgreSQL stays usable after the
    error (e.g. the plugin's table is missing because ``invoke migrate`` has
    not run). Logged once per process.
    """
    global _db_error_logged  # one warning per process
    try:
        with transaction.atomic():
            return query()
    except DatabaseError:
        if not _db_error_logged:
            _db_error_logged = True
            logger.warning(
                "inventree-mcp: cannot read MCP user state (run `invoke migrate`)",
                exc_info=True,
            )
        return default


def _state_row(user):
    from ..models import MCPUserState

    return MCPUserState.objects.filter(user_id=user.pk).first()


def _state(user):
    """The user's MCPUserState row, or None (also when the table cannot be read)."""
    return _read(lambda: _state_row(user), None)


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
    return _read(lambda: _is_due(_state_row(user)), False)


def is_blocked(user) -> bool:
    """Whether *user*'s MCP access has been revoked (one indexed row).

    Honoured whether or not MEMBERSHIP_ENFORCED is on: "off" stops re-checks
    and new blocks, it does not lift existing ones (unblock_user() or an SSO
    login that passes the identity checks does).
    """
    if user is None or not getattr(user, "pk", None) or not models_available():
        return False
    from ..models import MCPUserState

    return _read(
        lambda: MCPUserState.objects.filter(
            user_id=user.pk, access_revoked_at__isnull=False
        ).exists(),
        False,
    )


# -- the windows shared by refresh grants and long-lived credentials -------------


def within_grace(state) -> bool:
    """The last successful check is younger than MEMBERSHIP_GRACE_HOURS (provider unreachable)."""
    checked = state.membership_checked_at if state else None
    grace = timedelta(hours=plugin_setting("MEMBERSHIP_GRACE_HOURS"))
    return checked is not None and checked > timezone.now() - grace


def within_relogin(user, state) -> bool:
    """The last login is younger than RELOGIN_INTERVAL_HOURS (no upstream refresh token).

    ``MCPUserState.last_login_at``, falling back to ``user.last_login`` for
    logins that bypass allauth.
    """
    last_login = (state.last_login_at if state else None) or user.last_login
    relogin = timedelta(hours=plugin_setting("RELOGIN_INTERVAL_HOURS"))
    return last_login is not None and last_login > timezone.now() - relogin


def long_lived_credential_allowed(user) -> tuple[bool, str]:
    """Whether an API token or Basic credential of *user* may still use the MCP endpoint.

    Static credentials follow the refresh grant's windows, judged by the last
    recorded outcome (the background re-check records it): refused with
    "unreachable_past_grace" when the provider was unreachable and the last
    successful check is older than MEMBERSHIP_GRACE_HOURS, and with
    "relogin_required" when there is no upstream token and the last login is
    older than RELOGIN_INTERVAL_HOURS. Always allowed while not enforced.
    """
    if not enforced():
        return True, ""
    state = _state(user)
    error = state.last_recheck_error if state else ""
    if not error:
        return True, ""
    if error in NO_TOKEN_CODES:
        if within_relogin(user, state):
            return True, ""
        return False, "relogin_required"
    if within_grace(state):
        return True, ""
    return False, "unreachable_past_grace"


def recheck_queued_key(user_id) -> str:
    """Cache key of the transport's "background re-check queued" marker for a user."""
    return f"inventree_mcp:recheck_queued:{user_id}"


# -- decision functions (called by token.py) -----------------------------------


def token_grant_allowed(user) -> tuple[bool, str]:
    """Gate for the authorization_code grant: (allowed, oauth error code)."""
    if is_blocked(user):
        return False, "invalid_grant"
    return True, ""


def refresh_grant_allowed(user) -> tuple[bool, str]:
    """Gate for the refresh_token grant: re-check when due, then decide (DESIGN §7.1 table).

    A refusal is ``(False, "invalid_grant")``; token.py answers it before DOT
    runs, so nothing is rotated or revoked unless recheck() blocked the user
    (or revoked the MCP tokens of a member without MCP permission).
    """
    if is_blocked(user):
        return False, "invalid_grant"
    if not enforced():
        return True, ""

    try:
        outcome = _recheck(user)
    except Exception as exc:  # noqa: BLE001 - e.g. the lock failed: grace decides
        logger.warning(
            "inventree-mcp: membership re-check for user id %s failed (%s)",
            user.pk,
            type(exc).__name__,
        )
        outcome = Outcome.UNREACHABLE
    if outcome in (Outcome.MEMBER, Outcome.NOT_DUE, Outcome.DISABLED):
        return True, ""
    if outcome in (Outcome.NOT_MEMBER, Outcome.NO_MCP_PERMISSION):
        return False, "invalid_grant"

    state = _state(user)

    if outcome is _LEASE_HELD:
        # Overdue, and another process's re-check is in flight: never wait for
        # it, but hold the refresh to the windows of the last recorded outcome
        # (as for an unreachable provider). The lease is left alone.
        error = state.last_recheck_error if state else ""
        if error in NO_TOKEN_CODES:
            allowed = within_relogin(user, state)
        else:
            allowed = within_grace(state)
        if allowed:
            return True, ""
        logger.info(
            "inventree-mcp: refresh refused for user id %s: re-check in progress "
            "elsewhere and the %s window has passed",
            user.pk,
            "re-login" if error in NO_TOKEN_CODES else "grace",
        )
        return False, "invalid_grant"

    if outcome is Outcome.UNREACHABLE:
        if within_grace(state):
            return True, ""
        logger.info(
            "inventree-mcp: refresh refused for user id %s: identity provider "
            "unreachable past the grace period",
            user.pk,
        )
        return False, "invalid_grant"

    # NO_UPSTREAM_TOKEN: degraded mode, the last interactive login decides.
    if within_relogin(user, state):
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

    Three steps, so that no row lock is held across the network (WP2 F2):

    1. a short transaction locks the MCPUserState row, re-reads it and takes
       the re-check lease (``recheck_lease_until``, RECHECK_LEASE_SECONDS),
       then commits. A re-check that finds a live lease returns NOT_DUE ("in
       progress") without calling out, so the upstream refresh token is never
       redeemed twice at once;
    2. outside any transaction (autocommit): discovery, redeem, storing the
       rotated upstream tokens (durable at once, so neither a later failure
       nor a killed process loses them), ID-token verification and userinfo;
    3. a second short transaction under the row lock: core's group sync, the
       classification, the stamp and the block or revocation.

    The lease is released in ``finally``; one left by a killed process expires
    on its own. A login (signals.py) only ever waits for steps 1 and 3.
    """
    outcome = _recheck(user, force=force)
    return Outcome.NOT_DUE if outcome is _LEASE_HELD else outcome


def _recheck(user, *, force: bool = False):
    """recheck(), but a live lease of another process gives _LEASE_HELD."""
    if not enforced():
        return Outcome.DISABLED
    if not force and not recheck_due(user):
        return Outcome.NOT_DUE

    lease = _take_lease(user, force=force)
    if lease is _LEASE_HELD or isinstance(lease, Outcome):
        return lease
    try:
        return _recheck_under_lease(user, lease)
    finally:
        _release_lease(user, lease)


def _take_lease(user, *, force: bool):
    """Step 1 of recheck(): the lease's expiry, an Outcome when there is nothing to do,
    or _LEASE_HELD when another re-check holds a live lease."""
    with transaction.atomic():
        state = _locked_state(user)
        if state.access_revoked_at is not None:
            return Outcome.NOT_MEMBER  # blocked until the next SSO login
        if not force and not _is_due(state):
            return Outcome.NOT_DUE  # another re-check has just succeeded
        now = timezone.now()
        if state.recheck_lease_until is not None and state.recheck_lease_until > now:
            logger.debug(
                "inventree-mcp: membership re-check for user id %s in progress", user.pk
            )
            return _LEASE_HELD
        state.recheck_lease_until = now + timedelta(seconds=RECHECK_LEASE_SECONDS)
        state.save(update_fields=["recheck_lease_until"])
        return state.recheck_lease_until


def _release_lease(user, lease) -> None:
    """Drop *lease*, unless another re-check took the row over after it expired."""
    from ..models import MCPUserState

    try:
        MCPUserState.objects.filter(user_id=user.pk, recheck_lease_until=lease).update(
            recheck_lease_until=None
        )
    except DatabaseError:  # e.g. the caller's transaction is broken: it expires
        logger.warning(
            "inventree-mcp: could not release the re-check lease of user id %s",
            user.pk,
            exc_info=True,
        )


def _recheck_under_lease(user, lease) -> Outcome:
    """Steps 2 and 3 of recheck(), with the lease (its expiry *lease*) held."""
    from django.contrib.auth import get_user_model

    context = {"issuer_host": "unknown", "user_id": user.pk, "lease": lease}
    try:
        account, groups = _check_upstream(user, context)
    except _NoUpstreamToken as exc:
        _record_error(user, exc.code)
        _warn_no_upstream_token(user, exc.code)
        # Nothing to re-check with until the next login: keep the
        # transport from queueing this user again within the interval (F5).
        cache.set(
            recheck_queued_key(user.pk),
            True,
            plugin_setting("MEMBERSHIP_RECHECK_INTERVAL_MINUTES") * 60,
        )
        return Outcome.NO_UPSTREAM_TOKEN
    except _RevokedUpstream:
        logger.info(
            "inventree-mcp: identity provider %s revoked the upstream grant of user id %s",
            context["issuer_host"],
            user.pk,
        )
        block_user(user, REVOKED_UPSTREAM)
        return Outcome.NOT_MEMBER
    except Exception as exc:  # noqa: BLE001 - _Unreachable, or anything unexpected
        return _inconclusive(user, exc, context)

    with transaction.atomic():
        state = _locked_state(user)
        try:
            with transaction.atomic():
                _sync_groups(account, groups)
                verdict, reason = _classify(get_user_model().objects.get(pk=user.pk))
        except Exception as exc:  # noqa: BLE001 - the sync failed: inconclusive
            return _inconclusive(user, exc, context)

        if verdict is _Verdict.IDENTITY_REFUSED:
            block_user(user, reason)
            return Outcome.NOT_MEMBER

        # The identity is confirmed (member, or member without MCP permission).
        state.membership_checked_at = timezone.now()
        state.last_recheck_error = ""
        state.save(update_fields=["membership_checked_at", "last_recheck_error"])

        if verdict is _Verdict.NO_MCP_PERMISSION:
            # An authorisation refusal: no block, the upstream token is kept,
            # so a new ruleset takes effect at the next authorize.
            count = revoke_all_mcp_tokens(user)
            logger.info(
                "inventree-mcp: membership re-check for user id %s: member without "
                "MCP permission, %s tokens revoked (issuer %s)",
                user.pk,
                count,
                context["issuer_host"],
            )
            return Outcome.NO_MCP_PERMISSION

    logger.info(
        "inventree-mcp: membership re-check for user id %s: member (issuer %s)",
        user.pk,
        context["issuer_host"],
    )
    return Outcome.MEMBER


def _inconclusive(user, exc: Exception, context: dict) -> Outcome:
    """Record and log a re-check that could neither confirm nor deny membership."""
    code = exc.code if isinstance(exc, _Unreachable) else "error"
    if code == "error":
        logger.debug("inventree-mcp: re-check failure", exc_info=exc)
    _record_error(user, code)
    logger.warning(
        "inventree-mcp: membership re-check for user id %s inconclusive (%s%s), issuer %s",
        user.pk,
        code,
        "" if isinstance(exc, _Unreachable) else f": {type(exc).__name__}",
        context["issuer_host"],
    )
    return Outcome.UNREACHABLE


def _record_error(user, code: str) -> None:
    """Store *code* as the user's last_recheck_error (the row exists from the lease)."""
    from ..models import MCPUserState

    MCPUserState.objects.filter(user_id=user.pk).update(last_recheck_error=code[:255])


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


class _Verdict(Enum):
    """How _check_upstream() classifies the synced user."""

    MEMBER = "member"
    IDENTITY_REFUSED = "identity_refused"
    NO_MCP_PERMISSION = "no_mcp_permission"


#: _Unreachable codes after which the provider is left alone for PROVIDER_BACKOFF_SECONDS.
_BACKOFF_CODES = frozenset({"timeout", "connection_error"})


def _backoff_key(provider_id: str) -> str:
    return f"inventree_mcp:idp_backoff:{sha256(provider_id.encode()).hexdigest()}"


def _starts_backoff(code: str) -> bool:
    return code in _BACKOFF_CODES or code.endswith("http_5xx")


def _check_upstream(user, context: dict) -> tuple:
    """Step 2 of recheck(): redeem, store, verify. Returns (SocialAccount, groups claim).

    Raises _NoUpstreamToken, _Unreachable or _RevokedUpstream. Sets
    ``context["issuer_host"]`` for the caller's log lines.
    """
    from allauth.socialaccount.models import SocialAccount

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

    # During an outage, one failed call per provider and minute is enough.
    backoff_key = _backoff_key(provider_id)
    if code := cache.get(backoff_key):
        raise _Unreachable(code)
    try:
        return account, _redeem_and_verify(app, provider_id, account, token, context)
    except _Unreachable as exc:
        if _starts_backoff(exc.code):
            cache.set(backoff_key, exc.code, PROVIDER_BACKOFF_SECONDS)
        raise


def _redeem_and_verify(app, provider_id, account, token, context: dict) -> list[str]:
    """The outbound calls: discovery, redeem, store, verify, userinfo. Returns the groups claim.

    Runs outside any transaction of recheck(), so _store_tokens() commits on
    its own (autocommit) before anything else can fail.
    """
    from allauth.socialaccount.adapter import get_adapter

    with get_adapter().get_requests_session() as session:
        config = _discovery(session, app)
        adapter = _adapter_class()(provider_id, config)
        _assert_lease(context)
        data = _redeem(session, app, adapter, token.token_secret)
        # The old upstream refresh token is spent now: store the rotated one
        # first, so no later failure (or a killed process) loses it.
        _store_tokens(adapter, token, data)

        claims = _verify_id_token(config, app, data)
        subject_field = app.settings.get("uid_field", "sub")
        if claims is not None and str(claims.get(subject_field)) != account.uid:
            raise _Unreachable("subject_mismatch")
        return _groups_claim(session, adapter, data, claims, subject_field, account)


def _assert_lease(context: dict) -> None:
    """Right before the redeem: this re-check still holds its lease, or nothing is redeemed.

    A lease that expired while an earlier call was slow may have been taken
    over; redeeming then would spend the upstream refresh token twice.
    """
    from ..models import MCPUserState

    current = (
        MCPUserState.objects
        .filter(user_id=context["user_id"])
        .values_list("recheck_lease_until", flat=True)
        .first()
    )
    if current is None or current != context["lease"]:
        raise _Unreachable("lease_lost")


def _classify(fresh) -> tuple[_Verdict, str]:
    """Identity refusals block; a lacking MCP permission only revokes (overview decisions)."""
    from . import policy

    if not fresh.is_active:
        return _Verdict.IDENTITY_REFUSED, "inactive"
    if not policy.in_required_groups(fresh):
        return _Verdict.IDENTITY_REFUSED, NOT_IN_REQUIRED_GROUPS
    ok, reason = policy.may_connect(fresh)
    if ok:
        return _Verdict.MEMBER, ""
    if reason == "no_mcp_permission":
        return _Verdict.NO_MCP_PERMISSION, ""
    return _Verdict.IDENTITY_REFUSED, reason


def _social_token(account, app):
    """allauth's SocialToken of *account* for *app* (settings-backed apps store app=NULL)."""
    from allauth.socialaccount.models import SocialToken

    tokens = SocialToken.objects.filter(account=account)
    tokens = tokens.filter(app=app) if app.pk else tokens.filter(app__isnull=True)
    return tokens.first()


def _http(session, method: str, url: str, **kwargs):
    """One outbound call; network errors become _Unreachable.

    HTTP_TIMEOUT_SECONDS bounds connect and every read; the body is streamed
    so the whole call, body included, also ends after HTTP_DEADLINE_SECONDS.
    """
    import requests

    deadline = time.monotonic() + HTTP_DEADLINE_SECONDS
    try:
        response = session.request(
            method, url, timeout=HTTP_TIMEOUT_SECONDS, stream=True, **kwargs
        )
        try:
            body = []
            for chunk in response.iter_content(8192):
                if time.monotonic() > deadline:
                    raise _Unreachable("timeout")
                body.append(chunk)
        finally:
            response.close()
        response._content = b"".join(body)  # what requests does without stream=True
        return response
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


_ADAPTER_CLASS = None


def _adapter_class():
    """allauth's OIDC adapter, fed the cached discovery document instead of fetching it.

    allauth's ``openid_config`` would fetch the document on every adapter
    instance with allauth's own timeout; overriding that one property keeps
    the adapter's endpoint and client-authentication logic
    (``access_token_url``, ``profile_url``, ``basic_auth``) while the fetch
    uses the fixed timeout and the cache (_discovery()). Built on first use:
    allauth's provider views must not be imported at plugin load.
    """
    global _ADAPTER_CLASS  # built once per process
    if _ADAPTER_CLASS is None:
        from allauth.socialaccount.providers.openid_connect.views import (
            OpenIDConnectOAuth2Adapter,
        )

        class CachedConfigAdapter(OpenIDConnectOAuth2Adapter):
            def __init__(self, provider_id, config: dict):
                super().__init__(None, provider_id)
                self._config = config

            @property
            def openid_config(self):
                return self._config

        _ADAPTER_CLASS = CachedConfigAdapter
    return _ADAPTER_CLASS


def _discovery(session, app) -> dict:
    """The provider's discovery document, fetched with the fixed timeout and cached for 10 minutes."""
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
        # allauth's OIDC account data prefers "userinfo" (provider._pick_data), so
        # seed it from the stored ID token claims rather than a bare stub.
        id_token = extra.get("id_token")
        userinfo = (
            dict(id_token) if isinstance(id_token, dict) else {"sub": account.uid}
        )
    userinfo[key] = groups
    extra["userinfo"] = userinfo
    account.extra_data = extra
    account.save(update_fields=["extra_data"])

    ensure_sso_groups(None, SocialLogin(account=account))


# -- blocking -----------------------------------------------------------------------


def block_user(user, reason: str) -> None:
    """Block *user*: stamp the block, revoke every MCP token, delete the upstream token.

    Lifted by the next SSO login that passes the identity checks (signals.py)
    or unblock_user().
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


def mcp_token_querysets(user_ids):
    """(refresh, access): the live façade tokens of *user_ids*, as two querysets.

    Refresh tokens: unrevoked. Access tokens: not expired. Both only of MCP
    (DCR/CIMD) applications (clients.mcp_applications()). Lazy, so an access
    queryset evaluated after revoking the refresh tokens no longer sees the
    paired access tokens DOT removed with them.
    """
    from oauth2_provider.models import get_access_token_model, get_refresh_token_model

    from .clients import mcp_applications

    applications = mcp_applications()
    refresh = get_refresh_token_model().objects.filter(
        user_id__in=user_ids, revoked__isnull=True, application__in=applications
    )
    access = get_access_token_model().objects.filter(
        user_id__in=user_ids, expires__gt=timezone.now(), application__in=applications
    )
    return refresh, access


def revoke_all_mcp_tokens(user) -> int:
    """Revoke every live token *user* holds for an MCP (DCR/CIMD) application.

    Refresh tokens go through DOT's RefreshToken.revoke(), which also removes
    the paired access token. Live access tokens without a live refresh token
    are revoked with AccessToken.revoke() (expired ones are DOT's
    cleartokens' business). Returns the number of tokens revoked.
    """
    refresh, access = mcp_token_querysets([user.pk])
    count = 0

    for refresh_token in refresh:
        refresh_token.revoke()
        count += 1

    for access_token in access:
        access_token.revoke()
        count += 1

    logger.info("Revoked %s MCP tokens for user id %s", count, user.pk)
    return count
