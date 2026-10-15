import {
  checkPluginVersion,
  type InvenTreePluginContext
} from '@inventreedb/ui';
import { Alert, LoadingOverlay, Stack, Title } from '@mantine/core';
import { getGroup } from './lib/api';
import { ApiErrorAlert } from './lib/Feedback';
import { McpRolesEditor } from './lib/McpRolesEditor';
import { useAsync } from './lib/useAsync';

function MCPRolesPanel({
  context
}: Readonly<{ context: InvenTreePluginContext }>) {
  const group = Number(context.id);
  const valid = Number.isInteger(group) && group > 0;
  const detail = useAsync(
    () => getGroup(context.api, group),
    [context.api, group],
    valid
  );

  if (!valid) {
    return (
      <Alert color='yellow' title='No group'>
        This panel needs a group id in the plugin context.
      </Alert>
    );
  }
  if (detail.error)
    return <ApiErrorAlert error={detail.error} plugin={false} />;

  return (
    <Stack pos='relative'>
      <LoadingOverlay visible={detail.loading && !detail.data} />
      <Title order={5}>MCP Roles</Title>
      {detail.data && (
        <McpRolesEditor
          api={context.api}
          group={group}
          interactiveRows={detail.data.roles}
        />
      )}
    </Stack>
  );
}

/** Group panel for the future core sidebar injection (context.id = group pk). */
export function renderMCPRolesPanel(context: InvenTreePluginContext) {
  checkPluginVersion(context);
  return <MCPRolesPanel context={context} />;
}
