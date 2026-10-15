Title: Preserve `next` through account_login and honour it in the React login flow

## Motivation

Anything built on `LoginRequiredMixin`, including django-oauth-toolkit's authorize view, sends anonymous browsers to `/accounts/login/?next=...`. InvenTree maps `account_login` to a `RedirectView` that drops the query string, and the React login only honours router state. An OAuth authorization flow started by a third-party client therefore cannot resume after the user logs in: the browser lands on the dashboard and the client never receives its code.

## Changes

- `InvenTree/views.py`: `AccountLoginRedirectView` on `account_login` (`InvenTree/urls.py`) forwards a same-origin `next` (`url_has_allowed_host_and_scheme`) as `/<web>?next=...` and drops every other parameter, in particular the legacy `?login=&password=` arguments.
- `src/frontend/src/functions/auth.tsx`:
  - `sanitizeNextPath()`: accepts same-origin paths only; rejects absolute URLs to other hosts, `//` and `/\` prefixes, scheme URLs and values that normalise to a protocol-relative path (`/..//evil.com`).
  - `getNextPath(redirect)`: reads `next` from the current URL (`/web/login?next=`, `/web/logged-in?next=`) or from the router state's `queryParams`.
  - `followRedirect()`: prefers a valid `next`; paths under the web base go through the router, anything else uses `window.location.assign`. Password login, `checkLoginState`, the `?login=&password=` flow and the MFA path all go through it.
  - `keepNextState()`: keeps `next` when moving between `/logged-in`, `/login` and `/provider-signup`.
  - `ProviderLogin(provider, process, redirect)`: the SSO `callback_url` becomes `/web/logged-in?next=...` when a `next` exists (login process only). `SSOButton` passes the router state.
- `LoggedIn.tsx`: preserves `next` when bouncing to `/login` after an SSO error.

## Tests

- `InvenTree/test_auth.py::TestAuth::test_account_login_preserves_next`: forwarded, other parameters dropped, off-site `next` ignored.
- `InvenTree/test_views.py::test_url_login` now expects `/web?next=%2F` (the credentials are no longer part of the redirect, `next` is).
- The sanitiser was exercised against `//evil.com`, `/\evil.com`, `https://evil.com`, `javascript:`, `/..//evil.com`, tab-injected and normal paths.
- CI on the fork (`Alacrity-Education/InvenTree`, branch `upstream-patches`): see the PR checks.
- Not yet included: a Playwright test for the `next` flow. Happy to add one if you want it in this PR.

## Notes

- When frontend and backend run on different origins (vite dev server), a `next` outside the web base resolves against the frontend origin.
- No API version bump.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
