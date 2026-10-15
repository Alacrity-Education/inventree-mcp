import {
  checkPluginVersion,
  type InvenTreePluginContext
} from '@inventreedb/ui';
import {
  Accordion,
  Alert,
  Badge,
  Grid,
  Loader,
  LoadingOverlay,
  Group as MGroup,
  NavLink,
  ScrollArea,
  Stack,
  Table,
  Text,
  Title,
  Tooltip
} from '@mantine/core';
import { IconKeyOff } from '@tabler/icons-react';
import { useCallback, useRef, useState } from 'react';
import {
  type Api,
  type GroupDetail,
  type GroupMember,
  getEffective,
  getGroup,
  listConnections,
  patchCoreRuleSet,
  type RuleSet,
  revokeGroupTokens
} from './lib/api';
import {
  ApiErrorAlert,
  ConfirmButton,
  notifyError,
  notifyOk
} from './lib/Feedback';
import { McpRolesEditor } from './lib/McpRolesEditor';
import { RoleGrid } from './lib/RoleGrid';
import { summarizeRoles } from './lib/summary';
import { useAsync } from './lib/useAsync';
import { useGroups } from './lib/useGroups';
import { useVisible } from './lib/useVisible';

/** One member; effective MCP access and connection count load when the row is first visible. */
function MemberRow({
  api,
  member
}: Readonly<{ api: Api; member: GroupMember }>) {
  const ref = useRef<HTMLTableRowElement>(null);
  const visible = useVisible(ref);
  const effective = useAsync(
    () => getEffective(api, member.pk),
    [api, member.pk],
    visible
  );
  const conns = useAsync(
    () => listConnections(api, member.pk),
    [api, member.pk],
    visible
  );

  let access: React.ReactNode;
  if (effective.error) {
    access = (
      <Text size='sm' c='dimmed'>
        n/a
      </Text>
    );
  } else if (!effective.data) {
    access = <Loader size='xs' />;
  } else {
    const { count, lines } = summarizeRoles(effective.data.effective);
    access = (
      <MGroup gap='xs'>
        <Tooltip
          disabled={!count}
          label={
            <Stack gap={0}>
              {lines.map((l) => (
                <Text key={l} size='xs'>
                  {l}
                </Text>
              ))}
            </Stack>
          }
        >
          <Badge color={count ? 'blue' : 'gray'} variant='light'>
            {effective.data.superuser ? 'superuser, ' : ''}
            {count} {count === 1 ? 'role' : 'roles'}
          </Badge>
        </Tooltip>
        {!effective.data.may_connect && (
          <Badge color='red' variant='light'>
            cannot connect
            {effective.data.reason ? `: ${effective.data.reason}` : ''}
          </Badge>
        )}
      </MGroup>
    );
  }

  return (
    <Table.Tr ref={ref}>
      <Table.Td>{member.username}</Table.Td>
      <Table.Td>{access}</Table.Td>
      <Table.Td>
        {conns.error ? (
          'n/a'
        ) : conns.data ? (
          conns.data.length
        ) : (
          <Loader size='xs' />
        )}
      </Table.Td>
    </Table.Tr>
  );
}

function MembersTable({
  api,
  members
}: Readonly<{ api: Api; members: GroupMember[] }>) {
  if (!members.length)
    return <Text c='dimmed'>This group has no members.</Text>;
  return (
    <Table striped withTableBorder withColumnBorders>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Username</Table.Th>
          <Table.Th>Effective MCP access</Table.Th>
          <Table.Th>Connections</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {members.map((m) => (
          <MemberRow key={m.pk} api={api} member={m} />
        ))}
      </Table.Tbody>
    </Table>
  );
}

function GroupPane({ api, group }: Readonly<{ api: Api; group: number }>) {
  const detail = useAsync<GroupDetail>(
    () => getGroup(api, group),
    [api, group]
  );
  // bumped to reload the Members table (effective access, connection counts)
  const [membersKey, setMembersKey] = useState(0);
  const refreshMembers = useCallback(() => setMembersKey((k) => k + 1), []);

  const saveCore = useCallback(
    async (rows: RuleSet[]) => {
      try {
        for (const row of rows) await patchCoreRuleSet(api, row);
        notifyOk('Updated', 'Group roles updated');
        detail.reload();
        refreshMembers();
      } catch (error) {
        notifyError('Saving roles failed', error);
        throw error;
      }
    },
    [api, detail.reload, refreshMembers]
  );

  const revoke = useCallback(async () => {
    try {
      const r = await revokeGroupTokens(api, group);
      notifyOk(
        r.queued ? 'Queued' : 'Revoked',
        r.queued
          ? `Revocation queued for ${r.users} members`
          : `Revoked ${r.tokens} token(s) of ${r.users} user(s)`
      );
      refreshMembers();
    } catch (error) {
      notifyError('Revoking MCP tokens failed', error);
    }
  }, [api, group, refreshMembers]);

  if (detail.error)
    return <ApiErrorAlert error={detail.error} plugin={false} />;
  if (!detail.data) return <LoadingOverlay visible />;

  const g = detail.data;
  return (
    <Stack>
      <MGroup justify='space-between'>
        <Title order={3}>{g.name}</Title>
        <ConfirmButton
          label='Revoke MCP tokens for this group'
          color='red'
          variant='outline'
          leftSection={<IconKeyOff size={16} />}
          message={`Revoke all MCP connections of every member of "${g.name}"? They must reconnect.`}
          confirmLabel='Revoke'
          onConfirm={revoke}
        />
      </MGroup>
      <Accordion multiple defaultValue={['roles', 'mcp', 'members']}>
        <Accordion.Item value='roles'>
          <Accordion.Control>
            <Title order={5}>Roles</Title>
          </Accordion.Control>
          <Accordion.Panel>
            <RoleGrid rows={g.roles} editable onSave={saveCore} />
          </Accordion.Panel>
        </Accordion.Item>
        <Accordion.Item value='mcp'>
          <Accordion.Control>
            <Title order={5}>MCP Roles</Title>
          </Accordion.Control>
          <Accordion.Panel>
            <McpRolesEditor
              api={api}
              group={g.pk}
              interactiveRows={g.roles}
              onChanged={refreshMembers}
            />
          </Accordion.Panel>
        </Accordion.Item>
        <Accordion.Item value='members'>
          <Accordion.Control>
            <Title order={5}>Members</Title>
          </Accordion.Control>
          <Accordion.Panel>
            <MembersTable key={membersKey} api={api} members={g.users} />
          </Accordion.Panel>
        </Accordion.Item>
      </Accordion>
    </Stack>
  );
}

function GroupsPage({
  context
}: Readonly<{ context: InvenTreePluginContext }>) {
  const api = context.api;
  const groups = useGroups(api);
  const [selected, setSelected] = useState<number>();

  if (!context.user.isStaff()) {
    return (
      <Alert color='red' title='Not allowed'>
        MCP Access administration requires a staff account.
      </Alert>
    );
  }

  return (
    <Stack p='md'>
      <Title order={2}>MCP Access</Title>
      <Text c='dimmed'>
        A member's MCP access is their interactive roles (across all their
        groups) intersected with the MCP roles of their groups, per table. A
        warning mark on an MCP flag means this group grants no interactive
        access for it: the flag takes effect only for members who hold the role
        through another group, or for tables shared with another role.
      </Text>
      {!!groups.error && <ApiErrorAlert error={groups.error} plugin={false} />}
      <Grid gap='lg'>
        <Grid.Col span={{ base: 12, md: 3 }}>
          <ScrollArea h={600} type='auto'>
            {groups.loading && <Loader size='sm' />}
            {groups.data?.map((g) => (
              <NavLink
                key={g.pk}
                label={g.name}
                active={g.pk === selected}
                onClick={() => setSelected(g.pk)}
              />
            ))}
          </ScrollArea>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 9 }}>
          <Stack pos='relative'>
            {selected === undefined ? (
              <Text c='dimmed'>Select a group.</Text>
            ) : (
              <GroupPane key={selected} api={api} group={selected} />
            )}
          </Stack>
        </Grid.Col>
      </Grid>
    </Stack>
  );
}

/** Entry point called by InvenTree for the `groups` route. */
export function renderGroupsPage(context: InvenTreePluginContext) {
  checkPluginVersion(context);
  return <GroupsPage context={context} />;
}
