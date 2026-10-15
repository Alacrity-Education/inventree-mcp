Title: Allow OAUTH2_PROVIDER options to be set via config.yaml / environment

## Motivation

`OAUTH2_PROVIDER` in `settings.py` is a fixed dict. A deployment cannot change django-oauth-toolkit options such as `DCR_ENABLED`, `CIMD_ENABLED`, `ALLOW_LOCALHOST_LOOPBACK` or the token lifetimes without patching core. Plugins that expose an OAuth-protected endpoint (for example an MCP server that Claude connects to by OAuth) need dynamic client registration and client-ID metadata documents enabled, which today is impossible from configuration.

## Changes

- `InvenTree/config.py`: new `merge_oauth2_provider_settings(base, overrides)`. Returns a new dict, overrides win, and only the sorted key names are logged (never values).
- `InvenTree/settings.py`: after the hard-coded dict, `OAUTH2_PROVIDER` is merged with `get_setting('INVENTREE_OAUTH2_PROVIDER', 'oauth2_provider', None, typecast=dict)`. `SCOPES`, `OIDC_ENABLED`, `OIDC_RSA_PRIVATE_KEY` and `PKCE_REQUIRED` stay as they are unless explicitly overridden. The environment variable takes JSON.
- `config_template.yaml`: commented example.
- `docs/docs/start/config.md`: new "OAuth2 Provider Options" section.

Behaviour is unchanged for deployments that set nothing.

## Tests

- `InvenTree/tests.py::test_oauth2_provider_override`: merge precedence, the base dict is not mutated, key-only logging, JSON through `get_setting`.
- CI on the fork (`Alacrity-Education/InvenTree`, branch `upstream-patches`): see the PR checks.

## Notes

- No API version bump: the REST schema does not change.
- Downstream use: the `inventree-mcp` plugin currently injects these settings at runtime; with this change it can tell administrators to set `INVENTREE_OAUTH2_PROVIDER` instead.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
