import { MantineProvider } from '@mantine/core';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { RuleSet } from './api';
import { RoleGrid, SHADOW_TOOLTIP } from './RoleGrid';

const rows: RuleSet[] = [
  {
    pk: 2,
    name: 'stock',
    label: 'Stock',
    can_view: true,
    can_add: false,
    can_change: false,
    can_delete: false
  },
  {
    pk: 1,
    name: 'part',
    label: 'Part',
    can_view: true,
    can_add: false,
    can_change: false,
    can_delete: false
  }
];

const wrap = (ui: React.ReactElement) =>
  render(<MantineProvider>{ui}</MantineProvider>);

describe('RoleGrid', () => {
  it('renders rows sorted by label', () => {
    wrap(<RoleGrid rows={rows} editable />);
    const labels = screen
      .getAllByRole('row')
      .slice(1)
      .map((r) => r.textContent);
    expect(labels[0]).toContain('Part');
    expect(labels[1]).toContain('Stock');
  });

  it('toggles a cell and passes the edited row to onSave', async () => {
    const onSave = vi.fn();
    wrap(<RoleGrid rows={rows} editable onSave={onSave} />);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Part can_change' }));
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const saved = onSave.mock.calls[0][0] as RuleSet[];
    expect(saved).toHaveLength(1);
    expect(saved[0]).toMatchObject({
      pk: 1,
      name: 'part',
      can_view: true,
      can_change: true
    });
  });

  it('does not allow edits when read-only', () => {
    wrap(<RoleGrid rows={rows} />);
    expect(
      screen
        .getByRole('checkbox', { name: 'Part can_view' })
        .hasAttribute('disabled')
    ).toBe(true);
    expect(screen.queryByText('Save')).toBeNull();
  });

  it('flags cells granted in MCP but not interactively', () => {
    const interactive = rows.map((r) =>
      r.name === 'stock' ? { ...r, can_view: false } : r
    );
    wrap(<RoleGrid rows={rows} shadowRows={interactive} />);
    const flags = screen.getAllByTestId('shadow-flag');
    expect(flags).toHaveLength(1);
    expect(flags[0].getAttribute('aria-label')).toBe(SHADOW_TOOLTIP);
  });
});
