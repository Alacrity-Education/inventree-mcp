import { describe, expect, it, vi } from 'vitest';
import {
  type Api,
  patchCoreRuleSet,
  patchMcpRuleSet,
  type RuleSet,
  roleMapToRows
} from './api';

describe('roleMapToRows', () => {
  it('uses the API label when present and humanizes the name otherwise', () => {
    const rows = roleMapToRows({
      part: {
        label: 'Parts',
        view: true,
        add: false,
        change: true,
        delete: false
      },
      part_category: { view: true, add: false, change: false, delete: false }
    });
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({
      name: 'part',
      label: 'Parts',
      can_view: true,
      can_change: true,
      can_add: false,
      can_delete: false
    });
    expect(rows[1].label).toBe('Part Category');
  });

  it('lets an explicit label map win and tolerates undefined', () => {
    const rows = roleMapToRows(
      {
        stock: {
          label: 'Stock Items',
          view: true,
          add: false,
          change: false,
          delete: false
        }
      },
      { stock: 'Stock' }
    );
    expect(rows[0].label).toBe('Stock');
    expect(roleMapToRows(undefined)).toEqual([]);
  });
});

describe('save endpoints', () => {
  const row: RuleSet = {
    pk: 7,
    name: 'part',
    label: 'Parts',
    group: 3,
    can_view: true,
    can_add: false,
    can_change: true,
    can_delete: false,
    edited: true
  };
  const flags = {
    can_view: true,
    can_add: false,
    can_change: true,
    can_delete: false
  };

  it('PATCHes the core ruleset endpoint for interactive roles', async () => {
    const api = { patch: vi.fn().mockResolvedValue({}) };
    await patchCoreRuleSet(api as unknown as Api, row);
    expect(api.patch).toHaveBeenCalledWith('/api/user/ruleset/7/', flags);
  });

  it('PATCHes the plugin ruleset endpoint for MCP roles', async () => {
    const api = { patch: vi.fn().mockResolvedValue({}) };
    await patchMcpRuleSet(api as unknown as Api, row);
    expect(api.patch).toHaveBeenCalledWith(
      '/plugin/inventree-mcp/api/rulesets/7/',
      flags
    );
  });
});
