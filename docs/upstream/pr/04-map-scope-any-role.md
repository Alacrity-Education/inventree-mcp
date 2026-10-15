Title: Accept any one role's scope for tables shared by several rulesets

## Motivation

Several tables belong to more than one ruleset: `part_part` to *part* and *build*, `stock_stockitem` to *stock* and *build*, `company_company` / `company_contact` / `company_address` to *purchase_order*, *sales_order* and *return_order*, `company_supplierpart` / `company_manufacturerpart` to *part* and *purchase_order*, and a few more.

The role check for a request (`users.permissions.check_user_permission`) accepts **any** of the table's roles. The OAuth2 scope check required **all** of them: `map_scope(roles=...)` returned one alternative holding every role's scope. A token issued with exactly the scopes the *Parts* role grants (`r:view:part`) could not list parts unless it also held `r:view:build`.

## Changes

- `InvenTree/permissions.py` `map_scope()`: one alternative per role (`[[scope(role)] for role in roles]`), which `TokenMatchesOASRequirements` treats as OR. `only_read`, `map_read` and the `OPTIONS` path are unchanged.
- `map_scope(roles=...)` has three callers, and all three change:
  1. `InvenTreeRoleScopeMixin` (every endpoint of a multi-role table): the intended fix.
  2. `DataImporterPermission` (`/api/importer/session/`, `.../accept_rows/`, `/api/importer/column-mapping/`, `/api/importer/row/`): reads stay `g:read`; writes needed `r:change:<role>` for **all eleven** roles, now **any one** passes the scope check.
  3. `ContentTypePermission` (`GenericMetadataView` and `SimpleGenericMetadataView`, i.e. `/api/metadata/...`, which the per-model `.../metadata/` paths redirect to): every method needed the scope of all eleven roles, now any one passes.
  
  For 2 and 3 the object-level checks still run unchanged and check the *user's* roles. This is the same "any one role" rule the role check applies, but it is a deliberate relaxation of the token boundary on these two endpoint families: a token with one role's scope can now read metadata of an object of another role, or drive an import into any model, when the user holds the other role interactively. If you prefer to keep the all-roles requirement there, I can narrow the change to `InvenTreeRoleScopeMixin` and give the other two classes their own variant.
- `InvenTree/api_version.py`: bumped to v558, because the OpenAPI security requirements of these endpoints change from one requirement holding every role's scope to one requirement per role.

## Tests

- `InvenTree/tests.py::OAuth2ScopeMapTest` (DOT's `TokenMatchesOASRequirements` with in-memory tokens):
  - `test_map_scope_any_role`: every method of a two-role table, a single role unchanged, `OPTIONS` still `g:read`;
  - `test_multi_role_table_accepts_any_role_scope`: `PartList` accepts `r:view:part` or `r:view:build`, refuses `r:view:stock`;
  - `test_content_type_permission_accepts_any_role_scope`;
  - `test_data_importer_permission_accepts_any_role_scope`.
- CI on the fork (`Alacrity-Education/InvenTree`, branch `upstream-patches`): see the PR checks, including the schema diff.

## Notes

- The PR number in `api_version.py` is filled in once this PR has one.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
