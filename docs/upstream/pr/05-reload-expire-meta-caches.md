Title: Expire model meta caches after a plugin registry reload

## Motivation

Since #13007 the plugin package is purged from `sys.modules` on every registry collect, so a plugin's models module is imported inside `Apps.populate()` during `registry._reload_apps()`. `populate()` runs while `apps.ready` is False, and `Apps.clear_cache()` skips the per-model cache expiry in that state. Core models therefore keep the relation trees and field caches built before the reload and never learn about the plugin models registered during it.

Observable effect after any `reload_plugins(collect=True)` on a running instance (plugin install, activation, the registry hash check): `Group._meta.get_fields()` no longer lists a plugin model's foreign key to `Group`, reverse accessors are missing, and `on_delete=CASCADE` into the plugin's table is never collected. Deleting the group then fails on the deferred database constraint at commit:

```
django.db.utils.IntegrityError: insert or update on table "inventree_mcp_mcpruleset" violates foreign key constraint "..._fk_auth_group_id"
```

Before #13007 this was masked: `AppMixin._reregister_contrib_apps()` reloaded the models module after the registry was ready, and those `register_model()` calls expired the caches.

## Changes

- `plugin/registry.py` `_reload_apps()`: call `apps.clear_cache()` once the registry is populated again, on both the `populate` and the `set_installed_apps` paths.
- `plugin/test_plugin.py::RegistryTests::test_reload_expires_model_meta_caches`: the relation tree cached on `Group` before a full reload must not be the one in place afterwards.

## Tests

- The new test fails without the change and passes with it (run in `inventree/inventree:latest`).
- CI on the fork (`Alacrity-Education/InvenTree`, branch `upstream-patches`): see the PR checks.

## Note on plugin test suites

The purge also breaks `unittest.mock` for plugin test suites: the test modules are imported once at discovery, every test class triggers a collect, and from then on patch targets name re-imported modules the code under test no longer uses. The `inventree-mcp` plugin disables the purge for its test run as a workaround. If you have a preferred way for plugins to opt out during testing, I am happy to add it here or in a follow-up.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
