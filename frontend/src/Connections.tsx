import {
  checkPluginVersion,
  type InvenTreePluginContext
} from '@inventreedb/ui';
import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  CopyButton,
  Group,
  List,
  Loader,
  Stack,
  Table,
  Text,
  Title,
  Tooltip
} from '@mantine/core';
import { IconCheck, IconCopy, IconPlugConnected } from '@tabler/icons-react';
import {
  type Connection,
  getCurrentUserPk,
  getEffective,
  getStatus,
  listConnections,
  type McpStatus,
  revokeConnection,
  roleMapToRows
} from './lib/api';
import {
  ApiErrorAlert,
  ConfirmButton,
  notifyError,
  notifyOk
} from './lib/Feedback';
import { RoleGrid } from './lib/RoleGrid';
import { useAsync } from './lib/useAsync';

function fmt(value?: string | null): string {
  if (!value) return '-';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

function Row({
  label,
  children
}: Readonly<{ label: string; children: React.ReactNode }>) {
  return (
    <Group gap='sm'>
      <Text fw={600} w={170}>
        {label}
      </Text>
      {children}
    </Group>
  );
}

function StatusCard({ status }: Readonly<{ status: McpStatus }>) {
  const m = status.membership;
  return (
    <Card withBorder>
      <Stack gap='xs'>
        <Title order={4}>Status</Title>
        <Row label='MCP OAuth'>
          <Badge color={status.oauth_enabled ? 'green' : 'gray'}>
            {status.oauth_enabled ? 'enabled' : 'disabled'}
          </Badge>
        </Row>
        <Row label='You can connect'>
          <Badge color={status.may_connect ? 'green' : 'red'}>
            {status.may_connect ? 'yes' : 'no'}
          </Badge>
          {!status.may_connect && status.reason && (
            <Text size='sm'>{status.reason}</Text>
          )}
        </Row>
        <Row label='Membership check'>
          <Text size='sm'>{fmt(m?.checked_at)}</Text>
          {m?.blocked && (
            <Badge color='red'>blocked{m.reason ? `: ${m.reason}` : ''}</Badge>
          )}
        </Row>
      </Stack>
    </Card>
  );
}

function PermissionsCard({
  context
}: Readonly<{ context: InvenTreePluginContext }>) {
  const eff = useAsync(async () => {
    const pk = await getCurrentUserPk(context.api, context);
    return getEffective(context.api, pk);
  }, [context.api]);

  return (
    <Card withBorder>
      <Stack gap='xs'>
        <Title order={4}>Your MCP permissions</Title>
        <Text size='sm' c='dimmed'>
          What an MCP client can do on your behalf: your interactive roles
          intersected with the MCP roles of your groups.
        </Text>
        {eff.error ? (
          <ApiErrorAlert error={eff.error} />
        ) : eff.data ? (
          <RoleGrid rows={roleMapToRows(eff.data.effective)} editable={false} />
        ) : (
          <Loader size='sm' />
        )}
      </Stack>
    </Card>
  );
}

function ConnectionsCard({
  context
}: Readonly<{ context: InvenTreePluginContext }>) {
  const conns = useAsync(() => listConnections(context.api), [context.api]);

  const revoke = async (c: Connection) => {
    try {
      await revokeConnection(context.api, c.pk);
      notifyOk('Revoked', `Connection "${c.client_name}" revoked`);
      conns.reload();
    } catch (error) {
      notifyError('Revoking the connection failed', error);
    }
  };

  return (
    <Card withBorder>
      <Stack gap='xs'>
        <Title order={4}>Connections</Title>
        {conns.error ? (
          <ApiErrorAlert error={conns.error} />
        ) : !conns.data ? (
          <Loader size='sm' />
        ) : conns.data.length === 0 ? (
          <Text c='dimmed'>No MCP clients are connected.</Text>
        ) : (
          <Table striped withTableBorder>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Client</Table.Th>
                <Table.Th>Created</Table.Th>
                <Table.Th>Expires</Table.Th>
                <Table.Th>Scopes</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {conns.data.map((c) => (
                <Table.Tr key={c.pk}>
                  <Table.Td>
                    <Group gap='xs'>
                      <Text>{c.client_name}</Text>
                      {c.client_trusted && (
                        <Badge color='green' variant='light'>
                          trusted
                        </Badge>
                      )}
                    </Group>
                    {c.client_host && (
                      <Text size='xs' c='dimmed'>
                        {c.client_host}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>{fmt(c.created)}</Table.Td>
                  <Table.Td>{fmt(c.expires)}</Table.Td>
                  <Table.Td>
                    <Tooltip
                      label={(c.scopes ?? []).join(' ')}
                      multiline
                      maw={400}
                    >
                      <Badge variant='light'>
                        {(c.scopes ?? []).length} scopes
                      </Badge>
                    </Tooltip>
                  </Table.Td>
                  <Table.Td>
                    <ConfirmButton
                      label='Revoke'
                      size='xs'
                      variant='light'
                      message={`Revoke the connection of "${c.client_name}"? The client must reconnect.`}
                      confirmLabel='Revoke'
                      onConfirm={() => revoke(c)}
                    />
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Stack>
    </Card>
  );
}

function ConnectCard({ url }: Readonly<{ url: string }>) {
  const command = `claude mcp add --transport http inventree ${url}`;
  const copy = (value: string, label: string) => (
    <CopyButton value={value}>
      {({ copied, copy: doCopy }) => (
        <Button
          size='xs'
          variant='light'
          onClick={doCopy}
          leftSection={
            copied ? <IconCheck size={14} /> : <IconCopy size={14} />
          }
        >
          {copied ? 'Copied' : label}
        </Button>
      )}
    </CopyButton>
  );

  return (
    <Card withBorder>
      <Stack gap='xs'>
        <Title order={4}>Connect</Title>
        <Text fw={600}>MCP URL</Text>
        <Group gap='sm'>
          <Code>{url}</Code>
          {copy(url, 'Copy URL')}
        </Group>
        <Text fw={600}>Claude Code</Text>
        <Group gap='sm'>
          <Code>{command}</Code>
          {copy(command, 'Copy command')}
        </Group>
        <Text fw={600}>claude.ai</Text>
        <List type='ordered' size='sm'>
          <List.Item>
            Open Settings, then Connectors, and choose Add custom connector.
          </List.Item>
          <List.Item>Paste the MCP URL above and save.</List.Item>
          <List.Item>
            Click Connect and sign in to InvenTree when asked.
          </List.Item>
        </List>
      </Stack>
    </Card>
  );
}

function ConnectionsPage({
  context
}: Readonly<{ context: InvenTreePluginContext }>) {
  const status = useAsync(() => getStatus(context.api), [context.api]);
  const url =
    status.data?.mcp_url ||
    `${window.location.origin}/plugin/inventree-mcp/mcp/`;

  return (
    <Stack p='md'>
      <Group gap='xs'>
        <IconPlugConnected />
        <Title order={2}>MCP</Title>
      </Group>
      {status.error ? (
        <ApiErrorAlert error={status.error} />
      ) : status.data ? (
        <StatusCard status={status.data} />
      ) : (
        <Loader size='sm' />
      )}
      <PermissionsCard context={context} />
      <ConnectionsCard context={context} />
      <ConnectCard url={url} />
      {status.data && !status.data.oauth_enabled && (
        <Alert color='gray'>
          OAuth connections are disabled on this server.
        </Alert>
      )}
    </Stack>
  );
}

/** Entry point called by InvenTree for the `connections` route. */
export function renderConnectionsPage(context: InvenTreePluginContext) {
  checkPluginVersion(context);
  return <ConnectionsPage context={context} />;
}
