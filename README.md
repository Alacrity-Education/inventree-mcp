# InvenTreeMCP

[![PyPI](https://img.shields.io/pypi/v/inventree-mcp)](https://pypi.org/project/inventree-mcp/)
[![CI](https://github.com/inventree/inventree-mcp/actions/workflows/ci.yaml/badge.svg)](https://github.com/inventree/inventree-mcp/actions/workflows/ci.yaml)
[![codecov](https://codecov.io/gh/inventree/inventree-mcp/graph/badge.svg)](https://codecov.io/gh/inventree/inventree-mcp)

An MCP (Model Context Protocol) server for InvenTree, exposed as an InvenTree plugin. It lets MCP
clients (Claude, other MCP-aware agents) query InvenTree inventory data over a Streamable HTTP
endpoint.

## Design

Every tool is a thin wrapper around InvenTree's own REST API view classes (see
[`inventree_mcp/proxy.py`](inventree_mcp/proxy.py)), dispatched as the authenticated caller - MCP
requests go through exactly the same permission checks, filtering, and serialization as the regular
REST API. Tool code never queries the Django ORM directly.

Currently read-only, covering parts, stock items/locations, part categories, purchase/sales/return/
build orders (with line items and allocations), companies, contacts, addresses, manufacturer/
supplier parts, BOM items, attachments, parameters, stock tracking history, test results, and
project codes. Once write tools land, the `MCP_READ_ONLY` setting (see Configuration) will block
them by default regardless of the calling user's permissions.

Each tool's `outputSchema` and filter/ordering options are derived live from InvenTree's own
serializers and views (not hand-maintained), so they can't drift as InvenTree evolves. Call
`describe_filters(resource)` to see what's available for a given resource. The set of tools an MCP
client sees is also filtered to what the calling user can actually use - though every call is still
permission-checked in full regardless of what was advertised.

## Setup

### 1. Install the plugin

Install via the InvenTree plugin manager, or via pip:

```bash
pip install inventree-mcp
```

Then enable the plugin under **Admin > Plugins**, and configure its settings (see
Configuration below).

### 2. Connect: OAuth sign-in (recommended) or an API token

With **Enable OAuth Sign-in** turned on (see [Connect with OAuth](#connect-with-oauth)), MCP clients
such as Claude connect by signing in through the browser and need no token at all. Otherwise,
create an InvenTree API token for your MCP client and configure it as below.

### 3. Configure your MCP client

The endpoint is `<your-inventree-server>/plugin/inventree-mcp/mcp/`, using Streamable HTTP
transport with an `Authorization: Token <token>` header.

For a client that supports remote Streamable HTTP servers directly, add:

```json
{
  "mcpServers": {
    "inventree": {
      "url": "https://<your-inventree-server>/plugin/inventree-mcp/mcp/",
      "headers": {
        "Authorization": "Token <your-api-token>"
      }
    }
  }
}
```

For a client that only supports local (stdio) servers, bridge it with
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote)

*Note: You will need to have node available on your system path*

```json
{
  "mcpServers": {
    "inventree": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "https://<your-inventree-server>/plugin/inventree-mcp/mcp/",
        "--header",
        "Authorization: Token <your-api-token>"
      ]
    }
  }
}
```

*Note: On Windows, you may need to substitute `npx` with `C:\\PROGRA~1\\nodejs\\npx.cmd` in the command field.*

In a setup where the server is running a self-signed certificate, you may need to use the following `env` arguments to disable certificate verification:

```json
"args": [
  ...
],
"env": {
  "NODE_TLS_REJECT_UNAUTHORIZED": "0"
}
```

## Connect with OAuth

The plugin can act as an OAuth 2.1 authorization server for its MCP endpoint, so a user connects
Claude (claude.ai, Claude Desktop, Claude Code) by signing in to InvenTree in the browser. Tokens
are issued and renewed automatically; nothing is copied by hand.

**Connect from Claude:**

- claude.ai / Claude Desktop: *Settings > Connectors > Add custom connector*, URL
  `https://<your-inventree-server>/plugin/inventree-mcp/mcp/`.
- Claude Code: `claude mcp add --transport http inventree https://<your-inventree-server>/plugin/inventree-mcp/mcp/`,
  then run `/mcp` inside Claude Code and choose *Authenticate*.

**What the user sees:** the browser opens InvenTree. Without a session it goes straight to the
single sign-on provider (e.g. Authentik) and comes back. Claude's web clients from trusted hosts are
approved without a consent page; other clients, and every client that receives the result on a
local port (Claude Code), show a consent page with the client, the signed-in user, the permissions
and the token lifetimes. Access is the user's InvenTree permissions, limited to the OAuth scopes
granted (read-only scopes while **Read Only** is on).

**Prerequisites:**

- InvenTree >= 1.5 (django-oauth-toolkit >= 3.4); older versions keep working without the OAuth
  endpoints.
- Single sign-on configured in InvenTree (an `openid_connect` social provider, *Enable SSO* on), or
  users sign in to InvenTree first and then reconnect.
- `SITE_URL` set and the reverse proxy forwarding `X-Forwarded-Proto` / `X-Forwarded-Host`, so the
  advertised URLs use the public origin.
- InvenTree's CORS whitelist (`cors.whitelist`) containing `https://claude.ai` and
  `https://claude.com` (browser preflights to `/plugin/` are answered by InvenTree's CORS middleware).
- Plugin settings: **Enable OAuth Sign-in** (`OAUTH_ENABLED`) on; URL integration, schedule
  integration and app integration (`ENABLE_PLUGINS_APP`, needed for the plugin's models from the
  next releases on) enabled for plugins.
- A reverse proxy that **replaces** `X-Forwarded-For` with the client address instead of appending
  to it, and gunicorn not reachable directly. InvenTree takes the client IP from the *first*
  `X-Forwarded-For` entry, which a client can set itself when the proxy appends; the registration
  rate limit (`REGISTER_RATE_LIMIT_PER_MINUTE`) is per client IP. Caddy replaces the header by
  default (as long as `trusted_proxies` is not configured); with nginx use
  `proxy_set_header X-Forwarded-For $remote_addr;`, not `$proxy_add_x_forwarded_for`.

**How it works:** a `401` from the MCP endpoint carries `WWW-Authenticate: Bearer
resource_metadata="…"`. The protected-resource metadata names the issuer
`https://<server>/plugin/inventree-mcp`, whose metadata lists the authorize, token, registration and
revocation endpoints under `/plugin/inventree-mcp/oauth/`. These are thin subclasses of
django-oauth-toolkit's own views; clients register dynamically (RFC 7591) or identify with a client ID
metadata document URL from a trusted host. Tokens are ordinary InvenTree OAuth2 tokens (visible in
the admin under *Django OAuth Toolkit*). InvenTree's own `/o/` endpoints are not changed.

### InvenTree 1.5.x: reverse-proxy rewrites for discovery

MCP clients look up the authorization-server metadata at the RFC 8414 location
`/.well-known/oauth-authorization-server/plugin/inventree-mcp`. InvenTree 1.5.x does not let plugins
serve anything under `/.well-known/` (it answers with a redirect to the login page, and Claude Code
gives up on the HTML it gets). Newer InvenTree versions (with plugin `/.well-known/` support,
inventree/InvenTree#12698) redirect these paths to the plugin by themselves; on 1.5.x add the
rewrites below to the reverse proxy. While they are missing, the plugin logs a warning
("cannot serve /.well-known/oauth-authorization-server/...") at the first OAuth request.

Caddy (inside the site block, before `reverse_proxy`):

```
rewrite /.well-known/oauth-authorization-server/plugin/inventree-mcp /plugin/inventree-mcp/.well-known/oauth-authorization-server
rewrite /.well-known/openid-configuration/plugin/inventree-mcp /plugin/inventree-mcp/.well-known/openid-configuration
rewrite /.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp* /plugin/inventree-mcp/.well-known/oauth-protected-resource
```

nginx (inside the `server` block that proxies to InvenTree):

```
location = /.well-known/oauth-authorization-server/plugin/inventree-mcp {
    rewrite ^ /plugin/inventree-mcp/.well-known/oauth-authorization-server last;
}
location = /.well-known/openid-configuration/plugin/inventree-mcp {
    rewrite ^ /plugin/inventree-mcp/.well-known/openid-configuration last;
}
location ~ ^/\.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp/?$ {
    rewrite ^ /plugin/inventree-mcp/.well-known/oauth-protected-resource last;
}
```

The first line is the one Claude Code needs. The `openid-configuration` line serves clients that
probe the OpenID form, and the protected-resource line clients that do not follow the
`resource_metadata` URL of the `401` challenge (Claude does); both are harmless otherwise.

The `openid-configuration` document is an alias of the RFC 8414 authorization-server metadata.
It is not a full OpenID Connect discovery document (no `jwks_uri`, `subject_types_supported` or
`id_token_signing_alg_values_supported`; the plugin issues no ID tokens), so a strict OpenID
Connect client rejects it. MCP clients only read the OAuth fields.

## Configuration

Under **Settings > Plugin Settings**:

- **Require Authentication** (`REQUIRE_AUTH`, default `True`): reject unauthenticated requests.
  Only disable for local testing.
- **Read Only** (`MCP_READ_ONLY`, default `True`): block all write actions via MCP, regardless of
  the calling user's permissions. A plugin-wide kill switch, independent of per-user roles.
- **Log Tool Calls** (`MCP_LOG_TOOL_CALLS`, default `False`): log every tool call for debugging.

OAuth sign-in (see [Connect with OAuth](#connect-with-oauth)):

| Setting | Default | Meaning |
|---|---|---|
| `OAUTH_ENABLED` | `False` | Serve OAuth discovery and the OAuth endpoints. Off: the endpoints answer 404 and the 401 challenge carries no `resource_metadata`. InvenTree 1.5.x also needs the [proxy rewrites](#inventree-15x-reverse-proxy-rewrites-for-discovery). |
| `TRUSTED_CLIENT_HOSTS` | `claude.ai,claude.com,.anthropic.com` | Hosts whose client ID metadata documents (`https` client IDs) are accepted, Django `ALLOWED_HOSTS` syntax. |
| `TRUSTED_CLIENT_ORIGINS` | `https://claude.ai,https://claude.com` | Browser origins allowed to call register / token / revoke (CORS). |
| `AUTO_APPROVE_TRUSTED_CLIENTS` | `True` | Skip consent for trusted-host clients, never for loopback redirect URIs. |
| `ACCESS_TOKEN_LIFETIME_SECONDS` | `3600` | Access token lifetime. |
| `REFRESH_TOKEN_LIFETIME_DAYS` | `30` | Refresh token idle lifetime (rotated on every use). |
| `AUTH_CODE_LIFETIME_SECONDS` | `600` | Authorization code lifetime. |
| `ALLOW_LOCALHOST_LOOPBACK` | `True` | Accept `http://localhost` redirect URIs on any port, like `127.0.0.1`. |
| `REGISTER_RATE_LIMIT_PER_MINUTE` | `30` | Client registrations per minute and IP (`0` disables). |
| `UNUSED_CLIENT_RETENTION_DAYS` | `90` | Daily cleanup of registered clients without a live token. |
| `OAUTH_FORCE_SSO` | `True` | Send the browser straight to the SSO provider when it has no InvenTree session. |
| `SSO_PROVIDER_ID` | empty | allauth provider id to sign in with; empty selects the single configured `openid_connect` provider. |

The lifetime settings are injected into django-oauth-toolkit at runtime and therefore apply to
**every** OAuth2 application on the instance while `OAUTH_ENABLED` is on (InvenTree has no
configuration hook for them). Every InvenTree process (web workers, background workers) applies them
at start-up and picks up changes within a few seconds. Turning `OAUTH_ENABLED` off restores
InvenTree's defaults.

## Authentication

Access follows the calling user's normal InvenTree role assignments. Supported auth methods:

- An InvenTree API token: `Authorization: Token <token>`.
- Basic auth (username/password).
- An OAuth2 bearer token: `Authorization: Bearer <token>`, from the OAuth sign-in above or any other
  InvenTree OAuth2 application. A scoped token (e.g. `r:view:part`) narrows access *below* the
  underlying user's roles - useful for issuing an agent a tightly-scoped token without creating a
  separate low-privilege user.

Session/cookie auth is not supported (not meaningful for a machine client).
