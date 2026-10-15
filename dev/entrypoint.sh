#!/bin/bash
# Dev entrypoint wrapper (see dev/README.md). The stock image entrypoint (init.sh) only prepares directories and
# config.yaml; it does not run `invoke update`, so plugins.txt would never be installed. This wrapper:
#   1. installs the plugin (editable, from the mounted checkout) and django_slowtests (needed by `manage.py test`)
#      into the container's site-packages (every container start, because a recreated container loses them);
#   2. unless DEV_PATCH_SSO_SIGNUP=0, patches an InvenTree 1.5.7 bug that makes the first SSO login of a new user
#      fail with a 500 (see below);
#   3. on the server only (DEV_RUN_UPDATE=1): runs `invoke update` (migrations, static files, plugins.txt);
#   4. execs the original init.sh with the original command.
set -euo pipefail
cd /home/inventree

PLUGIN_DIR=/home/inventree/plugins/inventree-mcp
if [[ ! -f "$PLUGIN_DIR/pyproject.toml" ]]; then
    echo "dev-entrypoint: $PLUGIN_DIR is not the plugin checkout (bind mount missing?)" >&2
    exit 1
fi

# init.sh first: it creates data/{static,media,backup} and copies config.yaml when up.sh did not render one
# (it cannot exec the command yet, so run it in a subshell with a no-op command).
./init.sh true

# We run as root on the bind-mounted checkout: hand what root wrote there (inventree_mcp.egg-info, __pycache__)
# back to the checkout's owner. pip writes egg-info here, and so do the later plugins.txt installs.
fix_checkout_ownership() {
    find "$PLUGIN_DIR" -xdev \( -name .git -o -path "$PLUGIN_DIR/deploy" \) -prune -o \
        \( ! -uid "$(stat -c %u "$PLUGIN_DIR")" -exec chown -h --reference="$PLUGIN_DIR" {} + \) 2>/dev/null || true
}

echo "dev-entrypoint: installing the plugin (editable) and django_slowtests"
pip install --quiet --disable-pip-version-check -e "$PLUGIN_DIR" "django_slowtests==1.1.1"
fix_checkout_ownership

# InvenTree 1.5.7 (stable) bug: CustomSocialAccountAdapter.save_user() receives the allauth SocialLogin object but sets
# the "_is_registering" marker on it instead of on the User, so users.models.sync_user_email_address still creates the
# EmailAddress and allauth's setup_user_email() then trips its "no EmailAddress yet" assertion (500 on the callback
# of every new SSO user; the account and group sync are saved, only that first login fails). Master dropped the
# marker altogether (InvenTree PR #12842). Production installs on 1.5.x have this bug unpatched: set
# DEV_PATCH_SSO_SIGNUP=0 in dev/inventree.env to see it. Guarded and idempotent: only patches the exact buggy line.
AUTH_OVERRIDES=/home/inventree/src/backend/InvenTree/InvenTree/auth_overrides.py
if [[ "${DEV_PATCH_SSO_SIGNUP:-1}" != "1" ]]; then
    echo "dev-entrypoint: DEV_PATCH_SSO_SIGNUP=${DEV_PATCH_SSO_SIGNUP}, InvenTree SSO signup bug left unpatched"
    if grep -q '^        getattr(user, "user", user)._is_registering = True' "$AUTH_OVERRIDES"; then
        echo "dev-entrypoint: note: auth_overrides.py was patched by an earlier start of this container; recreate it" \
             "(dev/compose.sh up -d --force-recreate inventree-server inventree-worker) to drop the patch"
    fi
elif grep -q '^        user._is_registering = True' "$AUTH_OVERRIDES"; then
    echo "dev-entrypoint: patching InvenTree SSO signup bug in auth_overrides.py (upstream PR #12842)"
    sed -i 's/^        user._is_registering = True/        getattr(user, "user", user)._is_registering = True/' "$AUTH_OVERRIDES"
else
    echo "dev-entrypoint: InvenTree SSO signup patch not needed (buggy line not present in auth_overrides.py)"
fi

if [[ "${DEV_RUN_UPDATE:-0}" == "1" ]]; then
    echo "dev-entrypoint: invoke update"
    invoke update --skip-backup --no-frontend
    fix_checkout_ownership  # invoke update reinstalls plugins.txt (-e the checkout): PKG-INFO is rewritten as root
fi

# InvenTree's plugin registry may install plugins.txt once more when it loads in the server or worker process
# (plugins file hash check), again rewriting egg-info as root after this script has exec'd. Keep fixing the
# ownership for the first five minutes in a detached background loop.
( for _ in $(seq 30); do sleep 10; fix_checkout_ownership; done ) </dev/null >/dev/null 2>&1 &

exec ./init.sh "$@"
