"""Configure the dev InvenTree (idempotent). Run inside the server container:

    python src/backend/InvenTree/manage.py shell < dev/configure_inventree.py   (see up.sh)

Sets global settings for plugins and SSO, makes sure the dev admin account works, and creates the groups
"Users" (view on every role) and "MCP Users" (no interactive roles; MCP roles: view on every role, once the
plugin's tables exist). admin is put into "MCP Users" so the password-mode smoke test has MCP roles too.
"""

import json

from common.models import InvenTreeSetting
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

# Authentik group -> InvenTree group (a plain literal: authentik/sso_smoke.py reads it with ast to derive its expectations)
SSO_GROUP_MAP = {'inventree-users': 'Users', 'inventree-mcp': 'MCP Users'}

SETTINGS = {
    'ENABLE_PLUGINS_URL': True,
    'ENABLE_PLUGINS_APP': True,
    'ENABLE_PLUGINS_SCHEDULE': True,
    'ENABLE_PLUGINS_INTERFACE': True,
    'LOGIN_ENABLE_SSO': True,
    'LOGIN_ENABLE_SSO_REG': True,
    'LOGIN_ENABLE_SSO_GROUP_SYNC': True,
    'LOGIN_SIGNUP_SSO_AUTO': True,
    'SSO_GROUP_KEY': 'groups',
    'SSO_GROUP_MAP': json.dumps(SSO_GROUP_MAP),
    'SSO_REMOVE_GROUPS': True,
    # the demo data names a default group for new sign-ups ('all access'); a guest must end up in no group
    'SIGNUP_GROUP': '',
}


def _same(stored, value):
    return stored == value or str(stored).lower() == str(value).lower()


changed = []
for key, value in SETTINGS.items():
    if not _same(InvenTreeSetting.get_setting(key), value):
        changed.append(key)
    InvenTreeSetting.set_setting(key, value, None)
    stored = InvenTreeSetting.get_setting(key)
    ok = _same(stored, value)
    print(f'setting {key} = {stored!r}' + ('' if ok else f'  <-- expected {value!r}'))
    assert ok, f'could not set {key}'

# Demo data may replace the admin user; make sure admin / admin works for API tests.
User = get_user_model()
admin, created = User.objects.get_or_create(
    username='admin', defaults={'email': 'admin@example.test'}
)
admin.is_staff = admin.is_superuser = admin.is_active = True
admin.set_password('admin')
admin.save()
print(f'admin user {"created" if created else "present"} (password admin)')

from users.models import RuleSet
from users.ruleset import RULESET_CHOICES

users_group, _ = Group.objects.get_or_create(name='Users')
mcp_group, _ = Group.objects.get_or_create(name='MCP Users')
# Core creates the RuleSet rows for a new group; make sure they exist for groups that pre-date that.
for group in (users_group, mcp_group):
    existing = set(group.rule_sets.values_list('name', flat=True))
    for name, _label in RULESET_CHOICES:
        if name not in existing:
            RuleSet.objects.create(group=group, name=name)
rows = RuleSet.objects.filter(group=users_group).update(
    can_view=True, can_add=False, can_change=False, can_delete=False
)
print(f'group Users: {rows} rulesets view-only')
print(f'group MCP Users: {mcp_group.rule_sets.filter(can_view=True).count()} rulesets with view (expect 0)')
admin.groups.add(mcp_group)

# The plugin's second permission set (WP3): MCP Users may view everything through MCP (their interactive roles,
# from "Users", still apply). Skipped while the plugin's app or migration is not there yet (first up.sh run).
try:
    from inventree_mcp.auth.signals import ensure_rulesets
    from inventree_mcp.models import MCPRuleSet

    ensure_rulesets(mcp_group)
    MCPRuleSet.objects.filter(group=mcp_group).update(can_view=True)
    print(f'group MCP Users: {MCPRuleSet.objects.filter(group=mcp_group, can_view=True).count()} MCP rulesets with view')
except Exception as exc:  # noqa: BLE001 - dev convenience only
    print(f'group MCP Users: MCP rulesets not set ({type(exc).__name__}); re-run up.sh after the plugin migrated')
print('configure_inventree: done')
# up.sh restarts the server when this line is present: gunicorn workers that started before ENABLE_PLUGINS_URL was
# switched on never serve plugin URLs (intermittent 403/404 on /plugin/...) until they are restarted.
if changed:
    print('SETTINGS_CHANGED ' + ','.join(changed))
