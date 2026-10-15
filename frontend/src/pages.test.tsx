import { MantineProvider } from '@mantine/core';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@inventreedb/ui', () => ({ checkPluginVersion: () => undefined }));
const show = vi.hoisted(() => vi.fn());
vi.mock('@mantine/notifications', () => ({ notifications: { show } }));

import { renderConnectionsPage } from './Connections';
import { renderGroupsPage } from './Groups';

const CORE = '/api/user/ruleset/11/';
const PLUGIN = '/plugin/inventree-mcp/api/rulesets/21/';

function rule(pk: number, can_view: boolean) {
  return {
    pk,
    name: 'part',
    label: 'Parts',
    can_view,
    can_add: false,
    can_change: false,
    can_delete: false
  };
}

/** A minimal `context.api`: routes by URL, records every call. */
function makeApi(routes: Record<string, (config?: any) => unknown>) {
  const handler = (method: string) =>
    vi.fn(async (url: string, ...rest: any[]) => {
      const fn = routes[`${method} ${url}`];
      if (!fn) throw new Error(`unexpected ${method} ${url}`);
      return { data: fn(rest[rest.length - 1]) };
    });
  return {
    get: handler('GET'),
    post: handler('POST'),
    patch: handler('PATCH'),
    delete: handler('DELETE')
  };
}

const wrap = (ui: React.ReactElement) =>
  render(<MantineProvider>{ui}</MantineProvider>);

const context = (api: unknown, extra: object = {}) =>
  ({
    api,
    user: { isStaff: () => true, userId: () => 5 },
    ...extra
  }) as any;

beforeEach(() => show.mockReset());

describe('Groups page save paths', () => {
  const routes = () => ({
    'GET /api/user/group/': () => [{ pk: 3, name: 'MCP Users' }],
    'GET /api/user/group/3/': () => ({
      pk: 3,
      name: 'MCP Users',
      roles: [rule(11, true)],
      users: []
    }),
    'GET /plugin/inventree-mcp/api/rulesets/': () => [rule(21, true)],
    [`PATCH ${CORE}`]: () => ({}),
    [`PATCH ${PLUGIN}`]: () => ({})
  });

  async function open(api: ReturnType<typeof makeApi>) {
    wrap(renderGroupsPage(context(api)));
    fireEvent.click(await screen.findByText('MCP Users'));
    await screen.findByText('Revoke MCP tokens for this group');
    await waitFor(() =>
      expect(
        screen.getAllByRole('checkbox', { name: 'Parts can_change' })
      ).toHaveLength(2)
    );
  }

  it('saves the interactive grid through the core ruleset endpoint', async () => {
    const api = makeApi(routes());
    await open(api);
    const [core] = screen.getAllByRole('checkbox', {
      name: 'Parts can_change'
    });
    fireEvent.click(core);
    fireEvent.click(screen.getAllByText('Save')[0]);
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(api.patch).toHaveBeenCalledWith(CORE, {
      can_view: true,
      can_add: false,
      can_change: true,
      can_delete: false
    });
  });

  it('saves the MCP grid through the plugin ruleset endpoint', async () => {
    const api = makeApi(routes());
    await open(api);
    const mcp = screen.getAllByRole('checkbox', {
      name: 'Parts can_change'
    })[1];
    fireEvent.click(mcp);
    fireEvent.click(screen.getAllByText('Save')[1]);
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(api.patch).toHaveBeenCalledWith(PLUGIN, {
      can_view: true,
      can_add: false,
      can_change: true,
      can_delete: false
    });
  });

  it('reports a queued revocation instead of a token count', async () => {
    const api = makeApi({
      ...routes(),
      'POST /plugin/inventree-mcp/api/rulesets/revoke/': () => ({
        users: 25,
        tokens: 40,
        queued: true
      })
    });
    await open(api);
    fireEvent.click(screen.getByText('Revoke MCP tokens for this group'));
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }));
    await waitFor(() => expect(show).toHaveBeenCalled());
    expect(show.mock.calls[0][0].message).toBe(
      'Revocation queued for 25 members'
    );
  });

  it('reports a synchronous revocation with the token count', async () => {
    const api = makeApi({
      ...routes(),
      'POST /plugin/inventree-mcp/api/rulesets/revoke/': () => ({
        users: 2,
        tokens: 3,
        queued: false
      })
    });
    await open(api);
    fireEvent.click(screen.getByText('Revoke MCP tokens for this group'));
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }));
    await waitFor(() => expect(show).toHaveBeenCalled());
    expect(show.mock.calls[0][0].message).toBe(
      'Revoked 3 token(s) of 2 user(s)'
    );
  });
});

describe('Connections page', () => {
  const status = (oauth: boolean) => ({
    oauth_enabled: oauth,
    mcp_url: 'https://inv.example/plugin/inventree-mcp/mcp/',
    membership: { checked_at: null, blocked: false, reason: '' },
    scopes: [],
    may_connect: true,
    reason: ''
  });
  const connection = {
    pk: 42,
    client_name: 'Claude',
    client_trusted: false,
    created: '2026-01-01T00:00:00Z',
    expires: null,
    scopes: ['g:read']
  };
  const base = (oauth: boolean, list: () => unknown[]) => ({
    'GET /plugin/inventree-mcp/api/status/': () => status(oauth),
    'GET /plugin/inventree-mcp/api/effective/5/': () => ({ effective: {} }),
    'GET /plugin/inventree-mcp/api/connections/': list
  });

  it('confirms, DELETEs the connection and reloads the list', async () => {
    let live = [connection];
    const api = makeApi({
      ...base(true, () => live),
      'DELETE /plugin/inventree-mcp/api/connections/42/': () => {
        live = [];
        return {};
      }
    });
    wrap(renderConnectionsPage(context(api)));
    const row = (await screen.findByText('Claude')).closest(
      'tr'
    ) as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'Revoke' }));
    // nothing is deleted until the modal is confirmed
    expect(api.delete).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke' }));
    await waitFor(() =>
      expect(api.delete).toHaveBeenCalledWith(
        '/plugin/inventree-mcp/api/connections/42/'
      )
    );
    await screen.findByText('No MCP clients are connected.');
    const listCalls = api.get.mock.calls.filter(
      (c) => c[0] === '/plugin/inventree-mcp/api/connections/'
    );
    expect(listCalls.length).toBeGreaterThanOrEqual(2);
  });

  it('shows the OAuth command once the status is known', async () => {
    const api = makeApi(base(true, () => []));
    wrap(renderConnectionsPage(context(api)));
    expect(screen.queryByText('Connect')).toBeNull();
    await screen.findByText('Connect');
    expect(
      screen.getByText(
        'claude mcp add --transport http inventree https://inv.example/plugin/inventree-mcp/mcp/'
      )
    ).toBeTruthy();
  });

  it('shows the API-token command when OAuth is disabled', async () => {
    const api = makeApi(base(false, () => []));
    wrap(renderConnectionsPage(context(api)));
    const cmd = await screen.findByText(
      /--header "Authorization: Token <your-api-token>"/
    );
    expect(cmd.textContent).toContain(
      'https://inv.example/plugin/inventree-mcp/mcp/'
    );
    expect(screen.getByText(/OAuth sign-in is off/)).toBeTruthy();
    expect(screen.queryByText('claude.ai')).toBeNull();
  });
});
