"""Configure the beta InvenTree (idempotent, non-destructive on the restored instance data). up.sh runs it inside the
server container:

    python src/backend/InvenTree/manage.py shell < configure_inventree.py

- plugin integration switched on;
- SSO settings only when .env names an Authentik application (SSO_CONFIGURED=1), otherwise left as they are;
- the admin account only created when no user "admin" exists (existing passwords are never changed);
- the groups in MCP_GROUPS (default ItAdmins) get the MCP view roles and are the groups MCP requires (existing groups
  that the SSO group sync maintains; a group that does not exist is created without interactive roles);
- the plugin's settings: OAuth on; the SSO-bound settings follow whether a social login provider is configured
  (through .env or the restored database).
"""

import json
import os

from allauth.socialaccount.models import SocialApp
from common.models import InvenTreeSetting
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

MCP_GROUPS = [g.strip() for g in (os.environ.get('MCP_GROUPS') or 'ItAdmins').split(',') if g.strip()]


def _same(stored, value):
    return stored == value or str(stored).lower() == str(value).lower()


def set_settings(values):
    changed = []
    for key, value in values.items():
        if not _same(InvenTreeSetting.get_setting(key), value):
            InvenTreeSetting.set_setting(key, value, None)
            changed.append(key)
        stored = InvenTreeSetting.get_setting(key)
        assert _same(stored, value), f'could not set {key}: {stored!r} != {value!r}'
    return changed


# Social login providers: from config.yaml (.env) or from the restored database (allauth SocialApp rows)
config_apps = getattr(django_settings, 'SOCIALACCOUNT_PROVIDERS', {}).get('openid_connect', {}).get('APPS', [])
db_apps = list(SocialApp.objects.all())
env_sso = os.environ.get('SSO_CONFIGURED') == '1' and bool(config_apps)
provider_id = ''
if config_apps:
    provider_id = config_apps[0].get('provider_id', '')
elif db_apps:
    provider_id = db_apps[0].provider_id or db_apps[0].provider
sso_available = bool(config_apps or db_apps)
print(f'social login providers: config={len(config_apps)} database={len(db_apps)} provider_id={provider_id!r}')

changed = set_settings({
    'ENABLE_PLUGINS_URL': True,
    'ENABLE_PLUGINS_APP': True,
    'ENABLE_PLUGINS_SCHEDULE': True,
    'ENABLE_PLUGINS_INTERFACE': True,
})
if env_sso:
    values = {'LOGIN_ENABLE_SSO': True, 'LOGIN_ENABLE_SSO_REG': True, 'LOGIN_SIGNUP_SSO_AUTO': True,
              'LOGIN_ENABLE_SSO_GROUP_SYNC': True, 'SSO_GROUP_KEY': 'groups', 'SSO_REMOVE_GROUPS': True}
    group_map = os.environ.get('SSO_GROUP_MAP', '').strip()
    if group_map:
        json.loads(group_map)
        values['SSO_GROUP_MAP'] = group_map
    changed += set_settings(values)
sso_login = sso_available and _same(InvenTreeSetting.get_setting('LOGIN_ENABLE_SSO'), True)
print(f'global settings changed: {", ".join(changed) or "none"}; SSO login {"on" if sso_login else "off"}')

from django.db import connection

if 'inventree_mcp_mcpruleset' not in connection.introspection.table_names():
    # ENABLE_PLUGINS_APP was off when `invoke update` ran: the plugin's migrations have not been applied yet
    print('configure_inventree: NEEDS_MIGRATE')
    raise SystemExit(0)

User = get_user_model()
if not User.objects.filter(username='admin').exists():
    admin = User.objects.create_superuser(
        'admin', os.environ.get('INVENTREE_ADMIN_EMAIL', ''), os.environ.get('INVENTREE_ADMIN_PASSWORD') or None
    )
    print('admin user created (password: ADMIN_PASSWORD in .env)')
else:
    print(f'users: {User.objects.count()} (restored, passwords untouched)')

from inventree_mcp.auth.signals import ensure_rulesets
from inventree_mcp.models import MCPRuleSet
from users.models import RuleSet
from users.ruleset import RULESET_CHOICES

for group_name in MCP_GROUPS:
    group, created = Group.objects.get_or_create(name=group_name)
    existing = set(group.rule_sets.values_list('name', flat=True))
    for name, _label in RULESET_CHOICES:
        if name not in existing:
            RuleSet.objects.create(group=group, name=name)
    ensure_rulesets(group)
    granted = MCPRuleSet.objects.filter(group=group, can_view=True).count()
    if granted == 0:
        granted = MCPRuleSet.objects.filter(group=group).update(can_view=True)
    print(f'group {group_name}: {"created, " if created else ""}{granted} MCP roles with view, {group.user_set.count()} members')

from plugin import registry

plugin = registry.get_plugin('inventree-mcp')
if plugin is None:
    registry.reload_plugins(full_reload=True, collect=True)
    plugin = registry.get_plugin('inventree-mcp')
assert plugin is not None, 'inventree-mcp is not loaded'

plugin_values = {
    'OAUTH_ENABLED': True,
    'OAUTH_FORCE_SSO': sso_login,
    'SSO_PROVIDER_ID': provider_id if sso_login else '',
    'MEMBERSHIP_ENFORCED': sso_login,
    'MCP_REQUIRED_GROUPS': ','.join(MCP_GROUPS) if sso_login else '',
}
plugin_changed = []
for key, value in plugin_values.items():
    if not _same(plugin.get_setting(key), value):
        plugin.set_setting(key, value)
        plugin_changed.append(key)
print(f'plugin settings changed: {", ".join(plugin_changed) or "none"}')
print('configure_inventree: done')
if changed:
    print('SETTINGS_CHANGED ' + ','.join(changed))
