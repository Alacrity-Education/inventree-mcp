import {
  Button,
  Checkbox,
  Group,
  Stack,
  Table,
  Text,
  Tooltip
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconCircleCheck,
  IconReload
} from '@tabler/icons-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { FLAGS, type Flag, type RuleSet } from './api';

export const SHADOW_TOOLTIP =
  'No effect: MCP access never exceeds interactive access';

const COLUMNS: { flag: Flag; label: string }[] = [
  { flag: 'can_view', label: 'View' },
  { flag: 'can_change', label: 'Change' },
  { flag: 'can_add', label: 'Add' },
  { flag: 'can_delete', label: 'Delete' }
];

export interface RoleGridProps {
  rows: RuleSet[];
  editable?: boolean;
  /** Called with the edited rows when Save is pressed. */
  onSave?: (rows: RuleSet[]) => void | Promise<void>;
  /**
   * Interactive rows to compare against. A cell that is granted in `rows`
   * but not in the matching shadow row (by name) is flagged as having no effect.
   */
  shadowRows?: RuleSet[];
  title?: string;
}

/**
 * Role checkbox grid shared by the interactive and MCP tables. Mirrors the
 * look and Save/Reset behaviour of InvenTree's own RoleTable.
 */
export function RoleGrid({
  rows,
  editable = false,
  onSave,
  shadowRows,
  title
}: Readonly<RoleGridProps>) {
  const [items, setItems] = useState<RuleSet[]>(rows);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setItems(rows);
  }, [rows]);

  const edited = useMemo(() => items.some((r) => r.edited), [items]);

  // Always the same order, without mutating state
  const sorted = useMemo(
    () => [...items].sort((a, b) => a.label.localeCompare(b.label)),
    [items]
  );

  const shadowByName = useMemo(() => {
    if (!shadowRows) return undefined;
    return new Map(shadowRows.map((r) => [r.name, r]));
  }, [shadowRows]);

  const onToggle = useCallback(
    (name: string, flag: Flag) => {
      if (!editable) return;
      setItems((prev) =>
        prev.map((r) =>
          r.name === name ? { ...r, [flag]: !r[flag], edited: true } : r
        )
      );
    },
    [editable]
  );

  const save = useCallback(async () => {
    if (!onSave) return;
    setSaving(true);
    try {
      await onSave(items.filter((r) => r.edited));
      setItems((prev) => prev.map((r) => ({ ...r, edited: false })));
    } catch (error) {
      // The caller reports the failure; keep the edits so they can be retried
      console.error(error);
    } finally {
      setSaving(false);
    }
  }, [onSave, items]);

  const isShadowed = (rule: RuleSet, flag: Flag): boolean => {
    if (!shadowByName || !rule[flag]) return false;
    return !shadowByName.get(rule.name)?.[flag];
  };

  return (
    <Stack gap='xs'>
      {title && <Text fw={600}>{title}</Text>}
      <Table striped withColumnBorders withRowBorders withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>
              <Text fw={700}>Role</Text>
            </Table.Th>
            {COLUMNS.map((c) => (
              <Table.Th key={c.flag}>
                <Text fw={700}>{c.label}</Text>
              </Table.Th>
            ))}
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {sorted.map((rule) => (
            <Table.Tr key={rule.pk ?? rule.name}>
              <Table.Td>
                <Group gap='xs'>
                  <Text>{rule.label}</Text>
                  {rule.edited && <Text>*</Text>}
                </Group>
              </Table.Td>
              {FLAGS.map((flag) => {
                const shadow = isShadowed(rule, flag);
                return (
                  <Table.Td key={flag}>
                    <Group gap={4} wrap='nowrap'>
                      <Checkbox
                        aria-label={`${rule.label} ${flag}`}
                        disabled={!editable}
                        checked={rule[flag]}
                        color={shadow ? 'yellow' : undefined}
                        onChange={() => onToggle(rule.name, flag)}
                      />
                      {shadow && (
                        <Tooltip label={SHADOW_TOOLTIP}>
                          <IconAlertTriangle
                            size={16}
                            color='var(--mantine-color-yellow-6)'
                            aria-label={SHADOW_TOOLTIP}
                            data-testid='shadow-flag'
                          />
                        </Tooltip>
                      )}
                    </Group>
                  </Table.Td>
                );
              })}
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {editable && (
        <Group justify='right'>
          <Button
            color='red'
            disabled={!edited || saving}
            onClick={() => setItems(rows)}
            leftSection={<IconReload size={16} />}
          >
            Reset
          </Button>
          <Button
            color='green'
            disabled={!edited}
            loading={saving}
            onClick={save}
            leftSection={<IconCircleCheck size={16} />}
          >
            Save
          </Button>
        </Group>
      )}
    </Stack>
  );
}
