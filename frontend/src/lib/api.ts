import type { InvenTreePluginContext } from '@inventreedb/ui';

/** The authenticated axios instance handed to plugins by InvenTree. */
export type Api = InvenTreePluginContext['api'];

export const PLUGIN_SLUG = 'inventree-mcp';
const PLUGIN_BASE = `/plugin/${PLUGIN_SLUG}/`;
const CORE_BASE = '/api/';

export type Flag = 'can_view' | 'can_add' | 'can_change' | 'can_delete';
export const FLAGS: Flag[] = [
  'can_view',
  'can_change',
  'can_add',
  'can_delete'
];

/** Core `RuleSetSerializer` shape, also used by the plugin's MCPRuleSet API. */
export interface RuleSet {
  pk?: number;
  group?: number;
  name: string;
  label: string;
  can_view: boolean;
  can_add: boolean;
  can_change: boolean;
  can_delete: boolean;
  edited?: boolean;
}

export interface Group {
  pk: number;
  name: string;
}

export interface GroupMember {
  pk: number;
  username: string;
  first_name?: string;
  last_name?: string;
}

export interface GroupDetail extends Group {
  roles: RuleSet[];
  users: GroupMember[];
}

export interface RoleFlags {
  /** Display name from the API (core's RuleSet label). */
  label?: string;
  view: boolean;
  add: boolean;
  change: boolean;
  delete: boolean;
}
export type RoleMap = Record<string, RoleFlags>;

/** `GET api/effective/<user_pk>/` */
export interface Effective {
  user: number;
  username: string;
  superuser: boolean;
  roles: RoleMap;
  mcp_roles: RoleMap;
  effective: RoleMap;
  scopes: string[];
  may_connect: boolean;
  reason: string;
}

/** `GET api/connections/` */
export interface Connection {
  pk: number;
  client_name: string;
  client_host?: string;
  client_trusted: boolean;
  created: string;
  expires: string | null;
  access_expires?: string | null;
  scopes: string[];
  last_used?: string | null;
}

/** `GET api/status/` */
export interface McpStatus {
  oauth_enabled: boolean;
  mcp_url: string;
  membership: {
    checked_at: string | null;
    blocked: boolean;
    reason: string;
  };
  scopes: string[];
  may_connect: boolean;
  reason: string;
}

export interface RevokeResult {
  users: number;
  tokens: number;
  /** True above the synchronous limit: the revocation is still pending and `tokens` is the count at queueing time. */
  queued?: boolean;
}

/** Accept both plain arrays and DRF paginated `{results: []}` payloads. */
function asList<T>(data: any): T[] {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.results)) return data.results;
  return [];
}

function flagsOf(row: RuleSet) {
  return {
    can_view: row.can_view,
    can_add: row.can_add,
    can_change: row.can_change,
    can_delete: row.can_delete
  };
}

// ---- Error helpers ------------------------------------------------------

export function httpStatus(error: unknown): number | undefined {
  return (error as any)?.response?.status;
}

/** The plugin API answers 404 when the plugin API is not reachable (plugin or URL integration off, older plugin). */
export function isNotWired(error: unknown): boolean {
  return httpStatus(error) === 404;
}

export function errorMessage(error: unknown): string {
  const data = (error as any)?.response?.data;
  const detail = data?.detail ?? data?.error ?? data?.message;
  if (typeof detail === 'string') return detail;
  const status = httpStatus(error);
  const msg = (error as any)?.message ?? 'Unknown error';
  return status ? `${msg} (HTTP ${status})` : String(msg);
}

// ---- Core endpoints -----------------------------------------------------

export async function listGroups(api: Api): Promise<Group[]> {
  const r = await api.get(`${CORE_BASE}user/group/`);
  return asList<Group>(r.data).sort((a, b) => a.name.localeCompare(b.name));
}

export async function getGroup(api: Api, pk: number): Promise<GroupDetail> {
  const r = await api.get(`${CORE_BASE}user/group/${pk}/`, {
    params: { role_detail: true, user_detail: true }
  });
  return {
    pk: r.data.pk,
    name: r.data.name,
    roles: r.data.roles ?? [],
    users: r.data.users ?? []
  };
}

export async function patchCoreRuleSet(api: Api, row: RuleSet): Promise<void> {
  await api.patch(`${CORE_BASE}user/ruleset/${row.pk}/`, flagsOf(row));
}

export async function getCurrentUserPk(
  api: Api,
  context?: Pick<InvenTreePluginContext, 'user'>
): Promise<number> {
  const fromContext = context?.user?.userId?.();
  if (typeof fromContext === 'number') return fromContext;
  const r = await api.get(`${CORE_BASE}user/me/`);
  return r.data.pk;
}

// ---- Plugin endpoints (all under /plugin/inventree-mcp/) -----------------

export async function listMcpRuleSets(
  api: Api,
  group: number
): Promise<RuleSet[]> {
  const r = await api.get(`${PLUGIN_BASE}api/rulesets/`, { params: { group } });
  return asList<RuleSet>(r.data);
}

export async function patchMcpRuleSet(api: Api, row: RuleSet): Promise<void> {
  await api.patch(`${PLUGIN_BASE}api/rulesets/${row.pk}/`, flagsOf(row));
}

export async function copyRuleSets(api: Api, group: number): Promise<void> {
  await api.post(`${PLUGIN_BASE}api/rulesets/copy/`, { group });
}

export async function clearRuleSets(api: Api, group: number): Promise<void> {
  await api.post(`${PLUGIN_BASE}api/rulesets/clear/`, { group });
}

export async function revokeGroupTokens(
  api: Api,
  group: number
): Promise<RevokeResult> {
  const r = await api.post(`${PLUGIN_BASE}api/rulesets/revoke/`, { group });
  return r.data;
}

export async function getEffective(api: Api, user: number): Promise<Effective> {
  const r = await api.get(`${PLUGIN_BASE}api/effective/${user}/`);
  return r.data;
}

/** Own connections; staff may pass `user` to list somebody else's. */
export async function listConnections(
  api: Api,
  user?: number
): Promise<Connection[]> {
  const r = await api.get(`${PLUGIN_BASE}api/connections/`, {
    params: user === undefined ? undefined : { user }
  });
  return asList<Connection>(r.data);
}

export async function revokeConnection(api: Api, pk: number): Promise<void> {
  await api.delete(`${PLUGIN_BASE}api/connections/${pk}/`);
}

export async function getStatus(api: Api): Promise<McpStatus> {
  const r = await api.get(`${PLUGIN_BASE}api/status/`);
  return r.data;
}

// ---- Conversions --------------------------------------------------------

export function humanize(name: string): string {
  return name
    .split('_')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

/** Turn an `{name: {view, add, change, delete}}` map into grid rows. */
export function roleMapToRows(
  map: RoleMap | undefined,
  labels: Record<string, string> = {}
): RuleSet[] {
  return Object.entries(map ?? {}).map(([name, f], i) => ({
    pk: i + 1,
    name,
    label: labels[name] ?? f.label ?? humanize(name),
    can_view: !!f.view,
    can_add: !!f.add,
    can_change: !!f.change,
    can_delete: !!f.delete
  }));
}
