# Beta deployment (i.beta.alacrity.ro)

InvenTree from the fork's patched image (`ghcr.io/alacrity-education/inventree:1.6.0-alacrity.1`: master plus the
five core patches) with `inventree-mcp` 0.4.0 baked in, Postgres, Redis and a local Caddy on port **8999**. The
external reverse proxy terminates TLS for `https://i.beta.alacrity.ro` and forwards to this port unchanged; the local
Caddy does the rest (discovery rewrites, static/media, the public origin headers, code-free access log).

```bash
deploy/beta/up.sh          # build, start, demo data (first run), configure; idempotent
deploy/beta/down.sh        # stop (keeps the database and data/)
deploy/beta/down.sh -v     # stop and delete everything
docker compose -p inventree-mcp-beta logs -f inventree-server inventree-worker proxy
docker compose -p inventree-mcp-beta exec inventree-server python src/backend/InvenTree/manage.py shell
```

`up.sh` writes `.env` on the first run (generated database and admin passwords, git-ignored). The admin password is
`ADMIN_PASSWORD` in `.env`; the user is `admin`.

## Data

`data/` and `pgdata/` are the instance copy (`inventree-demo.zip`, 2026-10-15: nine users, five groups, eight
Authentik accounts, Postgres 16). The first start migrates the database forward to the image's InvenTree version.
`DEMO_DATA=1` in `.env` would load the demo dataset into an empty database instead.

## Connecting Claude

- Claude Code: `claude mcp add --transport http inventree https://i.beta.alacrity.ro/plugin/inventree-mcp/mcp/`
- claude.ai / Desktop: add a custom connector with that URL. Sign in to InvenTree in the browser when asked.

MCP access comes from the MCP roles of a user's groups. `up.sh` gives the groups in `MCP_GROUPS` (default `ItAdmins`)
view on everything through MCP and makes them the groups MCP requires. With `MCP_WRITE=1` (the default in `.env.example`)
it also switches the plugin's `MCP_READ_ONLY` setting off and gives those groups all four MCP flags (view, add, change,
delete) on `part`, `part_category`, `stock` and `stock_location`, so the 15 write tools work (the users need the same
interactive roles); `MCP_WRITE=0` keeps `MCP_READ_ONLY` on. Your SSO group sync rewrites memberships at
every login, so use groups that Authentik maps, not manual ones. Staff change MCP roles under *MCP Access* and in the
group editor's "MCP Roles" panel.

## Single sign-on against Authentik

The database already has SSO on (provider id `authentik`, group sync with the Board/Clockworks/Everyone/ItAdmins/
Volunteers map), but the client credentials lived in the production server's environment. To log in on the beta:

1. In Authentik, on the production InvenTree provider, add the redirect URI
   `https://i.beta.alacrity.ro/accounts/authentik/login/callback/` (keep the production one), and make sure the
   provider allows the `offline_access` scope (the membership re-check redeems the refresh token it yields).
2. Put the provider's issuer (`https://auth.alacrity.ro/application/o/<slug>`), client id and client secret into
   `.env` as `AUTHENTIK_ISSUER`, `AUTHENTIK_CLIENT_ID`, `AUTHENTIK_CLIENT_SECRET`.
3. Re-run `up.sh`. It writes the provider into `data/config.yaml`, forces SSO for the MCP sign-in, enables the
   membership re-check (`MEMBERSHIP_ENFORCED`, `MCP_REQUIRED_GROUPS`) and restarts InvenTree.

Without the client, `up.sh` keeps the SSO settings as they are, and the OAuth connection works with password accounts
only (`admin`'s password is the production one; `ADMIN_PASSWORD` in `.env` is used only if no admin exists).

## External proxy

Only forwarding is needed. For the plugin's per-client rate limits to see real client addresses, the proxy must send
`X-Forwarded-For` and its address must be trusted by the local Caddy: `TRUSTED_PROXIES` in `.env` (default
`private_ranges`; set the proxy's IP or CIDR if it connects from a public address).

## Checks after a change

```bash
curl -s -H 'Host: i.beta.alacrity.ro' http://127.0.0.1:8999/.well-known/oauth-authorization-server/plugin/inventree-mcp | head -c 200
curl -si -H 'Host: i.beta.alacrity.ro' http://127.0.0.1:8999/plugin/inventree-mcp/mcp/ -X POST | grep -i www-authenticate
```

Operations guide: `docs/OPERATIONS.md` (settings, outcomes, failure modes, logging).
