import { Group, LoadingOverlay, Stack } from '@mantine/core';
import { IconCopy, IconEraser } from '@tabler/icons-react';
import { useCallback } from 'react';
import {
  type Api,
  clearRuleSets,
  copyRuleSets,
  listMcpRuleSets,
  patchMcpRuleSet,
  type RuleSet
} from './api';
import {
  ApiErrorAlert,
  ConfirmButton,
  notifyError,
  notifyOk
} from './Feedback';
import { RoleGrid } from './RoleGrid';
import { useAsync } from './useAsync';

/**
 * "MCP Roles" of one group: editable grid saving to the plugin API, with the
 * Copy from roles / Clear buttons. `interactiveRows` (the group's core roles)
 * drives the "no effect" flags.
 */
export function McpRolesEditor({
  api,
  group,
  interactiveRows,
  onChanged
}: Readonly<{
  api: Api;
  group: number;
  interactiveRows?: RuleSet[];
  onChanged?: () => void;
}>) {
  const rules = useAsync(() => listMcpRuleSets(api, group), [api, group]);

  const save = useCallback(
    async (rows: RuleSet[]) => {
      try {
        for (const row of rows) await patchMcpRuleSet(api, row);
        notifyOk('Updated', 'MCP roles updated');
        rules.reload();
        onChanged?.();
      } catch (error) {
        notifyError('Saving MCP roles failed', error);
        throw error;
      }
    },
    [api, rules.reload, onChanged]
  );

  const bulk = useCallback(
    (action: (api: Api, group: number) => Promise<void>, done: string) =>
      async () => {
        try {
          await action(api, group);
          notifyOk('Updated', done);
          rules.reload();
          onChanged?.();
        } catch (error) {
          notifyError('Request failed', error);
        }
      },
    [api, group, rules.reload, onChanged]
  );

  if (rules.error) return <ApiErrorAlert error={rules.error} />;

  return (
    <Stack gap='sm' pos='relative'>
      <LoadingOverlay visible={rules.loading && !rules.data} />
      <Group>
        <ConfirmButton
          label='Copy from roles'
          color='blue'
          variant='light'
          leftSection={<IconCopy size={16} />}
          message="Replace this group's MCP roles with a copy of its interactive roles?"
          confirmLabel='Copy'
          onConfirm={bulk(
            copyRuleSets,
            'MCP roles copied from interactive roles'
          )}
        />
        <ConfirmButton
          label='Clear'
          color='red'
          variant='light'
          leftSection={<IconEraser size={16} />}
          message="Remove all MCP permissions of this group? Members' MCP tokens lose access."
          confirmLabel='Clear'
          onConfirm={bulk(clearRuleSets, 'MCP roles cleared')}
        />
      </Group>
      <RoleGrid
        rows={rules.data ?? []}
        editable
        onSave={save}
        shadowRows={interactiveRows}
      />
    </Stack>
  );
}
